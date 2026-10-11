"""Durable shape and bounded submission contracts for Action USER messages.

The HTTP wire models that carry a message in are in ``action_message_http``.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .image import ImageInput

ACTION_MESSAGE_ID_MAX_CODEPOINTS = 128
ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS = 32_000
ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS = 8_000
ACTION_MESSAGE_MAX_IMAGES = 32
# A runaway guard, not a product rule: one message naming this many projects is
# already far past what a person types.
ACTION_MESSAGE_MAX_PROJECT_REFS = 32
ACTION_PROJECT_REF_MAX_PATHS = 32
# The workspace settings IPC bounds (frontend/electron/src/ipc/schemas/limits.ts)
# for a project name and a folder path; a reference copies those values.
ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS = 200
ACTION_PROJECT_REF_PATH_MAX_CODEPOINTS = 4096
# The same runaway guard for named tasks and suggestions. The composer shortens a
# title to one line well inside the bound.
ACTION_MESSAGE_MAX_WORK_REFS = 32
ACTION_WORK_REF_TITLE_MAX_CODEPOINTS = 200
ACTION_MESSAGE_MAX_FILES = 10
# The user's chat messages one hand-off relays as their words.
ACTION_MESSAGE_MAX_RELAYED_ITEMS = 32
# The document formats the read tool extracts, with the name the model sees for
# each. The Electron attach IPC admits the same extensions.
ACTION_FILE_TYPE_LABEL_BY_EXTENSION: Mapping[str, str] = MappingProxyType(
    {
        ".docx": "Word document",
        ".ipynb": "Jupyter notebook",
        ".pdf": "PDF",
        ".pptx": "PowerPoint presentation",
        ".xlsx": "Excel workbook",
    }
)
# The workspace directory submission links attached files into.
ACTION_ATTACHMENTS_DIRNAME = "attachments"
# A macOS file name limit, in UTF-8 bytes.
ACTION_FILE_NAME_MAX_BYTES = 255
_ATTACHMENT_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _bounded_text(value: str, *, limit: int) -> str:
    normalized = _non_blank_text(value)
    if len(normalized) > limit:
        raise PydanticCustomError(
            "action_message_too_long",
            "value exceeds the Unicode code-point limit",
            {"limit": limit, "unit": "unicode_code_points"},
        )
    return normalized


def _non_blank_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise PydanticCustomError("action_message_blank", "value must not be blank")
    return normalized


def _bounded_id(value: str) -> str:
    return _bounded_text(value, limit=ACTION_MESSAGE_ID_MAX_CODEPOINTS)


def bounded_content(value: str) -> str:
    return _bounded_text(value, limit=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS)


ActionMessageId = Annotated[
    str,
    Field(
        json_schema_extra={
            "minLength": 1,
            "maxLength": ACTION_MESSAGE_ID_MAX_CODEPOINTS,
            "pattern": r"\S",
        }
    ),
    AfterValidator(_bounded_id),
]
type ActionMessageFailureType = Literal[
    "ActionNotFound",
    "ActionConflict",
    "MessageIdentityConflict",
    "ExpectedProcessConflict",
    "MessageWithdrawn",
]


def _bounded_items(*, limit: int, unit: str) -> Callable[[object], object]:
    """Bound a raw item count before any item is validated."""

    def validate(value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        if len(value) > limit:
            raise PydanticCustomError(
                "action_message_too_many",
                "value contains too many items",
                {"limit": limit, "unit": unit},
            )
        # strict モデルは JSON 配列をそのまま tuple として受け取らないため、
        # before バリデータで正規化しておく。
        return tuple(value)

    return validate


bounded_images = _bounded_items(
    limit=ACTION_MESSAGE_MAX_IMAGES, unit="image_references"
)
bounded_project_refs = _bounded_items(
    limit=ACTION_MESSAGE_MAX_PROJECT_REFS, unit="project_references"
)
bounded_work_refs = _bounded_items(
    limit=ACTION_MESSAGE_MAX_WORK_REFS, unit="work_references"
)
bounded_files = _bounded_items(limit=ACTION_MESSAGE_MAX_FILES, unit="files")


def _bounded_project_name(value: str) -> str:
    return _bounded_text(value, limit=ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS)


def _absolute_project_path(value: str) -> str:
    if len(value) > ACTION_PROJECT_REF_PATH_MAX_CODEPOINTS:
        raise PydanticCustomError(
            "action_message_too_long",
            "value exceeds the Unicode code-point limit",
            {
                "limit": ACTION_PROJECT_REF_PATH_MAX_CODEPOINTS,
                "unit": "unicode_code_points",
            },
        )
    if not os.path.isabs(value):
        raise PydanticCustomError(
            "action_message_invalid", "project folder path must be absolute"
        )
    return value


class ActionProjectRef(BaseModel):
    """A workspace project the user named in their text, copied at send time.

    ``start`` and ``end`` are Unicode code-point offsets into the trimmed text the
    reference belongs to. The name and folders are the ones the project had when
    the message was sent; a later rename or deletion does not rewrite the message.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    project_id: str
    display_name: str
    paths: tuple[Annotated[str, AfterValidator(_absolute_project_path)], ...]
    start: NonNegativeInt
    end: NonNegativeInt

    _validate_project_id = field_validator("project_id")(_bounded_id)
    _validate_display_name = field_validator("display_name")(_bounded_project_name)
    _validate_paths = field_validator("paths", mode="before")(
        _bounded_items(limit=ACTION_PROJECT_REF_MAX_PATHS, unit="project_paths")
    )

    @property
    def span_text(self) -> str:
        return self.display_name


def _bounded_work_title(value: str) -> str:
    return _bounded_text(value, limit=ACTION_WORK_REF_TITLE_MAX_CODEPOINTS)


class ActionWorkRef(BaseModel):
    """A task (``action``) or suggestion the user named in their text, by id.

    The title is the one the composer showed when the message was sent; a later
    change to the task or suggestion does not rewrite the message. ``start`` and
    ``end`` are Unicode code-point offsets, as on ``ActionProjectRef``. Nothing
    here proves the id is the sender's: whoever reads it reads as its own user.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["action", "suggestion"]
    id: str
    title: str
    start: NonNegativeInt
    end: NonNegativeInt

    _validate_id = field_validator("id")(_bounded_id)
    _validate_title = field_validator("title")(_bounded_work_title)

    @property
    def span_text(self) -> str:
        return self.title


def _attachment_file_name(value: str) -> str:
    """Admit only a name that is safe as one path component and keeps its type."""

    if (
        not unicodedata.is_normalized("NFC", value)
        or value.startswith(".")
        or len(value.encode()) > ACTION_FILE_NAME_MAX_BYTES
        or any(
            character in "/\\:" or unicodedata.category(character) == "Cc"
            for character in value
        )
        or os.path.splitext(value)[1].lower() not in ACTION_FILE_TYPE_LABEL_BY_EXTENSION
    ):
        raise PydanticCustomError(
            "action_message_invalid", "attached file name is not allowed"
        )
    return value


class FileAttachmentInput(BaseModel):
    """A document the user attached, staged by Electron main before the send.

    The staged copy is ``generated/attachments/{user}/{attachment_id}{ext}``
    under the artifact root, where ``ext`` is the name's lowercased extension.
    Submission moves it into the Action workspace as
    ``attachments/{attachment_id}/{name}``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    attachment_id: Annotated[str, Field(pattern=_ATTACHMENT_ID_PATTERN)]
    name: Annotated[str, AfterValidator(_attachment_file_name)]
    byte_size: PositiveInt

    @property
    def extension(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def workspace_path(self) -> str:
        """The path relative to the Action workspace the model opens."""

        return f"{ACTION_ATTACHMENTS_DIRNAME}/{self.attachment_id}/{self.name}"


class _SpanRef(Protocol):
    @property
    def start(self) -> int: ...
    @property
    def end(self) -> int: ...
    @property
    def span_text(self) -> str: ...


def require_ref_spans[RefT: _SpanRef](
    refs: tuple[RefT, ...],
    info: ValidationInfo,
    *,
    text_field: str,
    beside_field: str | None = None,
) -> tuple[RefT, ...]:
    """Check each reference names its own span of the text, in order, and none
    overlaps a reference of ``beside_field`` into the same text.

    A field that failed its own validation is absent from ``info.data``; its
    error is the one reported.
    """

    if not refs or text_field not in info.data:
        return refs
    text = info.data[text_field]
    if text is None:
        raise PydanticCustomError(
            "action_message_not_allowed",
            "references require the text they point into",
        )
    beside: tuple[_SpanRef, ...] = (
        info.data.get(beside_field, ()) if beside_field is not None else ()
    )
    previous_end = 0
    for ref in refs:
        if (
            ref.start < previous_end
            or text[ref.start : ref.end] != ref.span_text
            or any(ref.start < other.end and other.start < ref.end for other in beside)
        ):
            raise PydanticCustomError(
                "action_message_invalid",
                "reference does not name its own span of the text",
            )
        previous_end = ref.end
    return refs


class SuggestionApprovalInput(BaseModel):
    """Suggestion provenance available only to the internal adapter."""

    model_config = ConfigDict(extra="forbid", strict=True)

    suggestion_id: str
    approved_at: str
    summary: str | None = None
    organization_name: str | None = None
    project_name: str | None = None

    _validate_suggestion_id = field_validator("suggestion_id")(_non_blank_text)
    _validate_approved_at = field_validator("approved_at")(_non_blank_text)


class ChatHandoffInput(BaseModel):
    """What Pantaray's chat put into this message when it handed work to the task.

    ``relayed_item_ids`` names the user's chat messages whose words are in the
    message, verbatim: ``content``, or ``supplement`` on a Suggestion approval.
    ``note`` is the chat's own instruction beside them. With nothing relayed and
    no approval, ``content`` is the chat's instruction itself. Either way, what
    the chat wrote is Pantaray's, never shown as the user's words.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relayed_item_ids: tuple[str, ...]
    note: Annotated[str, AfterValidator(_non_blank_text)] | None = None

    _validate_item_ids = field_validator("relayed_item_ids", mode="before")(
        _bounded_items(limit=ACTION_MESSAGE_MAX_RELAYED_ITEMS, unit="relayed_items")
    )


class ActionUserMessageInput(BaseModel):
    """Canonical durable USER envelope used by internal Action callers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    message_id: str
    content: str
    images: tuple[ImageInput, ...] = ()
    language: Literal["en", "ja"] | None = None
    suggestion_approval: SuggestionApprovalInput | None = None
    supplement: Annotated[str, AfterValidator(_non_blank_text)] | None = None
    # Each list points into the one text it names: ``content`` or ``supplement``.
    project_refs: tuple[ActionProjectRef, ...] = ()
    supplement_project_refs: tuple[ActionProjectRef, ...] = ()
    files: tuple[FileAttachmentInput, ...] = ()
    chat_handoff: ChatHandoffInput | None = None
    # After the project lists: a task or suggestion never shares a project's span.
    work_refs: tuple[ActionWorkRef, ...] = ()
    supplement_work_refs: tuple[ActionWorkRef, ...] = ()

    _validate_message_id = field_validator("message_id")(_non_blank_text)
    _validate_content = field_validator("content")(_non_blank_text)
    _bound_project_refs = field_validator(
        "project_refs", "supplement_project_refs", mode="before"
    )(bounded_project_refs)
    _bound_work_refs = field_validator(
        "work_refs", "supplement_work_refs", mode="before"
    )(bounded_work_refs)
    _bound_files = field_validator("files", mode="before")(bounded_files)

    @field_validator("project_refs")
    @classmethod
    def _validate_project_refs(
        cls, refs: tuple[ActionProjectRef, ...], info: ValidationInfo
    ) -> tuple[ActionProjectRef, ...]:
        return require_ref_spans(refs, info, text_field="content")

    @field_validator("supplement_project_refs")
    @classmethod
    def _validate_supplement_project_refs(
        cls, refs: tuple[ActionProjectRef, ...], info: ValidationInfo
    ) -> tuple[ActionProjectRef, ...]:
        return require_ref_spans(refs, info, text_field="supplement")

    @field_validator("work_refs")
    @classmethod
    def _validate_work_refs(
        cls, refs: tuple[ActionWorkRef, ...], info: ValidationInfo
    ) -> tuple[ActionWorkRef, ...]:
        return require_ref_spans(
            refs, info, text_field="content", beside_field="project_refs"
        )

    @field_validator("supplement_work_refs")
    @classmethod
    def _validate_supplement_work_refs(
        cls, refs: tuple[ActionWorkRef, ...], info: ValidationInfo
    ) -> tuple[ActionWorkRef, ...]:
        return require_ref_spans(
            refs, info, text_field="supplement", beside_field="supplement_project_refs"
        )

    @property
    def is_chat_instruction(self) -> bool:
        """``content`` is what Pantaray's chat wrote, not the user's words."""

        handoff = self.chat_handoff
        return (
            handoff is not None
            and not handoff.relayed_item_ids
            and self.suggestion_approval is None
        )

    @model_validator(mode="after")
    def _require_relayed_words_for_a_note(self) -> Self:
        if (
            self.is_chat_instruction
            and self.chat_handoff is not None
            and self.chat_handoff.note is not None
        ):
            raise PydanticCustomError(
                "action_message_not_allowed",
                "a chat note goes beside relayed words; alone it is the content",
            )
        return self

    @model_validator(mode="after")
    def _require_suggestion_approval_for_supplement(self) -> Self:
        if self.supplement is not None and self.suggestion_approval is None:
            raise PydanticCustomError(
                "action_message_not_allowed",
                "supplement requires Suggestion approval metadata",
            )
        return self


def validate_action_user_message_for_submit(
    message: ActionUserMessageInput,
) -> ActionUserMessageInput:
    """Validate only new submissions without narrowing the durable V1 reader."""

    _bounded_id(message.message_id)
    bounded_content(message.content)
    bounded_images(message.images)
    if message.supplement is not None:
        _bounded_text(
            message.supplement,
            limit=ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS,
        )
    approval = message.suggestion_approval
    if approval is not None:
        _bounded_id(approval.suggestion_id)
    return message


__all__ = [
    "ACTION_ATTACHMENTS_DIRNAME",
    "ACTION_FILE_NAME_MAX_BYTES",
    "ACTION_FILE_TYPE_LABEL_BY_EXTENSION",
    "ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS",
    "ACTION_MESSAGE_ID_MAX_CODEPOINTS",
    "ACTION_MESSAGE_MAX_FILES",
    "ACTION_MESSAGE_MAX_IMAGES",
    "ACTION_MESSAGE_MAX_PROJECT_REFS",
    "ACTION_MESSAGE_MAX_WORK_REFS",
    "ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS",
    "ACTION_PROJECT_REF_MAX_PATHS",
    "ACTION_PROJECT_REF_NAME_MAX_CODEPOINTS",
    "ACTION_PROJECT_REF_PATH_MAX_CODEPOINTS",
    "ACTION_WORK_REF_TITLE_MAX_CODEPOINTS",
    "ActionMessageFailureType",
    "ActionMessageId",
    "ActionProjectRef",
    "ActionUserMessageInput",
    "ActionWorkRef",
    "ChatHandoffInput",
    "FileAttachmentInput",
    "SuggestionApprovalInput",
    "bounded_content",
    "bounded_files",
    "bounded_images",
    "bounded_project_refs",
    "bounded_work_refs",
    "require_ref_spans",
    "validate_action_user_message_for_submit",
]

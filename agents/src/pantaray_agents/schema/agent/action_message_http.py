"""HTTP wire contract for submitting, resuming and withdrawing Action messages.

The router turns a validated request into the durable ``ActionUserMessageInput``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from pantaray_agents.schema.action_conversation import ActionStatus

from .action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
    ACTION_MESSAGE_MAX_FILES,
    ACTION_MESSAGE_MAX_IMAGES,
    ACTION_MESSAGE_MAX_PROJECT_REFS,
    ACTION_MESSAGE_MAX_WORK_REFS,
    ActionMessageId,
    ActionProjectRef,
    ActionWorkRef,
    FileAttachmentInput,
    bounded_content,
    bounded_files,
    bounded_images,
    bounded_project_refs,
    bounded_work_refs,
    require_ref_spans,
)
from .base import AgentError
from .image import ImageInput

type ActionMessageValidationReason = Literal[
    "blank",
    "too_long",
    "not_allowed",
    "extra_field",
    "invalid",
    "too_many",
]
type ActionMessageValidationUnit = Literal["unicode_code_points"]


class _ActionMessageHttpModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ActionMessageHttpNewTarget(_ActionMessageHttpModel):
    kind: Literal["new"]
    approval_mode: Literal["prompt_each_time", "always_allow"] | None = None
    reply_to_suggestion_id: ActionMessageId | None = None


class ActionMessageHttpExistingTarget(_ActionMessageHttpModel):
    kind: Literal["existing"]
    action_id: ActionMessageId
    expected_process_id: ActionMessageId | None


ActionMessageHttpTarget = Annotated[
    ActionMessageHttpNewTarget | ActionMessageHttpExistingTarget,
    Field(discriminator="kind"),
]


class ActionMessageHttpMessage(_ActionMessageHttpModel):
    version: Literal[1]
    message_id: ActionMessageId
    content: Annotated[
        str,
        Field(
            json_schema_extra={
                "minLength": 1,
                "maxLength": ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
                "pattern": r"\S",
            }
        ),
    ]
    images: Annotated[
        tuple[ImageInput, ...],
        Field(json_schema_extra={"maxItems": ACTION_MESSAGE_MAX_IMAGES}),
    ]
    language: Literal["en", "ja"] | None = None
    project_refs: Annotated[
        tuple[ActionProjectRef, ...],
        Field(json_schema_extra={"maxItems": ACTION_MESSAGE_MAX_PROJECT_REFS}),
    ] = ()
    files: Annotated[
        tuple[FileAttachmentInput, ...],
        Field(json_schema_extra={"maxItems": ACTION_MESSAGE_MAX_FILES}),
    ] = ()
    # After ``project_refs``: a task or suggestion never shares a project's span.
    work_refs: Annotated[
        tuple[ActionWorkRef, ...],
        Field(json_schema_extra={"maxItems": ACTION_MESSAGE_MAX_WORK_REFS}),
    ] = ()

    _validate_content = field_validator("content")(bounded_content)
    _validate_images = field_validator("images", mode="before")(bounded_images)
    _bound_project_refs = field_validator("project_refs", mode="before")(
        bounded_project_refs
    )
    _bound_work_refs = field_validator("work_refs", mode="before")(bounded_work_refs)
    _bound_files = field_validator("files", mode="before")(bounded_files)

    @field_validator("project_refs")
    @classmethod
    def _validate_project_refs(
        cls, refs: tuple[ActionProjectRef, ...], info: ValidationInfo
    ) -> tuple[ActionProjectRef, ...]:
        return require_ref_spans(refs, info, text_field="content")

    @field_validator("work_refs")
    @classmethod
    def _validate_work_refs(
        cls, refs: tuple[ActionWorkRef, ...], info: ValidationInfo
    ) -> tuple[ActionWorkRef, ...]:
        return require_ref_spans(
            refs, info, text_field="content", beside_field="project_refs"
        )

    @field_validator("version", mode="before")
    @classmethod
    def _require_exact_wire_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise PydanticCustomError(
                "action_message_invalid",
                "HTTP Action message version must be the integer 1",
            )
        return value


class ActionMessageHttpRequest(_ActionMessageHttpModel):
    target: ActionMessageHttpTarget
    message: ActionMessageHttpMessage


class ActionResumeHttpRequest(_ActionMessageHttpModel):
    """Ask for the stopped run to be continued as one follow-up turn.

    There is no message: the caller pressed a button. ``message_id`` is the same
    user-wide idempotency key an ordinary submission carries, so a retry after a
    lost response continues the same turn instead of opening a second one.
    """

    message_id: ActionMessageId


class _ActionMessageHttpResponseBase(_ActionMessageHttpModel):
    action_id: str
    message_id: str
    step_id: str
    action_status: ActionStatus


class ActionMessageHttpStartedResponse(_ActionMessageHttpResponseBase):
    disposition: Literal["started"]
    process_id: str


class ActionMessageHttpDeferredResponse(_ActionMessageHttpResponseBase):
    disposition: Literal["pending", "not_executed"]
    process_id: None


ActionMessageHttpResponse = Annotated[
    ActionMessageHttpStartedResponse | ActionMessageHttpDeferredResponse,
    Field(discriminator="disposition"),
]


class ActionMessageHttpNotFoundFailure(_ActionMessageHttpModel):
    type: Literal["ActionNotFound"]


class ActionMessageHttpConflictFailure(_ActionMessageHttpModel):
    type: Literal[
        "ActionConflict",
        "MessageIdentityConflict",
        "ExpectedProcessConflict",
        "MessageWithdrawn",
    ]


class ActionMessageWithdrawConflictFailure(_ActionMessageHttpModel):
    """A run adopted the message before it could be withdrawn."""

    type: Literal["MessageAdopted"]


class ActionMessageHttpErrorDetail(_ActionMessageHttpModel):
    detail: str


class ActionMessageHttpServerErrorDetail(_ActionMessageHttpModel):
    detail: AgentError


ActionMessageHttpDefaultErrorDetail = (
    ActionMessageHttpErrorDetail | ActionMessageHttpServerErrorDetail
)


class ActionMessageValidationError(_ActionMessageHttpModel):
    type: Literal["ActionMessageValidationError"] = "ActionMessageValidationError"
    field: str
    reason: ActionMessageValidationReason
    limit: int | None
    unit: ActionMessageValidationUnit | None


__all__ = [
    "ActionMessageHttpConflictFailure",
    "ActionMessageHttpDefaultErrorDetail",
    "ActionMessageHttpDeferredResponse",
    "ActionMessageHttpErrorDetail",
    "ActionMessageHttpExistingTarget",
    "ActionMessageHttpMessage",
    "ActionMessageHttpNewTarget",
    "ActionMessageHttpNotFoundFailure",
    "ActionMessageHttpRequest",
    "ActionMessageHttpResponse",
    "ActionMessageHttpServerErrorDetail",
    "ActionMessageHttpStartedResponse",
    "ActionMessageHttpTarget",
    "ActionMessageValidationError",
    "ActionMessageValidationReason",
    "ActionMessageValidationUnit",
    "ActionMessageWithdrawConflictFailure",
    "ActionResumeHttpRequest",
]

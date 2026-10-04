"""Pure public-entry projection for durable Action conversation rows."""

from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    APPROVAL_DENIED_OUTPUT_KIND,
    NOT_EXECUTED_OUTPUT_KIND,
)
from pantaray_agents.agents.action_agent.tools.zanei_tools import (
    RECORDING_UNAVAILABLE_STATUS,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_STEP_NAME,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    TOOL_RESULT_BINARY_MEDIA_TYPE,
)
from pantaray_agents.schema.action_conversation import (
    RENDERER_PREPARING_OUTPUT_KIND,
    ApprovedSuggestion,
    ToolEntry,
    ToolEntryOutcome,
    UserEntry,
    UserEntryFile,
    UserEntryProjectRef,
    UserEntryStatus,
)
from pantaray_agents.schema.agent.action_message import (
    ActionProjectRef,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    parse_stored_action_user_message,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.tool_result import FormalToolStepOutput

from .history_queries import ActionHistoryToolRow, ActionHistoryUserRow
from .tool_subject import project_tool_output_preview, project_tool_subject

# The durable tool output is the only per-step record of what a tool attached; the
# runtime attachment itself lives in the checkpoint, which this projection does not read.
_LOCAL_IMAGE_SOURCE_KIND = "local_image_blob"
_IMAGE_MIME_TYPE_PREFIX = "image/"


class ActionConversationEntryIntegrityError(MigrationError):
    """One durable conversation row violates its public projection contract."""


def action_user_row_is_visible(row: ActionHistoryUserRow) -> bool:
    """Say whether a durable USER row is a message the conversation shows.

    A resume is a button press. It has to be a USER row because that is what
    opens a run, and its text is what the model reads to continue, but the user
    never wrote it, so no entry stands for it.
    """

    return row.step_name != ACTION_RESUME_STEP_NAME


def project_action_user_entry(row: ActionHistoryUserRow) -> UserEntry:
    """Project one adopted or unadopted durable USER row."""

    message_id = row.user_message_id
    try:
        message = parse_stored_action_user_message(
            message_id=message_id,
            message_json=row.user_message_json,
            user_request_text=row.user_request_text,
        )
    except ValueError as exc:
        raise ActionConversationEntryIntegrityError(str(exc)) from exc
    content: str | None
    images: tuple[ImageInput, ...]
    approved_suggestion: ApprovedSuggestion | None = None
    project_refs: tuple[ActionProjectRef, ...] = ()
    files: tuple[FileAttachmentInput, ...] = ()
    if message is None:
        content = row.user_request_text
        images = ()
    else:
        if message.suggestion_approval is not None:
            approved_suggestion = ApprovedSuggestion(
                suggestion_id=message.suggestion_approval.suggestion_id,
                content=message.content,
            )
            content = message.supplement
            project_refs = message.supplement_project_refs
        else:
            content = message.content
            project_refs = message.project_refs
        images = message.images
        files = message.files

    status: UserEntryStatus = (
        "adopted"
        if row.adopted_process_id is not None
        else "not_executed"
        if row.adoption_canceled_at is not None
        else "pending"
    )
    try:
        return UserEntry(
            step_kind="user",
            step_id=row.step_id,
            step_number=row.step_number,
            message_id=message_id,
            accepted_sequence=row.accepted_sequence,
            content=content,
            approved_suggestion=approved_suggestion,
            images=images,
            project_refs=tuple(
                UserEntryProjectRef(
                    display_name=ref.display_name, start=ref.start, end=ref.end
                )
                for ref in project_refs
            ),
            files=tuple(
                UserEntryFile(name=file.name, byte_size=file.byte_size)
                for file in files
            ),
            status=status,
        )
    except ValidationError as exc:
        raise ActionConversationEntryIntegrityError(
            "Action USER row cannot form a public entry"
        ) from exc


def _tool_entry_images(output: object) -> tuple[ImageInput, ...]:
    """Name the user-scoped images one tool step attached to its result.

    Anything the output calls an attachment but does not name as a readable local
    image - a workspace file, a blob, a malformed entry - is not an image here.
    """

    if not isinstance(output, dict):
        return ()
    attachments = output.get("attachments")
    if not isinstance(attachments, list):
        return ()
    images: list[ImageInput] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        mime_type = attachment.get("mime_type")
        storage_path = attachment.get("storage_path")
        if (
            attachment.get("source_kind") != _LOCAL_IMAGE_SOURCE_KIND
            or not isinstance(mime_type, str)
            or not mime_type.startswith(_IMAGE_MIME_TYPE_PREFIX)
            or not isinstance(storage_path, str)
            or not storage_path.strip()
        ):
            continue
        images.append(ImageInput(storage_path=storage_path))
    return tuple(images)


def _tool_entry_outcome(step: FormalToolStepOutput) -> ToolEntryOutcome:
    """Say whether the tool did what it was called for, refused to, or could not.

    A denied approval and a read taken while recording is off are both persisted as
    successful steps whose body says the call never happened, so the terminal status
    cannot carry that distinction. A call a user Stop reached before it was issued is
    persisted as an error step, yet it never ran either. A page render whose renderer
    is still being set up succeeds without drawing anything. Each producer marks its
    own body, and this is the one place that turns those markers into the closed
    outcome the row reads.
    """

    output = step.output
    if not isinstance(output, dict):
        return "completed"
    if (
        output.get("kind") == APPROVAL_DENIED_OUTPUT_KIND
        and output.get("executed") is False
    ):
        return "denied"
    if output.get("kind") == NOT_EXECUTED_OUTPUT_KIND:
        return "not_executed"
    if output.get("status") == RECORDING_UNAVAILABLE_STATUS:
        return "unavailable"
    if (
        step.status == "success"
        and output.get("kind") == RENDERER_PREPARING_OUTPUT_KIND
    ):
        return "preparing"
    return "completed"


def project_action_tool_entry(row: ActionHistoryToolRow) -> ToolEntry:
    """Project one visible durable Tool row without loading its output body."""

    output_available = False
    output_preview: str | None = None
    images: tuple[ImageInput, ...] = ()
    outcome: ToolEntryOutcome = "completed"
    if row.tool_output is not None:
        try:
            output = FormalToolStepOutput.model_validate_json(row.tool_output)
        except ValidationError as exc:
            raise ActionConversationEntryIntegrityError(
                "Action Tool output violates the durable schema"
            ) from exc
        if row.status == "processing":
            expected_status = "processing"
        elif row.status == "success":
            expected_status = "success"
        else:
            expected_status = "error"
        if output.status != expected_status:
            raise ActionConversationEntryIntegrityError(
                "Action Tool status and output are inconsistent"
            )
        output_available = row.status != "processing" and (
            output.output is not None
            and not (
                output.output_storage_kind == "action_file"
                and isinstance(output.output, dict)
                and output.output.get("storage") == "action_file"
                and output.output.get("media_type") == TOOL_RESULT_BINARY_MEDIA_TYPE
            )
        )
        images = _tool_entry_images(output.output)
        outcome = _tool_entry_outcome(output)
        if output_available:
            output_preview = project_tool_output_preview(
                row.tool_id, output.output, output.output_storage_kind
            )

    try:
        return ToolEntry(
            step_kind="tool",
            step_id=row.step_id,
            step_number=row.step_number,
            label=row.tool_id,
            status=row.status,
            outcome=outcome,
            subject=project_tool_subject(row.tool_id, row.tool_args),
            output_preview=output_preview,
            output_available=output_available,
            images=images,
        )
    except ValidationError as exc:
        raise ActionConversationEntryIntegrityError(
            "Action Tool row cannot form a public entry"
        ) from exc


__all__ = [
    "ActionConversationEntryIntegrityError",
    "action_user_row_is_visible",
    "project_action_tool_entry",
    "project_action_user_entry",
]

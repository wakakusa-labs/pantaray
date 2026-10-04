"""Public event payloads and durable read models for Action conversations."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
    model_validator,
)
from pydantic_core import PydanticCustomError

from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.utils.timestamps import normalize_iso8601_utc_z_microseconds

type ActionStatus = Literal["queued", "processing", "success", "error", "canceled"]
type RunStatus = Literal["running", "approval_pending", "success", "error", "canceled"]
type UserEntryStatus = Literal["adopted", "pending", "not_executed"]
type ActionStepStatus = Literal["processing", "success", "error", "timeout"]
type ToolEntryOutcome = Literal[
    "completed", "denied", "unavailable", "not_executed", "preparing"
]
# The body kind a successful page render returns while the renderer it needs is still
# being set up; the row says the pages will be shown once it is ready, not that it failed.
RENDERER_PREPARING_OUTPUT_KIND = "renderer_preparing"
type ActionToolOutputUnavailableReason = Literal["no_output", "binary"]

_ACTION_TOOL_STEP_NAME_PREFIX = "tool::"
_HIDDEN_ACTION_TOOL_STEP_NAMES = frozenset(
    {
        f"{_ACTION_TOOL_STEP_NAME_PREFIX}thinking",
        f"{_ACTION_TOOL_STEP_NAME_PREFIX}submit_final_answer",
        f"{_ACTION_TOOL_STEP_NAME_PREFIX}send_message",
    }
)
_TERMINAL_CONVERSATION_STATUSES = frozenset({"success", "error", "canceled"})
_OPEN_RUN_SORT_LAST = "Z"
_RUN_OUTCOME_SHAPES: dict[RunStatus, tuple[bool, bool, bool, bool]] = {
    "running": (False, False, False, False),
    "approval_pending": (False, False, False, False),
    "success": (True, True, False, True),
    "error": (True, False, True, True),
    "canceled": (True, False, True, True),
}


def visible_action_tool_id_from_step_name(step_name: str) -> str | None:
    """Return the tool identity only for a durable user-visible Action step."""

    if not step_name.startswith(_ACTION_TOOL_STEP_NAME_PREFIX):
        return None
    tool_id = step_name.removeprefix(_ACTION_TOOL_STEP_NAME_PREFIX)
    if not tool_id or step_name in _HIDDEN_ACTION_TOOL_STEP_NAMES:
        return None
    return tool_id


def _require_canonical_identity(value: str) -> str:
    if not value or value != value.strip():
        raise PydanticCustomError(
            "action_conversation_identity_not_canonical",
            "identity must be non-blank and have no surrounding whitespace",
        )
    return value


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise PydanticCustomError(
            "action_conversation_blank",
            "value must not be blank",
        )
    return value


def _require_page_unique(values: tuple[str | int, ...], code: str) -> None:
    if len(values) != len(set(values)):
        raise PydanticCustomError(code, "values must be unique within an Action page")


type ActionConversationIdentity = Annotated[
    str, AfterValidator(_require_canonical_identity)
]
type ActionConversationTimestamp = Annotated[
    str, AfterValidator(normalize_iso8601_utc_z_microseconds)
]
type NonBlankText = Annotated[str, AfterValidator(_require_non_blank)]


class _ActionConversationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ActionMessageAcceptedEventData(_ActionConversationModel):
    """Identity-only payload for ``action_message_accepted``."""

    action_id: ActionConversationIdentity
    message_id: ActionConversationIdentity
    step_id: ActionConversationIdentity


class ActionMessageAdoptedEventData(_ActionConversationModel):
    """Identity-only payload for ``action_message_adopted``."""

    action_id: ActionConversationIdentity
    message_id: ActionConversationIdentity
    step_id: ActionConversationIdentity
    process_id: ActionConversationIdentity


class ActionStepEventData(_ActionConversationModel):
    """Transient public state for one visible tool step."""

    action_id: ActionConversationIdentity
    process_id: ActionConversationIdentity
    step_kind: Literal["tool"]
    step_id: ActionConversationIdentity
    step_number: PositiveInt
    tool_id: ActionConversationIdentity
    label: NonBlankText
    # Persisted events from before subjects were published remain replayable.
    subject: NonBlankText | None = None
    status: ActionStepStatus
    started_at: ActionConversationTimestamp
    completed_at: ActionConversationTimestamp | None

    @model_validator(mode="after")
    def validate_completion_state(self) -> Self:
        if self.status == "processing" and self.completed_at is not None:
            raise PydanticCustomError(
                "action_step_completion_state_invalid",
                "processing step must not have a completion timestamp",
            )
        if self.status != "processing" and self.completed_at is None:
            raise PydanticCustomError(
                "action_step_completion_state_invalid",
                "terminal step must have a completion timestamp",
            )
        return self


class ActionAssistantMessageEventData(_ActionConversationModel):
    """A committed utterance is ready to read from the canonical conversation."""

    action_id: ActionConversationIdentity
    process_id: ActionConversationIdentity
    step_kind: Literal["assistant"]
    step_id: ActionConversationIdentity
    step_number: PositiveInt
    status: Literal["success"]


type ActionStepEventPayload = Annotated[
    ActionStepEventData | ActionAssistantMessageEventData,
    Field(discriminator="step_kind"),
]
_ACTION_STEP_EVENT_ADAPTER: TypeAdapter[ActionStepEventPayload] = TypeAdapter(
    ActionStepEventPayload
)


def parse_action_step_event(payload: object) -> ActionStepEventPayload:
    return _ACTION_STEP_EVENT_ADAPTER.validate_python(payload)


class PublicActionError(_ActionConversationModel):
    code: ActionConversationIdentity
    message: NonBlankText


class ApprovedSuggestion(_ActionConversationModel):
    """Approved proposal snapshot, separate from the user's optional addition."""

    suggestion_id: ActionConversationIdentity
    content: NonBlankText


class UserEntryProjectRef(_ActionConversationModel):
    """A workspace project named in ``UserEntry.content``, as it was when sent.

    ``start`` and ``end`` are Unicode code-point offsets into that content.
    """

    display_name: NonBlankText
    start: NonNegativeInt
    end: NonNegativeInt


class UserEntryFile(_ActionConversationModel):
    """A document attached to the message, as it was named when sent."""

    name: NonBlankText
    byte_size: PositiveInt


class UserEntry(_ActionConversationModel):
    step_kind: Literal["user"]
    step_id: ActionConversationIdentity
    step_number: PositiveInt | None
    message_id: ActionConversationIdentity | None
    accepted_sequence: PositiveInt
    content: NonBlankText | None
    approved_suggestion: ApprovedSuggestion | None
    images: tuple[ImageInput, ...]
    project_refs: tuple[UserEntryProjectRef, ...]
    files: tuple[UserEntryFile, ...]
    status: UserEntryStatus

    @model_validator(mode="after")
    def validate_user_entry(self) -> Self:
        if self.content is None and self.approved_suggestion is None:
            raise PydanticCustomError(
                "action_conversation_user_content_missing",
                "USER requires content or an approved suggestion",
            )
        if (self.status == "adopted") != (self.step_number is not None):
            raise PydanticCustomError(
                "action_conversation_user_timeline_position_invalid",
                "adopted USER requires a timeline position; unadopted USER must not have one",
            )
        if self.status != "adopted" and self.message_id is None:
            raise PydanticCustomError(
                "action_conversation_message_identity_missing",
                "pending or not-executed USER entry must have a message identity",
            )
        return self


class AssistantEntry(_ActionConversationModel):
    step_kind: Literal["assistant"]
    step_id: ActionConversationIdentity
    step_number: PositiveInt
    content: NonBlankText


class ToolEntry(_ActionConversationModel):
    step_kind: Literal["tool"]
    step_id: ActionConversationIdentity
    step_number: PositiveInt
    label: NonBlankText
    status: ActionStepStatus
    # Whether the tool actually did what it was called for. A denied approval and an
    # unreadable recorder are both persisted as successful steps that report the call did
    # not happen, so the terminal status alone cannot tell the row what to say.
    outcome: ToolEntryOutcome
    # The one argument this step acted on, and the head of its result when it has none.
    # Both are locale-neutral data; the sentence the row reads is the UI's.
    subject: NonBlankText | None
    output_preview: NonBlankText | None
    output_available: bool
    images: tuple[ImageInput, ...]

    @model_validator(mode="after")
    def validate_output_availability(self) -> Self:
        if self.status == "processing" and self.output_available:
            raise PydanticCustomError(
                "action_conversation_tool_output_invalid",
                "processing tool step cannot have available output",
            )
        return self


class ActionToolOutputDetail(_ActionConversationModel):
    content: str
    next_cursor: NonBlankText | None
    truncated: bool
    unavailable_reason: ActionToolOutputUnavailableReason | None

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.unavailable_reason is not None:
            if self.content or self.next_cursor is not None or self.truncated:
                raise PydanticCustomError(
                    "action_tool_output_unavailable_shape_invalid",
                    "unavailable output cannot contain content or pagination state",
                )
        elif (not self.content and not self.truncated) or (
            self.next_cursor is not None and self.truncated
        ):
            raise PydanticCustomError(
                "action_tool_output_page_shape_invalid",
                "available output requires content and one terminal disposition",
            )
        return self


type ActionConversationEntry = Annotated[
    UserEntry | AssistantEntry | ToolEntry,
    Field(discriminator="step_kind"),
]


class ActionRun(_ActionConversationModel):
    """One logical run keyed by its root USER-origin process."""

    run_id: ActionConversationIdentity
    status: RunStatus
    started_at: ActionConversationTimestamp
    completed_at: ActionConversationTimestamp | None
    completion_event_id: ActionConversationIdentity | None
    entries: tuple[ActionConversationEntry, ...]
    final_output: NonBlankText | None
    error: PublicActionError | None

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        timeline_positions = tuple(
            (entry.step_number, entry.step_id)
            for entry in self.entries
            if entry.step_number is not None
        )
        if len(timeline_positions) != len(self.entries) or timeline_positions != tuple(
            sorted(timeline_positions, reverse=True)
        ):
            raise PydanticCustomError(
                "action_conversation_run_entry_order_invalid",
                "run entries must follow the canonical timeline order",
            )
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise PydanticCustomError(
                "action_conversation_run_time_invalid",
                "run completion must not precede its start",
            )
        outcome_shape = (
            self.completed_at is not None,
            self.final_output is not None,
            self.error is not None,
            self.completion_event_id is not None,
        )
        if outcome_shape != _RUN_OUTCOME_SHAPES[self.status]:
            raise PydanticCustomError(
                "action_conversation_run_outcome_invalid",
                "run status, completion, final output, and error are inconsistent",
            )
        return self


class ActionConversationSummary(_ActionConversationModel):
    action_id: ActionConversationIdentity
    suggestion_id: ActionConversationIdentity | None
    approved_suggestion: ApprovedSuggestion | None
    status: ActionStatus
    latest_run_id: ActionConversationIdentity | None
    # Whether the user's Stop is still the Action's latest intent and a turn
    # continuing it can start, so the composer offers 「再開」.
    resumable: bool

    @model_validator(mode="after")
    def validate_latest_run(self) -> Self:
        if (
            self.approved_suggestion is not None
            and self.approved_suggestion.suggestion_id != self.suggestion_id
        ):
            raise PydanticCustomError(
                "action_conversation_approved_suggestion_relation_invalid",
                "approved suggestion must belong to the Action",
            )
        if self.status in {"queued", "processing"} and self.latest_run_id is None:
            raise PydanticCustomError(
                "action_conversation_latest_run_missing",
                "active Action must have a latest run identity",
            )
        if self.resumable and self.status != "canceled":
            raise PydanticCustomError(
                "action_conversation_resumable_status_invalid",
                "only a canceled Action can be resumable",
            )
        return self


class ActionConversationPage(_ActionConversationModel):
    action: ActionConversationSummary
    runs: tuple[ActionRun, ...]
    unadopted_messages: tuple[UserEntry, ...]
    next_cursor: NonBlankText | None

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        run_entries = tuple(entry for run in self.runs for entry in run.entries)
        if any(
            message.status not in {"pending", "not_executed"}
            or (
                message.status == "pending"
                and self.action.status in _TERMINAL_CONVERSATION_STATUSES
            )
            for message in self.unadopted_messages
        ):
            raise PydanticCustomError(
                "action_conversation_user_placement_invalid",
                "unadopted USER must be pending or not-executed; terminal Actions require not-executed",
            )
        page_entries = run_entries + self.unadopted_messages
        user_entries = tuple(
            entry for entry in page_entries if isinstance(entry, UserEntry)
        )
        if any(
            entry.approved_suggestion is not None
            and entry.approved_suggestion.suggestion_id != self.action.suggestion_id
            for entry in user_entries
        ):
            raise PydanticCustomError(
                "action_conversation_approved_suggestion_relation_invalid",
                "approved suggestion must belong to the page Action",
            )
        step_ids = tuple(entry.step_id for entry in page_entries)
        _require_page_unique(step_ids, "action_conversation_step_identity_duplicate")
        message_ids = tuple(
            entry.message_id for entry in user_entries if entry.message_id is not None
        )
        _require_page_unique(
            message_ids, "action_conversation_message_identity_duplicate"
        )
        accepted_sequences = tuple(entry.accepted_sequence for entry in user_entries)
        _require_page_unique(
            accepted_sequences, "action_conversation_accepted_sequence_duplicate"
        )
        run_ids = tuple(run.run_id for run in self.runs)
        _require_page_unique(run_ids, "action_conversation_run_identity_duplicate")
        latest_run = next(
            (run for run in self.runs if run.run_id == self.action.latest_run_id),
            None,
        )
        ordered_runs = sorted(
            self.runs,
            key=lambda run: (run.started_at, run.completed_at or _OPEN_RUN_SORT_LAST),
        )
        if any(
            run.run_id != self.action.latest_run_id
            and (
                run.status not in _TERMINAL_CONVERSATION_STATUSES
                or (
                    latest_run is not None
                    and run.completed_at is not None
                    and run.completed_at > latest_run.started_at
                )
            )
            for run in self.runs
        ) or any(
            earlier.completed_at is None or earlier.completed_at > later.started_at
            for earlier, later in pairwise(ordered_runs)
        ):
            raise PydanticCustomError(
                "action_conversation_nonlatest_run_invalid",
                "visible logical runs must be terminal, ordered, and non-overlapping",
            )
        action_is_terminal = self.action.status in _TERMINAL_CONVERSATION_STATUSES
        run_is_terminal = (
            latest_run is not None
            and latest_run.status in _TERMINAL_CONVERSATION_STATUSES
        )
        if latest_run is not None and (
            (
                latest_run.status == "approval_pending"
                and self.action.status != "processing"
            )
            or action_is_terminal != run_is_terminal
            or (action_is_terminal and latest_run.status != self.action.status)
        ):
            raise PydanticCustomError(
                "action_conversation_latest_status_mismatch",
                "terminal Action and latest run statuses must agree",
            )
        return self


__all__ = [
    "AssistantEntry",
    "ApprovedSuggestion",
    "ActionConversationIdentity",
    "ActionConversationEntry",
    "ActionConversationPage",
    "ActionConversationSummary",
    "ActionConversationTimestamp",
    "ActionMessageAcceptedEventData",
    "ActionMessageAdoptedEventData",
    "ActionRun",
    "ActionStatus",
    "ActionStepEventData",
    "ActionAssistantMessageEventData",
    "ActionStepEventPayload",
    "parse_action_step_event",
    "ActionStepStatus",
    "ActionToolOutputDetail",
    "ActionToolOutputUnavailableReason",
    "PublicActionError",
    "RENDERER_PREPARING_OUTPUT_KIND",
    "RunStatus",
    "ToolEntry",
    "ToolEntryOutcome",
    "UserEntry",
    "UserEntryStatus",
    "visible_action_tool_id_from_step_name",
]

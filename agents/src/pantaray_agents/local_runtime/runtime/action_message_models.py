"""Typed command and result models for Action USER message submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.schema.agent.action_message import (
    ActionMessageFailureType,
    ActionMessageId,
    ActionUserMessageInput,
    validate_action_user_message_for_submit,
)

# Every USER row carries ``step_type = 'user_request'``, so the run it opens is
# found the same way whoever sent it. ``step_name`` is what separates the two
# origins: a resume is a button press with no message to show, so the
# conversation projection drops its row while the run itself stays ordinary.
ACTION_USER_STEP_NAME = "user_request"
ACTION_RESUME_STEP_NAME = "resume_request"

# What the model reads at the top of a resumed turn. It is the whole message the
# user "sent", so it must say what happened and what not to redo. A resume also
# carries the messages the user sent while the run worked (Send now), which this
# text must not talk over: it never asks to continue ahead of them.
ACTION_RESUME_REQUEST_TEXT = (
    "The user stopped this run before it finished and has started a new turn. "
    "Any messages the user sent while it worked come before this one: they say "
    "what the user wants now, so follow them even where they differ from the work "
    "already done. Otherwise pick up from where the work stopped: do not repeat a "
    "tool call that already took effect, and do not restate what you already "
    "reported."
)


class NewActionTarget(BaseModel):
    """Create a new Action, optionally from an approved Suggestion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["new"] = "new"
    suggestion_id: ActionMessageId | None = None
    approval_mode: ApprovalMode | None = None
    reply_to_suggestion_id: ActionMessageId | None = None

    @model_validator(mode="after")
    def _separate_approval_and_reply(self) -> Self:
        if self.suggestion_id is not None and self.reply_to_suggestion_id is not None:
            raise ValueError("Suggestion approval and reply origins cannot be combined")
        return self


class ExistingActionTarget(BaseModel):
    """Append a USER turn to one existing Action conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["existing"] = "existing"
    action_id: ActionMessageId
    expected_process_id: ActionMessageId | None


ActionMessageTarget = Annotated[
    NewActionTarget | ExistingActionTarget,
    Field(discriminator="kind"),
]


class SubmitActionMessageCommand(BaseModel):
    """The single typed input accepted by ``submit_action_message``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: ActionMessageId
    target: ActionMessageTarget = Field(default_factory=NewActionTarget)
    message: ActionUserMessageInput
    # ``resume`` is the user asking for the stopped run to be continued. It is an
    # ordinary follow-up turn; only the step name and this guard differ.
    origin: Literal["user", "resume"] = "user"

    _validate_message = field_validator("message")(
        validate_action_user_message_for_submit
    )


@dataclass(frozen=True, slots=True)
class StartedActionMessageResult:
    disposition: Literal["started"]
    action_id: str
    message_id: str
    user_step_id: str
    action_status: ActionStatus
    process_id: str
    job_id: str
    inserted: bool


@dataclass(frozen=True, slots=True)
class DeferredActionMessageResult:
    disposition: Literal["pending", "not_executed"]
    action_id: str
    message_id: str
    user_step_id: str
    action_status: ActionStatus
    process_id: None
    job_id: None
    inserted: bool


type SubmitActionMessageResult = (
    StartedActionMessageResult | DeferredActionMessageResult
)


@dataclass(frozen=True, slots=True)
class _ExistingSubmission:
    result: SubmitActionMessageResult
    action_created: bool
    initial_approval_mode: ApprovalMode | None
    suggestion_id: str | None
    reply_to_suggestion_id: str | None
    expected_process_id: str | None
    user_message_json: str


@dataclass(frozen=True, slots=True)
class _StepPosition:
    step_number: int
    local_step_number: int
    short_step_id: str
    parent_step_id: str | None


class ActionMessageSubmissionError(RuntimeError):
    """Base failure raised by the canonical Action message submit boundary."""


class ActionMessageConflictError(ActionMessageSubmissionError):
    """The message identity or requested Action transition conflicts with storage."""

    failure_type: ActionMessageFailureType = "ActionConflict"


class ActionNotFoundError(ActionMessageSubmissionError):
    failure_type: ActionMessageFailureType = "ActionNotFound"


class MessageIdentityConflictError(ActionMessageConflictError):
    failure_type: ActionMessageFailureType = "MessageIdentityConflict"


class ExpectedProcessConflictError(ActionMessageConflictError):
    failure_type: ActionMessageFailureType = "ExpectedProcessConflict"


class MessageWithdrawnError(ActionMessageConflictError):
    """The user withdrew this message before any run took it; it never runs."""

    failure_type: ActionMessageFailureType = "MessageWithdrawn"


__all__ = [
    "ACTION_RESUME_REQUEST_TEXT",
    "ACTION_RESUME_STEP_NAME",
    "ACTION_USER_STEP_NAME",
    "ActionMessageConflictError",
    "ActionNotFoundError",
    "ActionMessageSubmissionError",
    "ActionMessageTarget",
    "DeferredActionMessageResult",
    "ExistingActionTarget",
    "ExpectedProcessConflictError",
    "MessageIdentityConflictError",
    "MessageWithdrawnError",
    "NewActionTarget",
    "StartedActionMessageResult",
    "SubmitActionMessageCommand",
    "SubmitActionMessageResult",
]

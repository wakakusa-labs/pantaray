"""Typed definitions for local runtime job payloads."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

LanguageCode = Literal["en", "ja"]


class ActionUserStepContinuationRef(TypedDict):
    """Start or continue an Action from one durable USER step."""

    kind: Literal["user_step"]
    user_step_id: str


class ActionToolApprovalContinuationRef(TypedDict):
    """Resume an Action from one decided tool approval request."""

    kind: Literal["tool_approval"]
    approval_session_id: str
    tool_request_id: str


ActionContinuationRef = (
    ActionUserStepContinuationRef | ActionToolApprovalContinuationRef
)


class ActionJobPayload(TypedDict):
    """Identity-only payload enqueued to the local Action worker."""

    job_id: str
    process_id: str
    action_id: str
    user_id: str
    continuation_ref: ActionContinuationRef


class ActionJobRuntimePayload(TypedDict):
    """Validated identity-only Action worker payload."""

    job_id: str
    process_id: str
    action_id: str
    user_id: str
    continuation_ref: ActionContinuationRef


class ActionSubagentJobPayload(TypedDict):
    """Closed payload bound to one durable Action subagent process."""

    job_id: str
    process_id: str
    user_id: str
    action_id: str
    parent_process_id: str
    inference_profile_id: str
    # The parent's frozen Executing head: the context message the child starts from.
    action_context: str
    task: str
    context_refs: list[str]
    resource_claim_ids: list[str]


class SuggestionJobPayload(TypedDict):
    """Payload enqueued to the local suggestion worker.

    `insight_id` names the short Insight whose `reconsideration_reason` asked
    for the Suggestion; it is the only Suggestion trigger.
    """

    job_id: str
    process_id: str
    suggestion_id: str
    user_id: str
    enqueued_at: str
    insight_id: str


class SuggestionJobRuntimePayload(TypedDict):
    """Suggestion worker payload after JSON parsing."""

    job_id: str
    process_id: str
    suggestion_id: str
    user_id: str
    enqueued_at: str
    insight_id: str


class ActivitySummaryJobPayload(TypedDict):
    """Payload enqueued to the local activity-summary worker."""

    job_id: str
    process_id: str
    summary_id: str
    user_id: str
    enqueued_at: str
    summary_type: str
    period_start: str
    period_end: str


class InsightJobPayload(TypedDict):
    """Payload enqueued to the local short Insight worker.

    `insight_id` is the run identity: the same value identifies the run, the
    stored short Insight and the stored activity log.
    """

    job_id: str
    process_id: str
    insight_id: str
    user_id: str
    period_start: str
    period_end: str
    enqueued_at: str


class MemoryUpdateActionTerminal(TypedDict):
    """One completed Action turn handed to the unified Memory agent.

    The turn binding is captured when the Action terminal is recorded: the
    published revision and the turn's last step number are not derivable later
    because a following turn on the same Action supersedes both.
    """

    source_id: str
    action_id: str
    action_completed_at: str
    source_action_revision_id: NotRequired[str]
    turn_start_step_number: int
    turn_end_step_number: int
    action_prompt_name: str
    action_prompt_version: str
    suggestion_id: NotRequired[str]


class MemoryUpdateJobPayload(TypedDict):
    """Payload enqueued when coalesced Memory Agent triggers become due."""

    job_id: str
    process_id: str
    user_id: str
    enqueued_at: str
    short_insight_ids: list[str]
    summary_ids: list[str]
    action_terminals: list[MemoryUpdateActionTerminal]


__all__ = [
    "ActionContinuationRef",
    "ActionToolApprovalContinuationRef",
    "ActionUserStepContinuationRef",
    "ActivitySummaryJobPayload",
    "ActionJobPayload",
    "ActionJobRuntimePayload",
    "ActionSubagentJobPayload",
    "InsightJobPayload",
    "MemoryUpdateActionTerminal",
    "MemoryUpdateJobPayload",
    "SuggestionJobPayload",
    "SuggestionJobRuntimePayload",
]

"""The events a hook can listen to: a closed list, each with its writer's type.

An event is written in the transaction of the write it records, through
``inbox.record_event``, so a committed write never lacks its event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import ClassVar, Final, Literal, get_args

from pantaray_agents.hooks.definitions import EventValue
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_ACTION_JOB_TYPE,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE,
    PROCESS_KIND_BY_JOB_TYPE,
)

JobPhase = Literal["started", "completed", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class JobLifecycleEvent:
    """A job was claimed (started) or reached its terminal status."""

    job_type: str
    phase: JobPhase

    @property
    def name(self) -> str:
        return f"{self.job_type}.{self.phase}"

    def payload(self) -> Mapping[str, EventValue]:
        return {}


@dataclass(frozen=True, slots=True)
class _DomainEvent:
    NAME: ClassVar[str]

    @property
    def name(self) -> str:
        return self.NAME

    def payload(self) -> Mapping[str, EventValue]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InsightRecorded(_DomainEvent):
    """A short Insight was committed (a skipped window records none)."""

    NAME: ClassVar[str] = "insight.recorded"
    insight_id: str


@dataclass(frozen=True, slots=True)
class ActivitySummaryRecorded(_DomainEvent):
    """An activity summary succeeded."""

    NAME: ClassVar[str] = "activity_summary.recorded"
    summary_id: str
    summary_type: str


@dataclass(frozen=True, slots=True)
class ActionTurnEnded(_DomainEvent):
    """An Action turn reached its terminal, with what it completed.

    The binding is captured here because a following turn on the same Action
    supersedes both the published revision and the turn's last step number. A
    failed or canceled turn publishes no revision.
    """

    NAME: ClassVar[str] = "action.turn_ended"
    source_id: str
    action_id: str
    action_completed_at: str
    source_action_revision_id: str | None
    turn_start_step_number: int
    turn_end_step_number: int
    action_prompt_name: str
    action_prompt_version: str
    suggestion_id: str | None


@dataclass(frozen=True, slots=True)
class ChatMessageRecorded(_DomainEvent):
    """The user or Pantaray said something in the chat."""

    NAME: ClassVar[str] = "chat.message_recorded"
    sequence: int


@dataclass(frozen=True, slots=True)
class SuggestionDismissed(_DomainEvent):
    """The user dismissed a suggestion; it may be undone or answered later."""

    NAME: ClassVar[str] = "suggestion.dismissed"
    suggestion_id: str


HookEvent = (
    JobLifecycleEvent
    | InsightRecorded
    | ActivitySummaryRecorded
    | ActionTurnEnded
    | ChatMessageRecorded
    | SuggestionDismissed
)

# An Action run's terminal is written by the Action terminal repository, not
# by `finalize_local_job`, so its lifecycle events would miss every finished
# turn; no hook may listen to them.
_ACTION_JOB_TYPES: Final = frozenset(
    {LOCAL_ACTION_JOB_TYPE, LOCAL_ACTION_SUBAGENT_JOB_TYPE}
)

EVENT_NAMES: Final = frozenset(
    JobLifecycleEvent(job_type=job_type, phase=phase).name
    for job_type in PROCESS_KIND_BY_JOB_TYPE.keys() - _ACTION_JOB_TYPES
    for phase in get_args(JobPhase)
) | {
    event.NAME
    for event in (
        InsightRecorded,
        ActivitySummaryRecorded,
        ActionTurnEnded,
        ChatMessageRecorded,
        SuggestionDismissed,
    )
}

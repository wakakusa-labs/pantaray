"""The events a hook can listen to: a closed list, each with its writer's type.

An event is written in the transaction of the write it records, through
``inbox.record_event``, so a committed write never lacks its event.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_args

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


HookEvent = JobLifecycleEvent

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
)

"""The built-in hook definitions and the names they may use."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from pantaray_agents.hooks.definitions import (
    HookDefinition,
    HookDefinitionError,
    RunJob,
    load_hook_definitions,
)
from pantaray_agents.local_runtime.activity_summary_schedule import SummaryType
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
    LOCAL_INSIGHT_JOB_TYPE,
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
    LOCAL_SUGGESTION_JOB_TYPE,
)

from .events import EVENT_NAMES

GATE_NAMES: Final = frozenset(
    {"owner_settled", "recording_ready", "background_can_send", "todo_ux_enabled"}
)
# Not a job: the pickup's own submits are idempotent per TODO, so the hook runs
# it in place rather than through the queue.
TODO_PICKUP_RUN: Final = "todo_pickup"
PREDICATE_NAMES: Final = frozenset({"still_dismissed_without_reply"})


def summary_type(run: RunJob) -> SummaryType:
    match dict(run.args):
        case {"summary_type": "1h" | "24h" | "1w" | "1m" | "3m" as value, **rest} if (
            not rest
        ):
            return value
    raise HookDefinitionError(f"a summary hook needs one summary_type: {run.args}")


def _validate_run(run: RunJob) -> None:
    if run.job == LOCAL_ACTIVITY_SUMMARY_JOB_TYPE:
        summary_type(run)
    elif (
        run.job
        not in {
            LOCAL_INSIGHT_JOB_TYPE,
            LOCAL_MEMORY_UPDATE_JOB_TYPE,
            LOCAL_SUGGESTION_JOB_TYPE,
            TODO_PICKUP_RUN,
        }
        or run.args
    ):
        raise HookDefinitionError(f"a hook cannot run {run.job} with {run.args}")


@cache
def builtin_hooks() -> tuple[HookDefinition, ...]:
    return load_hook_definitions(
        (Path(__file__).resolve().parent / "builtin.toml").read_text(encoding="utf-8"),
        gate_names=GATE_NAMES,
        event_names=EVENT_NAMES,
        predicate_names=PREDICATE_NAMES,
        validate_run=_validate_run,
    )

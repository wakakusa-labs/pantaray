"""The jobs a hook can run, and the enqueue the engine calls when one fires."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pantaray_agents.hooks.definitions import HookDefinitionError, RunJob
from pantaray_agents.hooks.engine import Fire, FireOutcome
from pantaray_agents.local_runtime.activity_summary_schedule import SummaryType
from pantaray_agents.local_runtime.runtime.activity_summary_scheduler import (
    enqueue_scheduled_activity_summary,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    enqueue_short_insight_job,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
    LOCAL_INSIGHT_JOB_TYPE,
)


def _summary_type(run: RunJob) -> SummaryType:
    match dict(run.args):
        case {"summary_type": "1h" | "24h" | "1w" | "1m" | "3m" as value, **rest} if (
            not rest
        ):
            return value
    raise HookDefinitionError(f"a summary hook needs one summary_type: {run.args}")


def validate_run(run: RunJob) -> None:
    if run.job == LOCAL_ACTIVITY_SUMMARY_JOB_TYPE:
        _summary_type(run)
    elif run.job != LOCAL_INSIGHT_JOB_TYPE or run.args:
        raise HookDefinitionError(f"a hook cannot run {run.job} with {run.args}")


def enqueue_hook_job(
    fire: Fire, *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> FireOutcome:
    run = fire.hook.run
    if run.job == LOCAL_INSIGHT_JOB_TYPE:
        # A later window while a run is active is dropped, not queued: the run
        # reads from the source cursor, so the next window covers it.
        enqueue_short_insight_job(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            moment=fire.occurrence,
        )
        return "done"
    return asyncio.run(
        enqueue_scheduled_activity_summary(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            summary_type=_summary_type(run),
            now=fire.now,
        )
    )

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pantaray_agents.tasks.internal_jobs.action_subagent import (
    run_action_subagent_job,
)
from pantaray_agents.tasks.internal_jobs.activity import run_activity_summary_job
from pantaray_agents.tasks.internal_jobs.insight import run_insight_job
from pantaray_agents.tasks.internal_jobs.memory_update import run_memory_update
from pantaray_agents.tasks.internal_jobs.suggestion import run_suggestion_job
from pantaray_agents.tasks.types import (
    ActionJobRuntimePayload,
    ActionSubagentJobPayload,
    ActivitySummaryJobPayload,
    InsightJobPayload,
    MemoryUpdateJobPayload,
    SuggestionJobRuntimePayload,
)

from ..storage.migrations import MigrationError
from .admission import admission_is_open
from .connection_store import (
    llm_connection_can_send,
    peek_llm_connection,
    read_llm_route,
)
from .identity import current_owner_id
from .job_claim import (
    FINAL_JOB_STATUS,
    PROCESS_RUNTIME_STATUS,
    claim_next_pending_job,
    peek_next_pending_job_type,
)
from .job_executor import (
    LocalJobExecutionError as LocalJobExecutionError,
)
from .job_executor import (
    execute_claimed_local_job,
)
from .job_payload_models import (
    parse_action_job_payload_json,
    parse_action_subagent_job_payload_json,
    parse_activity_summary_job_payload_json,
    parse_insight_job_payload_json,
    parse_memory_update_job_payload_json,
    parse_suggestion_job_payload_json,
)
from .job_types import (
    LOCAL_ACTION_JOB_TYPE,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE,
    LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
    LOCAL_INSIGHT_JOB_TYPE,
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
    LOCAL_SUGGESTION_JOB_TYPE,
)
from .session_store import (
    AUTH_CONTEXT_PRESENT_STATE,
    read_auth_context_state,
    read_configured,
)

logger = logging.getLogger(__name__)

# Jobs the user is waiting on: the Action turn they submitted, and the subagent
# that turn spawned and cannot finish without.
USER_STARTED_JOB_TYPES: Final[frozenset[str]] = frozenset(
    {LOCAL_ACTION_JOB_TYPE, LOCAL_ACTION_SUBAGENT_JOB_TYPE}
)


@dataclass(frozen=True)
class LocalWorkerSpec:
    job_type: str
    parse_payload: Callable[[str], object]
    run: Callable[[object], None]
    process_pending_status: PROCESS_RUNTIME_STATUS
    process_running_status: PROCESS_RUNTIME_STATUS
    process_success_status: PROCESS_RUNTIME_STATUS
    process_failure_status: PROCESS_RUNTIME_STATUS
    success_job_status: FINAL_JOB_STATUS
    failure_job_status: FINAL_JOB_STATUS
    finalize_after_run: bool = True


def claimable_specs(
    specs: Mapping[str, LocalWorkerSpec],
) -> Mapping[str, LocalWorkerSpec]:
    """The subset of ``specs`` the runtime may claim right now.

    Nothing at all is claimed while the admission gate is closed, not even the
    Action the user just submitted: an identity change is in flight, and a job
    claimed now would start against an identity that is about to be replaced.
    The gate reopens at the end of the barrier and the job is claimed on the next
    poll (design 6.2).

    Nothing is claimed before Electron main applies ``configure``: the runtime
    has not been told the connection yet, and the wait is over in milliseconds
    (design 6.2).

    After that the two kinds part ways. A job the user started is always
    claimed, even with no route and even while the cloud session is expired, so
    that it fails where the GUI shows it rather than sitting queued in silence
    (design 6.4 step 3 and 7.1 step 5). Background jobs have nobody waiting, and
    failing one is final: the activity summary cursor has already advanced and
    the memory triggers are already dispatched, so that period's summary,
    Insight and memory never come back, not even once the user connects. They
    are claimed only when the request could really be sent.

    That means a live cloud session, or a stored connection that can send right
    now. The two states that come back on their own -- an expired cloud session
    and a lapsed ChatGPT token -- are neither, so their background jobs wait
    rather than spend their one attempt (design 6.4 step 3, 6.6).
    """
    if not admission_is_open():
        return {}
    if not read_configured():
        return {}
    if background_requests_can_send():
        return specs
    return {
        job_type: spec
        for job_type, spec in specs.items()
        if job_type in USER_STARTED_JOB_TYPES
    }


def background_requests_can_send() -> bool:
    """Whether work nobody is waiting on could really send a request right now.

    That is a live cloud session, or a stored connection that can send now.
    ``read_llm_route`` answers ``direct`` only for a stored connection with no
    cloud session; it folds an expired session into ``cloud``, which is why a
    live session is checked by state rather than by route. Whether the
    connection itself can send is the same predicate `llm_proxy/client.py`
    refuses on, so background work started here is never one the route turns
    away.
    """
    connection = peek_llm_connection()
    if read_llm_route() == "direct" and connection is not None:
        return llm_connection_can_send(connection)
    return read_auth_context_state() == AUTH_CONTEXT_PRESENT_STATE


ACTION_PROCESS_STATUS_ENQUEUED: PROCESS_RUNTIME_STATUS = "enqueued"
ACTION_PROCESS_STATUS_RUNNING: PROCESS_RUNTIME_STATUS = "running"
ACTION_PROCESS_STATUS_COMPLETED: PROCESS_RUNTIME_STATUS = "completed"
ACTION_PROCESS_STATUS_CANCELED: PROCESS_RUNTIME_STATUS = "canceled"

SUGGESTION_PROCESS_STATUS_ENQUEUED: PROCESS_RUNTIME_STATUS = "enqueued"
SUGGESTION_PROCESS_STATUS_RUNNING: PROCESS_RUNTIME_STATUS = "running"
SUGGESTION_PROCESS_STATUS_SUCCESS: PROCESS_RUNTIME_STATUS = "success"
SUGGESTION_PROCESS_STATUS_ERROR: PROCESS_RUNTIME_STATUS = "error"

INSIGHT_PROCESS_STATUS_ENQUEUED: PROCESS_RUNTIME_STATUS = "enqueued"
INSIGHT_PROCESS_STATUS_RUNNING: PROCESS_RUNTIME_STATUS = "running"
INSIGHT_PROCESS_STATUS_SUCCESS: PROCESS_RUNTIME_STATUS = "success"
INSIGHT_PROCESS_STATUS_ERROR: PROCESS_RUNTIME_STATUS = "error"

MEMORY_PROCESS_STATUS_ENQUEUED: PROCESS_RUNTIME_STATUS = "enqueued"
MEMORY_PROCESS_STATUS_RUNNING: PROCESS_RUNTIME_STATUS = "running"
MEMORY_PROCESS_STATUS_SUCCESS: PROCESS_RUNTIME_STATUS = "success"
MEMORY_PROCESS_STATUS_ERROR: PROCESS_RUNTIME_STATUS = "error"

PROCESS_STATUS_ABANDONED: PROCESS_RUNTIME_STATUS = "abandoned"

ACTIVITY_SUMMARY_PROCESS_STATUS_ENQUEUED: PROCESS_RUNTIME_STATUS = "enqueued"
ACTIVITY_SUMMARY_PROCESS_STATUS_RUNNING: PROCESS_RUNTIME_STATUS = "running"
ACTIVITY_SUMMARY_PROCESS_STATUS_SUCCESS: PROCESS_RUNTIME_STATUS = "success"
ACTIVITY_SUMMARY_PROCESS_STATUS_ERROR: PROCESS_RUNTIME_STATUS = "error"


def build_default_local_worker_specs(
    *,
    action_runner: Callable[[ActionJobRuntimePayload], None],
) -> Mapping[str, LocalWorkerSpec]:
    def _run_action_payload(payload: object) -> None:
        action_runner(cast(ActionJobRuntimePayload, payload))

    def _run_action_subagent_payload(payload: object) -> None:
        run_action_subagent_job(cast(ActionSubagentJobPayload, payload))

    def _run_suggestion_payload(payload: object) -> None:
        run_suggestion_job(cast(SuggestionJobRuntimePayload, payload))

    def _run_activity_summary_payload(payload: object) -> None:
        run_activity_summary_job(cast(ActivitySummaryJobPayload, payload))

    def _run_insight_payload(payload: object) -> None:
        run_insight_job(cast(InsightJobPayload, payload))

    def _run_memory_update_payload(payload: object) -> None:
        run_memory_update(cast(MemoryUpdateJobPayload, payload))

    return {
        LOCAL_ACTION_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_ACTION_JOB_TYPE,
            parse_payload=parse_action_job_payload_json,
            run=_run_action_payload,
            process_pending_status=ACTION_PROCESS_STATUS_ENQUEUED,
            process_running_status=ACTION_PROCESS_STATUS_RUNNING,
            process_success_status=ACTION_PROCESS_STATUS_COMPLETED,
            process_failure_status=ACTION_PROCESS_STATUS_CANCELED,
            success_job_status="completed",
            failure_job_status="canceled",
            finalize_after_run=False,
        ),
        LOCAL_ACTION_SUBAGENT_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_ACTION_SUBAGENT_JOB_TYPE,
            parse_payload=parse_action_subagent_job_payload_json,
            run=_run_action_subagent_payload,
            process_pending_status=ACTION_PROCESS_STATUS_ENQUEUED,
            process_running_status=ACTION_PROCESS_STATUS_RUNNING,
            process_success_status=ACTION_PROCESS_STATUS_COMPLETED,
            process_failure_status="failed",
            success_job_status="completed",
            failure_job_status="failed",
            finalize_after_run=False,
        ),
        LOCAL_SUGGESTION_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_SUGGESTION_JOB_TYPE,
            parse_payload=parse_suggestion_job_payload_json,
            run=_run_suggestion_payload,
            process_pending_status=SUGGESTION_PROCESS_STATUS_ENQUEUED,
            process_running_status=SUGGESTION_PROCESS_STATUS_RUNNING,
            process_success_status=SUGGESTION_PROCESS_STATUS_SUCCESS,
            process_failure_status=SUGGESTION_PROCESS_STATUS_ERROR,
            success_job_status="completed",
            failure_job_status="failed",
        ),
        LOCAL_ACTIVITY_SUMMARY_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_ACTIVITY_SUMMARY_JOB_TYPE,
            parse_payload=parse_activity_summary_job_payload_json,
            run=_run_activity_summary_payload,
            process_pending_status=ACTIVITY_SUMMARY_PROCESS_STATUS_ENQUEUED,
            process_running_status=ACTIVITY_SUMMARY_PROCESS_STATUS_RUNNING,
            process_success_status=ACTIVITY_SUMMARY_PROCESS_STATUS_SUCCESS,
            process_failure_status=ACTIVITY_SUMMARY_PROCESS_STATUS_ERROR,
            success_job_status="completed",
            failure_job_status="failed",
        ),
        LOCAL_INSIGHT_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_INSIGHT_JOB_TYPE,
            parse_payload=parse_insight_job_payload_json,
            run=_run_insight_payload,
            process_pending_status=INSIGHT_PROCESS_STATUS_ENQUEUED,
            process_running_status=INSIGHT_PROCESS_STATUS_RUNNING,
            process_success_status=INSIGHT_PROCESS_STATUS_SUCCESS,
            process_failure_status=INSIGHT_PROCESS_STATUS_ERROR,
            success_job_status="completed",
            failure_job_status="failed",
        ),
        LOCAL_MEMORY_UPDATE_JOB_TYPE: LocalWorkerSpec(
            job_type=LOCAL_MEMORY_UPDATE_JOB_TYPE,
            parse_payload=parse_memory_update_job_payload_json,
            run=_run_memory_update_payload,
            process_pending_status=MEMORY_PROCESS_STATUS_ENQUEUED,
            process_running_status=MEMORY_PROCESS_STATUS_RUNNING,
            process_success_status=MEMORY_PROCESS_STATUS_SUCCESS,
            process_failure_status=MEMORY_PROCESS_STATUS_ERROR,
            success_job_status="completed",
            failure_job_status="failed",
        ),
    }


def run_next_local_job(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    claimed_by: str,
    specs: Mapping[str, LocalWorkerSpec],
) -> bool:
    if not specs:
        raise MigrationError("specs must not be empty")
    claimable = claimable_specs(specs)
    if not claimable:
        return False
    owner_user_id = current_owner_id()

    while True:
        next_job_type = peek_next_pending_job_type(
            db_path=str(db_path),
            busy_timeout_ms=busy_timeout_ms,
            job_types=tuple(claimable),
            owner_user_id=owner_user_id,
        )
        if next_job_type is None:
            return False
        spec = claimable[next_job_type]
        claimed_job = claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=busy_timeout_ms,
            job_type=spec.job_type,
            owner_user_id=owner_user_id,
            claimed_by=claimed_by,
            process_running_status=spec.process_running_status,
            expected_process_pending_status=spec.process_pending_status,
        )
        if claimed_job is None:
            continue
        return execute_claimed_local_job(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            claimed_by=claimed_by,
            claimed_job=claimed_job,
            spec=spec,
        )


def drain_local_jobs(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    claimed_by: str,
    specs: Mapping[str, LocalWorkerSpec],
    max_jobs: int,
) -> int:
    if max_jobs <= 0:
        raise MigrationError("max_jobs must be positive")

    completed = 0
    for _ in range(max_jobs):
        executed = run_next_local_job(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            claimed_by=claimed_by,
            specs=specs,
        )
        if not executed:
            break
        completed += 1
    return completed


def run_next_local_action_job(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    runner: Callable[[ActionJobRuntimePayload], None],
    claimed_by: str,
) -> bool:
    return run_next_local_job(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        claimed_by=claimed_by,
        specs={
            LOCAL_ACTION_JOB_TYPE: build_default_local_worker_specs(
                action_runner=runner
            )[LOCAL_ACTION_JOB_TYPE]
        },
    )


def drain_local_action_jobs(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    runner: Callable[[ActionJobRuntimePayload], None],
    max_jobs: int,
    claimed_by: str,
) -> int:
    return drain_local_jobs(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        claimed_by=claimed_by,
        specs={
            LOCAL_ACTION_JOB_TYPE: build_default_local_worker_specs(
                action_runner=runner
            )[LOCAL_ACTION_JOB_TYPE]
        },
        max_jobs=max_jobs,
    )

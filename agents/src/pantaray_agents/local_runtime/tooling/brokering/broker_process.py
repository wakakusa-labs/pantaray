"""Start, read and stop the processes a run keeps running past its bash calls.

Every call returns the process as the supervisor finds it, in one shape, and
turns the supervisor's refusals into errors that tell the model what to do.
The starting call's command audit row is written as ``running`` and gets the
process's end once a call, or the run's end, first sees it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.tools.contract import BrokerPolicyError

from ...storage.migrations import MigrationError
from ..repository.command_invocation_audits import (
    TerminalOutcome,
    end_running_command_invocation_audit,
)
from ..resources.resource_store import ToolRuntimeResourceSessionConflictError
from ..sandbox.command_egress import command_network_unavailable_error
from ..sandbox.command_sandbox_client import write_command_invocation_audit
from ..sandbox.egress_proxy import EgressProxyStartError, NetworkDestination
from ..sandbox.process_supervisor import (
    ProcessLimitError,
    ProcessRunEndingError,
    ProcessState,
    ProcessStatus,
    UnknownProcessError,
    read_process_output,
    start_process,
    stop_owner_processes,
    stop_process,
)
from ..sandbox.sandbox_denial import sandbox_denial_hint
from .broker_common import BrokerContext
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedCommandRequest
from .process_protocol import (
    ValidatedReadProcessOutputRequest,
    ValidatedStopProcessRequest,
)

PROCESS_LIMIT_REACHED = "PROCESS_LIMIT_REACHED"
PROCESS_NOT_FOUND = "PROCESS_NOT_FOUND"
PROCESS_RUN_ENDING = "PROCESS_RUN_ENDING"

# How an ended process is audited, in the outcomes a one-shot command has: a
# stop is the run cancelling it, and a helper that never reported the end is
# the broker's failure. A command that exits by a signal is ``signaled``.
_OUTCOME_BY_STATUS: dict[ProcessStatus, TerminalOutcome] = {
    "exited": "exited",
    "stopped": "canceled",
    "limit_exceeded": "budget_exceeded",
    "spawn_failed": "spawn_failed",
    "failed": "broker_failed",
}

logger = logging.getLogger(__name__)


class ProcessToolOutput(BaseModel):
    """A process as one call finds it: output new since the previous call that
    returned it, and where it connected over its whole life."""

    model_config = ConfigDict(extra="forbid", strict=True)

    process_id: str
    status: ProcessStatus
    exit_code: int | None = None
    signal: int | None = None
    # Why it could not start (spawn_failed) or what failed (failed).
    error: str | None = None
    stdout: str
    stderr: str
    # Unread output dropped from the middle, marked inline in the text.
    stdout_dropped_bytes: int
    stderr_dropped_bytes: int
    network: list[NetworkDestination]
    # For an exit the sandbox may have caused, as a one-shot command's error has.
    llm_feedback: str | None = None


def _denial_hint(state: ProcessState) -> str | None:
    """The one-shot command's hint, read from the output this call returns."""

    if state.exit_code is None:
        return None
    return sandbox_denial_hint(
        terminal_outcome=_terminal_outcome(state),
        exit_code=state.exit_code,
        stdout=state.stdout.text,
        stderr=state.stderr.text,
        cwd=Path(state.request.cwd),
        credentials_readable=state.request.use_login_environment,
        network_allowed=state.request.network_policy == "allow",
    )


def _process_outcome(state: ProcessState) -> UnprojectedBrokerToolOutcome:
    output = ProcessToolOutput(
        process_id=state.process_id,
        status=state.status,
        exit_code=state.exit_code,
        signal=state.signal,
        error=state.error,
        stdout=state.stdout.text,
        stderr=state.stderr.text,
        stdout_dropped_bytes=state.stdout.dropped_bytes,
        stderr_dropped_bytes=state.stderr.dropped_bytes,
        network=list(state.network),
        llm_feedback=_denial_hint(state),
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output.model_dump(mode="json", exclude_none=True),
        stdout_text=state.stdout.text,
        stderr_text=state.stderr.text,
    )


def _terminal_outcome(state: ProcessState) -> TerminalOutcome:
    if state.status == "exited" and state.signal is not None:
        return "signaled"
    return _OUTCOME_BY_STATUS[state.status]


def _end_audit(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    invocation_id: str,
    outcome: TerminalOutcome,
    state: ProcessState | None,
) -> None:
    """Write how the process ended to its starting call's row, once.

    ``state`` is None when the call ended without one: nothing observed the
    process's output or exit.
    """

    try:
        end_running_command_invocation_audit(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            invocation_id=invocation_id,
            terminal_outcome=outcome,
            exit_code=None if state is None else state.exit_code,
            signal=None if state is None else state.signal,
            stdout_bytes=0 if state is None else state.stdout_bytes,
            stderr_bytes=0 if state is None else state.stderr_bytes,
            budget_exceeded_kind=(
                "temp_storage_limit"
                if state is not None and state.status == "limit_exceeded"
                else None
            ),
            updated_at=now_utc_iso(),
        )
    except (sqlite3.Error, MigrationError):
        # The call's own result goes on; the row stays running until startup
        # recovery ends it as unobserved.
        logger.exception(
            "failed to audit the end of the process started by %s", invocation_id
        )


def _audit_end(*, db_path: Path, busy_timeout_ms: int, state: ProcessState) -> None:
    if state.status != "running":
        _end_audit(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            invocation_id=state.tool_invocation_id,
            outcome=_terminal_outcome(state),
            state=state,
        )


def _listed(process_ids: tuple[str, ...]) -> str:
    return ", ".join(process_ids) if process_ids else "none"


async def start_background_command(
    *, context: BrokerContext, request: ValidatedCommandRequest
) -> UnprojectedBrokerToolOutcome:
    """Start the command as a process of the calling run; returns it running,
    or ended when it ended within the start window."""

    # Written before the start: a call cancelled in the start window, whose
    # process the supervisor then stops, still leaves its row.
    write_command_invocation_audit(
        context=context,
        request=request,
        terminal_outcome="running",
        exit_code=None,
        signal_value=None,
        stdout_bytes=0,
        stderr_bytes=0,
        budget_exceeded_kind=None,
        sandbox_violation_kind=None,
        sandbox_violation_summary=None,
        updated_at=now_utc_iso(),
    )
    try:
        state = await _start(context=context, request=request)
    except (Exception, asyncio.CancelledError) as exc:
        _end_audit(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            invocation_id=cast(str, request.tool_invocation_id),
            outcome=(
                "canceled"
                if isinstance(exc, asyncio.CancelledError)
                # Refused: the command was never started.
                else "spawn_failed"
                if isinstance(exc, BrokerPolicyError)
                else "broker_failed"
            ),
            state=None,
        )
        raise
    _audit_end(
        db_path=context.db_path, busy_timeout_ms=context.busy_timeout_ms, state=state
    )
    return _process_outcome(state)


async def _start(
    *, context: BrokerContext, request: ValidatedCommandRequest
) -> ProcessState:
    try:
        return await start_process(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            owner_process_id=context.actor_process_id,
            request=request,
        )
    except EgressProxyStartError as exc:
        raise command_network_unavailable_error() from exc
    except ProcessLimitError as exc:
        raise BrokerPolicyError(
            "The command did not start: this run already has "
            f"{len(exc.running_ids)} running processes ({_listed(exc.running_ids)}).",
            code=PROCESS_LIMIT_REACHED,
            fix_hint="Stop a process that is no longer needed with stop_process, "
            "then start this one again.",
        ) from exc
    except (ProcessRunEndingError, ToolRuntimeResourceSessionConflictError) as exc:
        raise BrokerPolicyError(
            "The command did not start: this run is ending.",
            code=PROCESS_RUN_ENDING,
        ) from exc


async def run_process_control(
    *,
    context: BrokerContext,
    request: ValidatedReadProcessOutputRequest | ValidatedStopProcessRequest,
) -> UnprojectedBrokerToolOutcome:
    """Read or stop one process of the calling run."""

    process_id = request.args.process_id
    try:
        if isinstance(request, ValidatedReadProcessOutputRequest):
            state = await read_process_output(
                owner_process_id=context.actor_process_id,
                process_id=process_id,
                wait_seconds=request.args.wait_seconds,
            )
        else:
            state = await stop_process(
                owner_process_id=context.actor_process_id, process_id=process_id
            )
    except UnknownProcessError as exc:
        raise BrokerPolicyError(
            f"This run has no process {process_id!r}. Its running processes: "
            f"{_listed(exc.running_ids)}.",
            code=PROCESS_NOT_FOUND,
            fix_hint="Use a process_id a bash call with run_in_background "
            "returned in this run.",
        ) from exc
    _audit_end(
        db_path=context.db_path, busy_timeout_ms=context.busy_timeout_ms, state=state
    )
    return _process_outcome(state)


async def stop_run_processes(
    *, db_path: Path, busy_timeout_ms: int, owner_process_ids: tuple[str, ...]
) -> None:
    """At the end of these runs: stop their processes, audit how each ended,
    and let the supervisor forget them. Only the run end calls it, on a thread
    of its own that a cancelled caller cannot cut short."""

    for states in await asyncio.gather(
        *(
            stop_owner_processes(owner_process_id=owner_process_id)
            for owner_process_id in owner_process_ids
        )
    ):
        for state in states:
            _audit_end(db_path=db_path, busy_timeout_ms=busy_timeout_ms, state=state)


__all__ = [
    "PROCESS_LIMIT_REACHED",
    "PROCESS_NOT_FOUND",
    "PROCESS_RUN_ENDING",
    "ProcessToolOutput",
    "run_process_control",
    "start_background_command",
    "stop_run_processes",
]

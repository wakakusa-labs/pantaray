"""Start, read and stop the processes a run keeps running past its bash calls.

Every call returns the process as the supervisor finds it, in one shape, and
turns the supervisor's refusals into errors that tell the model what to do.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from pantaray_agents.tools.contract import BrokerPolicyError

from ..resources.resource_store import ToolRuntimeResourceSessionConflictError
from ..sandbox.command_egress import command_network_unavailable_error
from ..sandbox.egress_proxy import EgressProxyStartError, NetworkDestination
from ..sandbox.process_supervisor import (
    ProcessLimitError,
    ProcessRunEndingError,
    ProcessState,
    ProcessStatus,
    UnknownProcessError,
    read_process_output,
    start_process,
    stop_process,
)
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
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output.model_dump(mode="json", exclude_none=True),
        stdout_text=state.stdout.text,
        stderr_text=state.stderr.text,
    )


def _listed(process_ids: tuple[str, ...]) -> str:
    return ", ".join(process_ids) if process_ids else "none"


async def start_background_command(
    *, context: BrokerContext, request: ValidatedCommandRequest
) -> UnprojectedBrokerToolOutcome:
    """Start the command as a process of the calling run; returns it running,
    or ended when it ended within the start window."""

    try:
        state = await start_process(
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
    return _process_outcome(state)


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
    return _process_outcome(state)


__all__ = [
    "PROCESS_LIMIT_REACHED",
    "PROCESS_NOT_FOUND",
    "PROCESS_RUN_ENDING",
    "ProcessToolOutput",
    "run_process_control",
    "start_background_command",
]

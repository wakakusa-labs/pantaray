"""Persist broker failures and cancellations with their diagnostic evidence."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import cast

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from ..sandbox.command_sandbox_client import canceled_command_output
from ..tool_result_finalization import (
    FinalizedToolOutput,
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)

BROKER_CANCELED_MESSAGE = "canceled before completion"


def finalize_broker_invocation_error(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    invocation_id: str,
    error: BaseException,
) -> FinalizedToolOutput:
    output = build_runtime_tool_error_output(
        error_type=error.__class__.__name__, message=str(error)
    )
    error_payload = cast(dict[str, JSONValue], output["error"])
    error_payload["traceback"] = "".join(traceback.format_exception(error))
    return finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=invocation_id,
                completed_at=now_utc_iso(),
                status="failed",
                completion_scope="execution",
            ),
            output=output,
        ),
    )


def finalize_canceled_broker_invocation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    invocation_id: str,
    error: BaseException,
) -> FinalizedToolOutput:
    """Close a canceled invocation with whatever the call had already produced.

    A canceled command is killed mid-stream, so its terminal row carries the
    output written before the kill instead of an empty error: that is what the
    Supervisor and the user get to see about work that did happen.

    The reason stays neutral here. This boundary owns "the call did not finish",
    not why: the same cancellation reaches it from a user Stop, from the
    per-call ``asyncio.timeout`` of a parallel batch, and from worker shutdown.
    The caller that knows which one it was writes the reason on the step row
    (``TOOL_BATCH_TIMEOUT`` for a timeout, ``ACTION_TOOL_CANCELED`` for a Stop).
    """

    partial = canceled_command_output(error)
    output = build_runtime_tool_error_output(
        error_type="ToolCallCanceled",
        message=BROKER_CANCELED_MESSAGE,
    )
    if partial is not None:
        output = {
            **output,
            "exit_code": -1,
            "stdout": partial.stdout,
            "stderr": partial.stderr,
        }
    return finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=invocation_id,
                completed_at=now_utc_iso(),
                status="canceled",
                completion_scope="execution",
            ),
            output=output,
            stdout_text=partial.stdout if partial is not None else None,
            stderr_text=partial.stderr if partial is not None else None,
        ),
    )

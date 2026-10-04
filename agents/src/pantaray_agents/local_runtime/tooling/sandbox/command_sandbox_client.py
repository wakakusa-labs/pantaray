from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ...storage.migrations import MigrationError
from ..action_session_temp_paths import create_private_temp_dir
from ..brokering.broker_common import (
    BROKER_TOOL_TIMEOUT_ERROR_TYPE,
    BrokerContext,
    BrokerExecutionError,
)
from ..brokering.broker_outcome import UnprojectedBrokerToolOutcome
from ..brokering.broker_protocol import (
    BashToolOutput,
    ToolError,
    ValidatedCommandRequest,
)
from ..repository import (
    CommandInvocationAuditUpsertInput,
    upsert_command_invocation_audit,
)
from ..repository.command_invocation_audits import TerminalOutcome
from ..resources.cleanup import CleanupFailure, terminate_process_group
from ..resources.resource_tracking import (
    mark_resource_cleaned_if_tracked,
    mark_resource_cleanup_failed_if_tracked,
    register_path_resource,
    register_process_group_resource,
)
from .command_sandbox_protocol import (
    BrokerToSandboxCommandRequest,
    SandboxCommandCompletion,
    SandboxOutputChunk,
    iter_decoded_messages,
)
from .command_sandbox_request import build_sandbox_request
from .sandbox_denial import WRITE_FOLDER_REQUEST_HINT, is_likely_sandbox_denied

SANDBOX_TEMP_DIR_PREFIX = "pantaray-command-sandbox-"
type CreateSubprocessExec = Callable[..., Awaitable[asyncio.subprocess.Process]]
type RegisterResource = Callable[..., str | None]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CanceledCommandOutput:
    """What a killed command had already written when it was stopped."""

    stdout: str
    stderr: str


_CANCELED_OUTPUT_ATTR = "pantaray_canceled_command_output"


def attach_canceled_command_output(
    error: BaseException,
    *,
    stdout: str,
    stderr: str,
) -> None:
    """Carry the partial output out on the cancellation that ends the command.

    The cancellation itself must keep propagating, so the only channel that
    survives to the broker - the boundary that owns this invocation's terminal
    row - is the exception instance it is about to re-raise.
    """

    setattr(
        error,
        _CANCELED_OUTPUT_ATTR,
        CanceledCommandOutput(stdout=stdout, stderr=stderr),
    )


def canceled_command_output(error: BaseException) -> CanceledCommandOutput | None:
    """Read back the partial output of a canceled command, when there was one."""

    value = getattr(error, _CANCELED_OUTPUT_ATTR, None)
    return value if isinstance(value, CanceledCommandOutput) else None


def _cleanup_temp_dir(temp_dir: Path) -> str | None:
    try:
        shutil.rmtree(temp_dir)
    except OSError as exc:
        return str(exc)
    return None


def _finalize_temp_dir_resource(
    *,
    context: BrokerContext,
    temp_dir: Path,
    temp_resource_id: str | None,
) -> None:
    temp_cleanup_error = _cleanup_temp_dir(temp_dir)
    if temp_cleanup_error is not None:
        mark_resource_cleanup_failed_if_tracked(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            resource_id=temp_resource_id,
            failed_at=now_utc_iso(),
            cleanup_error=temp_cleanup_error,
        )
        return
    mark_resource_cleaned_if_tracked(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        resource_id=temp_resource_id,
        cleaned_at=now_utc_iso(),
    )


def _write_command_invocation_audit(
    *,
    context: BrokerContext,
    request: ValidatedCommandRequest,
    terminal_outcome: str,
    exit_code: int | None,
    signal_value: int | None,
    stdout_bytes: int,
    stderr_bytes: int,
    budget_exceeded_kind: str | None,
    sandbox_violation_kind: str | None,
    sandbox_violation_summary: str | None,
    updated_at: str,
) -> None:
    upsert_command_invocation_audit(
        db_path=context.db_path,
        busy_timeout_ms=context.busy_timeout_ms,
        audit=CommandInvocationAuditUpsertInput(
            invocation_id=cast(str, request.tool_invocation_id),
            approval_session_id=request.approval_session_id,
            execution_kind=request.execution_kind,
            executable_source_kind=request.executable_source_kind,
            resolved_executable_path=request.resolved_executable_path,
            terminal_outcome=terminal_outcome,  # type: ignore[arg-type]
            exit_code=exit_code,
            signal=signal_value,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_max_bytes=request.stdout_max_bytes,
            stderr_max_bytes=request.stderr_max_bytes,
            temp_storage_limit_bytes=request.temp_storage_limit_bytes,
            child_count_limit=request.child_count_limit,
            open_file_lease_limit=request.open_file_lease_limit,
            budget_exceeded_kind=budget_exceeded_kind,  # type: ignore[arg-type]
            sandbox_violation_kind=sandbox_violation_kind,  # type: ignore[arg-type]
            sandbox_violation_summary=sandbox_violation_summary,
            created_at=request.requested_at,
            updated_at=updated_at,
        ),
    )


async def spawn_sandbox_helper(
    create_subprocess_exec: CreateSubprocessExec,
) -> asyncio.subprocess.Process:
    """Start the helper as the leader of its own process group."""

    return await create_subprocess_exec(
        sys.executable,
        "-m",
        "pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
    )


@dataclass(slots=True)
class CapturedCommandOutput:
    """The output received so far, still readable after a cancellation."""

    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SandboxExchange:
    completion: SandboxCommandCompletion | None
    receipt_error: str | None
    helper_stderr: str


async def exchange_with_sandbox_helper(
    *,
    process: asyncio.subprocess.Process,
    sandbox_request: BrokerToSandboxCommandRequest,
    captured: CapturedCommandOutput,
) -> SandboxExchange:
    """Send the request, collect output until the receipt, then reap the group.

    A cancellation propagates with the group still running: the caller kills it,
    because only the caller knows what else the cancellation must record.
    """

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    completion: SandboxCommandCompletion | None = None
    receipt_error: str | None = None
    try:
        try:
            process.stdin.write(
                sandbox_request.model_dump_json().encode("utf-8") + b"\n"
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as send_error:
            receipt_error = f"command sandbox closed its input: {type(send_error).__name__}: {send_error}"
        finally:
            process.stdin.close()
        try:
            async for message in iter_decoded_messages(process.stdout):
                if isinstance(message, SandboxOutputChunk):
                    if message.stream == "stdout":
                        captured.stdout.append(message.data)
                    else:
                        captured.stderr.append(message.data)
                    continue
                if isinstance(message, SandboxCommandCompletion):
                    completion = message
                    break
        except ValueError as decode_error:
            receipt_error = f"invalid command sandbox receipt: {decode_error}"
        # The helper stays alive after reporting completion so its tracked group
        # still owns every command process until the broker has reaped the group.
        if process.returncode is None:
            await terminate_process_group(
                process=process,
                signal_to_send=signal.SIGKILL,
                failure_message="failed to terminate completed command sandbox process group",
            )
        else:
            await process.wait()
    except asyncio.CancelledError:
        stderr_task.cancel()
        raise
    return SandboxExchange(
        completion=completion,
        receipt_error=receipt_error,
        helper_stderr=(await stderr_task).decode("utf-8", errors="replace"),
    )


def classify_command_outcome(
    *,
    sandbox_request: BrokerToSandboxCommandRequest,
    exchange: SandboxExchange,
    stdout_text: str,
    stderr_text: str,
) -> tuple[TerminalOutcome, BashToolOutput]:
    completion = exchange.completion
    terminal_outcome: TerminalOutcome
    error: ToolError | None = None
    exit_code = (
        completion.exit_code
        if completion is not None and completion.exit_code is not None
        else -1
    )
    signal_value = completion.signal if completion is not None else None
    if completion is None:
        terminal_outcome = "broker_failed"
        error = ToolError(
            type="CommandSandboxError",
            message=exchange.receipt_error
            or "command sandbox ended without a completion receipt; inspect stdout and stderr",
            exit_code=exit_code,
        )
    elif completion.outcome == "timed_out":
        terminal_outcome = "timed_out"
        error = ToolError(
            type=BROKER_TOOL_TIMEOUT_ERROR_TYPE,
            message=f"command timed out after {sandbox_request.timeout_ms} ms; captured output is partial",
            exit_code=exit_code,
        )
    elif completion.outcome == "canceled":
        terminal_outcome = "budget_exceeded"
        limit_kind = completion.budget_exceeded_kind
        assert limit_kind is not None  # Validated at the helper protocol boundary.
        limits = {
            "stdout_limit": sandbox_request.stdout_max_bytes,
            "stderr_limit": sandbox_request.stderr_max_bytes,
            "temp_storage_limit": sandbox_request.temp_storage_limit_bytes,
        }
        error = ToolError(
            type="CommandResourceLimitExceededError",
            message=f"command exceeded {limit_kind} ({limits[limit_kind]} bytes); captured output is partial",
            code=limit_kind,
            exit_code=exit_code,
        )
    else:
        terminal_outcome = completion.outcome
        if completion.outcome != "exited" or exit_code != 0:
            reason = (
                f"command terminated by signal {signal_value}"
                if completion.outcome == "signaled"
                else "command could not be started"
                if completion.outcome == "spawn_failed"
                else f"command exited with code {exit_code}"
            )
            error = ToolError(
                type="CommandExecutionError",
                message=stderr_text or stdout_text or reason,
                exit_code=exit_code,
            )
    return terminal_outcome, BashToolOutput(
        status="success" if error is None else "error",
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        signal=signal_value,
        error=error,
    )


async def run_command_via_sandbox(
    *,
    context: BrokerContext,
    request: ValidatedCommandRequest,
    create_subprocess_exec: CreateSubprocessExec = asyncio.create_subprocess_exec,
    register_process_group_resource_fn: RegisterResource = register_process_group_resource,
    register_path_resource_fn: RegisterResource = register_path_resource,
) -> UnprojectedBrokerToolOutcome:
    tool_invocation_id = request.tool_invocation_id
    if tool_invocation_id is None:
        raise BrokerExecutionError("command execution requires an invocation id")
    temp_dir = create_private_temp_dir(
        db_path=context.db_path, prefix=SANDBOX_TEMP_DIR_PREFIX
    )
    try:
        temp_resource_id = register_path_resource_fn(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            execution_session_id=context.execution_session.execution_session_id,
            tool_invocation_id=tool_invocation_id,
            action_id=context.execution_session.action_id,
            resource_kind="temp_dir",
            resource_path=temp_dir,
            created_at=request.requested_at,
        )
    except Exception as exc:
        cleanup_error = _cleanup_temp_dir(temp_dir)
        if cleanup_error is not None:
            raise BrokerExecutionError(
                "failed to remove untracked command sandbox directory"
            ) from exc
        raise
    try:
        sandbox_request = build_sandbox_request(request=request, temp_dir=temp_dir)
    except (MigrationError, OSError, ValueError):
        _finalize_temp_dir_resource(
            context=context, temp_dir=temp_dir, temp_resource_id=temp_resource_id
        )
        raise
    try:
        process = await spawn_sandbox_helper(create_subprocess_exec)
    except Exception as exc:
        _write_command_invocation_audit(
            context=context,
            request=request,
            terminal_outcome="broker_failed",
            exit_code=None,
            signal_value=None,
            stdout_bytes=0,
            stderr_bytes=0,
            budget_exceeded_kind=None,
            sandbox_violation_kind=None,
            sandbox_violation_summary=str(exc),
            updated_at=now_utc_iso(),
        )
        _finalize_temp_dir_resource(
            context=context,
            temp_dir=temp_dir,
            temp_resource_id=temp_resource_id,
        )
        raise
    try:
        process_resource_id = register_process_group_resource_fn(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            execution_session_id=context.execution_session.execution_session_id,
            tool_invocation_id=request.tool_invocation_id,
            action_id=context.execution_session.action_id,
            pid=process.pid,
            created_at=request.requested_at,
        )
    except Exception as exc:
        process_cleanup_error: str | None = None
        try:
            await terminate_process_group(
                process=process,
                signal_to_send=signal.SIGKILL,
                failure_message="failed to terminate command sandbox after resource registration failure",
            )
        except CleanupFailure as cleanup_exc:
            process_cleanup_error = str(cleanup_exc)
        _write_command_invocation_audit(
            context=context,
            request=request,
            terminal_outcome="broker_failed",
            exit_code=None,
            signal_value=None,
            stdout_bytes=0,
            stderr_bytes=0,
            budget_exceeded_kind=None,
            sandbox_violation_kind=None,
            sandbox_violation_summary="failed to register command sandbox cleanup resource",
            updated_at=now_utc_iso(),
        )
        _finalize_temp_dir_resource(
            context=context,
            temp_dir=temp_dir,
            temp_resource_id=temp_resource_id,
        )
        if process_cleanup_error is not None:
            raise BrokerExecutionError(process_cleanup_error) from exc
        raise BrokerExecutionError(
            "failed to register command sandbox cleanup resource"
        ) from exc

    captured = CapturedCommandOutput()
    try:
        exchange = await exchange_with_sandbox_helper(
            process=process, sandbox_request=sandbox_request, captured=captured
        )
    except asyncio.CancelledError as canceled:
        # 監査行の signal は、送ろうとしたシグナルではなく OS の wait status が示す
        # 「実際にプロセスを終わらせたシグナル」を載せる。cancel が届く前に自分で
        # 終了していた場合も、kill に失敗した場合も、SIGKILL による終了ではない。
        if process.returncode is None:
            try:
                await terminate_process_group(
                    process=process,
                    signal_to_send=signal.SIGKILL,
                    failure_message="failed to terminate canceled command sandbox process group",
                )
            except CleanupFailure as exc:
                mark_resource_cleanup_failed_if_tracked(
                    db_path=context.db_path,
                    busy_timeout_ms=context.busy_timeout_ms,
                    resource_id=process_resource_id,
                    failed_at=now_utc_iso(),
                    cleanup_error=str(exc),
                )
        terminating_signal = (
            -process.returncode
            if process.returncode is not None and process.returncode < 0
            else None
        )
        canceled_stdout = "".join(captured.stdout)
        canceled_stderr = "".join(captured.stderr)
        try:
            _write_command_invocation_audit(
                context=context,
                request=request,
                terminal_outcome="canceled",
                exit_code=None,
                signal_value=terminating_signal,
                stdout_bytes=len(canceled_stdout.encode("utf-8")),
                stderr_bytes=len(canceled_stderr.encode("utf-8")),
                budget_exceeded_kind=None,
                sandbox_violation_kind=None,
                sandbox_violation_summary=None,
                updated_at=now_utc_iso(),
            )
        except Exception:
            # The kill already happened; a lost audit row must not replace the
            # cancellation, which the caller is waiting on to finalize the call.
            logger.exception(
                "failed to audit the canceled command invocation %s",
                tool_invocation_id,
            )
        attach_canceled_command_output(
            canceled,
            stdout=canceled_stdout,
            stderr=canceled_stderr,
        )
        # The temp dir and the process-group row stay registered: the Action's
        # post-terminal cleanup pass owns reclaiming them, and doing filesystem
        # work here could only turn a cancellation into a different failure.
        raise

    stdout_text = "".join(captured.stdout)
    stderr_text = "".join(captured.stderr) + exchange.helper_stderr

    _finalize_temp_dir_resource(
        context=context,
        temp_dir=temp_dir,
        temp_resource_id=temp_resource_id,
    )

    completion = exchange.completion
    terminal_outcome, output = classify_command_outcome(
        sandbox_request=sandbox_request,
        exchange=exchange,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    # Only here: an Action command can ask for write folders. A suggestion
    # command cannot, and a subagent is refused them without asking (its
    # failed call shows the error message alone, without this feedback). An
    # unsandboxed run was blocked by something other than the sandbox.
    if (
        output.error is not None
        and not request.run_outside_sandbox
        and is_likely_sandbox_denied(
            terminal_outcome=terminal_outcome,
            exit_code=output.exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
        )
    ):
        output.error.llm_feedback = WRITE_FOLDER_REQUEST_HINT
    _write_command_invocation_audit(
        context=context,
        request=request,
        terminal_outcome=terminal_outcome,
        exit_code=completion.exit_code if completion is not None else None,
        signal_value=output.signal,
        stdout_bytes=len(stdout_text.encode("utf-8")),
        stderr_bytes=len(stderr_text.encode("utf-8")),
        budget_exceeded_kind=completion.budget_exceeded_kind
        if completion is not None
        else None,
        sandbox_violation_kind=None,
        sandbox_violation_summary=None,
        updated_at=now_utc_iso(),
    )
    return UnprojectedBrokerToolOutcome(
        status="success" if output.error is None else "error",
        output=output.model_dump(mode="python", exclude_none=True),
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )

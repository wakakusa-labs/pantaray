"""Run a sandboxed command that belongs to no Action.

An Action's command is audited and its process group and temp dir are tracked
for crash recovery, all against the Action's execution session. A command with
no Action has none of those rows, so this path records nothing and reclaims its
process group and temp dir itself before it returns or propagates.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import signal
from collections.abc import Callable
from pathlib import Path

from ..action_session_temp_paths import create_private_temp_dir
from ..brokering.broker_protocol import BashToolOutput
from ..repository.command_invocation_audits import TerminalOutcome
from ..resources.cleanup import CleanupFailure, terminate_process_group
from .command_sandbox_client import (
    SANDBOX_TEMP_DIR_PREFIX,
    CapturedCommandOutput,
    CreateSubprocessExec,
    classify_command_outcome,
    exchange_with_sandbox_helper,
    spawn_sandbox_helper,
)
from .command_sandbox_protocol import BrokerToSandboxCommandRequest

logger = logging.getLogger(__name__)


async def run_unrecorded_sandbox_command(
    *,
    db_path: Path,
    build_request: Callable[[Path], BrokerToSandboxCommandRequest],
    create_subprocess_exec: CreateSubprocessExec = asyncio.create_subprocess_exec,
) -> tuple[TerminalOutcome, BashToolOutput]:
    """Run one command in a fresh temp dir that `build_request` receives."""

    temp_dir = create_private_temp_dir(db_path=db_path, prefix=SANDBOX_TEMP_DIR_PREFIX)
    try:
        sandbox_request = build_request(temp_dir)
        process = await spawn_sandbox_helper(create_subprocess_exec)
        captured = CapturedCommandOutput()
        try:
            exchange = await exchange_with_sandbox_helper(
                process=process, sandbox_request=sandbox_request, captured=captured
            )
        except BaseException:
            # Cancellation included: no recovery pass knows this group exists.
            if process.returncode is None:
                try:
                    await terminate_process_group(
                        process=process,
                        signal_to_send=signal.SIGKILL,
                        failure_message="failed to terminate unrecorded command sandbox",
                    )
                except CleanupFailure:
                    logger.exception("failed to terminate an unrecorded command")
            raise
    finally:
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            # A leftover temp dir is not worth failing a finished command over.
            logger.warning("failed to remove a command temp dir", exc_info=True)
    return classify_command_outcome(
        sandbox_request=sandbox_request,
        exchange=exchange,
        stdout_text="".join(captured.stdout),
        stderr_text="".join(captured.stderr) + exchange.helper_stderr,
    )


__all__ = ["run_unrecorded_sandbox_command"]

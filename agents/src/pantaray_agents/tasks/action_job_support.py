from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

import pantaray_agents.dependencies as deps
from pantaray_agents.action_status import (
    FAILED_ACTION_TERMINAL_STATUSES,
    ActionTerminalStatus,
    FinalizeActionTerminalCommand,
    is_action_terminal_status,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    ActionTerminalRepository,
    InvalidActionMemoryDraftError,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    resolve_local_runtime_db_config,
)
from pantaray_agents.schema.agent.action import RuntimeStateCheckpointPayload
from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

logger = logging.getLogger(__name__)

_TERMINAL_PERSIST_RETRY_INITIAL_DELAY_SECONDS: Final[float] = 1.0
_TERMINAL_PERSIST_RETRY_MAX_DELAY_SECONDS: Final[float] = 10.0
# 1.0s * 2 ** 8 = 256s already exceeds the capped delay by a wide margin.
_TERMINAL_PERSIST_RETRY_MAX_EXPONENT: Final[int] = 8
ACTION_JOB_EXCEPTION_ERROR_CODE: Final[str] = "ACTION_JOB_EXCEPTION"
ACTION_JOB_EXCEPTION_ERROR_TYPE: Final[str] = "internal_error"
ACTION_JOB_EXCEPTION_ERROR_MESSAGE: Final[str] = "Action job failed in worker"
ACTION_PROCESSING_ERROR_CODE: Final[str] = "ACTION_PROCESSING_ERROR"
ACTION_FAILURE_STAGE_START_FAILED: Final[str] = "start_failed"
ACTION_FAILURE_STAGE_PERSIST_FINAL_STATE_FAILED: Final[str] = (
    "persist_final_state_failed"
)
ACTION_FAILURE_CODE_PERSIST_TERMINAL_PAYLOAD_INVALID: Final[str] = (
    "ACTION_PERSIST_TERMINAL_PAYLOAD_INVALID"
)
ACTION_FAILURE_CODE_PERSIST_TERMINAL_STATE_INCONSISTENT: Final[str] = (
    "ACTION_PERSIST_TERMINAL_STATE_INCONSISTENT"
)
ACTION_FAILURE_CODE_PERSIST_TERMINAL_RUNTIME_DISABLED: Final[str] = (
    "ACTION_PERSIST_TERMINAL_RUNTIME_DISABLED"
)
ACTION_FAILURE_CODE_LEGACY_STATE_UNMIGRATED: Final[str] = (
    "ACTION_LEGACY_STATE_UNMIGRATED"
)
ACTION_HEADER_PROMPT_NAME: Final[str] = "action/executing"
ACTION_HEADER_PROMPT_VERSION: Final[str] = "1.0"


@dataclass(frozen=True)
class PersistedActionTerminalResult:
    """DB-first terminalization result."""

    action_status: ActionTerminalStatus
    process_completed_sequence: int
    action_failure_code: str | None = None
    final_output: str | None = None
    failure_stage: str | None = None
    failure_message_public: str | None = None


class NonRetryableTerminalPersistenceError(RuntimeError):
    """再試行しても解消しない terminal persist failure。"""


class InvalidTerminalPayloadError(NonRetryableTerminalPersistenceError):
    """terminal payload が canonical contract に違反している。"""


class InvalidTerminalStateError(NonRetryableTerminalPersistenceError):
    """terminal persistence の返却状態が設計と矛盾している。"""


class LocalRuntimeActionJobsDisabledError(NonRetryableTerminalPersistenceError):
    """Action job が local runtime 無効のまま起動された。"""


def is_non_retryable_terminal_persistence_error(exc: BaseException) -> bool:
    return isinstance(
        exc, (NonRetryableTerminalPersistenceError, InvalidActionMemoryDraftError)
    )


def is_retryable_terminal_persistence_error(exc: BaseException) -> bool:
    """Retry only failures explicitly classified as transient."""

    if is_non_retryable_terminal_persistence_error(exc):
        return False
    return is_retryable_repository_exception(exc)


def terminal_persistence_failure_code(exc: BaseException) -> str:
    if isinstance(exc, InvalidTerminalPayloadError):
        return ACTION_FAILURE_CODE_PERSIST_TERMINAL_PAYLOAD_INVALID
    if isinstance(exc, InvalidActionMemoryDraftError):
        return ACTION_FAILURE_CODE_PERSIST_TERMINAL_PAYLOAD_INVALID
    if isinstance(exc, InvalidTerminalStateError):
        return ACTION_FAILURE_CODE_PERSIST_TERMINAL_STATE_INCONSISTENT
    if isinstance(exc, LocalRuntimeActionJobsDisabledError):
        return ACTION_FAILURE_CODE_PERSIST_TERMINAL_RUNTIME_DISABLED
    if isinstance(exc, NonRetryableTerminalPersistenceError):
        return ACTION_FAILURE_CODE_PERSIST_TERMINAL_STATE_INCONSISTENT
    return ACTION_FAILURE_CODE_PERSIST_TERMINAL_STATE_INCONSISTENT


def terminal_retry_delay_seconds(*, consecutive_failures: int) -> float:
    attempts = max(1, int(consecutive_failures))
    # The exponent is clamped before it is expanded: a long child settlement
    # wait reaches four-digit attempt counts, and 2 ** 1024 overflows the float
    # conversion long before the capped delay would change.
    exponent = min(attempts - 1, _TERMINAL_PERSIST_RETRY_MAX_EXPONENT)
    delay = _TERMINAL_PERSIST_RETRY_INITIAL_DELAY_SECONDS * (2**exponent)
    return float(min(delay, _TERMINAL_PERSIST_RETRY_MAX_DELAY_SECONDS))


async def persist_terminal_action_status_strict(
    *,
    command: FinalizeActionTerminalCommand,
    job_id: str,
    runtime_state_checkpoint: RuntimeStateCheckpointPayload | None,
) -> PersistedActionTerminalResult:
    if deps.is_mock_mode():
        return PersistedActionTerminalResult(
            action_status=command.action_status,
            process_completed_sequence=0,
            action_failure_code=command.failure_code,
            final_output=command.final_output,
            failure_stage=command.failure_stage,
            failure_message_public=command.failure_message_public,
        )
    db_path, busy_timeout_ms = resolve_local_runtime_db_config(
        fallback=read_local_runtime_db_config
    )
    repo = ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    result = await repo.finalize_action_job_terminal(
        command=command,
        job_id=job_id,
        runtime_state_checkpoint=runtime_state_checkpoint,
    )
    action_status = str(result.action_status).strip().lower()
    if not is_action_terminal_status(action_status):
        raise InvalidTerminalPayloadError(
            f"Invalid action terminal status from local runtime: {result.action_status!r}"
        )

    return PersistedActionTerminalResult(
        action_status=action_status,
        process_completed_sequence=int(result.process_completed_sequence),
        action_failure_code=result.action_failure_code,
        final_output=result.final_output,
        failure_stage=result.failure_stage,
        failure_message_public=result.failure_message_public,
    )


__all__ = [
    "ACTION_FAILURE_STAGE_PERSIST_FINAL_STATE_FAILED",
    "ACTION_FAILURE_STAGE_START_FAILED",
    "ACTION_FAILURE_CODE_LEGACY_STATE_UNMIGRATED",
    "ACTION_FAILURE_CODE_PERSIST_TERMINAL_PAYLOAD_INVALID",
    "ACTION_FAILURE_CODE_PERSIST_TERMINAL_RUNTIME_DISABLED",
    "ACTION_FAILURE_CODE_PERSIST_TERMINAL_STATE_INCONSISTENT",
    "ACTION_HEADER_PROMPT_NAME",
    "ACTION_HEADER_PROMPT_VERSION",
    "ACTION_JOB_EXCEPTION_ERROR_CODE",
    "ACTION_JOB_EXCEPTION_ERROR_MESSAGE",
    "ACTION_JOB_EXCEPTION_ERROR_TYPE",
    "ACTION_PROCESSING_ERROR_CODE",
    "FAILED_ACTION_TERMINAL_STATUSES",
    "InvalidTerminalPayloadError",
    "InvalidTerminalStateError",
    "LocalRuntimeActionJobsDisabledError",
    "NonRetryableTerminalPersistenceError",
    "PersistedActionTerminalResult",
    "is_non_retryable_terminal_persistence_error",
    "is_retryable_terminal_persistence_error",
    "persist_terminal_action_status_strict",
    "terminal_retry_delay_seconds",
    "terminal_persistence_failure_code",
]

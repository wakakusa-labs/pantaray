from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from pantaray_agents.local_runtime.runtime.action_cancel_repository import (
    ActionCancelNotFoundError,
    ActionCancelRepository,
    ActionCancelStateConflictError,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.identity import (
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    append_local_process_event,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    ActionSessionTempPathError,
    resolve_action_session_temp_leaf,
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_authority import (
    load_action_session_temp_cleanup_receipt,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    ActionSessionTempCleanupAuthorityError,
    cleanup_action_session_temp,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    ActionCleanupPassResult,
    cancel_action_runtime_resources,
)
from pantaray_agents.local_runtime.tooling.resources.resource_db_support import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    list_inflight_tool_invocation_ids_for_action_session,
)
from pantaray_agents.local_runtime.tooling.resources.tool_invocation_recovery import (
    cancel_inflight_tool_invocation,
)

logger = logging.getLogger(__name__)

ACTION_CANCEL_STATE_CONFLICT_DETAIL = "Action cancel state conflict"
ACTION_CANCEL_CLEANUP_WARNING_EVENT = "action_cleanup_warning"
ACTION_CANCEL_CLEANUP_FAILED_WARNING_CODE = "ACTION_CANCEL_CLEANUP_FAILED"
ACTION_CANCEL_CLEANUP_ABANDONED_WARNING_CODE = "ACTION_CANCEL_CLEANUP_ABANDONED"
ACTION_CANCEL_CLEANUP_PERSISTENCE_WARNING_CODE = (
    "ACTION_CANCEL_CLEANUP_PERSISTENCE_FAILED"
)
ACTION_CANCEL_TOOL_INVOCATION_WARNING_EVENT = "action_tool_invocation_warning"
ACTION_CANCEL_TOOL_INVOCATION_WARNING_CODE = (
    "ACTION_CANCEL_TOOL_INVOCATION_RECOVERY_FAILED"
)


@dataclass(frozen=True, slots=True)
class ActionToolInvocationCancelWarning:
    attempted_invocation_count: int
    canceled_invocation_count: int
    persistence_failure_count: int
    failed_invocation_ids: tuple[str, ...]

    @property
    def warning_message(self) -> str:
        return (
            "failed to converge some tool invocations after action cancel: "
            f"attempted={self.attempted_invocation_count} "
            f"canceled={self.canceled_invocation_count} "
            f"persistence_failures={self.persistence_failure_count}"
        )


def _session_root_is_cleaned(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    session_id: str,
) -> bool:
    leaf = resolve_action_session_temp_leaf(
        paths=resolve_action_storage_paths(
            db_path=db_path.resolve(), user_id=user_id, action_id=action_id
        ),
        execution_session_id=session_id,
    )
    with sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro", uri=True
    ) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            """
            SELECT session.user_id = ? AND session.action_id = ?
                AND session.status IN ('completed', 'failed', 'canceled', 'expired')
                AND session.action_temp_dir IS ? AND action.user_id = ?
                AND root.action_id IS ? AND root.resource_path IS ?
                AND root.status = 'cleaned' AND root.cleaned_at IS NOT NULL
                AND root.cleanup_error IS NULL
                AND root.pid IS NULL AND root.pgid IS NULL
                AND root.process_start_signature IS NULL AND root.lock_id IS NULL
                AS is_cleaned
            FROM tool_runtime_resources AS root
            LEFT JOIN execution_sessions AS session
              ON session.execution_session_id = root.execution_session_id
            LEFT JOIN agent_actions AS action ON action.action_id = session.action_id
            WHERE root.execution_session_id = ?
              AND root.resource_kind = 'temp_dir'
              AND root.tool_invocation_id IS NULL
            """,
            (user_id, action_id, str(leaf), user_id, action_id, str(leaf), session_id),
        ).fetchmany(2)
    return len(rows) == 1 and rows[0][0] == 1


async def _run_cancel_cleanup_pass(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    action_id: str,
    execution_session_id: str,
    suggestion_id: str | None,
) -> ActionCleanupPassResult:
    result = await cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        action_id=action_id,
        execution_session_id=execution_session_id,
    )
    if result.warning_message is not None:
        logger.error(
            "Action %s cleanup failed: action_id=%s suggestion_id=%s message=%s",
            "post-terminal",
            action_id,
            suggestion_id,
            result.warning_message,
        )
    return result


def _persist_action_cleanup_warning(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    process_id: str,
    action_id: str,
    suggestion_id: str | None,
    user_id: str,
    cleanup_result: ActionCleanupPassResult,
) -> None:
    warning_message = cleanup_result.warning_message
    if warning_message is None:
        raise RuntimeError("cleanup warning message is missing")
    if cleanup_result.abandoned_count > 0:
        warning_code = ACTION_CANCEL_CLEANUP_ABANDONED_WARNING_CODE
    elif cleanup_result.persistence_failure_count > 0:
        warning_code = ACTION_CANCEL_CLEANUP_PERSISTENCE_WARNING_CODE
    else:
        warning_code = ACTION_CANCEL_CLEANUP_FAILED_WARNING_CODE
    payload: dict[str, object] = {
        "kind": "action",
        "action_id": action_id,
        "user_id": user_id,
        "severity": "warning",
        "warning_code": warning_code,
        "cleanup_phase": "post_terminal",
        "failure_count": cleanup_result.failure_count,
        "affected_resource_count": cleanup_result.affected_resource_count,
        "execution_failure_count": cleanup_result.execution_failure_count,
        "persistence_failure_count": cleanup_result.persistence_failure_count,
        "failed_resource_kinds": list(cleanup_result.failed_resource_kinds),
        "abandoned_count": cleanup_result.abandoned_count,
        "message": warning_message,
        "created_at": now_utc_iso(),
    }
    if suggestion_id is not None:
        payload["suggestion_id"] = suggestion_id
    append_local_process_event(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=process_id,
        event_type=ACTION_CANCEL_CLEANUP_WARNING_EVENT,
        payload=payload,
    )


def _persist_action_tool_invocation_warning(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    process_id: str,
    action_id: str,
    suggestion_id: str | None,
    user_id: str,
    warning: ActionToolInvocationCancelWarning,
) -> None:
    payload: dict[str, object] = {
        "kind": "action",
        "action_id": action_id,
        "user_id": user_id,
        "severity": "warning",
        "warning_code": ACTION_CANCEL_TOOL_INVOCATION_WARNING_CODE,
        "attempted_invocation_count": warning.attempted_invocation_count,
        "canceled_invocation_count": warning.canceled_invocation_count,
        "persistence_failure_count": warning.persistence_failure_count,
        "failed_invocation_ids": list(warning.failed_invocation_ids),
        "message": warning.warning_message,
        "created_at": now_utc_iso(),
    }
    if suggestion_id is not None:
        payload["suggestion_id"] = suggestion_id
    append_local_process_event(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=process_id,
        event_type=ACTION_CANCEL_TOOL_INVOCATION_WARNING_EVENT,
        payload=payload,
    )


def _cancel_action_tool_invocations(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    execution_session_id: str,
    completed_at: str,
    suggestion_id: str | None,
) -> ActionToolInvocationCancelWarning | None:
    invocation_ids = list_inflight_tool_invocation_ids_for_action_session(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
        execution_session_id=execution_session_id,
    )
    if not invocation_ids:
        return None
    canceled_invocation_count = 0
    failed_invocation_ids: list[str] = []
    for invocation_id in invocation_ids:
        try:
            result = cancel_inflight_tool_invocation(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                invocation_id=invocation_id,
                completed_at=completed_at,
            )
        except Exception as exc:  # noqa: BLE001
            failed_invocation_ids.append(invocation_id)
            logger.error(
                "Failed to persist canceled tool invocation: action_id=%s suggestion_id=%s invocation_id=%s error=%s",
                action_id,
                suggestion_id,
                invocation_id,
                exc,
            )
            continue
        if result.canceled:
            canceled_invocation_count += 1
    if not failed_invocation_ids:
        return None
    return ActionToolInvocationCancelWarning(
        attempted_invocation_count=len(invocation_ids),
        canceled_invocation_count=canceled_invocation_count,
        persistence_failure_count=len(failed_invocation_ids),
        failed_invocation_ids=tuple(failed_invocation_ids),
    )


async def execute_action_cancel(
    *,
    user_id: str,
    action_id: str,
    reason: str | None,
    expected_process_id: str | None = None,
) -> bool:
    """Cancel the Action and report whether post-terminal cleanup converged."""

    verify_current_owner(user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    repository = ActionCancelRepository(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    try:
        result = await repository.cancel_processing_action(
            user_id=user_id,
            action_id=action_id,
            expected_process_id=expected_process_id,
            completed_at=now_utc_iso(),
            process_completed_event_id=str(uuid.uuid4()),
        )
    except ActionCancelNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Action not found") from exc
    except ActionCancelStateConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=ACTION_CANCEL_STATE_CONFLICT_DETAIL,
        ) from exc
    if result.action_status is None:
        # The Stop fence and the child cancel requests are durable; the parent
        # terminal follows once every child releases its mutation authority,
        # and its post-terminal cleanup belongs to the transaction that writes
        # it, not to this call.
        logger.info(
            "Action Stop fence recorded while a subagent child settles: "
            "action_id=%s reason=%s",
            action_id,
            reason,
        )
        return True
    if not result.changed and result.action_status != "canceled":
        return True
    if result.changed and result.process_id is None:
        raise RuntimeError("Canceled Action result has no process_id")

    if reason:
        logger.info(
            "Action canceled via API: action_id=%s reason=%s", action_id, reason
        )

    cleanup_complete = True
    for execution_session_id in result.execution_session_ids:
        session_cleanup_complete = await _cleanup_canceled_action_session(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            execution_session_id=execution_session_id,
            suggestion_id=result.suggestion_id,
            process_id=result.process_id,
        )
        cleanup_complete = cleanup_complete and session_cleanup_complete
    return cleanup_complete


async def _cleanup_canceled_action_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    execution_session_id: str,
    suggestion_id: str | None,
    process_id: str | None,
) -> bool:
    post_terminal_cleanup_incomplete = False
    cleanup_result: ActionCleanupPassResult | None = None
    tool_invocation_warning: ActionToolInvocationCancelWarning | None = None
    root_cleanup_incomplete = False
    try:
        cleanup_result = await _run_cancel_cleanup_pass(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            action_id=action_id,
            execution_session_id=execution_session_id,
            suggestion_id=suggestion_id,
        )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        post_terminal_cleanup_incomplete = True
        logger.warning(
            "Canceled Action resource cleanup unavailable: "
            "action_id=%s exception_type=%s",
            action_id,
            type(exc).__name__,
        )
    try:
        tool_invocation_warning = _cancel_action_tool_invocations(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            execution_session_id=execution_session_id,
            completed_at=now_utc_iso(),
            suggestion_id=suggestion_id,
        )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        post_terminal_cleanup_incomplete = True
        logger.warning(
            "Canceled Action tool-invocation cleanup unavailable: "
            "action_id=%s exception_type=%s",
            action_id,
            type(exc).__name__,
        )
    try:
        receipt = load_action_session_temp_cleanup_receipt(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            execution_session_id=execution_session_id,
        )
        if receipt is None:
            root_cleaned = _session_root_is_cleaned(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                action_id=action_id,
                session_id=execution_session_id,
            )
            root_cleanup_incomplete = not root_cleaned
            if root_cleanup_incomplete:
                logger.warning(
                    "Canceled Action root cleanup authority unavailable: "
                    "action_id=%s execution_session_id=%s",
                    action_id,
                    execution_session_id,
                )
        else:
            root_cleanup_outcome = await asyncio.to_thread(
                cleanup_action_session_temp,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                receipt=receipt,
            )
            if root_cleanup_outcome.current_resource.status != "cleaned":
                root_cleanup_incomplete = True
                logger.warning(
                    "Canceled Action root cleanup incomplete: "
                    "action_id=%s execution_session_id=%s status=%s",
                    action_id,
                    execution_session_id,
                    root_cleanup_outcome.current_resource.status,
                )
    except (
        ActionSessionTempCleanupAuthorityError,
        ActionSessionTempPathError,
        MigrationError,
        OSError,
        sqlite3.Error,
    ) as exc:
        root_cleanup_incomplete = True
        logger.warning(
            "Canceled Action root cleanup deferred to recovery: "
            "action_id=%s execution_session_id=%s exception_type=%s",
            action_id,
            execution_session_id,
            type(exc).__name__,
        )
    if process_id is None:
        if (
            cleanup_result is not None and cleanup_result.warning_message is not None
        ) or tool_invocation_warning is not None:
            logger.warning(
                "Canceled Action replay cleanup warning has no process identity; "
                "skipping warning event: action_id=%s",
                action_id,
            )
    elif cleanup_result is not None and cleanup_result.warning_message is not None:
        try:
            _persist_action_cleanup_warning(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                process_id=process_id,
                action_id=action_id,
                suggestion_id=suggestion_id,
                user_id=user_id,
                cleanup_result=cleanup_result,
            )
        except Exception as warning_exc:  # noqa: BLE001
            logger.error(
                "Failed to persist action cleanup warning: action_id=%s suggestion_id=%s error=%s",
                action_id,
                suggestion_id,
                warning_exc,
            )
    if process_id is not None and tool_invocation_warning is not None:
        try:
            _persist_action_tool_invocation_warning(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                process_id=process_id,
                action_id=action_id,
                suggestion_id=suggestion_id,
                user_id=user_id,
                warning=tool_invocation_warning,
            )
        except Exception as warning_exc:  # noqa: BLE001
            logger.error(
                "Failed to persist action tool invocation warning: action_id=%s suggestion_id=%s error=%s",
                action_id,
                suggestion_id,
                warning_exc,
            )
    if (
        (cleanup_result is not None and cleanup_result.warning_message is not None)
        or tool_invocation_warning is not None
        or root_cleanup_incomplete
        or post_terminal_cleanup_incomplete
    ):
        return False
    return True


__all__ = ["execute_action_cancel"]

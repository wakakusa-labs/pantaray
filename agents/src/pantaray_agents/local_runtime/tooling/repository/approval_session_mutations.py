from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from ...storage.transactions import immediate_transaction
from ..models import (
    ApprovalPreferenceUpsertInput,
    ApprovalSessionMutationResult,
    CapabilityGrantCreateInput,
    ToolInvocationStartInput,
)
from .approval_preferences import (
    _matching_preference_ids,
    _revoke_capability_grants_in_connection,
    _revoke_other_approval_preferences_in_connection,
    _revoke_superseded_capability_grant_in_connection,
    _upsert_approval_preference_in_connection,
    _upsert_capability_grant_in_connection,
)
from .approval_sessions import _load_approval_session_by_request_in_connection
from .common import _configure_connection
from .executions import (
    ToolInvocationSessionConflictError,
    _record_tool_invocation_start_in_connection,
)


class _ApprovalExecutionClaimConflict(RuntimeError):
    """Atomic execution claim failed and the transaction must roll back."""


def update_approval_session_decision(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool_request_id: str,
    action_id: str,
    approval_session_id: str | None,
    expected_current_status: str,
    next_status: str,
    decided_at: str,
) -> ApprovalSessionMutationResult:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            return apply_approval_decision_in_connection(
                connection=connection,
                user_id=user_id,
                tool_request_id=tool_request_id,
                action_id=action_id,
                approval_session_id=approval_session_id,
                expected_current_status=expected_current_status,
                next_status=next_status,
                decided_at=decided_at,
            )


def apply_approval_decision_with_grants(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool_request_id: str,
    action_id: str,
    approval_session_id: str | None,
    expected_current_status: str,
    next_status: str,
    decided_at: str,
    preference: ApprovalPreferenceUpsertInput | None = None,
    grants: tuple[CapabilityGrantCreateInput, ...] = (),
) -> ApprovalSessionMutationResult:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            return apply_approval_decision_in_connection(
                connection=connection,
                user_id=user_id,
                tool_request_id=tool_request_id,
                action_id=action_id,
                approval_session_id=approval_session_id,
                expected_current_status=expected_current_status,
                next_status=next_status,
                decided_at=decided_at,
                preference=preference,
                grants=grants,
            )


def interrupt_approval_session_for_tool_request(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    tool_request_id: str,
    interrupted_at: str,
) -> bool:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            cursor = connection.execute(
                """
                UPDATE approval_sessions
                SET status = 'interrupted', decided_at = ?
                WHERE
                    user_id = ?
                    AND action_id = ?
                    AND tool_request_id = ?
                    AND status = 'pending'
                    AND decided_at IS NULL
                """,
                (
                    interrupted_at,
                    user_id,
                    action_id,
                    tool_request_id,
                ),
            )
            return cursor.rowcount == 1


def apply_approval_decision_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    tool_request_id: str,
    action_id: str,
    approval_session_id: str | None,
    expected_current_status: str,
    next_status: str,
    decided_at: str,
    preference: ApprovalPreferenceUpsertInput | None = None,
    grants: tuple[CapabilityGrantCreateInput, ...] = (),
) -> ApprovalSessionMutationResult:
    session = _load_approval_session_by_request_in_connection(
        connection=connection,
        user_id=user_id,
        tool_request_id=tool_request_id,
    )
    if session is None:
        return ApprovalSessionMutationResult(status="not_found", approval_session=None)
    if session.action_id != action_id:
        return ApprovalSessionMutationResult(
            status="request_mismatch",
            approval_session=session,
        )
    if (
        approval_session_id is not None
        and session.approval_session_id != approval_session_id
    ):
        return ApprovalSessionMutationResult(
            status="request_mismatch",
            approval_session=session,
        )
    cursor = connection.execute(
        """
        UPDATE approval_sessions
        SET status = ?, decided_at = ?
        WHERE
            user_id = ?
            AND tool_request_id = ?
            AND status = ?
            AND decided_at IS NULL
        """,
        (
            next_status,
            decided_at,
            user_id,
            tool_request_id,
            expected_current_status,
        ),
    )
    if cursor.rowcount != 1:
        return ApprovalSessionMutationResult(
            status="conflict",
            approval_session=session,
        )
    if preference is not None:
        matching_preference_ids = _matching_preference_ids(
            connection=connection,
            preference=preference,
        )
        _upsert_approval_preference_in_connection(
            connection=connection,
            preference=preference,
        )
        _revoke_other_approval_preferences_in_connection(
            connection=connection,
            preference=preference,
            revoked_at=preference.updated_at,
        )
        _revoke_capability_grants_in_connection(
            connection=connection,
            preference_ids=matching_preference_ids,
            revoked_at=preference.updated_at,
            revocation_reason="approval preference replaced",
        )
    for grant in grants:
        _revoke_superseded_capability_grant_in_connection(
            connection=connection,
            grant=grant,
            revoked_at=grant.granted_at,
            revocation_reason="capability grant replaced",
        )
        _upsert_capability_grant_in_connection(connection=connection, grant=grant)
    return ApprovalSessionMutationResult(
        status="updated",
        approval_session=_load_approval_session_by_request_in_connection(
            connection=connection,
            user_id=user_id,
            tool_request_id=tool_request_id,
        ),
    )


def _invocation_is_claimable_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    tool_request_id: str,
    tool_invocation_id: str,
    invocation: ToolInvocationStartInput | None,
) -> bool:
    """Whether an unbound approval session may be bound to this invocation.

    The claim must always end up pointing at a real invocation row, so exactly
    one of the two shapes is accepted: the caller hands over an invocation for
    the claim to record (brokered tools), or the caller already recorded its own
    audit row for this same request (the Action runtime does this for tools it
    executes itself). A row that belongs to another user or request is never a
    valid target, and a row that already exists cannot be recorded twice.
    """

    existing = connection.execute(
        """
        SELECT user_id, tool_request_id
        FROM tool_invocations
        WHERE invocation_id = ?
        """,
        (tool_invocation_id,),
    ).fetchone()
    if invocation is not None:
        return existing is None
    return (
        existing is not None
        and existing["user_id"] == user_id
        and existing["tool_request_id"] == tool_request_id
    )


def claim_approval_execution_start(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool_request_id: str,
    action_id: str,
    approval_session_id: str | None,
    approval_source: Literal["settings", "prompt"],
    tool_invocation_id: str,
    started_at: str,
    invocation: ToolInvocationStartInput | None,
) -> ApprovalSessionMutationResult:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        try:
            with immediate_transaction(connection):
                session = _load_approval_session_by_request_in_connection(
                    connection=connection,
                    user_id=user_id,
                    tool_request_id=tool_request_id,
                )
                if session is None:
                    return ApprovalSessionMutationResult(
                        status="not_found",
                        approval_session=None,
                    )
                if session.action_id != action_id:
                    return ApprovalSessionMutationResult(
                        status="request_mismatch",
                        approval_session=session,
                    )
                if (
                    approval_session_id is not None
                    and session.approval_session_id != approval_session_id
                ):
                    return ApprovalSessionMutationResult(
                        status="request_mismatch",
                        approval_session=session,
                    )
                if session.approval_source != approval_source:
                    return ApprovalSessionMutationResult(
                        status="request_mismatch",
                        approval_session=session,
                    )
                if session.tool_invocation_id is not None:
                    if (
                        session.tool_invocation_id != tool_invocation_id
                        or session.claimed_at is not None
                        or invocation is not None
                    ):
                        return ApprovalSessionMutationResult(
                            status="conflict",
                            approval_session=session,
                        )
                elif not _invocation_is_claimable_in_connection(
                    connection=connection,
                    user_id=user_id,
                    tool_request_id=tool_request_id,
                    tool_invocation_id=tool_invocation_id,
                    invocation=invocation,
                ):
                    return ApprovalSessionMutationResult(
                        status="conflict",
                        approval_session=session,
                    )
                elif invocation is not None:
                    _record_tool_invocation_start_in_connection(
                        connection=connection,
                        invocation=invocation,
                    )
                cursor = connection.execute(
                    """
                    UPDATE approval_sessions
                    SET
                        tool_invocation_id = CASE
                            WHEN tool_invocation_id IS NULL THEN ?
                            ELSE tool_invocation_id
                        END,
                        claimed_at = ?
                    WHERE
                        user_id = ?
                        AND tool_request_id = ?
                        AND approval_source = ?
                        AND (
                            (tool_invocation_id IS NULL)
                            OR tool_invocation_id = ?
                        )
                        AND claimed_at IS NULL
                        AND status = 'approved_once'
                    """,
                    (
                        tool_invocation_id,
                        started_at,
                        user_id,
                        tool_request_id,
                        approval_source,
                        tool_invocation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise _ApprovalExecutionClaimConflict
                return ApprovalSessionMutationResult(
                    status="updated",
                    approval_session=_load_approval_session_by_request_in_connection(
                        connection=connection,
                        user_id=user_id,
                        tool_request_id=tool_request_id,
                    ),
                )
        except (_ApprovalExecutionClaimConflict, ToolInvocationSessionConflictError):
            session = _load_approval_session_by_request_in_connection(
                connection=connection,
                user_id=user_id,
                tool_request_id=tool_request_id,
            )
            return ApprovalSessionMutationResult(
                status="conflict",
                approval_session=session,
            )

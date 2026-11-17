from __future__ import annotations

import sqlite3
from typing import Literal

from ...storage.users import ensure_user_row
from ..models import CapabilityGrantCreateInput
from .common import _serialize_json

INTEGRATION_WRITE_SCOPE_PREFIX = "integration_write:"
# The stored modes of integration permission rows; 'never_allow' turns one off.
StoredApprovalMode = Literal["prompt_each_time", "always_allow", "never_allow"]


class IntegrationWriteAllowError(ValueError):
    """An integration write scope was about to be saved as always_allow."""


def _write_approval_preference_row(
    *,
    connection: sqlite3.Connection,
    preference_id: str,
    user_id: str,
    scope_type: Literal["global"],
    scope_ref: str | None,
    approval_mode: StoredApprovalMode,
    applies_to: tuple[str, ...],
    created_at: str,
    updated_at: str,
) -> None:
    """Insert or reactivate one global preference row; every writer goes through here."""

    # An integration write asks the user every time; no setting may skip that.
    if approval_mode == "always_allow" and any(
        scope.startswith(INTEGRATION_WRITE_SCOPE_PREFIX) for scope in applies_to
    ):
        raise IntegrationWriteAllowError(
            "integration writes cannot be allowed without asking"
        )
    ensure_user_row(connection, user_id=user_id, timestamp=created_at)
    connection.execute(
        """
        INSERT INTO approval_preferences(
            preference_id,
            user_id,
            scope_type,
            scope_ref,
            approval_mode,
            applies_to_json,
            created_at,
            updated_at,
            updated_by,
            revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', NULL)
        ON CONFLICT(preference_id) DO UPDATE SET
            approval_mode = excluded.approval_mode,
            applies_to_json = excluded.applies_to_json,
            updated_at = excluded.updated_at,
            updated_by = 'user',
            revoked_at = NULL
        """,
        (
            preference_id,
            user_id,
            scope_type,
            scope_ref,
            approval_mode,
            _serialize_json(list(applies_to)),
            created_at,
            updated_at,
        ),
    )


def _revoke_capability_grants_in_connection(
    *,
    connection: sqlite3.Connection,
    preference_ids: tuple[str, ...],
    revoked_at: str,
    revocation_reason: str,
) -> None:
    for preference_id in preference_ids:
        connection.execute(
            """
            UPDATE capability_grants
            SET revoked_at = ?, revocation_reason = ?
            WHERE preference_id = ? AND revoked_at IS NULL
            """,
            (revoked_at, revocation_reason, preference_id),
        )


def _upsert_capability_grant_in_connection(
    *,
    connection: sqlite3.Connection,
    grant: CapabilityGrantCreateInput,
) -> None:
    connection.execute(
        """
        INSERT INTO capability_grants(
            grant_id,
            user_id,
            preference_id,
            capability,
            scope_type,
            scope_ref,
            grant_source,
            granted_at,
            granted_by,
            revoked_at,
            revocation_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', NULL, NULL)
        ON CONFLICT(grant_id) DO UPDATE SET
            preference_id = excluded.preference_id,
            capability = excluded.capability,
            scope_type = excluded.scope_type,
            scope_ref = excluded.scope_ref,
            grant_source = excluded.grant_source,
            granted_at = excluded.granted_at,
            granted_by = 'user',
            revoked_at = NULL,
            revocation_reason = NULL
        """,
        (
            grant.grant_id,
            grant.user_id,
            grant.preference_id,
            grant.capability,
            grant.scope_type,
            grant.scope_ref,
            grant.grant_source,
            grant.granted_at,
        ),
    )

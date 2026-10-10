from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal, cast

from ...storage.migrations import MigrationError
from ...storage.users import ensure_user_row
from ..models import (
    ApprovalMode,
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    StoredApprovalPreference,
)
from .common import (
    WORKSPACE_EDIT_AND_COMMAND_SCOPE,
    _configure_connection,
    _deserialize_string_list,
    _serialize_json,
)


def upsert_approval_preference(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    preference: ApprovalPreferenceUpsertInput,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            _upsert_approval_preference_in_connection(
                connection=connection,
                preference=preference,
            )


def _upsert_approval_preference_in_connection(
    *,
    connection: sqlite3.Connection,
    preference: ApprovalPreferenceUpsertInput,
) -> None:
    ensure_user_row(
        connection,
        user_id=preference.user_id,
        timestamp=preference.created_at,
    )
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
            preference.preference_id,
            preference.user_id,
            preference.scope_type,
            preference.scope_ref,
            preference.approval_mode,
            _serialize_json(list(preference.applies_to)),
            preference.created_at,
            preference.updated_at,
        ),
    )


def _matching_preference_ids(
    *,
    connection: sqlite3.Connection,
    preference: ApprovalPreferenceUpsertInput,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT preference_id, applies_to_json
        FROM approval_preferences
        WHERE
            user_id = ?
            AND scope_type = ?
            AND ((? IS NULL AND scope_ref IS NULL) OR scope_ref = ?)
            AND revoked_at IS NULL
        """,
        (
            preference.user_id,
            preference.scope_type,
            preference.scope_ref,
            preference.scope_ref,
        ),
    ).fetchall()
    applies_to_set = set(preference.applies_to)
    matching_preference_ids: list[str] = []
    for row in rows:
        applies_to_raw = _deserialize_string_list(
            row["applies_to_json"], field_name="approval preference applies_to_json"
        )
        if not applies_to_set.isdisjoint(applies_to_raw):
            matching_preference_ids.append(str(row["preference_id"]))
    return tuple(matching_preference_ids)


def _revoke_other_approval_preferences_in_connection(
    *,
    connection: sqlite3.Connection,
    preference: ApprovalPreferenceUpsertInput,
    revoked_at: str,
) -> None:
    for preference_id in _matching_preference_ids(
        connection=connection,
        preference=preference,
    ):
        if preference_id == preference.preference_id:
            continue
        connection.execute(
            """
            UPDATE approval_preferences
            SET revoked_at = ?
            WHERE preference_id = ? AND revoked_at IS NULL
            """,
            (revoked_at, preference_id),
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


def _revoke_superseded_capability_grant_in_connection(
    *,
    connection: sqlite3.Connection,
    grant: CapabilityGrantCreateInput,
    revoked_at: str,
    revocation_reason: str,
) -> None:
    connection.execute(
        """
        UPDATE capability_grants
        SET revoked_at = ?, revocation_reason = ?
        WHERE
            user_id = ?
            AND preference_id = ?
            AND capability = ?
            AND scope_type = ?
            AND ((? IS NULL AND scope_ref IS NULL) OR scope_ref = ?)
            AND grant_id <> ?
            AND revoked_at IS NULL
        """,
        (
            revoked_at,
            revocation_reason,
            grant.user_id,
            grant.preference_id,
            grant.capability,
            grant.scope_type,
            grant.scope_ref,
            grant.scope_ref,
            grant.grant_id,
        ),
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


def create_capability_grant(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    grant: CapabilityGrantCreateInput,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            ensure_user_row(
                connection,
                user_id=grant.user_id,
                timestamp=grant.granted_at,
            )
            _revoke_superseded_capability_grant_in_connection(
                connection=connection,
                grant=grant,
                revoked_at=grant.granted_at,
                revocation_reason="capability grant replaced",
            )
            _upsert_capability_grant_in_connection(connection=connection, grant=grant)


def load_effective_approval_preference(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    applies_to: str,
) -> StoredApprovalPreference:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        return _load_effective_approval_preference_in_connection(
            connection=connection,
            user_id=user_id,
            applies_to=applies_to,
        )


def _load_effective_approval_preference_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    applies_to: str,
) -> StoredApprovalPreference:
    rows = connection.execute(
        """
        SELECT
            preference_id,
            approval_mode,
            applies_to_json,
            scope_type,
            scope_ref
        FROM approval_preferences
        WHERE
            user_id = ?
            AND revoked_at IS NULL
            AND scope_type = 'global'
            AND scope_ref IS NULL
        ORDER BY updated_at DESC
        """,
        (user_id,),
    ).fetchall()
    for row in rows:
        applies_to_raw = _deserialize_string_list(
            row["applies_to_json"], field_name="approval preference applies_to_json"
        )
        if applies_to not in applies_to_raw:
            continue
        return StoredApprovalPreference(
            preference_id=str(row["preference_id"]),
            scope_type=cast("Literal['global']", str(row["scope_type"])),
            scope_ref=(str(row["scope_ref"]) if row["scope_ref"] is not None else None),
            approval_mode=cast(ApprovalMode, str(row["approval_mode"])),
            applies_to=applies_to_raw,
        )
    # Until the user saves a choice, edits and commands inside the workspace run
    # without asking; every other kind of approval asks each time.
    return StoredApprovalPreference(
        preference_id=None,
        scope_type="global",
        scope_ref=None,
        approval_mode=(
            "always_allow"
            if applies_to == WORKSPACE_EDIT_AND_COMMAND_SCOPE
            else "prompt_each_time"
        ),
        applies_to=(applies_to,),
    )


def load_global_approval_preference(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    applies_to: str,
) -> StoredApprovalPreference:
    return load_effective_approval_preference(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        applies_to=applies_to,
    )


def load_active_capability_grants(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    applies_to: str,
) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        return _load_active_capability_grants_in_connection(
            connection=connection,
            user_id=user_id,
            applies_to=applies_to,
        )


def _load_active_capability_grants_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    applies_to: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT capability_grants.capability, approval_preferences.applies_to_json
        FROM capability_grants
        INNER JOIN approval_preferences
            ON approval_preferences.preference_id = capability_grants.preference_id
        WHERE
            capability_grants.user_id = ?
            AND capability_grants.revoked_at IS NULL
            AND capability_grants.preference_id IS NOT NULL
            AND approval_preferences.revoked_at IS NULL
            AND capability_grants.scope_type = 'global'
            AND capability_grants.scope_ref IS NULL
        ORDER BY capability_grants.granted_at DESC
        """,
        (user_id,),
    ).fetchall()
    capabilities: list[str] = []
    for row in rows:
        applies_to_raw = _deserialize_string_list(
            row["applies_to_json"], field_name="approval preference applies_to_json"
        )
        if applies_to not in applies_to_raw:
            continue
        capability = row["capability"]
        if capability is None:
            raise MigrationError("capability_grants.capability must not be NULL")
        capability_str = str(capability)
        if capability_str not in capabilities:
            capabilities.append(capability_str)
    return tuple(capabilities)


def apply_approval_preference_setting(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    preference: ApprovalPreferenceUpsertInput,
    grants: tuple[CapabilityGrantCreateInput, ...] = (),
    revoked_at: str | None = None,
    revocation_reason: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
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
            if revoked_at is not None:
                _revoke_capability_grants_in_connection(
                    connection=connection,
                    preference_ids=matching_preference_ids,
                    revoked_at=revoked_at,
                    revocation_reason=revocation_reason
                    or "approval preference changed",
                )
            for grant in grants:
                _revoke_superseded_capability_grant_in_connection(
                    connection=connection,
                    grant=grant,
                    revoked_at=grant.granted_at,
                    revocation_reason="capability grant replaced",
                )
                _upsert_capability_grant_in_connection(
                    connection=connection,
                    grant=grant,
                )

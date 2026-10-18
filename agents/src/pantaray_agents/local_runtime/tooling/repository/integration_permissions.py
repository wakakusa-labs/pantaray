"""Per-account permissions of connected integrations.

Each connected account keeps its permissions as global approval preferences:

- `integration_read:<toolkit>:<account>`: always_allow, with a capability grant of
  the same name, or never_allow.
- `integration_write:<toolkit>:<account>`: prompt_each_time or never_allow.
- `integration_tool:<account>:<slug>`: never_allow, one tool blocked.

No row means no permission: the resolver denies until a setting is saved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal, cast

from ..brokering.approval_identity import (
    build_approval_preference_id,
    build_capability_grant_id,
)
from ..models import CapabilityGrantCreateInput
from .approval_preferences import (
    INTEGRATION_WRITE_SCOPE_PREFIX,
    StoredApprovalMode,
    _revoke_capability_grants_in_connection,
    _upsert_capability_grant_in_connection,
    _write_approval_preference_row,
)
from .common import _configure_connection

IntegrationToolClass = Literal["read", "write"]
IntegrationAccess = Literal["off", "read_only", "read_write"]
IntegrationToolDecision = Literal["allow", "ask", "denied"]

INTEGRATION_READ_SCOPE_PREFIX = "integration_read:"
INTEGRATION_TOOL_SCOPE_PREFIX = "integration_tool:"
_SCOPE_SEPARATOR = ":"
_REVOCATION_REASON = "integration permission changed"

# (read scope mode, write scope mode) saved for each account setting.
_ACCESS_MODES: dict[
    IntegrationAccess, tuple[StoredApprovalMode, StoredApprovalMode]
] = {
    "off": ("never_allow", "never_allow"),
    "read_only": ("always_allow", "never_allow"),
    "read_write": ("always_allow", "prompt_each_time"),
}


def integration_read_scope(*, toolkit: str, account: str) -> str:
    return _scope(INTEGRATION_READ_SCOPE_PREFIX, toolkit, account)


def integration_write_scope(*, toolkit: str, account: str) -> str:
    return _scope(INTEGRATION_WRITE_SCOPE_PREFIX, toolkit, account)


def integration_tool_scope(*, account: str, slug: str) -> str:
    return _scope(INTEGRATION_TOOL_SCOPE_PREFIX, account, slug)


def _scope(prefix: str, *parts: str) -> str:
    # A separator inside a part would let one account's scope prefix-match
    # another's, so revoking one account could clear the other's blocks.
    for part in parts:
        if not part or _SCOPE_SEPARATOR in part:
            raise ValueError(
                f"integration scope parts must be non-empty without ':': {part!r}"
            )
    return prefix + _SCOPE_SEPARATOR.join(parts)


def set_integration_account_access(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    toolkit: str,
    account: str,
    access: IntegrationAccess,
    updated_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            _save_access(
                connection,
                user_id=user_id,
                toolkit=toolkit,
                account=account,
                access=access,
                updated_at=updated_at,
            )


def initialize_integration_account_access(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    toolkit: str,
    account: str,
    access: IntegrationAccess,
    updated_at: str,
) -> None:
    """Save the access only when the account has no saved setting yet.

    One immediate transaction, so callers racing on the same account save it
    once and never overwrite a setting saved first.
    """

    read_scope = integration_read_scope(toolkit=toolkit, account=account)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if _saved_mode(connection, user_id=user_id, scope=read_scope) is not None:
                return
            _save_access(
                connection,
                user_id=user_id,
                toolkit=toolkit,
                account=account,
                access=access,
                updated_at=updated_at,
            )


def _save_access(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    toolkit: str,
    account: str,
    access: IntegrationAccess,
    updated_at: str,
) -> None:
    read_mode, write_mode = _ACCESS_MODES[access]
    for scope, approval_mode in (
        (integration_read_scope(toolkit=toolkit, account=account), read_mode),
        (integration_write_scope(toolkit=toolkit, account=account), write_mode),
    ):
        _save_scope(
            connection,
            user_id=user_id,
            scope=scope,
            approval_mode=approval_mode,
            updated_at=updated_at,
        )


def set_integration_tool_blocked(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    account: str,
    slug: str,
    blocked: bool,
    updated_at: str,
) -> None:
    scope = integration_tool_scope(account=account, slug=slug)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            if blocked:
                _save_scope(
                    connection,
                    user_id=user_id,
                    scope=scope,
                    approval_mode="never_allow",
                    updated_at=updated_at,
                )
            else:
                _revoke_preferences(
                    connection,
                    preference_ids=(_preference_id(user_id=user_id, scope=scope),),
                    revoked_at=updated_at,
                )


def revoke_integration_account(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    toolkit: str,
    account: str,
    revoked_at: str,
) -> None:
    """Revoke every permission row of one account, leaving other accounts as they are."""

    tool_scope_prefix = (
        _scope(INTEGRATION_TOOL_SCOPE_PREFIX, account) + _SCOPE_SEPARATOR
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            rows = connection.execute(
                """
                SELECT DISTINCT preference.preference_id
                FROM approval_preferences AS preference,
                    json_each(preference.applies_to_json) AS scope
                WHERE
                    preference.user_id = ?
                    AND preference.revoked_at IS NULL
                    AND (
                        scope.value IN (?, ?)
                        OR substr(scope.value, 1, length(?)) = ?
                    )
                """,
                (
                    user_id,
                    integration_read_scope(toolkit=toolkit, account=account),
                    integration_write_scope(toolkit=toolkit, account=account),
                    tool_scope_prefix,
                    tool_scope_prefix,
                ),
            ).fetchall()
            _revoke_preferences(
                connection,
                preference_ids=tuple(str(row["preference_id"]) for row in rows),
                revoked_at=revoked_at,
            )


def resolve_integration_tool(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    toolkit: str,
    account: str,
    slug: str,
    tool_class: IntegrationToolClass,
) -> IntegrationToolDecision:
    """Decide one tool for one account: a tool block, then its class setting.

    `ask` means the user must approve the write for each execution.
    """

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        tool_scope = integration_tool_scope(account=account, slug=slug)
        if _saved_mode(connection, user_id=user_id, scope=tool_scope) == "never_allow":
            return "denied"
        if tool_class == "write":
            write_scope = integration_write_scope(toolkit=toolkit, account=account)
            write_mode = _saved_mode(connection, user_id=user_id, scope=write_scope)
            return "ask" if write_mode == "prompt_each_time" else "denied"
        read_scope = integration_read_scope(toolkit=toolkit, account=account)
        read_mode = _saved_mode(connection, user_id=user_id, scope=read_scope)
        return "allow" if read_mode == "always_allow" else "denied"


def load_integration_account_access(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    toolkit: str,
    account: str,
) -> IntegrationAccess:
    read_scope = integration_read_scope(toolkit=toolkit, account=account)
    write_scope = integration_write_scope(toolkit=toolkit, account=account)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        if _saved_mode(connection, user_id=user_id, scope=read_scope) != "always_allow":
            return "off"
        write_mode = _saved_mode(connection, user_id=user_id, scope=write_scope)
        return "read_write" if write_mode == "prompt_each_time" else "read_only"


def load_integration_blocked_tools(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, account: str
) -> frozenset[str]:
    """The slugs of the tools blocked for one account."""

    tool_scope_prefix = (
        _scope(INTEGRATION_TOOL_SCOPE_PREFIX, account) + _SCOPE_SEPARATOR
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        rows = connection.execute(
            """
            SELECT DISTINCT scope.value AS scope
            FROM approval_preferences AS preference,
                json_each(preference.applies_to_json) AS scope
            WHERE
                preference.user_id = ?
                AND preference.revoked_at IS NULL
                AND preference.approval_mode = 'never_allow'
                AND substr(scope.value, 1, length(?)) = ?
            """,
            (user_id, tool_scope_prefix, tool_scope_prefix),
        ).fetchall()
    return frozenset(str(row["scope"])[len(tool_scope_prefix) :] for row in rows)


def _preference_id(*, user_id: str, scope: str) -> str:
    return build_approval_preference_id(
        user_id=user_id, scope_type="global", scope_ref=None, applies_to=(scope,)
    )


def _save_scope(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    scope: str,
    approval_mode: StoredApprovalMode,
    updated_at: str,
) -> None:
    preference_id = _preference_id(user_id=user_id, scope=scope)
    _write_approval_preference_row(
        connection=connection,
        preference_id=preference_id,
        user_id=user_id,
        scope_type="global",
        scope_ref=None,
        approval_mode=approval_mode,
        applies_to=(scope,),
        created_at=updated_at,
        updated_at=updated_at,
    )
    if approval_mode != "always_allow":
        _revoke_capability_grants_in_connection(
            connection=connection,
            preference_ids=(preference_id,),
            revoked_at=updated_at,
            revocation_reason=_REVOCATION_REASON,
        )
        return
    _upsert_capability_grant_in_connection(
        connection=connection,
        grant=CapabilityGrantCreateInput(
            grant_id=build_capability_grant_id(
                user_id=user_id,
                scope_type="global",
                scope_ref=None,
                capability=scope,
                applies_to=(scope,),
            ),
            user_id=user_id,
            preference_id=preference_id,
            capability=scope,
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at=updated_at,
        ),
    )


def _revoke_preferences(
    connection: sqlite3.Connection,
    *,
    preference_ids: tuple[str, ...],
    revoked_at: str,
) -> None:
    connection.executemany(
        """
        UPDATE approval_preferences
        SET revoked_at = ?
        WHERE preference_id = ? AND revoked_at IS NULL
        """,
        [(revoked_at, preference_id) for preference_id in preference_ids],
    )
    _revoke_capability_grants_in_connection(
        connection=connection,
        preference_ids=preference_ids,
        revoked_at=revoked_at,
        revocation_reason=_REVOCATION_REASON,
    )


def _saved_mode(
    connection: sqlite3.Connection, *, user_id: str, scope: str
) -> StoredApprovalMode | None:
    # Like a saved always_allow elsewhere, it counts only with the grant written
    # beside it.
    row = connection.execute(
        """
        SELECT
            preference.approval_mode,
            EXISTS (
                SELECT 1
                FROM capability_grants AS grant_row
                WHERE
                    grant_row.preference_id = preference.preference_id
                    AND grant_row.capability = scope.value
                    AND grant_row.revoked_at IS NULL
            ) AS granted
        FROM approval_preferences AS preference,
            json_each(preference.applies_to_json) AS scope
        WHERE
            preference.user_id = ?
            AND preference.revoked_at IS NULL
            AND scope.value = ?
        ORDER BY preference.updated_at DESC
        LIMIT 1
        """,
        (user_id, scope),
    ).fetchone()
    if row is None or (row["approval_mode"] == "always_allow" and not row["granted"]):
        return None
    return cast(StoredApprovalMode, str(row["approval_mode"]))

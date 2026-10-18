from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.brokering.approval_identity import (
    build_approval_preference_id,
)
from pantaray_agents.local_runtime.tooling.models import ApprovalPreferenceUpsertInput
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_preference_setting,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.repository.approval_preferences import (
    IntegrationWriteAllowError,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    IntegrationToolClass,
    IntegrationToolDecision,
    integration_read_scope,
    integration_tool_scope,
    integration_write_scope,
    load_integration_account_access,
    resolve_integration_tool,
    revoke_integration_account,
    set_integration_account_access,
    set_integration_tool_blocked,
)

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
TOOLKIT = "gmail"
TIMESTAMP = "2026-10-18T00:00:00Z"
LATER = "2026-10-18T00:01:00Z"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    return path


def _set_access(db_path: Path, account: str, access: IntegrationAccess) -> None:
    set_integration_account_access(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        toolkit=TOOLKIT,
        account=account,
        access=access,
        updated_at=TIMESTAMP,
    )


def _block(db_path: Path, account: str, slug: str, *, blocked: bool) -> None:
    set_integration_tool_blocked(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        account=account,
        slug=slug,
        blocked=blocked,
        updated_at=LATER,
    )


def _resolve(
    db_path: Path, account: str, slug: str, tool_class: IntegrationToolClass
) -> IntegrationToolDecision:
    return resolve_integration_tool(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        toolkit=TOOLKIT,
        account=account,
        slug=slug,
        tool_class=tool_class,
    )


def _decisions(
    db_path: Path, account: str
) -> tuple[IntegrationToolDecision, IntegrationToolDecision]:
    return (
        _resolve(db_path, account, "GMAIL_FETCH_EMAILS", "read"),
        _resolve(db_path, account, "GMAIL_SEND_EMAIL", "write"),
    )


def test_an_account_without_saved_rows_is_denied_everything(db_path: Path) -> None:
    assert _decisions(db_path, "account-a") == ("denied", "denied")
    assert (
        load_integration_account_access(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id=USER_ID,
            toolkit=TOOLKIT,
            account="account-a",
        )
        == "off"
    )


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        ("off", ("denied", "denied")),
        ("read_only", ("allow", "denied")),
        ("read_write", ("allow", "ask")),
    ],
)
def test_account_setting_resolves_each_tool_class(
    db_path: Path,
    access: IntegrationAccess,
    expected: tuple[IntegrationToolDecision, IntegrationToolDecision],
) -> None:
    _set_access(db_path, "account-a", "read_write")
    _set_access(db_path, "account-a", access)

    assert _decisions(db_path, "account-a") == expected
    assert (
        load_integration_account_access(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id=USER_ID,
            toolkit=TOOLKIT,
            account="account-a",
        )
        == access
    )


def test_read_needs_the_grant_saved_with_it(db_path: Path) -> None:
    _set_access(db_path, "account-a", "read_only")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE capability_grants SET revoked_at = ?", (LATER,))

    assert _resolve(db_path, "account-a", "GMAIL_FETCH_EMAILS", "read") == "denied"


def test_a_tool_block_wins_over_the_class_setting_until_cleared(
    db_path: Path,
) -> None:
    _set_access(db_path, "account-a", "read_write")
    _block(db_path, "account-a", "GMAIL_FETCH_EMAILS", blocked=True)
    _block(db_path, "account-a", "GMAIL_SEND_EMAIL", blocked=True)

    assert _decisions(db_path, "account-a") == ("denied", "denied")
    assert _resolve(db_path, "account-a", "GMAIL_LIST_LABELS", "read") == "allow"

    _block(db_path, "account-a", "GMAIL_FETCH_EMAILS", blocked=False)
    _block(db_path, "account-a", "GMAIL_SEND_EMAIL", blocked=False)

    assert _decisions(db_path, "account-a") == ("allow", "ask")


def test_revoking_one_account_leaves_the_other_untouched(db_path: Path) -> None:
    # "account-a" is a prefix of "account-ab": revocation must match whole parts.
    for account in ("account-a", "account-ab"):
        _set_access(db_path, account, "read_write")
        _block(db_path, account, "GMAIL_SEND_EMAIL", blocked=True)

    revoke_integration_account(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        toolkit=TOOLKIT,
        account="account-a",
        revoked_at=LATER,
    )

    assert _decisions(db_path, "account-a") == ("denied", "denied")
    assert _resolve(db_path, "account-ab", "GMAIL_FETCH_EMAILS", "read") == "allow"
    assert _resolve(db_path, "account-ab", "GMAIL_SEND_EMAIL", "write") == "denied"
    assert _resolve(db_path, "account-ab", "GMAIL_CREATE_DRAFT", "write") == "ask"
    with sqlite3.connect(db_path) as connection:
        active = {
            scope
            for (scope,) in connection.execute(
                """
                SELECT scope.value
                FROM approval_preferences, json_each(applies_to_json) AS scope
                WHERE revoked_at IS NULL
                """
            )
        }
        active_grants = {
            capability
            for (capability,) in connection.execute(
                "SELECT capability FROM capability_grants WHERE revoked_at IS NULL"
            )
        }
    assert active == {
        integration_read_scope(toolkit=TOOLKIT, account="account-ab"),
        integration_write_scope(toolkit=TOOLKIT, account="account-ab"),
        integration_tool_scope(account="account-ab", slug="GMAIL_SEND_EMAIL"),
    }
    assert active_grants == {
        integration_read_scope(toolkit=TOOLKIT, account="account-ab")
    }

    # Reconnecting writes the setting again and brings the account back.
    _set_access(db_path, "account-a", "read_only")
    assert _decisions(db_path, "account-a") == ("allow", "denied")


def test_saving_an_integration_write_as_always_allow_is_rejected(
    db_path: Path,
) -> None:
    scope = integration_write_scope(toolkit=TOOLKIT, account="account-a")
    preference = ApprovalPreferenceUpsertInput(
        preference_id=build_approval_preference_id(
            user_id=USER_ID, scope_type="global", scope_ref=None, applies_to=(scope,)
        ),
        user_id=USER_ID,
        scope_type="global",
        scope_ref=None,
        approval_mode="always_allow",
        # Hiding the write scope beside another one must not get it through.
        applies_to=("screen_capture", scope),
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
    )

    with pytest.raises(IntegrationWriteAllowError):
        upsert_approval_preference(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, preference=preference
        )
    with pytest.raises(IntegrationWriteAllowError):
        apply_approval_preference_setting(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, preference=preference
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_preferences"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("account", "slug"), [("", "GMAIL_SEND_EMAIL"), ("a:b", "GMAIL_SEND_EMAIL")]
)
def test_scope_parts_cannot_carry_the_separator(account: str, slug: str) -> None:
    with pytest.raises(ValueError, match="integration scope parts"):
        integration_tool_scope(account=account, slug=slug)

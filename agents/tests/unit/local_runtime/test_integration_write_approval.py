"""AC-C1: an integration write asks every time, whatever auto-approval is on."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerApprovalRequiredError,
    BrokerContext,
    apply_approval_decision,
    ensure_tool_authorization,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.repository import set_action_approval_mode
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    integration_tool_scope,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)

BUSY_TIMEOUT_MS = 1_000
REQUESTED_AT = "2026-03-23T00:00:02Z"
SCOPE = integration_tool_scope(account="account-a", slug="GMAIL_SEND_EMAIL")
SUMMARY: dict[str, JSONValue] = {
    "kind": "integration_tool",
    "account": "account-a",
    "slug": "GMAIL_SEND_EMAIL",
    "arguments": {"to": "someone@example.com"},
}


def _auto_approving_context(tmp_path: Path, tool_id: str) -> BrokerContext:
    db_path, runtime = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    set_action_approval_mode(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id="action-1",
        approval_mode="always_allow",
        updated_at=REQUESTED_AT,
    )
    return load_broker_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        tool_id=tool_id,
        path_access_kind="none",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=runtime.manifest_id,
        execution_session_id=runtime.execution_session_id,
    )


def _authorize(
    context: BrokerContext, tool_request_id: str, *, scope: str | None
) -> tuple[str | None, str | None]:
    return ensure_tool_authorization(
        context=context,
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        command_summary=SUMMARY,
        requested_at=REQUESTED_AT,
        integration_tool_scope=scope,
    )


def _approval_source(context: BrokerContext, approval_session_id: str) -> str:
    with sqlite3.connect(context.db_path) as connection:
        row = connection.execute(
            "SELECT approval_source FROM approval_sessions"
            " WHERE approval_session_id = ?",
            (approval_session_id,),
        ).fetchone()
    return str(row[0])


# `read` already holds its capabilities in the session; `bash` is approved by
# the saved global always_allow and the Action-level always_allow.
@pytest.mark.parametrize("tool_id", ["read", "bash"])
def test_integration_write_prompts_every_time_under_auto_approval(
    tmp_path: Path, tool_id: str
) -> None:
    context = _auto_approving_context(tmp_path, tool_id)
    # Without the integration scope the same request runs without asking.
    assert _authorize(context, "request-control", scope=None)[1] != "prompt"

    with pytest.raises(BrokerApprovalRequiredError) as first:
        _authorize(context, "request-1", scope=SCOPE)
    assert _approval_source(context, first.value.approval_session_id) == "prompt"

    apply_approval_decision(
        db_path=context.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        tool_request_id="request-1",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=first.value.approval_session_id,
        decision="approved_once",
        decided_at=REQUESTED_AT,
    )
    assert _authorize(context, "request-1", scope=SCOPE) == (
        first.value.approval_session_id,
        "prompt",
    )

    # The approval covered that one request; the next write asks again.
    with pytest.raises(BrokerApprovalRequiredError) as second:
        _authorize(context, "request-2", scope=SCOPE)
    assert second.value.approval_session_id != first.value.approval_session_id

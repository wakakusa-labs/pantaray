"""A subagent's browser ``allow_writes`` takes the path its gated commands take:
its own approval review, then a pause for the user."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.stop_watch import ActionStopWatch
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    load_action_subagent_broker_authority,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
)
from pantaray_agents.local_runtime.runtime.browser_relay import browser_write_hosts
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    ApprovalReviewer,
    execute_action_subagent_broker_tool,
)
from pantaray_llm.profiles import ACTION_EXECUTING_PROFILE_ID

from .test_action_subagent_job import _Client, _running_child

_ARGS = {
    "action": "allow_writes",
    "host": "api.example.com",
    "justification": "Send the form you asked me to test.",
}


# Catches: a child's write request running unreviewed or on another run's
# approval, or one its review left to the user running instead of parking.
def test_a_childs_allow_writes_is_reviewed_then_parks_for_the_user(
    tmp_path: Path,
) -> None:
    db_path, payload = _running_child(tmp_path, profile_id=ACTION_EXECUTING_PROFILE_ID)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE execution_sessions SET tool_allowlist_json = "
            "json_insert(tool_allowlist_json, '$[#]', 'browser')"
        )
    client = _Client(
        review='{"decision": "ask_user", "reason": "Decide whether to send it."}'
    )

    async def call() -> None:
        await execute_action_subagent_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            payload=payload,
            authority=load_action_subagent_broker_authority(
                db_path=db_path, busy_timeout_ms=1_000, payload=payload
            ),
            tool_id="browser",
            args=dict(_ARGS),
            tool_request_id="child-request-1",
            call_id="call-browser",
            agents_md=None,  # type: ignore[arg-type]
            reviewer=ApprovalReviewer(
                client=client,  # type: ignore[arg-type]
                sink=CountingSink(),
                stop_watch=ActionStopWatch(probe=None),
            ),
        )

    with pytest.raises(ActionSubagentApprovalPause) as paused:
        asyncio.run(call())

    assert client.reviews == 1
    assert paused.value.tool_id == "browser"
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT status, review_reason, command_summary_json FROM approval_sessions"
        ).fetchone()
    assert session[:2] == ("pending", "Decide whether to send it.")
    assert json.loads(session[2])["summary_kind"] == "browser_write"
    assert browser_write_hosts(payload["process_id"]) == ()

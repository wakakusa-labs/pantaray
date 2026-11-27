"""create_hook asks the user, then saves the hook they said yes to."""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ALLOWED_TOOL_IDS,
    _bootstrap_runtime_db,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import hooks
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    CompletedToolControl,
    FailedToolControl,
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.validation import (
    validate_tool_args,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import TOOL_REGISTRY
from pantaray_agents.agents.action_agent.tools.hook_tools import (
    CREATE_HOOK_TOOL,
    DELETE_HOOK_TOOL,
    LIST_HOOKS_TOOL,
)
from pantaray_agents.local_runtime.hooks.agent_tasks import list_conversation_tasks
from pantaray_agents.local_runtime.hooks.controls import NewConversation, active_hooks
from pantaray_agents.local_runtime.runtime.action_approval_review import (
    pending_operation,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    apply_approval_decision,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationStartInput
from pantaray_agents.local_runtime.tooling.repository import (
    load_approval_session_by_request,
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_organization,
    create_workspace_project,
)
from pantaray_agents.schema.agent.base import JSONValue

BUSY_TIMEOUT_MS = 1_000
NOW = "2026-03-23T00:00:02Z"
_REQUESTS = itertools.count(1)
TOMORROW = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
DAILY: dict[str, JSONValue] = {
    "trigger": {"every": "day", "offset": "8h"},
    "send_to": "this_session",
    "message": "Look for the competitor's news since the last run.",
}
MEETING_MATCH: list[JSONValue] = [
    {"field": "bundle_id", "op": "equals", "values": ["us.zoom.xos"]},
    {"field": "window_title", "op": "contains", "values": ["Zoom Meeting", "(.*)"]},
]
MEETINGS: dict[str, JSONValue] = {
    "trigger": {"on": "activity", "match": MEETING_MATCH},
    "send_to": "new_session",
    "place": {"organization": "Acme", "project": "Sales"},
    "title": "Meeting notes",
    "message": "Start a notes outline for this meeting.",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        allowed_tool_ids=(*BROKER_ALLOWED_TOOL_IDS, CREATE_HOOK_TOOL.tool_id),
    )
    monkeypatch.setattr(
        hooks, "read_local_runtime_db_config", lambda: (db_path, BUSY_TIMEOUT_MS)
    )
    monkeypatch.setattr(
        hooks,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        display_name="Acme",
        icon_data_url=None,
        now=NOW,
    )
    for name, organization_ids in (
        ("Sales", (organization.organization_id,)),
        ("Hiring", ()),
    ):
        create_workspace_project(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            display_name=name,
            organization_ids=organization_ids,
            now=NOW,
        )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at=NOW,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["manifest_id"] = context.manifest_id  # type: ignore[attr-defined]
    state["execution_session_id"] = context.execution_session_id  # type: ignore[attr-defined]
    yield SimpleNamespace(db_path=db_path, context=context, state=state)


async def _preflight(env: SimpleNamespace, args: dict[str, JSONValue], request: str):
    validate_tool_args(CREATE_HOOK_TOOL, args)
    return await hooks.run_create_hook_preflight(
        step_id="step-1",
        tool_def=CREATE_HOOK_TOOL,
        state=env.state,
        args=args,
        tool_request_id=request,
        requested_at=NOW,
    )


def _decide(env: SimpleNamespace, request: str, decision: str) -> None:
    apply_approval_decision(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        tool_request_id=request,
        user_id="user-1",
        action_id="action-1",
        approval_session_id=None,
        decision=decision,  # type: ignore[arg-type]
        decided_at=NOW,
    )


async def _call(
    env: SimpleNamespace, args: dict[str, JSONValue], request: str
) -> dict[str, JSONValue]:
    """The call the Action runs once the preflight lets it."""

    record_tool_invocation_start(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=ToolInvocationStartInput(
            invocation_id=f"invocation-{request}",
            tool_request_id=request,
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id=CREATE_HOOK_TOOL.tool_id,
            manifest_id=env.context.manifest_id,
            execution_session_id=env.context.execution_session_id,
            cwd=".",
            timeout_ms=None,
            intent_class="automation_control",
            network_policy="deny",
            command_summary_json={},
            capability_snapshot_json={"required_capabilities": []},
            request_json={},
            status="running",
            started_at=NOW,
        ),
    )
    result = await hooks.run_create_hook_tool(
        step_id="step-1",
        tool_def=CREATE_HOOK_TOOL,
        state=env.state,
        args=args,
        tool_request_id=request,
        tool_invocation_id=f"invocation-{request}",
        requested_at=NOW,
    )
    assert isinstance(result.output, dict)
    return result.output


async def _create(env: SimpleNamespace, args: dict[str, JSONValue]) -> str:
    """Ask, say yes, run; the hook id it saved."""

    request = f"request-{next(_REQUESTS)}"
    asked = await _preflight(env, args, request)
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    _decide(env, request, "approved_once")
    assert isinstance(
        (await _preflight(env, args, request)).control, CompletedToolControl
    )
    output = await _call(env, args, request)
    assert output["status"] == "created"
    return str(output["hook_id"])


def _rows(env: SimpleNamespace, action_id: str = "action-1"):
    return list_conversation_tasks(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=action_id,
    )


def _run(
    env: SimpleNamespace, tool, args: dict[str, JSONValue]
) -> dict[str, JSONValue]:
    result = hooks.run_hook_tool(
        step_id="step-1", tool_def=tool, args=args, state=env.state
    )
    assert isinstance(result.output, dict)
    return result.output


def _version(env: SimpleNamespace) -> int:
    with closing(sqlite3.connect(env.db_path)) as connection:
        (version,) = connection.execute("SELECT version FROM user_hooks").fetchone()
    return int(version)


async def test_a_yes_saves_the_hook_it_showed_and_a_no_saves_nothing(
    env: SimpleNamespace,
) -> None:
    # Catches a hook saved before the user answered, one saved after a no,
    # and a yes that approves something other than what the panel showed.
    asked = await _preflight(env, DAILY, "request-no")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary == {
        "summary_kind": "hook",
        "replaces": False,
        "resumes": False,
        "trigger": {"every": "day", "offset": "8h"},
        "until": None,
        "send_to": "this_session",
        "place": None,
        "title": None,
        "message": DAILY["message"],
    }
    _decide(env, "request-no", "denied")
    assert isinstance(
        (await _preflight(env, DAILY, "request-no")).control, ApprovalDeniedToolControl
    )
    assert _rows(env) == ()

    hook_id = await _create(env, DAILY)
    (row,) = _rows(env)
    assert (row.task.hook_id, row.enabled, row.target) == (hook_id, True, "action-1")
    assert (row.task.schedule.every, row.task.schedule.offset) == ("day", "8h")
    assert row.task.message == DAILY["message"]


async def test_a_new_session_hook_saves_its_title_and_the_place_it_named(
    env: SimpleNamespace,
) -> None:
    # Catches a place saved by another id than the names shown, a title left
    # out, a condition shown in other words than the one saved, and a value
    # that looks like a pattern read as one.
    asked = await _preflight(env, MEETINGS, "request-meetings")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    summary = asked.control.command_summary
    assert summary["trigger"] == {"on": "activity", "match": MEETING_MATCH}
    assert (summary["send_to"], summary["title"]) == ("new_session", "Meeting notes")
    assert summary["place"] == {"organization": "Acme", "project": "Sales"}
    _decide(env, "request-meetings", "approved_once")
    output = await _call(env, MEETINGS, "request-meetings")

    assert output["next_run_at"] is None
    (row,) = _rows(env)
    assert isinstance(row.target, NewConversation)
    assert (row.target.title, row.target.organization_id) == ("Meeting notes", None)
    (listed,) = _run(env, LIST_HOOKS_TOOL, {})["hooks"]  # type: ignore[misc]
    assert listed["trigger"] == {"on": "activity", "match": MEETING_MATCH}  # type: ignore[index]
    assert listed["place"] == {"organization": None, "project": "Sales"}  # type: ignore[index]
    assert (listed["send_to"], listed["title"]) == ("new_session", "Meeting notes")  # type: ignore[index]


async def test_the_approval_review_never_answers_for_a_hook(
    env: SimpleNamespace,
) -> None:
    # Catches a hook the AI review could approve with no one asked, which
    # would let a turn set up its own later turns.
    asked = await _preflight(env, DAILY, "request-review")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    session = load_approval_session_by_request(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        tool_request_id="request-review",
    )
    assert session is not None
    summary = dict(session.command_summary_json)
    assert pending_operation(tool_id="create_hook", summary=summary, args=DAILY) is None


async def test_a_replace_asks_again_and_keeps_the_old_hook_until_yes(
    env: SimpleNamespace,
) -> None:
    # Catches an agent's change that fires before the user saw it, and a
    # replace that reaches another conversation's hook.
    hook_id = await _create(env, DAILY)
    changed = {
        **DAILY,
        "message": "Also check their pricing page.",
        "replaces": hook_id,
    }
    asked = await _preflight(env, changed, "request-update")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["replaces"] is True
    assert _rows(env)[0].task.message == DAILY["message"]

    _decide(env, "request-update", "approved_once")
    assert (await _call(env, changed, "request-update"))["status"] == "created"
    (row,) = _rows(env)
    assert row.task.hook_id == hook_id
    assert "pricing page" in row.task.message

    insert_agent_action(db_path=env.db_path, action_id="action-2", suggestion_id="s-2")
    env.state["action_id"] = "action-2"
    refused = await _preflight(env, changed, "request-other")
    assert isinstance(refused.control, FailedToolControl)
    assert _run(env, LIST_HOOKS_TOOL, {}) == {"hooks": []}
    assert _run(env, DELETE_HOOK_TOOL, {"hook_id": hook_id})["status"] == "error"
    assert len(_rows(env, "action-1")) == 1


async def test_a_replace_that_only_moves_where_it_goes_is_a_new_definition(
    env: SimpleNamespace,
) -> None:
    # Catches a changed title or place saved without a new version, so a fire
    # computed from the old definition writes over the new one.
    unplaced = {**MEETINGS, "place": None}
    hook_id = await _create(env, unplaced)
    await _create(env, {**unplaced, "replaces": hook_id})
    assert _version(env) == 1
    await _create(env, {**unplaced, "title": "Notes", "replaces": hook_id})
    assert _version(env) == 2
    await _create(env, {**MEETINGS, "title": "Notes", "replaces": hook_id})
    assert _version(env) == 3
    (row,) = _rows(env)
    assert isinstance(row.target, NewConversation)
    assert (row.target.title, row.target.project_id is not None) == ("Notes", True)


@pytest.mark.parametrize(
    "args",
    [
        {**DAILY, "trigger": {"at": "2020-01-01T09:00:00+09:00"}},
        {**DAILY, "trigger": {"at": "2030-01-01T09:00:00"}},
        {**DAILY, "trigger": {"every": "2h", "offset": "1h"}},
        {**DAILY, "trigger": {"every": "day", "offset": "25h"}},
        {**DAILY, "until": "2020-01-01T00:00:00+00:00"},
        # The first run is not before until: one at, and a repeat whose first
        # run, two days after the approval, is after an until one day away.
        {
            **DAILY,
            "trigger": {"at": "2030-01-03T09:00:00+09:00"},
            "until": "2030-01-02T00:00:00+09:00",
        },
        {
            **DAILY,
            "trigger": {"every": "2d"},
            "until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        {**DAILY, "title": "Notes"},
        {**DAILY, "place": {"organization": "Acme"}},
        {**MEETINGS, "title": None},
        {**MEETINGS, "title": "Notes\nand more"},
        {**MEETINGS, "place": {"organization": "Acme Inc"}},
        {**MEETINGS, "place": {"project": "Sale"}},
        {**MEETINGS, "place": {"organization": "Acme", "project": "Hiring"}},
    ],
)
async def test_a_hook_that_cannot_run_as_meant_is_refused_before_asking(
    env: SimpleNamespace, args: dict[str, JSONValue]
) -> None:
    # Catches the user asked to approve a hook that would never fire as meant
    # (a past or zone-less time, a misplaced offset), a title or place that
    # this_session would drop, and a new TODO with no title, a title it could
    # not have, or a place the workspace does not have.
    refused = await _preflight(env, args, f"request-{next(_REQUESTS)}")
    assert isinstance(refused.control, FailedToolControl)
    assert isinstance(refused.result.output, dict)
    assert refused.result.output["error"]["error_type"] == "invalid_hook"  # type: ignore[index]


async def test_an_unknown_place_is_refused_with_the_names_there_are(
    env: SimpleNamespace,
) -> None:
    # Catches a refusal that leaves the model guessing names, which would
    # make it invent a place each try.
    refused = await _preflight(
        env, {**MEETINGS, "place": {"project": "Sale"}}, "request-place"
    )
    assert isinstance(refused.result.output, dict)
    message = str(refused.result.output["error"]["message"])  # type: ignore[index]
    assert "'Hiring'" in message
    assert "'Sales'" in message


@pytest.mark.parametrize(
    "args",
    [
        {**DAILY, "replaces": "builtin.insight"},
        {**DAILY, "conversation": "action-2"},
        {**DAILY, "send_to": "action-2"},
        {**DAILY, "trigger": {"at": TOMORROW, "every": "day"}},
        {**MEETINGS, "trigger": {"on": "insight.recorded", "match": MEETING_MATCH}},
        {**MEETINGS, "trigger": {"on": "activity", "match": []}},
        {
            **MEETINGS,
            "trigger": {
                "on": "activity",
                "match": [{"field": "url", "op": "contains", "values": ["meet"]}],
            },
        },
        {
            **MEETINGS,
            "trigger": {
                "on": "activity",
                "match": [{"field": "app_name", "op": "matches", "values": ["Zoom"]}],
            },
        },
        {
            **MEETINGS,
            "trigger": {
                "on": "activity",
                "match": [{"field": "app_name", "op": "equals", "values": []}],
            },
        },
        {
            **MEETINGS,
            "trigger": {
                "on": "activity",
                "match": [{"field": "app_name", "op": "equals", "values": [" "]}],
            },
        },
    ],
)
def test_the_tool_cannot_name_a_conversation_a_built_in_or_an_unknown_condition(
    args: dict[str, JSONValue],
) -> None:
    # Catches a call that sends to a conversation the model named, reaches a
    # built-in hook, or saves a condition the watcher cannot match: an event
    # or field outside the closed list, an op other than equals or contains,
    # or a match with nothing to compare.
    with pytest.raises(ToolValidationError):
        validate_tool_args(CREATE_HOOK_TOOL, args)
    assert TOOL_REGISTRY[CREATE_HOOK_TOOL.tool_id] is CREATE_HOOK_TOOL


async def test_list_and_delete_reach_this_conversations_hooks(
    env: SimpleNamespace,
) -> None:
    # Catches a delete that leaves the hook firing, or one that cannot find it.
    hook_id = await _create(env, DAILY)
    (listed,) = _run(env, LIST_HOOKS_TOOL, {})["hooks"]  # type: ignore[misc]
    assert listed["hook_id"] == hook_id  # type: ignore[index]
    assert _run(env, DELETE_HOOK_TOOL, {"hook_id": hook_id}) == {
        "status": "deleted",
        "hook_id": hook_id,
    }
    assert _rows(env) == ()


@pytest.mark.parametrize("paused", ["switched off", "stopped by failures"])
async def test_an_approved_replace_turns_a_paused_hook_back_on(
    env: SimpleNamespace, paused: str
) -> None:
    # Catches a replace that approves the new content but leaves the hook off
    # or stopped, and a yes asked without saying the hook will run again.
    hook_id = await _create(env, DAILY)
    with closing(sqlite3.connect(env.db_path)) as connection, connection:
        if paused == "switched off":
            connection.execute("UPDATE user_hooks SET enabled = 0")
        else:
            connection.execute(
                "UPDATE user_hooks SET failure_count = 3,"
                " stopped_reason = 'turn_failed'"
            )
    changed = {
        **DAILY,
        "message": "Also check their pricing page.",
        "replaces": hook_id,
    }
    asked = await _preflight(env, changed, f"request-{paused}")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["resumes"] is True
    _decide(env, f"request-{paused}", "approved_once")
    assert (await _call(env, changed, f"request-{paused}"))["status"] == "created"

    with closing(sqlite3.connect(env.db_path)) as connection:
        active = active_hooks(connection, user_id="user-1", builtins=())
    assert list(active.tasks) == [hook_id]
    (row,) = _rows(env)
    assert (row.enabled, row.run.failure_count, row.run.stopped_reason) == (
        True,
        0,
        None,
    )


async def test_a_replace_of_a_running_hook_does_not_say_it_resumes(
    env: SimpleNamespace,
) -> None:
    # Catches a consent text that tells the user a running hook will resume.
    hook_id = await _create(env, DAILY)
    asked = await _preflight(env, {**DAILY, "replaces": hook_id}, "request-on")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["resumes"] is False


async def test_a_hook_whose_window_passed_during_the_approval_saves_nothing(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a save that recomputes the first run after a slow yes, stores a
    # time past until and reports a hook that will never run.
    args: dict[str, JSONValue] = {
        **DAILY,
        "trigger": {"every": "1h"},
        "until": (datetime.now(UTC) + timedelta(hours=1, minutes=1)).isoformat(),
    }
    asked = await _preflight(env, args, "request-slow")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    _decide(env, "request-slow", "approved_once")

    class _FiveMinutesLater(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
            return datetime.now(tz) + timedelta(minutes=5)

    monkeypatch.setattr(hooks, "datetime", _FiveMinutesLater)
    output = await _call(env, args, "request-slow")

    assert output["status"] == "error"
    assert output["error"]["error_type"] == "invalid_hook"  # type: ignore[index]
    assert _rows(env) == ()

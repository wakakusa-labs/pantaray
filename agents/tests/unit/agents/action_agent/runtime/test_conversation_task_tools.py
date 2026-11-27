"""schedule_continuation asks the user, then writes the task they said yes to."""

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

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    conversation_tasks,
)
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
from pantaray_agents.agents.action_agent.tools.conversation_task_tools import (
    LIST_SCHEDULED_CONTINUATIONS_TOOL,
    SCHEDULE_CONTINUATION_TOOL,
    STOP_SCHEDULED_CONTINUATION_TOOL,
)
from pantaray_agents.local_runtime.hooks.agent_tasks import list_conversation_tasks
from pantaray_agents.local_runtime.hooks.controls import active_hooks
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
from pantaray_agents.schema.agent.base import JSONValue

BUSY_TIMEOUT_MS = 1_000
NOW = "2026-03-23T00:00:02Z"
_REQUESTS = itertools.count(1)
TOMORROW = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
DAILY: dict[str, JSONValue] = {
    "prompt": "Look for the competitor's news since the last run.",
    "every": "day",
    "offset": "8h",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        allowed_tool_ids=(*BROKER_ALLOWED_TOOL_IDS, SCHEDULE_CONTINUATION_TOOL.tool_id),
    )
    monkeypatch.setattr(
        conversation_tasks,
        "read_local_runtime_db_config",
        lambda: (db_path, BUSY_TIMEOUT_MS),
    )
    monkeypatch.setattr(
        conversation_tasks,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
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
    yield SimpleNamespace(
        db_path=db_path,
        context=context,
        state=state,
    )


async def _preflight(env: SimpleNamespace, args: dict[str, JSONValue], request: str):
    return await conversation_tasks.run_schedule_continuation_preflight(
        step_id="step-1",
        tool_def=SCHEDULE_CONTINUATION_TOOL,
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
            tool_id=SCHEDULE_CONTINUATION_TOOL.tool_id,
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
    result = await conversation_tasks.run_schedule_continuation_tool(
        step_id="step-1",
        tool_def=SCHEDULE_CONTINUATION_TOOL,
        state=env.state,
        args=args,
        tool_request_id=request,
        tool_invocation_id=f"invocation-{request}",
        requested_at=NOW,
    )
    assert isinstance(result.output, dict)
    return result.output


async def _schedule(env: SimpleNamespace, args: dict[str, JSONValue]) -> str:
    """Ask, say yes, run; the task id it wrote."""

    request = f"request-{next(_REQUESTS)}"
    asked = await _preflight(env, args, request)
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    _decide(env, request, "approved_once")
    assert isinstance(
        (await _preflight(env, args, request)).control, CompletedToolControl
    )
    output = await _call(env, args, request)
    assert output["status"] == "scheduled"
    return str(output["task_id"])


def _tasks(env: SimpleNamespace, action_id: str = "action-1"):
    return list_conversation_tasks(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=action_id,
    )


def _run(
    env: SimpleNamespace, tool, args: dict[str, JSONValue]
) -> dict[str, JSONValue]:
    result = conversation_tasks.run_conversation_task_tool(
        step_id="step-1", tool_def=tool, args=args, state=env.state
    )
    assert isinstance(result.output, dict)
    return result.output


async def test_a_yes_writes_the_task_it_showed_and_a_no_writes_nothing(
    env: SimpleNamespace,
) -> None:
    # Catches a schedule written before the user answered, one written after
    # a no, and a yes that approves something other than what the panel showed.
    asked = await _preflight(env, DAILY, "request-no")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["prompt"] == DAILY["prompt"]
    assert (
        asked.control.command_summary["every"],
        asked.control.command_summary["offset"],
    ) == ("day", "8h")
    _decide(env, "request-no", "denied")
    assert isinstance(
        (await _preflight(env, DAILY, "request-no")).control, ApprovalDeniedToolControl
    )
    assert _tasks(env) == ()

    task_id = await _schedule(env, DAILY)
    (view,) = _tasks(env)
    assert (view.hook_id, view.enabled) == (task_id, True)
    assert (view.every, view.offset, view.message) == ("day", "8h", DAILY["prompt"])
    assert view.created_from_action_id == view.target_action_id == "action-1"


async def test_the_approval_review_never_answers_for_a_schedule(
    env: SimpleNamespace,
) -> None:
    # Catches a schedule the AI review could approve with no one asked, which
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
    assert (
        pending_operation(tool_id="schedule_continuation", summary=summary, args=DAILY)
        is None
    )


async def test_an_update_asks_again_and_keeps_the_old_task_until_yes(
    env: SimpleNamespace,
) -> None:
    # Catches an agent's change that fires before the user saw it, and an
    # update that reaches another conversation's schedule.
    task_id = await _schedule(env, DAILY)
    before = _tasks(env)[0].message
    changed = {**DAILY, "prompt": "Also check their pricing page.", "task_id": task_id}
    asked = await _preflight(env, changed, "request-update")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert _tasks(env)[0].message == before

    _decide(env, "request-update", "approved_once")
    assert (await _call(env, changed, "request-update"))["status"] == "scheduled"
    (view,) = _tasks(env)
    assert view.hook_id == task_id
    assert "pricing page" in view.message

    insert_agent_action(db_path=env.db_path, action_id="action-2", suggestion_id="s-2")
    env.state["action_id"] = "action-2"
    refused = await _preflight(env, changed, "request-other")
    assert isinstance(refused.control, FailedToolControl)
    assert _run(env, LIST_SCHEDULED_CONTINUATIONS_TOOL, {}) == {"tasks": []}
    assert (
        _run(env, STOP_SCHEDULED_CONTINUATION_TOOL, {"task_id": task_id})["status"]
        == "error"
    )
    assert len(_tasks(env, "action-1")) == 1


@pytest.mark.parametrize(
    "args",
    [
        {**DAILY, "at": TOMORROW},
        {"prompt": "x", "at": "2020-01-01T09:00:00+09:00"},
        {"prompt": "x", "at": "2030-01-01T09:00:00"},
        {"prompt": "x", "every": "2h", "offset": "1h"},
        {"prompt": "x", "every": "day", "offset": "25h"},
        {**DAILY, "until": "2020-01-01T00:00:00+00:00"},
        # The first run is not before until: one at, and a repeat whose first
        # run, two days after the approval, is after an until one day away.
        {
            "prompt": "x",
            "at": "2030-01-03T09:00:00+09:00",
            "until": "2030-01-02T00:00:00+09:00",
        },
        {
            "prompt": "x",
            "every": "2d",
            "until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    ],
)
async def test_a_schedule_that_cannot_run_is_refused_before_asking(
    env: SimpleNamespace, args: dict[str, JSONValue]
) -> None:
    # Catches the user asked to approve a schedule that would never fire as
    # meant: both triggers, a past or zone-less time, a misplaced offset.
    refused = await _preflight(env, args, f"request-{next(_REQUESTS)}")
    assert isinstance(refused.control, FailedToolControl)
    assert isinstance(refused.result.output, dict)
    assert refused.result.output["error"]["error_type"] == "invalid_schedule"  # type: ignore[index]


@pytest.mark.parametrize("task_id", ["builtin.insight", "../escape", "task.Bad"])
def test_the_tool_cannot_name_a_built_in_or_a_path(task_id: str) -> None:
    # Catches a task_id that would reach a built-in hook or a file outside
    # the tasks folder; a run mode other than continue cannot be given at all.
    with pytest.raises(ToolValidationError):
        validate_tool_args(SCHEDULE_CONTINUATION_TOOL, {**DAILY, "task_id": task_id})
    with pytest.raises(ToolValidationError):
        validate_tool_args(SCHEDULE_CONTINUATION_TOOL, {**DAILY, "conversation": "new"})
    assert (
        TOOL_REGISTRY[SCHEDULE_CONTINUATION_TOOL.tool_id] is SCHEDULE_CONTINUATION_TOOL
    )


async def test_list_and_stop_reach_this_conversations_tasks(
    env: SimpleNamespace,
) -> None:
    # Catches a stop that leaves the task firing, or one that cannot find it.
    task_id = await _schedule(env, DAILY)
    (listed,) = _run(env, LIST_SCHEDULED_CONTINUATIONS_TOOL, {})["tasks"]  # type: ignore[misc]
    assert listed["task_id"] == task_id  # type: ignore[index]
    assert _run(env, STOP_SCHEDULED_CONTINUATION_TOOL, {"task_id": task_id}) == {
        "status": "stopped",
        "task_id": task_id,
    }
    assert _tasks(env) == ()


@pytest.mark.parametrize("paused", ["switched off", "stopped by failures"])
async def test_an_approved_update_turns_a_paused_task_back_on(
    env: SimpleNamespace, paused: str
) -> None:
    # Catches an update that approves the new content but leaves the task off
    # or stopped, while the tool tells the model it is scheduled, and a yes
    # asked without saying the task will run again.
    task_id = await _schedule(env, DAILY)
    with closing(sqlite3.connect(env.db_path)) as connection, connection:
        if paused == "switched off":
            connection.execute("UPDATE user_hooks SET enabled = 0")
        else:
            connection.execute(
                "UPDATE user_hooks SET failure_count = 3,"
                " stopped_reason = 'turn_failed'"
            )
    changed = {**DAILY, "prompt": "Also check their pricing page.", "task_id": task_id}
    asked = await _preflight(env, changed, f"request-{paused}")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["resumes"] is True
    _decide(env, f"request-{paused}", "approved_once")
    output = await _call(env, changed, f"request-{paused}")

    assert "runs again" in str(output["message"])
    with closing(sqlite3.connect(env.db_path)) as connection:
        active = active_hooks(connection, user_id="user-1", builtins=())
    assert list(active.tasks) == [task_id]
    (view,) = _tasks(env)
    assert (view.enabled, view.failure_count, view.stopped_reason) == (True, 0, None)


async def test_an_update_of_a_running_task_does_not_say_it_resumes(
    env: SimpleNamespace,
) -> None:
    # Catches a consent text that tells the user a running task will resume.
    task_id = await _schedule(env, DAILY)
    asked = await _preflight(env, {**DAILY, "task_id": task_id}, "request-on")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    assert asked.control.command_summary["resumes"] is False


async def test_a_schedule_whose_window_passed_during_the_approval_writes_nothing(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a save that recomputes the first run after a slow yes, stores a
    # time past until and reports a schedule that will never run.
    args: dict[str, JSONValue] = {
        "prompt": "x",
        "every": "1h",
        "until": (datetime.now(UTC) + timedelta(hours=1, minutes=1)).isoformat(),
    }
    asked = await _preflight(env, args, "request-slow")
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    _decide(env, "request-slow", "approved_once")

    class _FiveMinutesLater(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
            return datetime.now(tz) + timedelta(minutes=5)

    monkeypatch.setattr(conversation_tasks, "datetime", _FiveMinutesLater)
    output = await _call(env, args, "request-slow")

    assert output["status"] == "error"
    assert output["error"]["error_type"] == "invalid_schedule"  # type: ignore[index]
    assert _tasks(env) == ()

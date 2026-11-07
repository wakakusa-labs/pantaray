from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.core import Rejected
from pantaray_agents.local_runtime.todos.goal_store import (
    add_ai_progress_record,
    add_user_progress_record,
    clear_project_goal,
    read_progress,
    set_project_goal,
)
from pantaray_agents.local_runtime.todos.model import Todo
from pantaray_agents.local_runtime.todos.progress import (
    ActionStepsEvidence,
    Goal,
    Measured,
    NoGoal,
    ProgressRecord,
)
from pantaray_agents.local_runtime.todos.store import (
    AllScope,
    AnyProject,
    OneProject,
    create_todo_item,
    delete_purged_todo,
    read_todo_revision,
    update_todo,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_deletion import (
    delete_workspace_project,
)

from .store_support import NOW, USER, T, count, execute, seed_action

LATER = replace(NOW, now="2026-11-05T03:00:00.000Z")


def _todo(client_todo_id: str = "c-1") -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=client_todo_id,
        project_id="project-1",
        text="Close the Acme deal",
        week_of="2026-11-04",
        due=None,
        clock=NOW,
    )
    assert isinstance(created, Todo)
    return created


def _set_goal(deadline: str = "2026-12-31") -> Goal | Rejected[str]:
    return set_project_goal(
        user_id=USER,
        project_id="project-1",
        text="Close 300M yen",
        target=300_000_000.0,
        unit="円",
        deadline=deadline,
        clock=NOW,
    )


def _user_record(
    todo_id: str, client_record_id: str = "r-1", *, quantity: float = 10.0
) -> ProgressRecord | Rejected[str] | None:
    return add_user_progress_record(
        user_id=USER,
        todo_id=todo_id,
        client_record_id=client_record_id,
        quantity=quantity,
        unit="円",
        note="Signed",
        clock=NOW,
    )


def _seed_second_run(db: Path) -> None:
    """Action `a` ran once (S-2-TOOL) and now runs again (S-4-TOOL, S-5-TOOL)."""

    seed_action(db, "a", message_id="m-1")
    tool = """INSERT INTO agent_action_steps(step_id, action_id, user_id, step_number,
      local_step_number, short_step_id, step_type, step_name, status, goal_handle,
      tool_output, created_at, completed_at) VALUES"""
    execute(
        db,
        f"""
        {tool} ('step-2', 'a', '{USER}', 2, 2, 'S-2-TOOL', 'tool_execution',
          'tool::read', 'success', 'S', '{{}}', '{T}', '{T}');
        INSERT INTO processes(process_id, user_id, kind, status, action_id,
          started_at, updated_at, heartbeat_at, next_event_seq)
          VALUES ('run-2', '{USER}', 'action', 'running', 'a', '{T}', '{T}', '{T}', 1);
        INSERT INTO agent_action_steps(step_id, action_id, user_id, step_number,
          local_step_number, short_step_id, step_type, step_name, status, goal_handle,
          user_message_id, user_message_json, user_request_text, accepted_sequence,
          adopted_process_id, created_at) VALUES ('user-2', 'a', '{USER}', 3, 3,
          'S-3-USER', 'user_request', 'user_request', 'success', 'S', 'm-2', '{{}}',
          'And the total?', 2, 'run-2', '{T}');
        {tool} ('step-4', 'a', '{USER}', 4, 4, 'S-4-TOOL', 'tool_execution',
          'tool::integration_run_tool', 'success', 'S', '{{}}', '{T}', '{T}');
        {tool} ('step-5', 'a', '{USER}', 5, 5, 'S-5-TOOL', 'tool_execution',
          'tool::web_extract', 'error', 'S', NULL, '{T}', '{T}');
        """,
    )


def _ai_record(todo_id: str, *refs: str) -> ProgressRecord | Rejected[str] | None:
    return add_ai_progress_record(
        user_id=USER,
        todo_id=todo_id,
        tool_step_id="step-6",
        action_id="a",
        process_id="run-2",
        evidence_refs=refs,
        quantity=30_000_000.0,
        unit="円",
        note="Acme signed",
        clock=NOW,
    )


def test_editing_a_goal_keeps_when_it_was_set_and_a_replay_writes_nothing(
    db: Path,
) -> None:
    first = _set_goal()
    revision = read_todo_revision(user_id=USER)

    assert _set_goal() == first
    assert read_todo_revision(user_id=USER) == revision
    edited = set_project_goal(
        user_id=USER,
        project_id="project-1",
        text="Close 400M yen",
        target=400_000_000.0,
        unit="円",
        deadline="2027-03-31",
        clock=LATER,
    )

    assert isinstance(edited, Goal) and edited.set_at == NOW.now
    assert read_todo_revision(user_id=USER) == revision + 1


def test_a_goal_needs_the_users_project_and_a_deadline_not_before_it(
    db: Path,
) -> None:
    assert _set_goal(deadline="2026-11-03") == Rejected("deadline_before_set")
    assert set_project_goal(
        user_id=USER,
        project_id="someone-elses",
        text="Goal",
        target=1.0,
        unit="件",
        deadline="2026-12-31",
        clock=NOW,
    ) == Rejected("project_not_found")
    assert count(db, "SELECT COUNT(*) FROM todo_goals") == 0


def test_deleting_the_project_removes_its_goal_and_bumps_the_revision(
    db: Path,
) -> None:
    _set_goal()
    revision = read_todo_revision(user_id=USER)

    delete_workspace_project(
        db_path=db, busy_timeout_ms=1_000, user_id=USER, project_id="project-1"
    )

    assert count(db, "SELECT COUNT(*) FROM todo_goals") == 0
    assert read_todo_revision(user_id=USER) == revision + 1


def test_a_replayed_user_record_returns_it_and_other_content_conflicts(
    db: Path,
) -> None:
    todo = _todo()
    first = _user_record(todo.todo_id)

    assert isinstance(first, ProgressRecord)
    assert _user_record(todo.todo_id) == first
    assert _user_record(todo.todo_id, quantity=11.0) == Rejected("record_conflict")
    assert count(db, "SELECT COUNT(*) FROM todo_progress_records") == 1


def test_a_deleted_todo_takes_no_record_and_its_records_stop_counting(
    db: Path,
) -> None:
    _set_goal()
    todo = _todo()
    _user_record(todo.todo_id)
    update_todo(
        user_id=USER,
        todo_id=todo.todo_id,
        step=lambda current: core.delete_by_user(current, now=NOW.now),
    )

    assert _user_record(todo.todo_id, "r-2") == Rejected("deleted")
    view = read_progress(
        user_id=USER,
        scope=AllScope(),
        project=OneProject("project-1"),
        period="week",
        clock=NOW,
    )
    assert view.records == () and view.rows[0].record_ids == ()


def test_purging_a_todo_purges_its_records(db: Path) -> None:
    todo = _todo()
    _user_record(todo.todo_id)
    deleted = update_todo(
        user_id=USER,
        todo_id=todo.todo_id,
        step=lambda current: core.delete_by_user(current, now=NOW.now),
    )
    assert isinstance(deleted, Todo)

    delete_purged_todo(user_id=USER, todo=deleted)

    assert count(db, "SELECT COUNT(*) FROM todo_progress_records") == 0


def test_the_ai_records_only_with_successful_tool_steps_of_its_own_run(
    db: Path,
) -> None:
    todo = _todo()
    _seed_second_run(db)

    # From the earlier run, failed, or unknown.
    assert _ai_record(todo.todo_id, "S-4-TOOL", "S-2-TOOL") == Rejected(
        "evidence_not_in_run"
    )
    assert _ai_record(todo.todo_id, "S-5-TOOL") == Rejected("evidence_not_in_run")
    assert _ai_record(todo.todo_id, "S-9-TOOL") == Rejected("evidence_not_in_run")
    assert count(db, "SELECT COUNT(*) FROM todo_progress_records") == 0

    recorded = _ai_record(todo.todo_id, "S-4-TOOL")

    assert isinstance(recorded, ProgressRecord)
    assert recorded.evidence == ActionStepsEvidence(action_id="a", step_ids=("step-4",))
    assert _ai_record(todo.todo_id, "S-4-TOOL") == recorded
    assert count(db, "SELECT COUNT(*) FROM todo_progress_records") == 1


def test_the_view_reports_a_named_project_without_a_goal_and_others_with_one(
    db: Path,
) -> None:
    _set_goal()
    todo = _todo()
    _user_record(todo.todo_id, quantity=30_000_000.0)
    execute(
        db,
        f"""INSERT INTO workspace_projects(project_id, user_id, display_name,
          created_at, updated_at) VALUES ('project-2', '{USER}', 'Two', '{T}', '{T}')""",
    )

    every = read_progress(
        user_id=USER, scope=AllScope(), project=AnyProject(), period="day", clock=NOW
    )
    named = read_progress(
        user_id=USER,
        scope=AllScope(),
        project=OneProject("project-2"),
        period="day",
        clock=NOW,
    )

    (goal,) = every.goals
    assert isinstance(goal, Measured) and goal.share == 0.1
    assert named.goals == (NoGoal(project_id="project-2"),)
    assert clear_project_goal(user_id=USER, project_id="project-1") is None
    assert (
        read_progress(
            user_id=USER,
            scope=AllScope(),
            project=AnyProject(),
            period="day",
            clock=NOW,
        ).goals
        == ()
    )

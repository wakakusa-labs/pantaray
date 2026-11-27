"""The tasks an Action schedules: they fire as the user said yes to, continue
the conversation that saved them, and go with it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.hooks import conversations, gates, runner, states
from pantaray_agents.local_runtime.hooks.agent_tasks import (
    list_conversation_tasks,
    new_task_id,
    save_task,
    stop_task,
)
from pantaray_agents.local_runtime.hooks.listing import (
    TaskView,
    list_hooks,
    set_hook_enabled,
)
from pantaray_agents.local_runtime.hooks.tasks import Task, TaskSchedule, build_task
from pantaray_agents.local_runtime.runtime.action_approval_review import (
    action_request_rows,
    action_request_words,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_HOOK_STEP_NAME,
    ActionMessageConflictError,
    NewActionTarget,
    SubmitActionMessageCommand,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.model import ActionConversation
from pantaray_agents.local_runtime.todos.pickup import pick_up_todos
from pantaray_agents.local_runtime.todos.reads import read_todo
from pantaray_agents.local_runtime.todos.writes import update_todo
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput

from ..migrated_db import prepare_test_database

USER = "user-1"
HOOK = new_task_id("request-1")
NINE = datetime(2026, 11, 21, 9, 0, 30, tzinfo=UTC)
# Before the 09:00 occurrence, which an approval then waits for.
APPROVED = datetime(2026, 11, 21, 8, 59, tzinfo=UTC)
HOURLY = TaskSchedule(at=None, every="hour", offset=None, until=None)
DAILY_AT_8 = TaskSchedule(at=None, every="day", offset="8h", until=None)
PROMPT = "Check the build and say what changed."


@dataclass
class _World:
    db: Path
    action_id: str | None = None

    def view(self, hook_id: str = HOOK) -> TaskView:
        (view,) = (
            view
            for view in list_hooks(db_path=self.db, busy_timeout_ms=1_000, user_id=USER)
            if view.hook_id == hook_id
        )
        assert isinstance(view, TaskView)
        return view

    def save(
        self,
        schedule: TaskSchedule = HOURLY,
        prompt: str = PROMPT,
        now: datetime = APPROVED,
    ) -> str:
        """The user's yes to a task: created first, replaced after."""
        change = "create" if self.action_id is None else "update"
        if self.action_id is None:
            self.action_id = _conversation(self)
        _save(self, self.action_id, build_task(HOOK, schedule, prompt), change, now)
        return self.action_id

    def enable(self, enabled: bool, hook_id: str = HOOK) -> None:
        assert (
            set_hook_enabled(
                db_path=self.db,
                busy_timeout_ms=1_000,
                user_id=USER,
                hook_id=hook_id,
                enabled=enabled,
            )
            is True
        )

    def tick(self, now: datetime) -> None:
        runner.run_hooks_once(
            db_path=self.db, busy_timeout_ms=1_000, user_id=USER, now=now
        )

    def turns(self) -> list[tuple[str, str]]:
        """Each task turn sent: its conversation and its run's status."""
        with closing(sqlite3.connect(self.db)) as connection:
            return [
                (str(action_id), str(status))
                for action_id, status in connection.execute(
                    """SELECT step.action_id, turn.status FROM agent_action_steps AS step
                       JOIN processes AS turn ON turn.process_id = step.adopted_process_id
                       WHERE step.step_name = ? ORDER BY step.created_at, step.rowid""",
                    (ACTION_HOOK_STEP_NAME,),
                )
            ]

    def end_latest_turn(self, status: str) -> None:
        """Move the newest run to ``status``, as its terminal would."""
        action_status = {
            "running": "processing",
            "paused": "processing",
            "completed": "success",
            "failed": "error",
            "canceled": "canceled",
        }[status]
        with closing(sqlite3.connect(self.db)) as connection, connection:
            (process_id, action_id) = connection.execute(
                "SELECT process_id, action_id FROM processes WHERE kind = 'action' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            connection.execute(
                "UPDATE processes SET status = ? WHERE process_id = ?",
                (status, process_id),
            )
            connection.execute(
                "UPDATE jobs SET status = ? WHERE process_id = ?", (status, process_id)
            )
            connection.execute(
                "UPDATE agent_actions SET status = ? WHERE action_id = ?",
                (action_status, action_id),
            )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    db = tmp_path / "runtime.db"
    prepare_test_database(db, 1_000, load_default_migrations())
    monkeypatch.setenv("LOCAL_DB_PATH", str(db))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(runner, "builtin_hooks", lambda: ())
    monkeypatch.setattr(gates, "owner_is_settled", lambda: True)
    monkeypatch.setattr(gates, "background_requests_can_send", lambda: True)
    monkeypatch.setattr(states, "local_zone_name", lambda: "UTC")
    register_logged_out_owner(USER)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) "
            "VALUES (?, 'en', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
            (USER,),
        )
    yield _World(db=db)
    reset_logged_out_owner()


def test_a_task_continues_the_conversation_that_wrote_it(world: _World) -> None:
    # Catches a fire per tick inside one occurrence, and a task that opens a
    # new conversation instead of continuing the one the user said yes in.
    world.save()
    world.tick(NINE)
    world.tick(NINE + timedelta(seconds=3))
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1))

    turns = world.turns()
    assert len(turns) == 2
    assert turns[0][0] == turns[1][0] == world.view().created_from_action_id
    assert world.view().last_result is None  # The second turn is still queued.


def test_the_approval_review_reads_a_task_turn_as_not_the_users_words(
    world: _World,
) -> None:
    # Catches a task's prompt stored as the user's message, which would let the
    # review treat a scheduled prompt as the user asking for this turn now.
    action_id = world.save()
    world.tick(NINE)

    with closing(sqlite3.connect(world.db)) as connection:
        connection.row_factory = sqlite3.Row
        rows = action_request_rows(connection, user_id=USER, action_id=action_id)
    user_messages, not_by_user = action_request_words(
        rows, lambda _row: (), lambda _ids: ()
    )
    assert [message.text for _, message in user_messages] == ["Watch the build for me."]
    assert not_by_user is not None and not_by_user.source == "hook"
    assert not_by_user.text.startswith("Check the build and say what changed.")


def test_a_busy_conversation_holds_the_task_then_it_fires_once(
    world: _World,
) -> None:
    # Catches a turn queued behind a running one, one sent while the user is
    # asked to approve, occurrences spent while waiting, and a catch-up that
    # sends every missed occurrence.
    world.save()
    world.tick(NINE)
    world.end_latest_turn("running")
    world.tick(NINE + timedelta(hours=1))
    world.end_latest_turn("paused")
    world.tick(NINE + timedelta(hours=2))
    assert len(world.turns()) == 1

    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=2, seconds=3))
    world.tick(NINE + timedelta(hours=2, seconds=6))
    assert len(world.turns()) == 2


def test_a_fire_repeated_after_a_crash_finds_its_turn(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a crash after the turn was sent, before the next fire time was
    # written, that sends the occurrence again after a restart or counts it
    # as a failure.
    world.save()
    write = runner.SqliteHookStateStore.write_next_fire_time
    writes: list[object] = []

    def crash_after_the_turn(
        self: runner.SqliteHookStateStore, hook_id: str, at: datetime | None
    ) -> None:
        writes.append(at)
        if len(writes) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        write(self, hook_id, at)

    monkeypatch.setattr(
        runner.SqliteHookStateStore, "write_next_fire_time", crash_after_the_turn
    )
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(seconds=3))

    assert len(world.turns()) == 1
    assert world.view().failure_count == 0


def test_a_turn_that_ends_during_a_tick_is_counted_before_the_next(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a turn that ends after the tick counted results but before the
    # next turn is sent: its failure would never count, so the third failure
    # in a row would not stop the task.
    world.save()
    for hour in range(2):
        world.tick(NINE + timedelta(hours=hour))
        world.end_latest_turn("failed")
    world.tick(NINE + timedelta(hours=2))
    count = runner.record_turn_results

    def ends_after_counting(connection: sqlite3.Connection, *, user_id: str) -> None:
        count(connection, user_id=user_id)
        world.end_latest_turn("failed")

    monkeypatch.setattr(runner, "record_turn_results", ends_after_counting)
    world.tick(NINE + timedelta(hours=3))

    assert len(world.turns()) == 3
    assert (world.view().failure_count, world.view().stopped_reason) == (
        3,
        "turn_failed",
    )


def test_newly_approved_content_starts_its_schedule_afresh(world: _World) -> None:
    # Catches a spent at task moved to a later time that never fires again,
    # and an unchanged approval that fires a spent at once more.
    at = TaskSchedule(
        at=datetime(2026, 11, 21, 9, tzinfo=UTC), every=None, offset=None, until=None
    )
    world.save(at)
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.save(at)
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1

    world.save(replace(at, at=datetime(2026, 11, 21, 11, tzinfo=UTC)))
    world.tick(NINE + timedelta(hours=1, seconds=3))
    assert len(world.turns()) == 1
    world.tick(NINE + timedelta(hours=2))
    assert len(world.turns()) == 2


def test_an_approval_waits_for_the_next_occurrence(world: _World) -> None:
    # Catches a just-approved every task that fires the occurrence already
    # past: "every day at 08:00" approved at 14:00 must not run at 14:00.
    world.save(DAILY_AT_8, now=datetime(2026, 11, 21, 14, 0, tzinfo=UTC))
    world.tick(datetime(2026, 11, 21, 14, 0, 3, tzinfo=UTC))
    assert world.turns() == []
    assert world.view().next_fire_at == datetime(2026, 11, 22, 8, 0, tzinfo=UTC)

    world.tick(datetime(2026, 11, 22, 8, 0, 3, tzinfo=UTC))
    assert len(world.turns()) == 1


def test_three_failures_in_a_row_stop_the_task_until_it_is_enabled(
    world: _World,
) -> None:
    # Catches a task that keeps failing unseen, a success that does not reset
    # the count, and a stop that enabling cannot undo.
    world.save()
    hour = NINE
    for result in ("failed", "failed", "completed", "failed", "failed"):
        world.tick(hour)
        world.end_latest_turn(result)
        hour += timedelta(hours=1)
    world.tick(hour)
    world.end_latest_turn("failed")
    world.tick(hour)
    assert world.view().failure_count == 3
    assert world.view().stopped_reason == "turn_failed"

    world.tick(hour + timedelta(hours=1))
    assert len(world.turns()) == 6

    world.enable(True)
    world.tick(hour + timedelta(hours=1, seconds=3))
    assert len(world.turns()) == 7
    assert world.view().stopped_reason is None


def test_a_turn_that_cannot_start_counts_as_a_failure(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a refused submit retried forever without a word.
    world.save()

    def refuse(_command: object) -> None:
        raise ActionMessageConflictError("Action checkpoint is not restored")

    monkeypatch.setattr(conversations, "submit_action_message", refuse)
    for second in (0, 3, 6, 9):
        world.tick(NINE + timedelta(seconds=second))

    view = world.view()
    assert (view.last_result, view.failure_count) == ("not_started", 3)
    assert view.stopped_reason == "start_failed"


def test_a_task_past_its_until_does_not_fire(world: _World) -> None:
    # Catches until ignored, so a task meant to end keeps starting turns.
    world.save(replace(HOURLY, until=datetime(2026, 11, 21, 9, 30, tzinfo=UTC)))
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1
    assert world.view().next_fire_at is None


def _conversation(world: _World) -> str:
    """A conversation the user started, its first turn ended."""

    action_id = submit_action_message(
        SubmitActionMessageCommand(
            user_id=USER,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="user-message-1", content="Watch the build for me."
            ),
        )
    ).action_id
    world.end_latest_turn("completed")
    return action_id


def _task(prompt: str = "Check the build.", request: str = "request-1") -> Task:
    return build_task(new_task_id(request), HOURLY, prompt)


def _save(
    world: _World,
    action_id: str,
    task: Task,
    change: str = "create",
    now: datetime = NINE - timedelta(minutes=1),
) -> None:
    assert (
        save_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=action_id,
            change=change,  # type: ignore[arg-type]
            task=task,
            now=now,
        )
        is None
    )


def _conversations(world: _World) -> set[str]:
    with closing(sqlite3.connect(world.db)) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT action_id FROM agent_actions")
        }


def test_an_update_replaces_the_prompt_the_next_turn_sends(world: _World) -> None:
    # Catches an update that leaves the old content approved, so the turn
    # keeps sending the prompt the user replaced.
    action_id = _conversation(world)
    task = _task()
    _save(world, action_id, task)
    replaced = build_task(task.hook_id, HOURLY, "Check the deploy.")
    _save(world, action_id, replaced, change="update")
    world.tick(NINE)

    with closing(sqlite3.connect(world.db)) as connection:
        (sent,) = connection.execute(
            "SELECT user_request_text FROM agent_action_steps WHERE step_name = ?",
            (ACTION_HOOK_STEP_NAME,),
        ).fetchall()
    assert str(sent[0]).startswith("Check the deploy.")


def test_a_conversation_sees_and_stops_only_its_own_tasks(world: _World) -> None:
    # Catches a list or stop that reaches another conversation's schedule.
    mine = _conversation(world)
    theirs = submit_action_message(
        SubmitActionMessageCommand(
            user_id=USER,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="user-message-2", content="Another thing."
            ),
        )
    ).action_id
    world.end_latest_turn("completed")
    own, other = _task(request="request-1"), _task(request="request-2")
    _save(world, mine, own)
    _save(world, theirs, other)

    listed = list_conversation_tasks(
        db_path=world.db, busy_timeout_ms=1_000, user_id=USER, action_id=mine
    )
    assert [view.hook_id for view in listed] == [own.hook_id]
    refusal = stop_task(
        db_path=world.db,
        busy_timeout_ms=1_000,
        user_id=USER,
        action_id=mine,
        hook_id=other.hook_id,
    )
    assert refusal == "not_found"

    assert (
        stop_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=mine,
            hook_id=own.hook_id,
        )
        is None
    )
    world.tick(NINE)
    assert [conversation for conversation, _ in world.turns()] == [theirs]


def test_deleting_the_conversation_deletes_its_tasks(world: _World) -> None:
    # Catches a deletion that leaves the task, its state or its inbox behind,
    # so its next fire reaches a conversation the user deleted.
    action_id = _conversation(world)
    task = _task()
    _save(world, action_id, task)
    with closing(sqlite3.connect(world.db)) as connection, connection:
        connection.execute(
            "INSERT INTO hook_inbox(hook_id, user_id, event_name, payload_json,"
            " occurred_at) VALUES (?, ?, 'activity', '{}', '2026-11-21T09:00:00Z')",
            (task.hook_id, USER),
        )
    delete_history_item(
        db_path=world.db,
        busy_timeout_ms=1_000,
        artifact_root=world.db.parent / "artifacts",
        user_id=USER,
        kind="conversation",
        item_id=action_id,
    )
    world.tick(NINE)
    world.tick(NINE + timedelta(hours=1))

    assert world.turns() == []
    assert _conversations(world) == set()
    with closing(sqlite3.connect(world.db)) as connection:
        for table in ("user_hooks", "hook_inbox"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )


def test_a_task_deleted_during_a_tick_sends_no_turn(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a fire that read the task before its conversation was deleted
    # and then sends its prompt to a new conversation.
    action_id = _conversation(world)
    task = _task()
    _save(world, action_id, task)
    read = runner.active_hooks

    def deleted_after_reading(*args: object, **kwargs: object) -> object:
        active = read(*args, **kwargs)  # type: ignore[arg-type]
        delete_history_item(
            db_path=world.db,
            busy_timeout_ms=1_000,
            artifact_root=world.db.parent / "artifacts",
            user_id=USER,
            kind="conversation",
            item_id=action_id,
        )
        return active

    monkeypatch.setattr(runner, "active_hooks", deleted_after_reading)
    world.tick(NINE)
    assert world.turns() == []
    assert _conversations(world) == set()


def test_a_tick_does_not_move_a_newer_approvals_time(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a tick that fired the old content and then wrote its next time
    # over the time the user's new approval set, firing the new content early.
    world.save()
    fire = runner.fire_task

    def approved_again_while_firing(*args: object, **kwargs: object) -> object:
        outcome = fire(*args, **kwargs)  # type: ignore[arg-type]
        world.save(DAILY_AT_8, now=NINE)
        return outcome

    monkeypatch.setattr(runner, "fire_task", approved_again_while_firing)
    world.tick(NINE)

    assert world.view().next_fire_at == datetime(2026, 11, 22, 8, 0, tzinfo=UTC)


def _new_conversation_each_fire(
    world: _World, organization_id: str | None = None, project_id: str | None = None
) -> str:
    """A task saved to start a new TODO's conversation on each fire; no tool
    writes one yet, so the row is moved off its conversation here."""

    origin = world.save()
    with closing(sqlite3.connect(world.db)) as connection, connection:
        connection.execute(
            "UPDATE user_hooks SET target_action_id = NULL, title = 'Build report',"
            " organization_id = ?, project_id = ?",
            (organization_id, project_id),
        )
    return origin


def _todos(world: _World) -> list[tuple[str, str | None, str | None, str | None]]:
    """Each TODO: its text, its place, and the conversation it shows."""

    with closing(sqlite3.connect(world.db)) as connection:
        todo_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT todo_id FROM todo_items ORDER BY created_at, rowid"
            )
        ]
    todos = []
    for todo_id in todo_ids:
        entry = read_todo(user_id=USER, todo_id=todo_id)
        assert entry is not None
        conversation = entry.conversation
        todos.append(
            (
                entry.todo.text,
                entry.todo.organization_id,
                entry.todo.project_id,
                conversation.action_id
                if isinstance(conversation, ActionConversation)
                else None,
            )
        )
    return todos


def _delete(world: _World, action_id: str) -> None:
    delete_history_item(
        db_path=world.db,
        busy_timeout_ms=1_000,
        artifact_root=world.db.parent / "artifacts",
        user_id=USER,
        kind="conversation",
        item_id=action_id,
    )


def test_a_task_without_a_target_opens_one_todo_and_conversation_per_fire(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a fire that continues the conversation that asked, a second run
    # started while the first is going, a fire repeated after a crash that
    # makes a second TODO or conversation for the same occurrence, and a
    # conversation the TODO does not show as its own.
    origin = _new_conversation_each_fire(world)
    write = runner.SqliteHookStateStore.write_next_fire_time
    writes: list[object] = []

    def crash_after_the_turn(
        self: runner.SqliteHookStateStore, hook_id: str, at: datetime | None
    ) -> None:
        writes.append(at)
        if len(writes) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        write(self, hook_id, at)

    monkeypatch.setattr(
        runner.SqliteHookStateStore, "write_next_fire_time", crash_after_the_turn
    )
    world.tick(NINE)
    world.tick(NINE + timedelta(seconds=3))
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1

    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1, seconds=3))
    first, second = (conversation for conversation, _ in world.turns())
    assert len({origin, first, second}) == 3
    assert _conversations(world) == {origin, first, second}
    assert _todos(world) == [
        ("Build report", None, None, first),
        ("Build report", None, None, second),
    ]


@pytest.mark.parametrize(
    ("organization_id", "project_id"),
    [("org-a", None), (None, "project-1")],
)
def test_a_new_conversations_todo_lands_in_the_tasks_place(
    world: _World, organization_id: str | None, project_id: str | None
) -> None:
    # Catches a TODO left in no place, or put in the wrong one.
    with closing(sqlite3.connect(world.db)) as connection, connection:
        connection.executescript(
            f"""INSERT INTO workspace_organizations(organization_id, user_id,
                  display_name, created_at, updated_at)
                VALUES ('org-a', '{USER}', 'A', 'T', 'T');
                INSERT INTO workspace_projects(project_id, user_id, display_name,
                  created_at, updated_at)
                VALUES ('project-1', '{USER}', 'One', 'T', 'T');"""
        )
    _new_conversation_each_fire(world, organization_id, project_id)
    world.tick(NINE)

    ((started, _),) = world.turns()
    assert _todos(world) == [("Build report", organization_id, project_id, started)]


def test_the_todo_pickup_never_opens_a_fires_todo(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a TODO the pickup starts with its own text, before or instead
    # of the task's message, when the fire's send failed after storing it.
    _new_conversation_each_fire(world)

    def refuse(_command: object) -> None:
        raise ActionMessageConflictError("Action checkpoint is not restored")

    with monkeypatch.context() as patched:
        patched.setattr(conversations, "submit_action_message", refuse)
        world.tick(NINE)
    assert pick_up_todos(user_id=USER) is True
    assert world.turns() == []
    assert [conversation for *_, conversation in _todos(world)] == [None]

    world.tick(NINE + timedelta(seconds=3))
    ((started, _),) = world.turns()
    assert [conversation for *_, conversation in _todos(world)] == [started]


def test_deleting_a_conversation_a_task_started_leaves_the_task(
    world: _World,
) -> None:
    # Catches a task that goes with the conversation it started, or that is
    # left behind when the conversation that asked for it is deleted.
    origin = _new_conversation_each_fire(world)
    world.tick(NINE)
    world.end_latest_turn("completed")
    ((started, _),) = world.turns()
    with closing(sqlite3.connect(world.db)) as connection:
        (todo_id,) = connection.execute("SELECT todo_id FROM todo_items").fetchone()
    update_todo(
        user_id=USER,
        todo_id=str(todo_id),
        step=lambda todo: core.delete_by_user(todo, now="2026-11-21T09:30:00Z"),
    )
    _delete(world, started)
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1
    assert world.view().target_action_id is None

    world.end_latest_turn("completed")
    _delete(world, origin)
    assert list_hooks(db_path=world.db, busy_timeout_ms=1_000, user_id=USER) == ()

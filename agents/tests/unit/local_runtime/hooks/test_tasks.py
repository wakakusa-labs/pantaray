"""Tasks in ~/.pantaray/tasks: approved by content, sent as conversation turns; an
Action's own tasks continue its conversation and go with it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.chat.work_list import read_chat_work_list
from pantaray_agents.local_runtime.hooks import conversations, gates, runner
from pantaray_agents.local_runtime.hooks.agent_tasks import (
    TaskSchedule,
    list_conversation_tasks,
    new_task_id,
    parse_task,
    save_task,
    stop_task,
    task_text,
)
from pantaray_agents.local_runtime.hooks.events import ActionTurnEnded
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.local_runtime.hooks.listing import (
    TaskView,
    approve_task,
    list_hooks,
    set_hook_enabled,
)
from pantaray_agents.local_runtime.hooks.tasks import Task
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
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput

from ..migrated_db import prepare_test_database

USER = "user-1"
HOOK = "task.build-report"
NINE = datetime(2026, 11, 21, 9, 0, 30, tzinfo=UTC)
# Before the 09:00 occurrence, which an approval then waits for.
APPROVED = datetime(2026, 11, 21, 8, 59, tzinfo=UTC)
HOURLY = """+++
trigger = { every = "hour", tz = "utc" }
conversation = "continue"
+++
Check the build and say what changed.
"""


@dataclass
class _World:
    db: Path
    tasks: Path

    def write(self, text: str = HOURLY, name: str = "build-report") -> None:
        self.tasks.mkdir(parents=True, exist_ok=True)
        (self.tasks / f"{name}.md").write_text(text, encoding="utf-8")

    def view(self, hook_id: str = HOOK) -> TaskView:
        (view,) = (
            view
            for view in list_hooks(db_path=self.db, busy_timeout_ms=1_000, user_id=USER)
            if view.hook_id == hook_id
        )
        assert isinstance(view, TaskView)
        return view

    def approve(self, hook_id: str = HOOK, now: datetime = APPROVED) -> None:
        refusal = approve_task(
            db_path=self.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            hook_id=hook_id,
            content_hash=self.view(hook_id).content_hash,
            now=now,
        )
        assert refusal is None

    def enable(self, enabled: bool, hook_id: str = HOOK) -> None:
        assert (
            set_hook_enabled(
                db_path=self.db,
                busy_timeout_ms=1_000,
                user_id=USER,
                hook_id=hook_id,
                enabled=enabled,
            )
            is None
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
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCAL_DB_PATH", str(db))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(runner, "builtin_hooks", lambda: ())
    monkeypatch.setattr(gates, "owner_is_settled", lambda: True)
    monkeypatch.setattr(gates, "background_requests_can_send", lambda: True)
    register_logged_out_owner(USER)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) "
            "VALUES (?, 'en', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
            (USER,),
        )
    yield _World(db=db, tasks=home / ".pantaray" / "tasks")
    reset_logged_out_owner()


def test_an_approved_task_starts_one_conversation_and_continues_it(
    world: _World,
) -> None:
    # Catches a fire per tick inside one occurrence, a continue task that
    # opens a new conversation each time, and its conversation left untitled
    # in the work list because Pantaray, not the user, wrote its first turn.
    world.write()
    world.approve()
    world.tick(NINE)
    world.tick(NINE + timedelta(seconds=3))
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1))

    turns = world.turns()
    assert len(turns) == 2
    assert turns[0][0] == turns[1][0] == world.view().action_id
    assert world.view().last_result is None  # The second turn is still queued.
    (listed,) = read_chat_work_list(user_id=USER).tasks
    assert listed.title == "Check the build and say what changed."


def test_the_approval_review_reads_a_task_turn_as_not_the_users_words(
    world: _World,
) -> None:
    # Catches a task's prompt stored as the user's message, which would let the
    # review treat a scheduled prompt as the user asking for this turn now.
    world.write()
    world.approve()
    world.tick(NINE)
    ((action_id, _),) = world.turns()

    with closing(sqlite3.connect(world.db)) as connection:
        connection.row_factory = sqlite3.Row
        rows = action_request_rows(connection, user_id=USER, action_id=action_id)
    user_messages, not_by_user = action_request_words(
        rows, lambda _row: (), lambda _ids: ()
    )
    assert user_messages == ()
    assert not_by_user is not None and not_by_user.source == "hook"
    assert not_by_user.text.startswith("Check the build and say what changed.")


def test_a_busy_conversation_holds_the_task_then_it_fires_once(
    world: _World,
) -> None:
    # Catches a turn queued behind a running one, one sent while the user is
    # asked to approve, occurrences spent while waiting, and a catch-up that
    # sends every missed occurrence.
    world.write()
    world.approve()
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


def test_a_new_task_opens_a_conversation_each_time_after_the_last_ends(
    world: _World,
) -> None:
    # Catches a new task that piles conversations onto one still running, and
    # one that continues the previous conversation instead.
    world.write(HOURLY.replace('"continue"', '"new"'))
    world.approve()
    world.tick(NINE)
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1

    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1, seconds=3))
    first, second = (conversation for conversation, _ in world.turns())
    assert first != second == world.view().action_id


@pytest.mark.parametrize("crash", ["recording the turn", "writing the next fire"])
def test_a_fire_repeated_after_a_crash_finds_its_turn(
    world: _World, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    # Catches a crash after the turn was sent (before it was recorded, or
    # before the next fire time was) that sends the occurrence again after a
    # restart, or fails it against the conversation it is now bound to.
    world.write()
    world.approve()
    if crash == "recording the turn":
        bind = conversations._bind

        def crash_once(*args: object, **kwargs: object) -> None:
            monkeypatch.setattr(conversations, "_bind", bind)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(conversations, "_bind", crash_once)
    else:
        write = runner.SqliteHookStateStore.write_next_fire_time
        writes: list[object] = []

        def crash_after_the_turn(
            self: runner.SqliteHookStateStore, hook_id: str, at: datetime | None
        ) -> None:
            writes.append(at)
            if len(writes) == 2:  # The first pins the occurrence before it fires.
                raise sqlite3.OperationalError("disk I/O error")
            write(self, hook_id, at)

        monkeypatch.setattr(
            runner.SqliteHookStateStore, "write_next_fire_time", crash_after_the_turn
        )
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(seconds=3))

    assert len(world.turns()) == 1
    assert world.view().action_id == world.turns()[0][0]
    assert world.view().failure_count == 0


def test_a_crash_before_binding_leaves_one_conversation(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a crash between sending a turn and binding its conversation
    # that makes the next occurrence open a second conversation, or send while
    # the first turn still runs.
    world.write()
    world.approve()
    bind = conversations._bind

    def crash_once(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(conversations, "_bind", bind)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(conversations, "_bind", crash_once)
    world.tick(NINE)
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1

    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1, seconds=3))
    (first, _), (second, _) = world.turns()
    assert first == second == world.view().action_id


def test_a_turn_that_ends_during_a_tick_is_counted_before_the_next(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a turn that ends after the tick counted results but before the
    # next turn is sent: its failure would never count, so the third failure
    # in a row would not stop the task.
    world.write()
    world.approve()
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
    # Catches a spent at task edited to a later time that never fires again,
    # and an unchanged approval that fires a spent at once more.
    at = """+++
trigger = { at = 2026-11-21T09:00:00Z }
conversation = "new"
+++
Send the reminder.
"""
    world.write(at)
    world.approve()
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.approve()
    world.tick(NINE + timedelta(hours=1))
    assert len(world.turns()) == 1

    world.write(at.replace("09:00:00Z", "11:00:00Z"))
    world.approve()
    world.tick(NINE + timedelta(hours=1, seconds=3))
    assert len(world.turns()) == 1
    world.tick(NINE + timedelta(hours=2))
    assert len(world.turns()) == 2


def test_an_approval_waits_for_the_next_occurrence(world: _World) -> None:
    # Catches a just-approved every task that fires the occurrence already
    # past: "every day at 08:00" approved at 14:00 must not run at 14:00.
    world.write(HOURLY.replace('"hour"', '"day"').replace("tz", 'offset = "8h", tz'))
    world.approve(now=datetime(2026, 11, 21, 14, 0, tzinfo=UTC))
    world.tick(datetime(2026, 11, 21, 14, 0, 3, tzinfo=UTC))
    assert world.turns() == []
    assert world.view().next_fire_at == datetime(2026, 11, 22, 8, 0, tzinfo=UTC)

    world.tick(datetime(2026, 11, 22, 8, 0, 3, tzinfo=UTC))
    assert len(world.turns()) == 1


def test_only_the_approved_content_fires(world: _World) -> None:
    # Catches a task that fires before approval, one that keeps firing after
    # its file was edited, and approval kept by name rather than by content.
    world.write()
    world.tick(NINE)
    assert world.turns() == []

    world.approve()
    world.write(HOURLY.replace("say what changed", "push a fix"))
    world.tick(NINE + timedelta(seconds=3))
    assert world.turns() == []
    assert not world.view().approved

    world.approve()
    world.tick(NINE + timedelta(seconds=6))
    assert len(world.turns()) == 1


def test_three_failures_in_a_row_stop_the_task_until_it_is_enabled(
    world: _World,
) -> None:
    # Catches a task that keeps failing unseen, a success that does not reset
    # the count, and a stop that enabling cannot undo.
    world.write()
    world.approve()
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
    world.write()
    world.approve()

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
    world.write(HOURLY.replace("+++\n", "+++\nuntil = 2026-11-21T09:00:00Z\n", 1))
    world.approve()
    world.tick(NINE)
    assert world.turns() == []


def test_an_on_task_hears_only_its_own_conversations_turns(world: _World) -> None:
    # Catches a task woken by every conversation's turn end, which would send
    # its prompt into work it has nothing to do with.
    # Bound by an earlier fire: the hourly version of it, edited since.
    world.write()
    world.approve()
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.write(
        HOURLY.replace(
            '{ every = "hour", tz = "utc" }', '{ on = ["action.turn_ended"] }'
        )
    )
    world.approve()
    action_id = world.view().action_id
    assert action_id is not None

    def turn_ended(of: str) -> None:
        with closing(sqlite3.connect(world.db)) as connection, connection:
            record_event(
                connection,
                user_id=USER,
                event=ActionTurnEnded(
                    source_id=f"{of}:2",
                    action_id=of,
                    action_completed_at="2026-11-21T09:01:00Z",
                    source_action_revision_id=None,
                    turn_start_step_number=1,
                    turn_end_step_number=2,
                    action_prompt_name="action",
                    action_prompt_version="v1",
                    suggestion_id=None,
                ),
                occurred_at="2026-11-21T09:01:00Z",
            )

    turn_ended("another-conversation")
    world.tick(NINE + timedelta(minutes=2))
    assert len(world.turns()) == 1

    turn_ended(action_id)
    world.tick(NINE + timedelta(minutes=2, seconds=3))
    world.tick(NINE + timedelta(minutes=2, seconds=6))
    assert [conversation for conversation, _ in world.turns()] == [action_id] * 2


HOURLY_SCHEDULE = TaskSchedule(at=None, every="hour", offset=None, until=None)


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
    task = parse_task(new_task_id(request), task_text(HOURLY_SCHEDULE, prompt))
    assert isinstance(task, Task)
    return task


def _save(world: _World, action_id: str, task: Task, change: str = "create") -> None:
    assert (
        save_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=action_id,
            change=change,  # type: ignore[arg-type]
            task=task,
            now=NINE - timedelta(minutes=1),
        )
        is None
    )


def _conversations(world: _World) -> set[str]:
    with closing(sqlite3.connect(world.db)) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT action_id FROM agent_actions")
        }


def test_a_written_task_continues_the_conversation_that_wrote_it(
    world: _World,
) -> None:
    # Catches an agent's task that opens a new conversation on its first fire
    # instead of continuing the one the user said yes in, and one written
    # without approval that never fires.
    action_id = _conversation(world)
    _save(world, action_id, _task())
    world.tick(NINE)
    world.end_latest_turn("completed")
    world.tick(NINE + timedelta(hours=1))

    assert [conversation for conversation, _ in world.turns()] == [action_id] * 2
    assert _conversations(world) == {action_id}
    (view,) = list_conversation_tasks(
        db_path=world.db, busy_timeout_ms=1_000, user_id=USER, action_id=action_id
    )
    assert (view.origin, view.created_from_action_id) == ("agent", action_id)


def test_an_update_replaces_the_prompt_the_next_turn_sends(world: _World) -> None:
    # Catches an update that leaves the old content approved, so the turn
    # keeps sending the prompt the user replaced.
    action_id = _conversation(world)
    task = _task()
    _save(world, action_id, task)
    replaced = parse_task(task.hook_id, task_text(HOURLY_SCHEDULE, "Check the deploy."))
    assert isinstance(replaced, Task)
    _save(world, action_id, replaced, change="update")
    world.tick(NINE)

    with closing(sqlite3.connect(world.db)) as connection:
        (sent,) = connection.execute(
            "SELECT user_request_text FROM agent_action_steps WHERE step_name = ?",
            (ACTION_HOOK_STEP_NAME,),
        ).fetchall()
    assert str(sent[0]).startswith("Check the deploy.")


def test_a_conversation_sees_and_stops_only_its_own_tasks(world: _World) -> None:
    # Catches a list or stop that reaches another conversation's schedule or
    # the user's own task file.
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
    world.write()  # The user's own task.

    listed = list_conversation_tasks(
        db_path=world.db, busy_timeout_ms=1_000, user_id=USER, action_id=mine
    )
    assert [view.hook_id for view in listed] == [own.hook_id]
    for hook_id in (other.hook_id, "task.build-report"):
        refusal = stop_task(
            db_path=world.db,
            busy_timeout_ms=1_000,
            user_id=USER,
            action_id=mine,
            hook_id=hook_id,
        )
        assert refusal == "not_found"
    assert (world.tasks / "build-report.md").is_file()

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
    assert not (world.tasks / f"{own.hook_id.removeprefix('task.')}.md").exists()
    world.tick(NINE)
    assert [conversation for conversation, _ in world.turns()] == [theirs]


def test_deleting_the_conversation_deletes_its_tasks(world: _World) -> None:
    # Catches a deletion that only unbinds the task, so its next fire opens a
    # new conversation the user deleted the work of.
    action_id = _conversation(world)
    task = _task()
    _save(world, action_id, task)
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
    assert not (world.tasks / f"{task.hook_id.removeprefix('task.')}.md").exists()
    with closing(sqlite3.connect(world.db)) as connection:
        for table in ("hook_settings", "hook_conversations", "hook_task_origins"):
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


def test_the_listing_names_who_wrote_each_task(world: _World) -> None:
    # Catches an agent's task listed as the user's, which the list could not
    # tell apart or trace to its conversation.
    action_id = _conversation(world)
    _save(world, action_id, _task())
    world.write()
    origins = {
        view.hook_id: (view.origin, view.created_from_action_id)
        for view in (world.view(new_task_id("request-1")), world.view())
    }
    assert origins == {
        new_task_id("request-1"): ("agent", action_id),
        "task.build-report": ("user", None),
    }

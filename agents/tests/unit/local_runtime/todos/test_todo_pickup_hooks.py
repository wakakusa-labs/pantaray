"""When builtin.todo_pickup and builtin.todo_pickup_daily start a TODO's Action."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents import todo_ux_feature
from pantaray_agents.local_runtime.hooks import gates, runner
from pantaray_agents.local_runtime.hooks.builtin import builtin_hooks
from pantaray_agents.local_runtime.runtime.identity import register_logged_out_owner
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.todos import pickup
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.model import Todo
from pantaray_agents.local_runtime.todos.writes import create_todo_item

from .store_support import TOKYO, USER

# Wednesday 2026-11-04, noon in Tokyo.
NOON = datetime(2026, 11, 4, 12, tzinfo=TOKYO)
THURSDAY = datetime(2026, 11, 5, tzinfo=TOKYO)


@dataclass
class _World:
    now: datetime = NOON
    owner_settled: bool = True
    can_send: bool = True


@pytest.fixture
def world(db: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    world = _World()
    hooks = tuple(
        hook for hook in builtin_hooks() if hook.id.startswith("builtin.todo_pickup")
    )
    monkeypatch.setattr(runner, "builtin_hooks", lambda: hooks)
    monkeypatch.setattr(runner, "_local_zone", lambda: TOKYO)
    monkeypatch.setattr(gates, "owner_is_settled", lambda: world.owner_settled)
    monkeypatch.setattr(gates, "background_requests_can_send", lambda: world.can_send)
    monkeypatch.setattr(pickup, "read_todo_clock", lambda: _clock(world.now))
    return world


def _clock(now: datetime) -> TodoClock:
    return TodoClock(now=format_utc_iso(now), zone=TOKYO)


def _tick(world: _World, db: Path, user_id: str = USER) -> None:
    runner.run_builtin_hooks_once(
        db_path=db, busy_timeout_ms=1_000, user_id=user_id, now=world.now
    )


def _write(world: _World, text: str, starts_on: str, user_id: str = USER) -> Todo:
    created = create_todo_item(
        user_id=user_id,
        client_todo_id=text,
        project_id=None,
        organization_id=None,
        text=text,
        starts_on=starts_on,
        due=None,
        clock=_clock(world.now),
    )
    assert isinstance(created, Todo)
    return created


def _action_count(db: Path) -> int:
    with closing(sqlite3.connect(db)) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0]
        )


def test_a_todo_saved_to_start_today_starts_without_waiting_for_the_next_day(
    db: Path, world: _World
) -> None:
    # Catches a save that records no event, or a pickup that runs only at the
    # day boundary, so a TODO for today waits until tomorrow.
    _tick(world, db)
    _write(world, "Send the quote", "2026-11-04")

    world.now += timedelta(seconds=3)
    _tick(world, db)
    assert _action_count(db) == 1


def test_a_todo_starts_once_when_the_local_date_reaches_its_start(
    db: Path, world: _World
) -> None:
    # Catches the day boundary taken in UTC (nine hours late in Tokyo), a
    # missing daily pass, and a TODO started again by a later pass.
    _tick(world, db)
    _write(world, "Book the room", "2026-11-05")
    _tick(world, db)
    world.now = THURSDAY - timedelta(seconds=1)
    _tick(world, db)
    assert _action_count(db) == 0

    for now in (THURSDAY, THURSDAY + timedelta(seconds=3)):
        world.now = now
        _tick(world, db)
    assert _action_count(db) == 1
    _write(world, "Unrelated, for later", "2026-12-01")
    world.now += timedelta(seconds=3)
    _tick(world, db)
    assert _action_count(db) == 1


def test_the_day_another_owner_spent_still_starts_this_owners_todos(
    db: Path, world: _World
) -> None:
    # Catches the daily occurrence shared across owners: after an account
    # switch, the new owner's TODO for today waited for the next day because
    # the previous owner's pickup had already spent today.
    other = "user-2"
    register_logged_out_owner(other)
    _write(world, "Starts tomorrow", "2026-11-05", user_id=other)
    _tick(world, db, other)

    register_logged_out_owner(USER)
    world.now = THURSDAY
    _tick(world, db)

    register_logged_out_owner(other)
    world.now += timedelta(seconds=3)
    _tick(world, db, other)
    assert _action_count(db) == 1
    register_logged_out_owner(USER)


@pytest.mark.parametrize("starts_on", ["2026-11-04", "2026-11-05"])
def test_a_failed_submit_is_tried_again_on_a_later_tick(
    db: Path,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    starts_on: str,
) -> None:
    # Catches the event or the daily occurrence spent although the submit
    # failed, so the TODO waits for an unrelated save or the next day. Both
    # live in the database, so a restart in between does not lose them.
    _tick(world, db)
    _write(world, "Call the bank", starts_on)
    if starts_on == "2026-11-05":
        _tick(world, db)
        world.now = THURSDAY
    real_submit = pickup.submit_action_message
    failures = iter([True])

    def submit_failing_once(command):  # type: ignore[no-untyped-def]
        if next(failures, False):
            raise sqlite3.OperationalError("database is locked")
        return real_submit(command)

    monkeypatch.setattr(pickup, "submit_action_message", submit_failing_once)
    _tick(world, db)
    assert _action_count(db) == 0

    world.now += timedelta(seconds=3)
    _tick(world, db)
    assert _action_count(db) == 1


@pytest.mark.parametrize(
    "close",
    [
        lambda world, _patch: setattr(world, "owner_settled", False),
        lambda world, _patch: setattr(world, "can_send", False),
        lambda _world, patch: patch.setattr(todo_ux_feature, "TODO_UX_ENABLED", False),
    ],
    ids=["owner_settled", "background_can_send", "todo_ux_enabled"],
)
def test_nothing_starts_while_a_gate_is_closed_and_it_starts_once_open(
    db: Path,
    world: _World,
    close: Callable[[_World, pytest.MonkeyPatch], None],
) -> None:
    # Catches an Action started for an owner about to be replaced, one that
    # would fail for good without a connection, or one with the TODO list off;
    # and a save or a day boundary spent while the gate was closed.
    _write(world, "Saved while closed", "2026-11-04")
    _write(world, "Starts while closed", "2026-11-05")
    with pytest.MonkeyPatch.context() as patch:
        close(world, patch)
        for now in (NOON, THURSDAY):
            world.now = now
            _tick(world, db)
        assert _action_count(db) == 0
        world.owner_settled = world.can_send = True

    world.now += timedelta(seconds=3)
    _tick(world, db)
    assert _action_count(db) == 2

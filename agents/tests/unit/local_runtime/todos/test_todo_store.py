from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import (
    TodoClock,
)
from pantaray_agents.local_runtime.todos.core import CheckSet, Rejected
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    AnyProject,
    NoOrganizationScope,
    NoProject,
    OrganizationScope,
    TodoProjectFilter,
    TodoScope,
)
from pantaray_agents.local_runtime.todos.model import Todo, TodoDue
from pantaray_agents.local_runtime.todos.reads import (
    TodoListSort,
    read_deleted_todos,
    read_todo_list,
    read_todo_revision,
    read_todo_week,
)
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    set_todo_check,
    update_todo,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_deletion import (
    delete_workspace_project,
)

from .store_support import NOW, USER, count, execute

PREVIOUS_WEEK_CLOCK = replace(NOW, now="2026-10-28T03:00:00.000Z")
# Monday 2026-10-26 at 00:30 in Tokyo, still Sunday in UTC.
PREVIOUS_MONDAY_CLOCK = replace(NOW, now="2026-10-25T15:30:00.000Z")


def _create(
    client_todo_id: str,
    *,
    text: str = "Send the quote",
    project_id: str | None = "project-1",
    starts_on: str = "2026-11-04",
    due: TodoDue | None = None,
    clock: TodoClock = NOW,
) -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=client_todo_id,
        project_id=project_id,
        text=text,
        starts_on=starts_on,
        due=due,
        clock=clock,
    )
    assert isinstance(created, Todo)
    return created


def _week_ids(
    *,
    scope: TodoScope = AllScope(),
    project: TodoProjectFilter = AnyProject(),
    week_of: str | None = None,
    clock: TodoClock = NOW,
) -> list[tuple[str, bool]]:
    page = read_todo_week(
        user_id=USER, scope=scope, project=project, week_of=week_of, clock=clock
    )
    return [(item.todo.text, item.carried) for item in page.items]


def test_a_replayed_create_returns_the_same_todo_and_other_content_conflicts(
    db: Path,
) -> None:
    first = _create("client-1")
    revision = read_todo_revision(user_id=USER)

    replayed = _create("client-1", text="  Send the quote ")
    conflicting = create_todo_item(
        user_id=USER,
        client_todo_id="client-1",
        project_id="project-1",
        text="Send the invoice",
        starts_on="2026-11-04",
        due=None,
        clock=NOW,
    )
    elsewhere = create_todo_item(
        user_id=USER,
        client_todo_id="client-2",
        project_id="someone-elses-project",
        text="Send the quote",
        starts_on="2026-11-04",
        due=None,
        clock=NOW,
    )

    assert replayed == first
    assert conflicting == Rejected("client_todo_conflict")
    assert elsewhere == Rejected("project_not_found")
    assert count(db, "SELECT COUNT(*) FROM todo_items") == 1
    assert first.author == "user" and first.starts_on == "2026-11-04"
    # Nothing changed, so the live session has nothing to reload.
    assert read_todo_revision(user_id=USER) == revision == 1


def test_checking_a_weekly_todo_again_never_adds_a_second_next_instance(
    db: Path,
) -> None:
    weekly = _create("client-1", due=TodoDue(date="2026-11-08", repeat="weekly"))

    for checked in (True, True, False, True):
        result = set_todo_check(
            user_id=USER, todo_id=weekly.todo_id, by="user", checked=checked, clock=NOW
        )
        assert isinstance(result, CheckSet)

    assert count(db, "SELECT COUNT(*) FROM todo_items") == 2
    assert _week_ids(week_of="2026-11-09") == [("Send the quote", False)]
    assert _week_ids() == [("Send the quote", False)]
    ai_uncheck = set_todo_check(
        user_id=USER, todo_id=weekly.todo_id, by="ai", checked=False, clock=NOW
    )
    assert ai_uncheck == Rejected("ai_cannot_uncheck")


def test_deleting_a_project_moves_its_todos_to_no_project(db: Path) -> None:
    _create("client-1")
    revision = read_todo_revision(user_id=USER)

    delete_workspace_project(
        db_path=db, busy_timeout_ms=1_000, user_id=USER, project_id="project-1"
    )

    assert _week_ids(project=NoProject()) == [("Send the quote", False)]
    assert _week_ids(scope=NoOrganizationScope()) == [("Send the quote", False)]
    # The move happened outside the store, and the live session still hears it.
    assert read_todo_revision(user_id=USER) == revision + 1


def test_an_organization_shows_the_todos_of_the_projects_it_has_now(
    db: Path,
) -> None:
    _create("client-1")
    _create("client-2", text="Book a room", project_id=None)
    in_a = OrganizationScope(organization_id="org-a")
    in_b = OrganizationScope(organization_id="org-b")
    assert _week_ids(scope=in_a) == [("Send the quote", False)]
    assert _week_ids(scope=NoOrganizationScope()) == [("Book a room", False)]

    execute(
        db,
        "UPDATE workspace_project_organizations SET organization_id = 'org-b' "
        "WHERE project_id = 'project-1'",
    )

    assert _week_ids(scope=in_a) == []
    assert _week_ids(scope=in_b) == [("Send the quote", False)]


def test_an_open_todo_from_an_earlier_week_is_carried_into_the_current_week(
    db: Path,
) -> None:
    _create("client-1", text="Open", starts_on="2026-10-28", clock=PREVIOUS_WEEK_CLOCK)
    done = _create(
        "client-2", text="Done", starts_on="2026-10-26", clock=PREVIOUS_MONDAY_CLOCK
    )
    set_todo_check(
        user_id=USER,
        todo_id=done.todo_id,
        by="user",
        checked=True,
        clock=PREVIOUS_MONDAY_CLOCK,
    )
    _create("client-3", text="Next week", starts_on="2026-11-10")

    assert _week_ids() == [("Open", True)]
    assert _week_ids(week_of="2026-10-26") == [("Done", False)]
    assert _week_ids(week_of="2026-11-09") == [("Next week", False)]


def test_a_todo_checked_before_its_week_began_stays_in_its_week(db: Path) -> None:
    early = _create("client-1", text="Book the room", starts_on="2026-11-10")
    set_todo_check(
        user_id=USER, todo_id=early.todo_id, by="user", checked=True, clock=NOW
    )

    assert _week_ids() == []
    assert _week_ids(week_of="2026-11-09") == [("Book the room", False)]


def _at(second: int) -> TodoClock:
    return replace(NOW, now=f"2026-11-04T03:00:{second:02d}.000Z")


def _list_texts(sort: TodoListSort) -> list[str]:
    page = read_todo_list(
        user_id=USER, scope=AllScope(), project=AnyProject(), sort=sort, clock=NOW
    )
    return [entry.todo.text for entry in page.items]


def test_the_list_shows_open_todos_in_the_asked_order_then_the_last_checked_first(
    db: Path,
) -> None:
    _create("c-1", text="No due", clock=_at(1))
    _create("c-2", text="Due late", due=TodoDue("2026-11-20", "none"), clock=_at(2))
    _create("c-3", text="Due soon", due=TodoDue("2026-11-06", "none"), clock=_at(3))
    _create("c-4", text="Starts later", starts_on="2026-11-30", clock=_at(4))
    first_checked = _create("c-5", text="Checked first", clock=_at(5))
    last_checked = _create("c-0", text="Checked last", clock=_at(0))
    deleted = _create("c-6", text="Deleted", clock=_at(6))
    for todo, at in ((first_checked, 7), (last_checked, 8)):
        set_todo_check(
            user_id=USER, todo_id=todo.todo_id, by="user", checked=True, clock=_at(at)
        )
    update_todo(
        user_id=USER,
        todo_id=deleted.todo_id,
        step=lambda todo: core.delete_by_user(todo, now=NOW.now),
    )

    checked = ["Checked last", "Checked first"]
    assert _list_texts("added") == [
        "No due",
        "Due late",
        "Due soon",
        "Starts later",
        *checked,
    ]
    assert _list_texts("due") == [
        "Due soon",
        "Due late",
        "No due",
        "Starts later",
        *checked,
    ]


def test_a_deleted_todo_is_listed_and_restorable_for_30_days_only(db: Path) -> None:
    kept = _create("client-1", text="Restored")
    expired = _create("client-2", text="Expired")
    for item in (kept, expired):
        update_todo(
            user_id=USER,
            todo_id=item.todo_id,
            step=lambda todo: core.delete_by_user(todo, now=NOW.now),
        )
    before = replace(NOW, now="2026-12-04T02:59:59.999Z")
    at_limit = replace(NOW, now="2026-12-04T03:00:00.000Z")

    listed = read_deleted_todos(user_id=USER, scope=AllScope(), clock=before)
    restored = update_todo(
        user_id=USER,
        todo_id=kept.todo_id,
        step=lambda todo: core.restore(todo, now=before.now),
    )

    assert {todo.text for todo in listed} == {"Restored", "Expired"}
    assert isinstance(restored, Todo) and restored.deletion is None
    assert read_deleted_todos(user_id=USER, scope=AllScope(), clock=at_limit) == ()
    assert update_todo(
        user_id=USER,
        todo_id=expired.todo_id,
        step=lambda todo: core.restore(todo, now=at_limit.now),
    ) == Rejected("expired")
    # Restored into its own week, which is now in the past, so it is carried.
    assert _week_ids(clock=at_limit) == [("Restored", True)]

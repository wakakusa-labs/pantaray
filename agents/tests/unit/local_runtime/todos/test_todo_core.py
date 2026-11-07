from __future__ import annotations

from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.local_runtime.todos.core import (
    CheckSet,
    Rejected,
    answer_deletion,
    create_todo,
    delete_by_user,
    purge_due,
    report_progress,
    request_deletion,
    restore,
    set_check,
)
from pantaray_agents.local_runtime.todos.model import (
    AiReport,
    DeletionRequest,
    RecurrenceOrigin,
    Todo,
    TodoAuthor,
    TodoCheck,
    TodoDeletion,
    TodoDue,
    WrittenOrigin,
)

from .support import NEXT_WEEK, PREVIOUS_WEEK, WEEK, WEEKLY_DUE, make_todo

TOKYO = ZoneInfo("Asia/Tokyo")
NOW = "2026-11-04T09:00:00.000Z"
LATER = "2026-11-05T09:00:00.000Z"
DELETED = TodoDeletion(by="user", at="2026-11-04T00:00:00.000Z")
PENDING = DeletionRequest(
    request_id="req-1", reason="Duplicate", requested_at=NOW, state="pending"
)
DECLINED = replace(PENDING, state="declined")


def _create(text: str) -> object:
    return create_todo(
        todo_id="todo-1",
        text=text,
        author="user",
        origin=WrittenOrigin(),
        project_id=None,
        week_of="2026-11-05",
        due=None,
        now=NOW,
    )


@pytest.mark.parametrize("text", ["", "   ", "first\nsecond", "a\rb", "a\u2028b"])
def test_create_rejects_text_that_is_not_one_line(text: str) -> None:
    assert _create(text) == Rejected("invalid_text")


def test_create_files_the_todo_under_the_monday_of_its_week() -> None:
    created = _create("  Send the quote \n")

    assert isinstance(created, Todo)
    assert (created.text, created.week_start) == ("Send the quote", WEEK)


def _set(todo: Todo, *, by: TodoAuthor, checked: bool, now: str = NOW) -> CheckSet:
    result = set_check(todo, by=by, checked=checked, now=now, zone=TOKYO)
    assert isinstance(result, CheckSet)
    return result


@pytest.mark.parametrize("checked_by", ["ai", "user"])
def test_only_the_user_can_remove_a_check(checked_by: TodoAuthor) -> None:
    todo = make_todo(check=TodoCheck(by=checked_by, at=NOW))

    rejected = set_check(todo, by="ai", checked=False, now=LATER, zone=TOKYO)

    assert rejected == Rejected("ai_cannot_uncheck")
    assert _set(todo, by="user", checked=False).todo.check is None


def test_checking_again_keeps_who_checked_first() -> None:
    todo = _set(make_todo(), by="user", checked=True).todo

    again = _set(todo, by="ai", checked=True, now=LATER)

    assert again.todo.check == TodoCheck(by="user", at=NOW)


@pytest.mark.parametrize(
    ("by", "checked"), [("user", True), ("user", False), ("ai", True)]
)
def test_deleted_todo_cannot_be_checked_or_unchecked(
    by: TodoAuthor, checked: bool
) -> None:
    todo = make_todo(deletion=DELETED, check=TodoCheck(by="ai", at=NOW))

    result = set_check(todo, by=by, checked=checked, now=LATER, zone=TOKYO)

    assert result == Rejected("deleted")


@pytest.mark.parametrize(
    ("week_start", "checked_at", "expected"),
    [
        (WEEK, NOW, (NEXT_WEEK, "2026-11-15")),
        (WEEK, "2026-11-08T05:00:00.000Z", (NEXT_WEEK, "2026-11-15")),
        # Sunday 11-29 in Tokyo, still 11-28 in UTC: 11-29 itself is not later.
        (WEEK, "2026-11-28T15:30:00.000Z", ("2026-11-30", "2026-12-06")),
        (PREVIOUS_WEEK, "2026-11-20T03:00:00.000Z", (NEXT_WEEK, "2026-11-22")),
    ],
    ids=["before-due", "on-due-date", "three-weeks-late", "keeps-week-offset"],
)
def test_next_instance_is_the_first_occurrence_due_after_the_check(
    week_start: str, checked_at: str, expected: tuple[str, str]
) -> None:
    todo = make_todo(author="ai", week_start=week_start, due=WEEKLY_DUE)

    next_todo = _set(todo, by="user", checked=True, now=checked_at).next_todo

    assert next_todo is not None and next_todo.todo_id != "todo-1"
    assert next_todo == make_todo(
        todo_id=next_todo.todo_id,
        author="ai",
        origin=RecurrenceOrigin(previous_todo_id="todo-1"),
        week_start=expected[0],
        due=TodoDue(date=expected[1], repeat="weekly"),
        created_at=checked_at,
    )


def test_only_weekly_todos_recur_and_rechecking_yields_the_same_next_id() -> None:
    todo = make_todo(due=WEEKLY_DUE)
    first = _set(todo, by="user", checked=True)
    unchecked = _set(first.todo, by="user", checked=False)
    second = _set(unchecked.todo, by="user", checked=True, now=LATER)
    other = _set(replace(todo, todo_id="todo-2"), by="user", checked=True)

    assert unchecked.next_todo is None
    assert first.next_todo and second.next_todo and other.next_todo
    assert second.next_todo.todo_id == first.next_todo.todo_id
    assert other.next_todo.todo_id != first.next_todo.todo_id
    one_off = make_todo(due=TodoDue(date="2026-11-08", repeat="none"))
    assert _set(one_off, by="user", checked=True).next_todo is None


def test_deleted_todo_refuses_a_report_and_a_second_user_deletion() -> None:
    todo = make_todo(deletion=DELETED)
    report = AiReport(kind="partial", note="Drafted", run_id="run-1", at=NOW)

    assert report_progress(todo, report) == Rejected("deleted")
    assert delete_by_user(todo, now=NOW) == Rejected("already_deleted")


def test_only_an_approved_request_deletes_and_keep_declines_it() -> None:
    requested = request_deletion(make_todo(), request_id="req-1", reason="x", now=NOW)
    assert isinstance(requested, Todo) and requested.deletion is None

    deleted = answer_deletion(
        requested, request_id="req-1", answer="approve", now=LATER
    )

    kept = answer_deletion(requested, request_id="req-1", answer="keep", now=LATER)

    assert isinstance(deleted, Todo) and isinstance(kept, Todo)
    assert deleted.deletion == TodoDeletion(by="ai_approved_by_user", at=LATER)
    assert (kept.deletion, kept.deletion_request) == (
        None,
        replace(DECLINED, reason="x"),
    )


def test_a_request_pending_when_the_user_deleted_cannot_delete_after_restore() -> None:
    deleted = delete_by_user(make_todo(deletion_request=PENDING), now=NOW)
    assert isinstance(deleted, Todo)
    restored = restore(deleted, now=LATER)
    assert isinstance(restored, Todo)

    assert answer_deletion(
        restored, request_id="req-1", answer="approve", now=LATER
    ) == Rejected("stale_request")
    declined = delete_by_user(make_todo(deletion_request=DECLINED), now=NOW)
    assert isinstance(declined, Todo) and declined.deletion_request == DECLINED


@pytest.mark.parametrize(
    ("todo", "expected"),
    [
        pytest.param(make_todo(deletion_request=PENDING), "already_pending"),
        pytest.param(make_todo(deletion_request=DECLINED), "declined_before"),
        pytest.param(make_todo(deletion=DELETED), "deleted"),
    ],
)
def test_request_deletion_refusals(todo: Todo, expected: str) -> None:
    rejected = request_deletion(todo, request_id="req-2", reason="Again", now=LATER)

    assert rejected == Rejected(expected)


@pytest.mark.parametrize(
    ("todo", "request_id"),
    [
        pytest.param(make_todo(deletion_request=PENDING), "req-0", id="other-request"),
        pytest.param(make_todo(), "req-1", id="no-request"),
        pytest.param(make_todo(deletion_request=DECLINED), "req-1", id="already-kept"),
        pytest.param(
            make_todo(deletion_request=PENDING, deletion=DELETED),
            "req-1",
            id="user-deleted-meanwhile",
        ),
    ],
)
def test_answer_to_a_request_that_is_not_pending_is_stale(
    todo: Todo, request_id: str
) -> None:
    rejected = answer_deletion(todo, request_id=request_id, answer="approve", now=LATER)

    assert rejected == Rejected("stale_request")


def test_restore_and_purge_switch_exactly_thirty_days_after_deletion() -> None:
    todo = make_todo(deletion=DELETED)
    before, at = "2026-12-03T23:59:59.999Z", "2026-12-04T00:00:00.000Z"

    restored = restore(todo, now=before)

    assert isinstance(restored, Todo) and restored.deletion is None
    assert restore(todo, now=at) == Rejected("expired")
    assert (purge_due(todo, now=before), purge_due(todo, now=at)) == (False, True)
    assert restore(make_todo(), now=NOW) == Rejected("not_deleted")
    assert not purge_due(make_todo(), now="2027-12-04T00:00:00.000Z")

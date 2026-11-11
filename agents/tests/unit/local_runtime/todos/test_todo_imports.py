from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    NoOrganizationScope,
    OrganizationScope,
    TodoScope,
)
from pantaray_agents.local_runtime.todos.imports import register as register_module
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftGroup,
    DraftIssue,
    DraftIssueReason,
    DraftTodo,
    ExistingOrganization,
    ExistingProject,
    ImportClosed,
    NewOrganization,
    NewProject,
    StaleRevision,
    TodoImportDraft,
    TodoImportResult,
    TodoImportView,
)
from pantaray_agents.local_runtime.todos.imports.register import register_todo_import
from pantaray_agents.local_runtime.todos.imports.resolution import (
    Workspace,
    resolve_draft,
)
from pantaray_agents.local_runtime.todos.imports.store import (
    cancel_todo_import,
    open_todo_import,
    read_todo_import,
    write_todo_import_draft,
)
from pantaray_agents.local_runtime.todos.reads import read_todo_revision
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_project,
)

from .store_support import NOW, USER, T, count, execute


def _group(
    organization: str | None, project: str | None, *texts: str, keep: bool = False
) -> DraftGroup:
    return DraftGroup(
        organization=organization,
        project=project,
        todos=tuple(DraftTodo(text=text, keep_duplicate=keep) for text in texts),
    )


def _write(import_id: str, revision: int, *groups: DraftGroup) -> object:
    return write_todo_import_draft(
        user_id=USER,
        import_id=import_id,
        draft=TodoImportDraft(groups=groups),
        expected_revision=revision,
        clock=NOW,
    )


def _drafted(scope: TodoScope, *groups: DraftGroup) -> TodoImportView:
    opened = open_todo_import(
        user_id=USER, import_id=str(uuid.uuid4()), scope=scope, clock=NOW
    )
    assert isinstance(opened, TodoImportView)
    written = _write(opened.import_id, opened.revision, *groups)
    assert isinstance(written, TodoImportView), written
    return written


def _register(view: TodoImportView) -> object:
    return register_todo_import(
        user_id=USER,
        import_id=view.import_id,
        expected_revision=view.revision,
        clock=NOW,
    )


def _seed_open_todos(db: Path, *texts: str) -> None:
    for index, text in enumerate(texts):
        execute(
            db,
            f"""
            INSERT INTO todo_items(todo_id, user_id, project_id, text, author,
              origin_kind, created_at) VALUES ('seed-{index}', '{USER}', 'project-1',
              '{text}', 'user', 'written', '{T}');
            """,
        )


# Organizations A (org-a) and B (org-b); One is in A, Bee in B, Loose in none.
_WORKSPACE = Workspace(
    organization_ids={"A": "org-a", "B": "org-b"},
    projects={
        "One": ExistingProject("project-1", "One", frozenset({"org-a"})),
        "Bee": ExistingProject("project-b", "Bee", frozenset({"org-b"})),
        "Loose": ExistingProject("project-x", "Loose", frozenset()),
    },
    open_todos=frozenset(),
)
_IN_A = OrganizationScope(organization_id="org-a")


@pytest.mark.parametrize(
    ("scope", "groups", "expected"),
    [
        (_IN_A, [_group(None, "New", "t")], None),
        (_IN_A, [_group("A", "One", "t")], None),
        (_IN_A, [_group("B", "New", "t")], "organization_outside_scope"),
        (_IN_A, [_group("C", "New", "t")], "organization_outside_scope"),
        (_IN_A, [_group(None, "Bee", "t")], "project_name_taken"),
        (_IN_A, [_group(None, "Loose", "t")], "project_name_taken"),
        (_IN_A, [_group(None, None, "t")], "project_required"),
        (OrganizationScope("org-gone"), [], "organization_not_found"),
        (NoOrganizationScope(), [_group(None, "Loose", "t")], None),
        (NoOrganizationScope(), [_group(None, None, "t")], None),
        (
            NoOrganizationScope(),
            [_group("A", "New", "t")],
            "organization_outside_scope",
        ),
        (NoOrganizationScope(), [_group(None, "One", "t")], "project_name_taken"),
        (AllScope(), [_group("C", "New", "t"), _group("B", "Bee", "t")], None),
        (AllScope(), [_group("A", None, "t")], "project_required"),
        (AllScope(), [_group(None, "One", "t")], "project_name_taken"),
        (AllScope(), [_group("C", "One", "t")], "project_name_taken"),
        (
            AllScope(),
            [_group("A", "New", "t"), _group("C", "New", "t")],
            "project_name_taken",
        ),
        (AllScope(), [_group("A", "New", "t"), _group("A", "New", "u")], None),
        (AllScope(), [_group(None, None, "two\nlines")], "invalid_text"),
        (AllScope(), [_group(None, None, "  ")], "invalid_text"),
    ],
)
def test_the_scope_decides_which_organizations_and_projects_a_draft_may_name(
    scope: TodoScope, groups: list[DraftGroup], expected: DraftIssueReason | None
) -> None:
    _, issue = resolve_draft(
        TodoImportDraft(groups=tuple(groups)), scope=scope, workspace=_WORKSPACE
    )
    assert (None if issue is None else issue.reason) == expected


def test_a_draft_holds_at_most_300_todos() -> None:
    def issue_of(size: int) -> DraftIssue | None:
        group = _group(None, None, *(f"t{i}" for i in range(size)))
        return resolve_draft(
            TodoImportDraft(groups=(group,)), scope=AllScope(), workspace=_WORKSPACE
        )[1]

    assert issue_of(300) is None
    assert issue_of(301) == DraftIssue("too_many_todos")


def test_a_read_marks_new_names_and_open_todos_already_in_the_project(
    db: Path,
) -> None:
    _seed_open_todos(db, "Send the quote", "Book")
    execute(
        db,
        f"UPDATE todo_items SET checked_by = 'user', checked_at = '{T}' "
        "WHERE text = 'Book'",
    )
    view = _drafted(
        AllScope(),
        _group("A", "One", "Send the quote", " Send the quote ", "Book"),
        _group("C", "Two", "Send the quote"),
    )

    existing, new = view.groups
    assert existing.organization == ExistingOrganization("org-a", "A")
    assert existing.project == ExistingProject("project-1", "One", frozenset({"org-a"}))
    assert [line.duplicate for line in existing.todos] == [True, True, False]
    assert (new.organization, new.project) == (NewOrganization("C"), NewProject("Two"))
    assert [line.duplicate for line in new.todos] == [False]
    assert view.issue is None


def test_a_write_is_refused_on_a_stale_revision_or_an_issue_and_changes_nothing(
    db: Path,
) -> None:
    first = _drafted(AllScope(), _group(None, None, "Plan"))
    revision = read_todo_revision(user_id=USER)

    stale = _write(first.import_id, first.revision - 1, _group(None, None, "Other"))
    refused = _write(first.import_id, first.revision, _group("B", "One", "Plan"))

    assert stale == StaleRevision(first)
    assert refused == DraftIssue("project_name_taken", group_index=0)
    assert read_todo_import(user_id=USER, import_id=first.import_id) == first
    assert read_todo_revision(user_id=USER) == revision


def test_registering_writes_the_draft_once_skipping_duplicates_not_kept(
    db: Path,
) -> None:
    _seed_open_todos(db, "Send the quote", "Call")
    book = DraftTodo(
        text="Book", due=dt.date(2026, 11, 20), starts_on=dt.date(2026, 11, 10)
    )
    view = _drafted(
        AllScope(),
        DraftGroup(organization="A", project="One", todos=(book,)),
        _group("A", "One", "Send the quote"),
        _group("A", "One", "Call", keep=True),
        _group("C", "Two", "Plan"),
        _group("C", "Two", "Review"),
        _group(None, None, "Loose end"),
    )
    revision = read_todo_revision(user_id=USER)

    result = _register(view)
    again = _register(view)

    assert isinstance(result, TodoImportResult)
    assert again == result
    assert (len(result.organization_ids), len(result.project_ids)) == (1, 1)
    assert (len(result.todo_ids), result.skipped_duplicates) == (5, 1)
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT todo.text, project.display_name, link.organization_id, "
            "todo.starts_on, todo.due_date, todo.author, todo.origin_kind "
            "FROM todo_items AS todo LEFT JOIN workspace_projects AS project "
            "ON project.project_id = todo.project_id "
            "LEFT JOIN workspace_project_organizations AS link "
            "ON link.project_id = todo.project_id "
            "WHERE todo.todo_id NOT LIKE 'seed-%' ORDER BY todo.text"
        ).fetchall()
    new_org = result.organization_ids[0]
    assert rows == [
        ("Book", "One", "org-a", "2026-11-10", "2026-11-20", "user", "written"),
        ("Call", "One", "org-a", None, None, "user", "written"),
        ("Loose end", None, None, None, None, "user", "written"),
        ("Plan", "Two", new_org, None, None, "user", "written"),
        ("Review", "Two", new_org, None, None, "user", "written"),
    ]
    assert count(db, "SELECT COUNT(*) FROM workspace_organizations") == 3
    assert read_todo_revision(user_id=USER) > revision
    closed = ImportClosed("registered")
    assert _write(view.import_id, view.revision) == closed
    assert cancel_todo_import(user_id=USER, import_id=view.import_id, clock=NOW) == (
        closed
    )


def test_registering_checks_the_workspace_again_and_writes_nothing_when_it_changed(
    db: Path,
) -> None:
    view = _drafted(_IN_A, _group(None, "Two", "Plan"))
    # Made elsewhere after the draft was written: the name now belongs to B.
    create_workspace_project(
        db_path=db,
        busy_timeout_ms=1_000,
        user_id=USER,
        display_name="Two",
        organization_ids=("org-b",),
        now=T,
    )

    refused = _register(view)

    assert refused == DraftIssue("project_name_taken", group_index=0)
    assert count(db, "SELECT COUNT(*) FROM todo_items") == 0
    reread = read_todo_import(user_id=USER, import_id=view.import_id)
    assert reread is not None
    assert (reread.state, reread.issue) == ("open", refused)


def test_a_registration_failing_part_way_leaves_nothing_written(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _drafted(AllScope(), _group("C", "Two", "Plan"))

    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(register_module, "insert_todo", fail)
    with pytest.raises(sqlite3.OperationalError):
        _register(view)

    assert count(db, "SELECT COUNT(*) FROM workspace_organizations") == 2
    assert count(db, "SELECT COUNT(*) FROM workspace_projects") == 1
    reread = read_todo_import(user_id=USER, import_id=view.import_id)
    assert reread is not None and reread.state == "open"


def test_a_canceled_import_takes_no_writes_and_registers_nothing(db: Path) -> None:
    view = _drafted(NoOrganizationScope(), _group(None, None, "Plan"))

    canceled = cancel_todo_import(user_id=USER, import_id=view.import_id, clock=NOW)

    assert isinstance(canceled, TodoImportView) and canceled.state == "canceled"
    again = cancel_todo_import(user_id=USER, import_id=view.import_id, clock=NOW)
    assert again == canceled
    assert _write(view.import_id, view.revision) == ImportClosed("canceled")
    registered = _register(view)
    assert registered == ImportClosed("canceled")
    assert count(db, "SELECT COUNT(*) FROM todo_items") == 0
    missing = OrganizationScope("org-gone")
    assert open_todo_import(
        user_id=USER, import_id="import-gone", scope=missing, clock=NOW
    ) == DraftIssue("organization_not_found")


def test_a_line_repeating_an_earlier_one_for_the_same_project_is_a_duplicate(
    db: Path,
) -> None:
    view = _drafted(
        AllScope(),
        _group("A", "One", "Plan", " Plan "),
        _group("A", "One", "Plan", keep=True),
        _group("C", "Two", "Ship"),
        _group("C", "Two", "Ship"),
        _group(None, None, "Loose", "Loose"),
        _group(None, "Three", "Loose"),
    )

    marks = [[line.duplicate for line in group.todos] for group in view.groups]
    result = _register(view)

    assert marks == [[False, True], [True], [False], [True], [False, True], [False]]
    assert isinstance(result, TodoImportResult)
    assert (len(result.todo_ids), result.skipped_duplicates) == (5, 3)
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT text, COUNT(*) FROM todo_items GROUP BY text ORDER BY text"
        ).fetchall()
    assert rows == [("Loose", 2), ("Plan", 2), ("Ship", 1)]


def test_registering_a_revision_rewritten_since_is_refused_and_creates_nothing(
    db: Path,
) -> None:
    seen = _drafted(AllScope(), _group("C", "Two", "Plan"))
    rewritten = _write(seen.import_id, seen.revision, _group("D", "Three", "Ship"))
    assert isinstance(rewritten, TodoImportView)

    refused = _register(seen)

    assert (seen.revision, rewritten.revision) == (2, 3)
    assert refused == StaleRevision(rewritten)
    assert count(db, "SELECT COUNT(*) FROM todo_items") == 0
    assert count(db, "SELECT COUNT(*) FROM workspace_organizations") == 2
    assert count(db, "SELECT COUNT(*) FROM workspace_projects") == 1
    assert read_todo_import(user_id=USER, import_id=seen.import_id) == rewritten

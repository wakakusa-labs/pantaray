from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.helpers import (
    _resolve_act_tool_contract,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    todo_import as todo_import_runtime,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import (
    TOOL_REGISTRY,
    WRITE_TODO_DRAFT_TOOL_ID,
)
from pantaray_agents.local_runtime.todos.action_link import (
    read_todo_import_for_action,
)
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    NoOrganizationScope,
    OrganizationScope,
    TodoScope,
)
from pantaray_agents.local_runtime.todos.imports.model import TodoImportResult
from pantaray_agents.local_runtime.todos.imports.register import register_todo_import
from pantaray_agents.local_runtime.todos.imports.start import (
    TodoImportStarted,
    start_todo_import,
    todo_import_message_id,
)
from pantaray_agents.schema.agent.action_message import FileAttachmentInput
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.image import ImageInput

from .store_support import NOW, USER, seed_action

IMPORT = "import-1"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(todo_import_runtime, "read_todo_clock", lambda: NOW)


def _start(
    scope: TodoScope = AllScope(), *, import_id: str = IMPORT
) -> TodoImportStarted:
    started = start_todo_import(
        user_id=USER,
        import_id=import_id,
        scope=scope,
        text="Call the bank\nRenew the passport",
        files=(),
        images=(),
        clock=NOW,
    )
    assert isinstance(started, TodoImportStarted), started
    return started


def _first_message(db: Path, action_id: str) -> dict[str, JSONValue]:
    with closing(sqlite3.connect(db)) as connection:
        row = connection.execute(
            "SELECT step.user_message_json FROM agent_actions AS action "
            "JOIN agent_action_steps AS step ON step.action_id = action.action_id "
            "AND step.user_message_id = action.initial_user_message_id "
            "WHERE action.action_id = ?",
            (action_id,),
        ).fetchone()
    return cast("dict[str, JSONValue]", json.loads(row[0]))


def _state(import_id: str | None) -> ActionAgentState:
    return cast(
        ActionAgentState,
        {
            "user_id": USER,
            "action_id": "a",
            "phase": "executing",
            "context": {"served_todo_id": None, "served_todo_import_id": import_id},
        },
    )


def _write(base_revision: int, *groups: JSONValue) -> dict[str, JSONValue]:
    result = todo_import_runtime.run_todo_import_tool(
        step_id="step-1",
        tool_def=TOOL_REGISTRY[WRITE_TODO_DRAFT_TOOL_ID],
        args={"base_revision": base_revision, "draft": {"groups": list(groups)}},
        state=_state(IMPORT),
    )
    return cast("dict[str, JSONValue]", result.output)


def _group(
    organization: str | None, project: str | None, *todos: JSONValue
) -> JSONValue:
    return {"organization": organization, "project": project, "todos": list(todos)}


def test_starting_an_import_again_answers_its_one_action(db: Path) -> None:
    first = _start()
    again = _start()

    assert again == first
    with closing(sqlite3.connect(db)) as connection:
        actions = connection.execute(
            "SELECT action_id FROM agent_actions WHERE initial_user_message_id = ?",
            (todo_import_message_id(IMPORT),),
        ).fetchall()
        drafts = connection.execute(
            "SELECT COUNT(*) FROM todo_import_drafts"
        ).fetchone()
    assert actions == [(first.action_id,)]
    assert drafts == (1,)
    assert first.view.revision == 1


def test_the_first_message_carries_the_text_and_the_import_scope(db: Path) -> None:
    started = _start(OrganizationScope(organization_id="org-a"))

    message = _first_message(db, started.action_id)

    assert message["content"] == "Call the bank\nRenew the passport"
    assert message["todo_import_handoff"] == {
        "import_id": IMPORT,
        "scope": "organization",
        "organization_name": "A",
    }


def test_files_alone_are_named_as_the_message(db: Path, tmp_path: Path) -> None:
    file = FileAttachmentInput(
        attachment_id="00000000-0000-4000-8000-000000000001",
        name="minutes.txt",
        byte_size=4,
    )
    # Where the attach IPC stages it under the artifact root.
    staged = tmp_path / "artifacts" / "generated" / "attachments" / USER
    staged.mkdir(parents=True)
    (staged / f"{file.attachment_id}.txt").write_bytes(b"Pay\n")

    started = start_todo_import(
        user_id=USER,
        import_id=IMPORT,
        scope=AllScope(),
        text=None,
        files=(file,),
        images=(),
        clock=NOW,
    )

    assert isinstance(started, TodoImportStarted)
    assert _first_message(db, started.action_id)["content"] == "minutes.txt"


def test_images_go_on_the_first_message_beside_the_files(
    db: Path, tmp_path: Path
) -> None:
    file = FileAttachmentInput(
        attachment_id="00000000-0000-4000-8000-000000000001",
        name="minutes.txt",
        byte_size=4,
    )
    staged = tmp_path / "artifacts" / "generated" / "attachments" / USER
    staged.mkdir(parents=True)
    (staged / f"{file.attachment_id}.txt").write_bytes(b"Pay\n")
    images = tuple(
        ImageInput(storage_path=f"{USER}/2026-11-04/{uuid.uuid4()}.png")
        for _ in range(2)
    )

    started = start_todo_import(
        user_id=USER,
        import_id=IMPORT,
        scope=AllScope(),
        text=None,
        files=(file,),
        images=images,
        clock=NOW,
    )

    assert isinstance(started, TodoImportStarted)
    message = _first_message(db, started.action_id)
    assert message["content"] == "minutes.txt, 2 images"
    assert message["images"] == [image.model_dump(mode="json") for image in images]
    files = message["files"]
    assert isinstance(files, list)
    assert [cast("dict[str, JSONValue]", item)["name"] for item in files] == [
        "minutes.txt"
    ]


def test_only_the_import_s_action_finds_it_and_is_offered_the_draft_tool(
    db: Path,
) -> None:
    started = _start()
    seed_action(db, "plain", message_id="m-1")

    assert read_todo_import_for_action(user_id=USER, action_id=started.action_id) == (
        IMPORT
    )
    assert read_todo_import_for_action(user_id=USER, action_id="plain") is None
    _, plain_tools, _ = _resolve_act_tool_contract(_state(None))
    _, import_tools, _ = _resolve_act_tool_contract(_state(IMPORT))
    assert WRITE_TODO_DRAFT_TOOL_ID not in plain_tools
    assert set(import_tools) - set(plain_tools) == {WRITE_TODO_DRAFT_TOOL_ID}


def test_a_write_returns_the_draft_as_the_workspace_sees_it(db: Path) -> None:
    _start()

    written = _write(
        1,
        _group("A", "One", {"text": "Call the bank"}),
        _group("New Co", "Launch", {"text": "Plan", "due": "2026-11-20"}),
    )

    assert written == {
        "status": "written",
        "revision": 2,
        "groups": [
            {
                "organization": {"name": "A", "new": False},
                "project": {"name": "One", "new": False},
                "todos": [_line("Call the bank")],
            },
            {
                "organization": {"name": "New Co", "new": True},
                "project": {"name": "Launch", "new": True},
                "todos": [_line("Plan", due="2026-11-20")],
            },
        ],
    }


def _line(text: str, *, due: str | None = None) -> JSONValue:
    return {
        "text": text,
        "due": due,
        "starts_on": None,
        "duplicate": False,
        "keep_duplicate": False,
    }


def test_a_stale_write_is_refused_with_the_current_draft(db: Path) -> None:
    _start()
    _write(1, _group(None, None, {"text": "Renew the passport"}))

    refused = _write(1, _group(None, None, {"text": "Call the bank"}))

    assert refused["status"] == "refused"
    assert refused["reason"] == "stale_revision"
    assert refused["current"] == {
        "revision": 2,
        "groups": [
            {
                "organization": None,
                "project": None,
                "todos": [_line("Renew the passport")],
            }
        ],
    }


def test_a_draft_outside_the_scope_is_refused_where_it_is(db: Path) -> None:
    _start(NoOrganizationScope())

    refused = _write(
        1,
        _group(None, None, {"text": "Renew the passport"}),
        _group("A", "One", {"text": "Call the bank"}),
    )

    assert {key: refused[key] for key in ("status", "reason", "group_index")} == {
        "status": "refused",
        "reason": "organization_outside_scope",
        "group_index": 1,
    }


def test_a_date_that_does_not_exist_is_refused_and_nothing_is_written(
    db: Path,
) -> None:
    _start()

    refused = _write(1, _group(None, None, {"text": "Pay", "due": "2026-02-30"}))

    assert (refused["status"], refused["reason"]) == ("refused", "invalid_draft")
    assert _write(1, _group(None, None, {"text": "Pay"}))["status"] == "written"


def test_a_registered_import_takes_no_more_writes(db: Path) -> None:
    _start()
    _write(1, _group(None, None, {"text": "Renew the passport"}))
    registered = register_todo_import(
        user_id=USER, import_id=IMPORT, expected_revision=2, clock=NOW
    )
    assert isinstance(registered, TodoImportResult)

    refused = _write(2, _group(None, None, {"text": "Call the bank"}))

    assert (refused["status"], refused["reason"]) == ("refused", "registered")

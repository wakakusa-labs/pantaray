"""The chat's routing tools over a migrated store: what they start, and once."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from pantaray_agents.agents.chat_agent import tools
from pantaray_agents.agents.chat_agent.context import turn_context
from pantaray_agents.agents.chat_agent.tools import chat_tools
from pantaray_agents.agents.chat_agent.turn import ChatTurnPlan
from pantaray_agents.local_runtime.chat.store import append_chat_item
from pantaray_agents.local_runtime.chat.work_list import (
    SubmittedMessage,
    read_chat_work_list,
    search_tasks,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    DeferredActionMessageResult,
    MessageIdentityConflictError,
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
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.chat import UserMessageContent
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_agents.tools.memory.sql import execute_memory_sql

USER = "user-1"
NOW = "2026-10-08T09:00:00Z"
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
IMAGE = {"kind": "image", "storage_path": f"{USER}/2026-10-08/{FILE_ID}.png"}


@pytest.fixture
def db_path() -> Iterator[Path]:
    register_logged_out_owner(USER)
    path = Path(os.environ["LOCAL_DB_PATH"])
    with sqlite3.connect(path) as connection:
        ensure_user_row(connection, user_id=USER, timestamp=NOW)
    yield path
    reset_logged_out_owner()


def _turn(key: str) -> Callable[..., Awaitable[JSONValue]]:
    """Calls of one turn ``key``, through the registry's checks."""

    registry = ReactToolRegistry(
        chat_tools(ChatTurnPlan(user_id=USER, key=key, cursor=0))
    )

    async def call(tool: str, **args: JSONValue) -> JSONValue:
        envelope = ToolCallEnvelope(tool_id=tool, reason=None, args=args)
        result = await registry.execute(
            ReactToolCall(tool_name=tool, tool_args=args, tool_call_envelope=envelope),
            1,
        )
        return result.output

    return call


def _actions(db_path: Path) -> list[tuple[str, str | None]]:
    with sqlite3.connect(db_path) as connection:
        return [
            (str(row[0]), row[1])
            for row in connection.execute(
                "SELECT action_id, suggestion_id FROM agent_actions ORDER BY created_at"
            )
        ]


async def test_a_turn_run_again_never_sends_a_call_twice(db_path: Path) -> None:
    first = await _turn("a0")(
        "start_action", message="Draft the Q3 report", attachments_from=[]
    )
    # The same turn after a crash, sending the same words again ...
    replayed = await _turn("a0")(
        "start_action", message="Draft the Q3 report", attachments_from=[]
    )
    # ... or, asked again, another request first: it is told what went in.
    rerun = _turn("a0")
    reordered = await rerun("start_action", message="Book a room", attachments_from=[])
    booked = await rerun("start_action", message="Book a room", attachments_from=[])

    assert isinstance(first, dict) and replayed == first
    assert isinstance(reordered, dict)
    assert reordered["error_code"] == "ALREADY_SENT_IN_THIS_TURN"
    assert first["action_id"] in str(reordered["message"])
    assert "Draft the Q3 report" in str(reordered["message"])
    assert isinstance(booked, dict) and booked != first
    assert len(_actions(db_path)) == 2


async def test_an_instruction_to_a_stopped_task_is_not_reported_done(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def kept_while_stopped(command: SubmitActionMessageCommand) -> object:
        return DeferredActionMessageResult(
            "not_executed",
            "act-1",
            command.message.message_id,
            "step",
            "canceled",
            None,
            None,
            True,
        )

    monkeypatch.setattr(tools, "submit_action_message", kept_while_stopped)

    result = await _turn("a0")(
        "send_to_action", action_id="act-1", message="Go on", attachments_from=[]
    )

    # Run again after a restart: what went in was dropped, not done.
    def already_in(command: SubmitActionMessageCommand) -> object:
        raise MessageIdentityConflictError("reworded")

    monkeypatch.setattr(tools, "submit_action_message", already_in)
    monkeypatch.setattr(
        tools,
        "read_submitted_message",
        lambda **_: SubmittedMessage("act-1", None, "Go on", dropped=True),
    )
    rerun = await _turn("a0")(
        "send_to_action", action_id="act-1", message="Keep going", attachments_from=[]
    )

    assert isinstance(result, dict) and result["error_code"] == "TASK_STOPPED"
    assert isinstance(rerun, dict) and rerun["error_code"] == "TASK_STOPPED"


async def test_an_instruction_reaches_the_running_task_it_names(db_path: Path) -> None:
    started = await _turn("a0")("start_action", message="Draft it", attachments_from=[])
    assert isinstance(started, dict)
    # The worker has started the task; a message now names its run.
    payload = claim_next_pending_action_job(
        db_path=db_path, busy_timeout_ms=1_000, owner_user_id=USER, claimed_by="w"
    )
    ActionJobRuntimeRepository(
        db_path=db_path, busy_timeout_ms=1_000
    ).prepare_execution(payload=payload, started_at=NOW)

    later = _turn("a1")
    sent = await later(
        "send_to_action",
        action_id=started["action_id"],
        message="Make the deadline two weeks later",
        attachments_from=[],
    )
    unknown = await later(
        "send_to_action", action_id="nope", message="x", attachments_from=[]
    )

    assert isinstance(sent, dict) and sent["action_id"] == started["action_id"]
    assert isinstance(unknown, dict) and unknown["error_code"] == "UNKNOWN_TASK"
    with sqlite3.connect(db_path) as connection:
        messages = connection.execute(
            "SELECT user_message_id FROM agent_action_steps "
            "WHERE user_message_id IS NOT NULL ORDER BY accepted_sequence"
        ).fetchall()
    assert [row[0] for row in messages] == [
        "chat-turn/a0/start_action/1",
        "chat-turn/a1/send_to_action/1",
    ]


async def test_a_file_goes_to_one_task_and_the_second_hand_off_says_so(
    db_path: Path,
) -> None:
    staged = (
        Path(os.environ["LOCAL_ARTIFACT_ROOT"])
        / f"generated/attachments/{USER}/{FILE_ID}.pdf"
    )
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"%PDF-1.7 staged")
    file = {"attachment_id": FILE_ID, "name": "Q3.pdf", "byte_size": 15}
    asked = append_chat_item(
        user_id=USER,
        message_id="m-1",
        content=UserMessageContent.model_validate_json(
            '{"kind": "user_message", "text": "Use this", "quote_item_id": null, '
            f'"images": [], "files": [{json.dumps(file)}]}}'
        ),
    )

    turn = _turn("a0")
    handed = await turn(
        "start_action", message="Summarize it", attachments_from=[asked.item_id]
    )
    twice = await turn(
        "start_action", message="Translate it", attachments_from=[asked.item_id]
    )

    assert isinstance(handed, dict) and "action_id" in handed
    assert not staged.exists()  # moved into the first task
    assert isinstance(twice, dict)
    assert twice["error_code"] == "ATTACHMENT_ALREADY_HANDED_OVER"
    assert handed["action_id"] in str(twice["message"])  # names where it went
    # The same turn run again after a crash finds what it started.
    rerun = await _turn("a0")(
        "start_action", message="Summarize it", attachments_from=[asked.item_id]
    )
    assert isinstance(rerun, dict) and rerun["action_id"] == handed["action_id"]
    # Adding to the task that holds the file needs no second hand-off.
    added = await turn(
        "send_to_action",
        action_id=handed["action_id"],
        message="Also make a comparison table",
        attachments_from=[asked.item_id],
    )
    assert isinstance(added, dict) and added["action_id"] == handed["action_id"]
    assert len(_actions(db_path)) == 1


async def test_a_yes_takes_up_the_open_suggestion_once(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer, "
            "suggestion_summary, interaction_contract, has_suggestion, created_at, "
            "updated_at) VALUES ('sug-1', ?, 'success', 'Tidy the invoice template.', "
            "'Tidy the invoice template', 'action_offer', 1, ?, ?)",
            (USER, NOW, NOW),
        )
    assert [s.suggestion_id for s in read_chat_work_list(user_id=USER).suggestions] == [
        "sug-1"
    ]

    pictured = append_chat_item(
        user_id=USER,
        message_id="m-1",
        content=UserMessageContent(
            kind="user_message",
            text="Yes, with this logo",
            quote_item_id=None,
            images=(ImageInput.model_validate(IMAGE),),
            files=(),
        ),
    )
    taken = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-1",
        supplement=None,
        attachments_from=[pictured.item_id],
    )
    # The same turn after a crash, as it was, and with an addition worded anew.
    replayed = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-1",
        supplement=None,
        attachments_from=[pictured.item_id],
    )
    again = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-1",
        supplement="with our logo",
        attachments_from=[],
    )
    gone = await _turn("a2")(
        "accept_suggestion", suggestion_id="sug-1", supplement=None, attachments_from=[]
    )

    assert isinstance(taken, dict) and replayed == taken
    assert isinstance(again, dict)
    assert again["error_code"] == "ALREADY_SENT_IN_THIS_TURN"
    # It shows what went in, attachments included, so a missing addition shows.
    assert f"image {IMAGE['storage_path']}" in str(again["message"])
    with sqlite3.connect(db_path) as connection:
        (message_json,) = connection.execute(
            "SELECT user_message_json FROM agent_action_steps "
            "WHERE user_message_id = 'chat-turn/a0/accept_suggestion/1'"
        ).fetchone()
    assert json.loads(message_json)["images"] == [IMAGE]
    assert _actions(db_path) == [(taken["action_id"], "sug-1")]
    assert isinstance(gone, dict) and gone["status"] == "error"
    assert read_chat_work_list(user_id=USER).suggestions == ()


def _overlay_task(message_id: str, text: str) -> str:
    return submit_action_message(
        SubmitActionMessageCommand(
            user_id=USER,
            target=NewActionTarget(),
            message=ActionUserMessageInput(message_id=message_id, content=text),
        )
    ).action_id


def _finish(db_path: Path, action_id: str, updated_at: str, answer: str = "") -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'success', updated_at = ?, "
            "final_output = ? WHERE action_id = ?",
            (updated_at, answer, action_id),
        )


async def test_the_work_list_keeps_one_cap_with_tasks_in_hand_first(
    db_path: Path,
) -> None:
    for n in range(12):
        _finish(
            db_path,
            _overlay_task(f"old-{n}", f"Finished {n}"),
            f"2026-10-01T00:00:{n:02d}Z",
        )
    waiting = _overlay_task("overlay-1", "Book the room\nfor Friday")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE processes SET status = 'paused' WHERE action_id = ?", (waiting,)
        )
    started = await _turn("a0")(
        "start_action", message="Draft the Q3 report", attachments_from=[]
    )

    work = read_chat_work_list(user_id=USER)
    shown = turn_context([], work).model_dump_json()

    assert isinstance(started, dict)
    # Wherever a task was started, it is Pantaray's own work.
    in_hand = [(t.title, t.awaiting_approval) for t in work.tasks[:2]]
    assert sorted(in_hand) == [("Book the room", True), ("Draft the Q3 report", False)]
    assert [t.title for t in work.tasks[2:]] == [
        f"Finished {n}" for n in range(11, 3, -1)
    ]
    assert (len(work.tasks), work.more_tasks) == (10, 4)
    assert "waiting for approval" in shown and "4 older tasks not listed here" in shown


async def test_search_tasks_finds_any_task_by_words_and_time(db_path: Path) -> None:
    quote = _overlay_task("old-quote", 'A社の見積書を作って。件名は "Q3 report"')
    _finish(
        db_path,
        quote,
        "2026-09-30T09:00:00.000Z",
        "見積書を作成しました。合計 200 万円。",
    )
    other = _overlay_task("old-other", "Book a room")
    _finish(db_path, other, "2026-10-01T00:00:00.500Z", "Booked room 3.")
    running = await _turn("a0")(
        "start_action", message="Draft the plan", attachments_from=[]
    )
    assert isinstance(running, dict)
    payload = claim_next_pending_action_job(
        db_path=db_path, busy_timeout_ms=1_000, owner_user_id=USER, claimed_by="w"
    )
    ActionJobRuntimeRepository(
        db_path=db_path, busy_timeout_ms=1_000
    ).prepare_execution(payload=payload, started_at=NOW)
    await _turn("a1")(
        "send_to_action",
        action_id=running["action_id"],
        message="Add the budget table",
        attachments_from=[],
    )

    def found(query: str | None, since: str | None = None) -> list[str]:
        tasks = search_tasks(
            user_id=USER, query=query, since=since, until=None, limit=5
        )
        return [task.action_id for task in tasks]

    assert found("見積書") == [quote]
    assert found('"Q3 report"') == [quote]  # quotes as typed, not as stored JSON
    assert found("content") == []  # never a key of how the request is stored
    assert found("budget table") == [running["action_id"]]  # a later instruction
    assert found("100%") == []
    # Since the start of Oct 1, in any offset, holds the one updated at 00:00:00.5.
    assert other in found(None, "2026-10-01T00:00:00Z")
    assert other in found(None, "2026-10-01T09:00:00+09:00")
    assert quote not in found(None, "2026-10-01T00:00:00Z")
    by_words = search_tasks(
        user_id=USER, query="見積書", since=None, until=None, limit=5
    )
    assert by_words[0].latest == "見積書を作成しました。合計 200 万円。"

    # The whole answer of a found task, as its description says to read it.
    answer = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        user_id=USER,
        sql=f"SELECT final_output FROM agent_actions WHERE action_id = '{quote}'",
        limit=1,
    )
    assert answer.data is not None
    assert answer.data["rows"] == [
        {"final_output": "見積書を作成しました。合計 200 万円。"}
    ]


async def test_a_rerun_that_skips_a_refused_call_finds_the_task(db_path: Path) -> None:
    first_run = _turn("a0")
    refused = await first_run("start_action", message="   ", attachments_from=[])
    started = await first_run("start_action", message="Draft it", attachments_from=[])
    # After a restart the model gets the wording right the first time.
    rerun = await _turn("a0")("start_action", message="Draft it", attachments_from=[])

    assert isinstance(refused, dict) and refused["error_code"] == "INVALID_MESSAGE"
    assert isinstance(started, dict) and rerun == started
    assert len(_actions(db_path)) == 1


async def test_a_named_project_goes_with_the_task_the_message_starts(
    db_path: Path,
) -> None:
    ref = {
        "project_id": "p-1",
        "display_name": "Aurora Web",
        "paths": ["/Users/me/aurora"],
        "start": 0,
        "end": 10,
    }
    asked = append_chat_item(
        user_id=USER,
        message_id="m-1",
        content=UserMessageContent.model_validate_json(
            json.dumps(
                {
                    "kind": "user_message",
                    "text": "Aurora Web で見積書を作って",
                    "quote_item_id": None,
                    "images": [],
                    "files": [],
                    "project_refs": [ref],
                }
            )
        ),
    )

    turn = _turn("a0")
    named = await turn(
        "start_action",
        message="Aurora Web のフォルダで見積書を作って",
        attachments_from=[asked.item_id],
    )
    # The model left the name out: it is added as the user wrote it.
    unnamed = await turn(
        "start_action", message="見積書を作って", attachments_from=[asked.item_id]
    )

    assert isinstance(named, dict) and isinstance(unnamed, dict)
    sent = [_sent_message(db_path, str(r["action_id"])) for r in (named, unnamed)]
    assert sent[0].content == "Aurora Web のフォルダで見積書を作って"
    assert sent[1].content == "見積書を作って\n@Aurora Web"
    for message in sent:
        (project,) = message.project_refs
        assert project.paths == ("/Users/me/aurora",)
        assert message.content[project.start : project.end] == "Aurora Web"


def _sent_message(db_path: Path, action_id: str) -> ActionUserMessageInput:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT user_message_json FROM agent_action_steps "
            "WHERE action_id = ? AND user_message_json IS NOT NULL",
            (action_id,),
        ).fetchone()
    return ActionUserMessageInput.model_validate_json(row[0])

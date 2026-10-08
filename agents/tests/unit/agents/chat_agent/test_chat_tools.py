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
from pantaray_agents.local_runtime.action_conversation.history_candidates import (
    register_conversation_history_casefold_sqlite,
)
from pantaray_agents.local_runtime.action_conversation.history_repository import (
    read_conversation_history_page_in_connection,
)
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
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    ChatHandoffInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
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
    first = await _turn("a0")("start_action", relay=[], note="Draft the Q3 report")
    # The same turn after a crash, sending the same words again ...
    replayed = await _turn("a0")("start_action", relay=[], note="Draft the Q3 report")
    # ... or, asked again, another request first: it is told what went in.
    rerun = _turn("a0")
    reordered = await rerun("start_action", relay=[], note="Book a room")
    booked = await rerun("start_action", relay=[], note="Book a room")

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
        "send_to_action", action_id="act-1", relay=[], note="Go on"
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
        "send_to_action", action_id="act-1", relay=[], note="Keep going"
    )

    assert isinstance(result, dict) and result["error_code"] == "TASK_STOPPED"
    assert isinstance(rerun, dict) and rerun["error_code"] == "TASK_STOPPED"


async def test_an_instruction_reaches_the_running_task_it_names(db_path: Path) -> None:
    started = await _turn("a0")("start_action", relay=[], note="Draft it")
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
        relay=[],
        note="Make the deadline two weeks later",
    )
    unknown = await later("send_to_action", action_id="nope", relay=[], note="x")

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
    handed = await turn("start_action", relay=[asked.item_id], note="Summarize it")
    twice = await turn("start_action", relay=[asked.item_id], note="Translate it")

    assert isinstance(handed, dict) and "action_id" in handed
    assert not staged.exists()  # moved into the first task
    assert isinstance(twice, dict)
    assert twice["error_code"] == "ATTACHMENT_ALREADY_HANDED_OVER"
    assert handed["action_id"] in str(twice["message"])  # names where it went
    # The same turn run again after a crash finds what it started.
    rerun = await _turn("a0")(
        "start_action", relay=[asked.item_id], note="Summarize it"
    )
    assert isinstance(rerun, dict) and rerun["action_id"] == handed["action_id"]
    # Adding to the task that holds the file needs no second hand-off.
    added = await turn(
        "send_to_action",
        action_id=handed["action_id"],
        relay=[asked.item_id],
        note="Also make a comparison table",
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
        relay=[pictured.item_id],
        note=None,
    )
    # The same turn after a crash, as it was, and with the addition left out.
    replayed = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-1",
        relay=[pictured.item_id],
        note=None,
    )
    again = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-1",
        relay=[],
        note=None,
    )
    gone = await _turn("a2")(
        "accept_suggestion", suggestion_id="sug-1", relay=[], note=None
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
    started = await _turn("a0")("start_action", relay=[], note="Draft the Q3 report")

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
    running = await _turn("a0")("start_action", relay=[], note="Draft the plan")
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
        relay=[],
        note="Add the budget table",
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
    refused = await first_run("start_action", relay=[], note="   ")
    started = await first_run("start_action", relay=[], note="Draft it")
    # After a restart the model gets the wording right the first time.
    rerun = await _turn("a0")("start_action", relay=[], note="Draft it")

    assert isinstance(refused, dict) and refused["error_code"] == "NOTHING_TO_SEND"
    assert isinstance(started, dict) and rerun == started
    assert len(_actions(db_path)) == 1


async def test_the_users_words_go_in_as_theirs_and_the_chats_as_its_note(
    db_path: Path,
) -> None:
    ref = {
        "project_id": "p-1",
        "display_name": "Aurora Web",
        "paths": ["/Users/me/aurora"],
        "start": 0,
        "end": 10,
    }
    earlier = _say_with("m-1", "前回の見積もりの件", ())
    named = _say_with("m-2", "Aurora Web で作り直して", (ref,))

    turn = _turn("a0")
    relayed = await turn("start_action", relay=[earlier, named], note=None)
    assert isinstance(relayed, dict) and "action_id" in relayed
    action_id = str(relayed["action_id"])
    await turn("send_to_action", action_id=action_id, relay=[], note="納期は金曜")
    await turn(
        "send_to_action", action_id=action_id, relay=[earlier], note="単価は据え置き"
    )
    nothing = await turn("start_action", relay=[], note=None)

    first, second, third = _sent_messages(db_path, action_id)
    # Relayed: the user's words, verbatim and in order, with their project's span moved.
    assert first.content == "前回の見積もりの件\n\nAurora Web で作り直して"
    (project,) = first.project_refs
    assert first.content[project.start : project.end] == "Aurora Web"
    assert project.paths == ("/Users/me/aurora",)
    assert first.chat_handoff == ChatHandoffInput(relayed_item_ids=(earlier, named))
    # The chat's own instruction is the message, marked as relaying none of theirs.
    assert second.content == "納期は金曜"
    assert second.chat_handoff == ChatHandoffInput(relayed_item_ids=())
    assert second.project_refs == ()
    # Both: the user's words, and the chat's note beside them.
    assert third.content == "前回の見積もりの件"
    assert third.chat_handoff == ChatHandoffInput(
        relayed_item_ids=(earlier,), note="単価は据え置き"
    )
    assert isinstance(nothing, dict) and nothing["error_code"] == "NOTHING_TO_SEND"


def _say_with(message_id: str, text: str, refs: tuple[dict[str, object], ...]) -> str:
    return append_chat_item(
        user_id=USER,
        message_id=message_id,
        content=UserMessageContent.model_validate_json(
            json.dumps(
                {
                    "kind": "user_message",
                    "text": text,
                    "quote_item_id": None,
                    "images": [],
                    "files": [],
                    "project_refs": list(refs),
                }
            )
        ),
    ).item_id


def _sent_messages(db_path: Path, action_id: str) -> list[ActionUserMessageInput]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT user_message_json FROM agent_action_steps "
            "WHERE action_id = ? AND user_message_json IS NOT NULL "
            "ORDER BY accepted_sequence",
            (action_id,),
        ).fetchall()
    return [ActionUserMessageInput.model_validate_json(row[0]) for row in rows]


async def test_a_yes_carries_what_the_chat_settled_with_the_user(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer, "
            "suggestion_summary, interaction_contract, has_suggestion, created_at, "
            "updated_at) VALUES ('sug-2', ?, 'success', 'Draft the case study.', "
            "'Draft the case study', 'action_offer', 1, ?, ?)",
            (USER, NOW, NOW),
        )
    yes = _say_with("m-1", "その条件でお願い", ())

    taken = await _turn("a0")(
        "accept_suggestion",
        suggestion_id="sug-2",
        relay=[yes],
        note="社名は匿名化する",
    )

    assert isinstance(taken, dict) and "action_id" in taken
    (message,) = _sent_messages(db_path, str(taken["action_id"]))
    # The suggestion is the content, the user's yes their words, the condition ours.
    assert message.content == "Draft the case study."
    assert message.supplement == "その条件でお願い"
    assert message.chat_handoff == ChatHandoffInput(
        relayed_item_ids=(yes,), note="社名は匿名化する"
    )
    assert "社名は匿名化する" in render_action_user_request_text(message)


async def test_the_history_finds_a_task_by_the_chats_note(db_path: Path) -> None:
    go = _say_with("m-1", "それで進めて", ())
    started = await _turn("a0")(
        "start_action", relay=[go], note="Aurora の見積書を金曜までに作る"
    )
    assert isinstance(started, dict)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        register_conversation_history_casefold_sqlite(connection)
        connection.execute("BEGIN")
        page = read_conversation_history_page_in_connection(
            connection=connection,
            user_id=USER,
            search_text="aurora",
            cursor=None,
            limit=10,
        )
    # Found by the note shown beside the user's words; titled by their words.
    assert [item.title for item in page.items] == ["それで進めて"]

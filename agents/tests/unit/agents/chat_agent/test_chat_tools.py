"""The chat's routing tools over a migrated store: what they start, and once."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from pantaray_agents.agents.chat_agent.context import turn_context
from pantaray_agents.agents.chat_agent.tools import chat_tools
from pantaray_agents.agents.chat_agent.turn import ChatTurnPlan
from pantaray_agents.local_runtime.chat.store import append_chat_item
from pantaray_agents.local_runtime.chat.work_list import read_chat_work_list
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
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
from pantaray_agents.schema.chat import UserMessageContent
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)

USER = "user-1"
NOW = "2026-10-08T09:00:00Z"
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


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


async def test_a_turn_run_again_finds_the_task_it_started(db_path: Path) -> None:
    first = await _turn("a0")(
        "start_action", message="Draft the Q3 report", attachments_from=[]
    )
    # The same turn after a crash: the model words the same request differently.
    again = await _turn("a0")(
        "start_action", message="Please draft the Q3 report", attachments_from=[]
    )
    other_turn = await _turn("a5")(
        "start_action", message="Book a room", attachments_from=[]
    )

    assert isinstance(first, dict) and again == first
    assert isinstance(other_turn, dict) and other_turn != first
    assert len(_actions(db_path)) == 2


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

    assert sent == started
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

    taken = await _turn("a0")(
        "accept_suggestion", suggestion_id="sug-1", supplement=None
    )
    again = await _turn("a0")(
        "accept_suggestion", suggestion_id="sug-1", supplement=None
    )
    gone = await _turn("a2")(
        "accept_suggestion", suggestion_id="sug-1", supplement=None
    )

    assert isinstance(taken, dict) and again == taken
    assert _actions(db_path) == [(taken["action_id"], "sug-1")]
    assert isinstance(gone, dict) and gone["status"] == "error"
    assert read_chat_work_list(user_id=USER).suggestions == ()


async def test_the_work_list_shows_only_the_tasks_the_chat_works_on(
    db_path: Path,
) -> None:
    submit_action_message(
        SubmitActionMessageCommand(
            user_id=USER,
            target=NewActionTarget(),
            message=ActionUserMessageInput(message_id="overlay-1", content="Not mine"),
        )
    )
    started = await _turn("a0")(
        "start_action",
        message="Draft the Q3 report\nwith charts",
        attachments_from=[],
    )

    work = read_chat_work_list(user_id=USER)
    shown = turn_context([], work).model_dump_json()

    assert isinstance(started, dict)
    assert [(t.action_id, t.title, t.status) for t in work.tasks] == [
        (started["action_id"], "Draft the Q3 report", "queued")
    ]
    assert started["action_id"] in shown and "Not mine" not in shown

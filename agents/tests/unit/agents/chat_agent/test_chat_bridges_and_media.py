"""What the chat is told about its work, and what it is shown of the user's."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.agents.chat_agent.context import ChatWindow
from pantaray_agents.agents.chat_agent.media import load_item_media
from pantaray_agents.agents.chat_agent.research import (
    chat_research_tools,
    chat_tool_results_root,
    discard_chat_tool_results,
)
from pantaray_agents.agents.chat_agent.tools import chat_tools
from pantaray_agents.agents.chat_agent.turn import (
    ChatTurnPlan,
    plan_chat_turn,
    run_chat_turn,
)
from pantaray_agents.conversation.loop import ConversationRequest
from pantaray_agents.local_runtime.chat.bridges import bridge_chat_events
from pantaray_agents.local_runtime.chat.store import (
    append_chat_item,
    read_chat_items_after,
    read_chat_items_for_turn,
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
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.chat import UserMessageContent
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.input_block import LlmInputImageBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

USER = "user-1"
LATER = "2999-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def owner() -> Iterator[Path]:
    register_logged_out_owner(USER)
    yield Path(os.environ["LOCAL_DB_PATH"])
    reset_logged_out_owner()


def _say(text: str, images: tuple[ImageInput, ...] = ()) -> None:
    append_chat_item(
        user_id=USER,
        message_id=f"m-{uuid.uuid4()}",
        content=UserMessageContent(
            kind="user_message", text=text, quote_item_id=None, images=images, files=()
        ),
    )


def _kinds() -> list[str]:
    return [item.content.kind for item in read_chat_items_after(user_id=USER, after=0)]


def _suggestion(db: Path, suggestion_id: str, created_at: str) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer, "
            "interaction_contract, has_suggestion, created_at, updated_at) "
            "VALUES (?, ?, 'success', 'Tidy the invoices.', 'action_offer', 1, ?, ?)",
            (suggestion_id, USER, created_at, created_at),
        )


async def test_a_new_suggestion_is_told_once_and_one_before_the_chat_never(
    owner: Path,
) -> None:
    _suggestion(owner, "before", "2000-01-01T00:00:00Z")
    assert bridge_chat_events(user_id=USER) == 0  # no chat yet
    _say("Hi")
    _suggestion(owner, "after", LATER)

    assert bridge_chat_events(user_id=USER) == 1
    assert bridge_chat_events(user_id=USER) == 0
    told = read_chat_items_after(user_id=USER, after=0)[-1]
    assert told.content.model_dump() == {
        "kind": "suggestion_event",
        "suggestion_id": "after",
    }
    shown = load_item_media(
        user_id=USER, items=read_chat_items_for_turn(user_id=USER, after=0)
    )
    assert shown.suggestions == {"after": "Tidy the invoices."}


async def test_the_chat_hears_when_its_own_task_waits_and_ends(owner: Path) -> None:
    _say("Draft the report")
    registry = ReactToolRegistry(
        chat_tools(ChatTurnPlan(user_id=USER, key="a0", cursor=0))
    )
    args = {"message": "Draft the report", "attachments_from": []}
    started = await registry.execute(
        ReactToolCall(
            tool_name="start_action",
            tool_args=args,
            tool_call_envelope=ToolCallEnvelope("start_action", None, args),
        ),
        1,
    )
    assert isinstance(started.output, dict)
    submit_action_message(  # the user's own task, from the Overlay
        SubmitActionMessageCommand(
            user_id=USER,
            target=NewActionTarget(),
            message=ActionUserMessageInput(message_id="overlay-1", content="Mine"),
        )
    )
    with sqlite3.connect(owner) as connection:
        (process_id,) = connection.execute(
            "SELECT adopted_process_id FROM agent_action_steps "
            "WHERE user_message_id = 'chat-turn/a0/start_action/1'"
        ).fetchone()
        connection.execute(
            "INSERT INTO process_events(process_id, event_seq, event_id, event_name, "
            "payload_json, created_at) VALUES (?, 100, 'pause-1', 'process_paused', "
            "'{}', ?)",
            (process_id, LATER),
        )
        connection.execute(
            "INSERT INTO process_events(process_id, event_seq, event_id, event_name, "
            "payload_json, created_at) VALUES (?, 101, 'end-1', 'process_completed', "
            "?, ?)",
            (process_id, '{"final_output": "Report drafted."}', LATER),
        )
        connection.execute(
            "UPDATE processes SET status = 'completed', terminal_event_id = 'end-1' "
            "WHERE process_id = ?",
            (process_id,),
        )
        # A later run has already replaced the Action's current answer.
        connection.execute(
            "UPDATE agent_actions SET final_output = '' WHERE action_id = ?",
            (started.output["action_id"],),
        )

    assert bridge_chat_events(user_id=USER) == 2
    assert bridge_chat_events(user_id=USER) == 0
    events = [
        item.content.model_dump()
        for item in read_chat_items_after(user_id=USER, after=0)
        if item.content.kind == "action_event"
    ]
    assert {(e["event"], e["final_answer_excerpt"]) for e in events} == {
        ("approval_pending", None),
        ("completed", "Report drafted."),
    }
    assert all(e["action_id"] == started.output["action_id"] for e in events)


async def test_an_attached_image_reaches_the_model_as_the_image(owner: Path) -> None:
    storage_path = f"{USER}/2026-10-08/{uuid.uuid4()}.png"
    image_file = (
        Path(os.environ["LOCAL_ARTIFACT_ROOT"]) / "generated/images" / storage_path
    )
    image_file.parent.mkdir(parents=True)
    image_file.write_bytes(b"\x89PNG\r\n\x1a\nnot-really")
    _say("What is in this?", (ImageInput(storage_path=storage_path),))
    sent: list[tuple[ConversationRequest, object]] = []

    async def send(
        request: ConversationRequest, _sink: object, before: object, images: object
    ) -> object:
        sent.append((request, dict(images)))  # type: ignore[call-overload]
        reply = LlmToolCall(
            call_id="r",
            name="reply",
            arguments={"text": "A logo.", "quote_item_id": None, "cards": []},
        )
        return _Reply(
            LlmActionTurnResponse(mode="action_turn", messages=[], calls=[reply])
        )

    plan = plan_chat_turn(user_id=USER, retry_of=None)
    assert plan is not None
    await run_chat_turn(plan, send=send, tools=(), window=ChatWindow.fresh())  # type: ignore[arg-type]

    request, images = sent[0]
    blocks = [
        block
        for item in request.conversation
        for block in getattr(item, "content", [])
        if isinstance(block, LlmInputImageBlock)
    ]
    assert len(blocks) == 1 and request.media_refs == (blocks[0].image.application_ref,)
    assert (
        isinstance(images, dict)
        and images[request.media_refs[0]]["storage_path"] == storage_path
    )
    assert _kinds() == ["user_message", "assistant_message"]


async def test_the_research_tools_read_and_leave_nothing_behind(owner: Path) -> None:
    tools = await chat_research_tools(
        db_path=owner, busy_timeout_ms=1_000, user_id=USER, run_id="a0"
    )
    names = {tool.name for tool in tools}
    spill = chat_tool_results_root(db_path=owner, run_id="a0")

    assert {"read", "list", "glob", "grep", "render_pdf_page", "memory_search"} <= names
    assert {"memory_sql", "web_search", "zanei_timeline"} <= names
    assert not names & {"apply_patch", "bash", "run_python", "start_action"}
    assert spill.is_dir()
    discard_chat_tool_results(db_path=owner, run_id="a0")
    assert not spill.exists()


class _Reply:
    def __init__(self, response: LlmActionTurnResponse) -> None:
        self.response = response
        self.provider_turn = None

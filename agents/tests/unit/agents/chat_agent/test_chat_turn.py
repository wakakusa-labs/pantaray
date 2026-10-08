"""What one chat turn reads, sends and appends, over a migrated chat store."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.chat_agent import turn as chat_turn
from pantaray_agents.agents.chat_agent.context import ChatWindow
from pantaray_agents.agents.chat_agent.tools import chat_tools
from pantaray_agents.agents.chat_agent.turn import (
    ChatTurnInterrupted,
    plan_chat_turn,
    run_chat_turn,
)
from pantaray_agents.agents.core.mixins.llm_usage import TokenSink
from pantaray_agents.conversation.loop import REPAIR_MAX_ATTEMPTS, ConversationRequest
from pantaray_agents.local_runtime.chat.store import (
    append_chat_item,
    chat_turn_end_message_id,
    read_chat_items_after,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatItem,
    TurnFailureContent,
    UserMessageContent,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
)
from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
    LlmCommentary,
)
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall
from pantaray_llm.errors import (
    PROXY_LLM_TOOL_CALL_INVALID,
    PROXY_UPSTREAM_UNAVAILABLE,
    LlmProxyExecutionError,
)

USER = "user-1"
_CALL_IDS = itertools.count()
type _Step = _Reply | BaseException | Callable[[], _Reply]


@pytest.fixture(autouse=True)
def owner() -> Iterator[None]:
    register_logged_out_owner(USER)
    yield
    reset_logged_out_owner()


@dataclass(frozen=True, slots=True)
class _Reply:
    response: LlmActionTurnResponse
    provider_turn: LlmProviderTurn | None = None


def _reply_call(text: str = "Done.", **arguments: JSONValue) -> _Reply:
    args: dict[str, JSONValue] = {"text": text, "quote_item_id": None, "cards": []}
    call_id = f"r{next(_CALL_IDS)}"
    return _turn(LlmToolCall(call_id=call_id, name="reply", arguments=args | arguments))


def _turn(*calls: LlmToolCall, text: str = "") -> _Reply:
    messages = (
        [LlmCommentary(phase="commentary", source_message_id="m", text=text)]
        if text
        else []
    )
    return _Reply(
        LlmActionTurnResponse(mode="action_turn", messages=messages, calls=list(calls))
    )


def _say(message_id: str, text: str) -> ChatItem:
    return append_chat_item(
        user_id=USER,
        message_id=message_id,
        content=UserMessageContent(
            kind="user_message", text=text, quote_item_id=None, images=(), files=()
        ),
    )


def _items() -> list[ChatItem]:
    return list(read_chat_items_after(user_id=USER, after=0))


def _texts(items: Sequence[LlmTurnItem]) -> list[str]:
    texts: list[str] = []
    for item in items:
        if isinstance(item, LlmTurnUserItem):
            block = item.content[0]
            assert isinstance(block, LlmInputTextBlock)
            texts.append(block.text)
        elif isinstance(item, LlmTurnAssistantItem):
            texts.extend(item.text)
    return texts


def _tool(name: str, run: Callable[[], None]) -> ReactToolDefinition:
    async def execute(_call: ReactToolCall, _step: int) -> ReactToolResult:
        run()
        return ReactToolResult(tool_name=name, status="success", output={"ok": True})

    return ReactToolDefinition(
        name=name,
        description=name,
        request_schema={"type": "object"},
        response_schema=react_tool_response_schema(
            success_schema={"type": "object", "required": ["ok"]}
        ),
        execute=execute,
    )


@dataclass
class _Model:
    script: list[_Step]
    requests: list[ConversationRequest] = field(default_factory=list)

    async def send(
        self,
        request: ConversationRequest,
        _sink: TokenSink,
        before_attempt: Callable[[], None],
        _user_images: object,
    ) -> _Reply:
        before_attempt()
        # What the real client builds: an unanswered or doubled call fails here.
        LlmActionTurnRequest(
            mode="action_turn",
            tools=list(request.tools),
            conversation=request.conversation,
        )
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step() if callable(step) else step


async def _run(
    model: _Model,
    *,
    tools: tuple[ReactToolDefinition, ...] = (),
    retry_of: str | None = None,
) -> ChatWindow:
    plan = plan_chat_turn(user_id=USER, retry_of=retry_of)
    assert plan is not None
    return await run_chat_turn(
        plan, send=model.send, tools=tools, window=ChatWindow.fresh()
    )


async def test_reply_ends_the_turn_with_its_quote_and_card() -> None:
    asked = _say("m-1", "Status of the report?")
    db_path = Path(os.environ["LOCAL_DB_PATH"])
    insert_agent_action(db_path=db_path, user_id=USER, action_id="act-1")
    card = {"kind": "action", "action_id": "act-1", "summary": "Q3 report"}
    model = _Model(
        [_reply_call("It is running.", quote_item_id=asked.item_id, cards=[card])]
    )

    await _run(model)

    reply = _items()[-1].content
    assert isinstance(reply, AssistantMessageContent)
    assert (reply.text, reply.quote_item_id) == ("It is running.", asked.item_id)
    assert [c.model_dump() for c in reply.cards] == [card]
    assert plan_chat_turn(user_id=USER, retry_of=None) is None


async def test_a_reply_naming_what_the_user_lacks_is_sent_back() -> None:
    _say("m-1", "Hi")
    bad = {"kind": "action", "action_id": "nope", "summary": "x"}
    model = _Model([_reply_call(cards=[bad]), _reply_call("Hello!")])

    await _run(model)

    # The refusal answers the reply call, and the model replies again.
    assert "a card names an Action" in str(model.requests[1].conversation[-1])
    assert [item.content.kind for item in _items()] == [
        "user_message",
        "assistant_message",
    ]


async def test_text_beside_calls_is_shown_before_the_calls_run() -> None:
    _say("m-1", "Check my calendar")
    shown: list[list[str]] = []

    def look() -> None:
        shown.append([str(getattr(i.content, "text", "")) for i in _items()])

    model = _Model(
        [
            _turn(
                LlmToolCall(call_id="c", name="look", arguments={}), text="Checking."
            ),
            _reply_call("Free at 3."),
        ]
    )

    await _run(model, tools=(_tool("look", look),))

    assert shown == [["Check my calendar", "Checking."]]
    assert [getattr(i.content, "text", None) for i in _items()][-1] == "Free at 3."


async def test_a_reply_that_repeats_what_was_shown_is_sent_back_once() -> None:
    _say("m-1", "Check my calendar")
    look = LlmToolCall(call_id="c", name="look", arguments={})
    model = _Model(
        [
            _turn(look, text="Checking your calendar."),
            _reply_call("Checking your calendar."),
            _reply_call("Free at 3."),
        ]
    )

    await _run(model, tools=(_tool("look", lambda: None),))

    # The user reads the line once, then the answer.
    assert [getattr(i.content, "text", None) for i in _items()] == [
        "Check my calendar",
        "Checking your calendar.",
        "Free at 3.",
    ]
    assert "already sees these words" in str(model.requests[2].conversation[-1])

    # Beside a reply alone nothing runs: the reply is all the user reads.
    _say("m-3", "Thanks")
    beside = _reply_call("You're welcome.")
    beside.response.messages.append(
        LlmCommentary(phase="commentary", source_message_id="m", text="You're welcome.")
    )
    await _run(_Model([beside]))
    assert [getattr(i.content, "text", None) for i in _items()][-2:] == [
        "Thanks",
        "You're welcome.",
    ]

    _say("m-2", "And tomorrow?")
    again = LlmToolCall(call_id="d", name="look", arguments={})
    stubborn = _Model(
        [
            _turn(again, text="Looking."),
            _reply_call("Looking."),
            _reply_call("Looking."),
        ]
    )
    await _run(stubborn, tools=(_tool("look", lambda: None),))
    # Sent back once only: a turn never fails over it.
    assert getattr(_items()[-1].content, "text", None) == "Looking."
    assert plan_chat_turn(user_id=USER, retry_of=None) is None


async def test_a_turn_whose_every_output_is_refused_logs_what_broke(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _say("m-1", "Hi")
    refused = LlmProxyExecutionError(
        error_code=PROXY_LLM_TOOL_CALL_INVALID,
        error_message="The output was a final answer in plain text: Hello there.",
        retryable=False,
        recovery="repair_next_turn",
        tool_call_violation_reason="invalid_response",
    )

    # A call id comes from the model's output too.
    reused = LlmToolCall(call_id="secret-id", name="look", arguments={})
    doubled = _turn(reused, LlmToolCall(call_id="secret-id", name="look", arguments={}))

    with caplog.at_level(logging.WARNING):
        await _run(
            _Model([refused] * (REPAIR_MAX_ATTEMPTS - 1) + [doubled]),
            tools=(_tool("look", lambda: None),),
        )

    failure = _items()[-1].content
    assert isinstance(failure, TurnFailureContent) and failure.reason == "llm_request"
    (logged,) = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "CHAT_TURN_FAILED" in r.getMessage()
    ]
    assert logged["error_class"] == "ConversationOutputInvalid"
    assert logged["violations"] == ["invalid_response"] * (REPAIR_MAX_ATTEMPTS - 1) + [
        "call_id_taken"
    ]
    # What was refused can quote the model's output: never logged.
    assert "Hello there" not in caplog.text
    assert "secret-id" not in caplog.text


async def test_an_answer_in_text_alone_is_sent_back_once() -> None:
    _say("m-1", "Hi")
    nudged = _Model([_turn(text="Hello there."), _reply_call("Hello!")])

    await _run(nudged)

    # The text never reached the user; only the reply did.
    assert [getattr(i.content, "text", None) for i in _items()] == ["Hi", "Hello!"]
    assert "only what you send with reply" in str(nudged.requests[1].conversation[-1])

    _say("m-2", "Again?")
    stubborn = _Model([_turn(text="Yes."), _turn(text="Yes, again.")])
    await _run(stubborn)
    assert getattr(_items()[-1].content, "text", None) == "Yes, again."
    assert plan_chat_turn(user_id=USER, retry_of=None) is None


async def test_items_that_arrive_mid_turn_are_read_before_the_next_call() -> None:
    _say("m-1", "Find the deck")
    model = _Model(
        [
            _turn(LlmToolCall(call_id="c", name="look", arguments={})),
            _reply_call("Found it, and noted the date."),
        ]
    )

    await _run(model, tools=(_tool("look", lambda: _say("m-2", "Also the date")),))

    assert any(
        "Also the date" in text for text in _texts(model.requests[1].conversation)
    )
    assert plan_chat_turn(user_id=USER, retry_of=None) is None


async def test_an_item_that_arrives_during_the_last_call_waits_for_the_next_turn() -> (
    None
):
    _say("m-1", "Hi")

    def late_reply() -> _Reply:
        _say("m-2", "Are you there?")
        return _reply_call("Hello!")

    await _run(_Model([late_reply]))

    plan = plan_chat_turn(user_id=USER, retry_of=None)
    assert plan is not None
    following = _Model([_reply_call("Yes.")])
    await _run(following)
    waiting = _texts(following.requests[0].conversation)[-1]
    assert waiting.endswith(f"{_items()[1].item_id}.")  # only m-2 waits


async def test_a_failed_call_keeps_what_was_said_and_can_run_again() -> None:
    asked = _say("m-1", "Summarize my week")
    unavailable = LlmProxyExecutionError(
        error_code=PROXY_UPSTREAM_UNAVAILABLE,
        error_message="down",
        retryable=True,
    )
    look = LlmToolCall(call_id="c", name="look", arguments={})
    await _run(
        _Model([_turn(look, text="One moment."), unavailable]),
        tools=(_tool("look", lambda: None),),
    )

    *said, failure = _items()
    assert failure.content.model_dump() == {
        "kind": "turn_failure",
        "reason": "llm_connection",
    }
    assert [getattr(i.content, "text", None) for i in said] == [
        "Summarize my week",
        "One moment.",
    ]
    # Nothing waits any more, until the user asks to run it again.
    assert plan_chat_turn(user_id=USER, retry_of=None) is None
    assert plan_chat_turn(user_id=USER, retry_of="another") is None

    retried = _Model([_reply_call("Here it is.")])
    await _run(retried, retry_of=failure.item_id)

    assert _texts(retried.requests[0].conversation)[-1].endswith(f"{asked.item_id}.")
    assert plan_chat_turn(user_id=USER, retry_of=failure.item_id) is None


async def test_a_turn_out_of_calls_fails_on_the_step_limit() -> None:
    _say("m-1", "Loop")
    look = LlmToolCall(call_id="c", name="look", arguments={})
    # More than the turn may send: the loop grants a last turn that reached
    # for other tools one more try.
    model = _Model(
        [_turn(look.model_copy(update={"call_id": f"c{n}"})) for n in range(12)]
    )

    await _run(model, tools=(_tool("look", lambda: None),))

    assert _items()[-1].content.model_dump() == {
        "kind": "turn_failure",
        "reason": "step_limit",
    }


async def test_a_turn_the_app_quit_in_runs_again_from_what_it_said() -> None:
    _say("m-1", "Plan my trip")
    first_plan = plan_chat_turn(user_id=USER, retry_of=None)
    look = LlmToolCall(call_id="c", name="look", arguments={})
    quit_app = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _run(
            _Model([_turn(look, text="Let me look."), quit_app]),
            tools=(_tool("look", lambda: None),),
        )

    again = plan_chat_turn(user_id=USER, retry_of=None)
    assert again == first_plan
    resumed = _Model([_reply_call("Booked a plan.")])
    await _run(resumed)

    assert "Let me look." in _texts(resumed.requests[0].conversation)
    assert [getattr(i.content, "text", None) for i in _items()] == [
        "Plan my trip",
        "Let me look.",
        "Booked a plan.",
    ]


async def test_a_turn_stops_unanswered_when_the_route_changes_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Hi")
    _switch_routes(monkeypatch, "first", "second")
    model = _Model([_reply_call("Hello!")])

    with pytest.raises(ChatTurnInterrupted):
        await _run(model)

    # Nothing reached the model under the new route, and the message still waits.
    assert model.requests == []
    assert plan_chat_turn(user_id=USER, retry_of=None) is not None


async def test_a_turn_planned_before_the_owner_changed_reads_and_sends_nothing() -> (
    None
):
    _say("m-1", "Hi")
    plan = plan_chat_turn(user_id=USER, retry_of=None)
    assert plan is not None
    register_logged_out_owner("someone-else")
    model = _Model([_reply_call("Hello!")])

    with pytest.raises(ChatTurnInterrupted):
        await run_chat_turn(plan, send=model.send, tools=(), window=ChatWindow.fresh())

    assert model.requests == []


async def test_calls_that_come_back_after_the_route_changed_do_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Find the deck")
    _switch_routes(monkeypatch, "first", "first", "second")
    ran: list[str] = []
    look = LlmToolCall(call_id="c", name="look", arguments={})

    with pytest.raises(ChatTurnInterrupted):
        await _run(
            _Model([_turn(look, text="Looking.")]),
            tools=(_tool("look", lambda: ran.append("look")),),
        )

    assert ran == []
    assert [getattr(i.content, "text", None) for i in _items()] == ["Find the deck"]


async def test_a_retry_reads_waiting_items_the_window_had_passed() -> None:
    asked = _say("m-1", "Summarize my week")
    failure = append_chat_item(
        user_id=USER,
        message_id=chat_turn_end_message_id("a0", "failure", asked.sequence),
        content=TurnFailureContent(kind="turn_failure", reason="llm_connection"),
    )
    plan = plan_chat_turn(user_id=USER, retry_of=failure.item_id)
    assert plan is not None
    model = _Model([_reply_call("Here it is.")])
    passed = replace(ChatWindow.fresh(), after=failure.sequence)

    await run_chat_turn(plan, send=model.send, tools=(), window=passed)

    assert any("Summarize my week" in t for t in _texts(model.requests[0].conversation))
    assert getattr(_items()[-1].content, "text", None) == "Here it is."


def _switch_routes(monkeypatch: pytest.MonkeyPatch, *routes: str) -> None:
    """The route reads ``routes`` in turn, the last one from then on."""

    reads = itertools.chain(routes, itertools.repeat(routes[-1]))
    monkeypatch.setattr(chat_turn, "read_route_inputs", lambda: next(reads))
    monkeypatch.setattr(
        chat_turn,
        "effective_route_identity",
        lambda inputs: SimpleNamespace(owner_id=USER, llm=inputs),
    )


async def test_a_transport_retry_never_goes_out_on_a_changed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Hi")
    _switch_routes(monkeypatch, "first", "first", "second")
    attempts: list[object] = []

    async def generate_content(**_kwargs: object) -> object:
        attempts.append(_kwargs)
        raise LlmProxyExecutionError(
            error_code=PROXY_UPSTREAM_UNAVAILABLE, error_message="down", retryable=True
        )

    async def no_backoff(_attempt: int) -> None:
        return None

    model = chat_turn.ChatModel(
        client=SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)
            )
        )
    )
    monkeypatch.setattr(model, "_sleep_llm_retry_backoff", no_backoff)

    with pytest.raises(ChatTurnInterrupted):
        await _run(SimpleNamespace(send=model.send))  # type: ignore[arg-type]

    assert len(attempts) == 1
    assert plan_chat_turn(user_id=USER, retry_of=None) is not None


async def test_a_send_that_fails_after_the_route_changed_leaves_no_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Hi")
    _switch_routes(monkeypatch, "first", "first", "second")
    failed = LlmProxyExecutionError(
        error_code=PROXY_UPSTREAM_UNAVAILABLE, error_message="down", retryable=True
    )

    with pytest.raises(ChatTurnInterrupted):
        await _run(_Model([failed]))

    assert [i.content.kind for i in _items()] == ["user_message"]
    assert plan_chat_turn(user_id=USER, retry_of=None) is not None


async def test_calls_do_not_run_when_the_route_changes_while_the_turn_speaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Find the deck")
    # Started, sent, and the reply landed on the first route; then it changed.
    _switch_routes(monkeypatch, "first", "first", "first", "second")
    ran: list[str] = []
    look = LlmToolCall(call_id="c", name="look", arguments={})

    with pytest.raises(ChatTurnInterrupted):
        await _run(
            _Model([_turn(look, text="Looking.")]),
            tools=(_tool("look", lambda: ran.append("look")),),
        )

    assert ran == []
    assert plan_chat_turn(user_id=USER, retry_of=None) is not None


async def test_a_route_change_during_one_call_stops_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _say("m-1", "Do both")
    routes = ["first"]
    monkeypatch.setattr(chat_turn, "read_route_inputs", lambda: routes[0])
    monkeypatch.setattr(
        chat_turn,
        "effective_route_identity",
        lambda inputs: SimpleNamespace(owner_id=USER, llm=inputs),
    )
    ran: list[str] = []

    def first() -> None:
        ran.append("one")
        routes[0] = "second"  # the account changes while this call runs

    calls = [LlmToolCall(call_id=f"c{n}", name=n, arguments={}) for n in ("one", "two")]

    with pytest.raises(ChatTurnInterrupted):
        await _run(
            _Model([_turn(*calls)]),
            tools=(_tool("one", first), _tool("two", lambda: ran.append("two"))),
        )

    assert ran == ["one"]


async def test_a_retried_turn_never_starts_a_task_twice() -> None:
    _say("m-1", "Draft the Q3 report")
    with sqlite3.connect(os.environ["LOCAL_DB_PATH"]) as connection:
        ensure_user_row(connection, user_id=USER, timestamp="2026-10-08T00:00:00Z")
    start = LlmToolCall(
        call_id="s1",
        name="start_action",
        arguments={"relay": [], "note": "Draft the Q3 report"},
    )
    unavailable = LlmProxyExecutionError(
        error_code=PROXY_UPSTREAM_UNAVAILABLE, error_message="down", retryable=True
    )

    async def run(model: _Model, retry_of: str | None = None) -> None:
        plan = plan_chat_turn(user_id=USER, retry_of=retry_of)
        assert plan is not None
        await run_chat_turn(
            plan, send=model.send, tools=chat_tools(plan), window=ChatWindow.fresh()
        )

    # The task starts, then the model fails before the turn can answer.
    await run(_Model([_turn(start), unavailable]))
    failure = _items()[-1]
    # The user retries; the model starts the same task again.
    again = start.model_copy(update={"call_id": "s2"})
    await run(_Model([_turn(again), _reply_call("On it.")]), retry_of=failure.item_id)

    with sqlite3.connect(os.environ["LOCAL_DB_PATH"]) as connection:
        actions = connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()
    assert actions == (1,)
    assert getattr(_items()[-1].content, "text", None) == "On it."


async def test_two_starts_in_one_breath_start_one_task() -> None:
    _say("m-1", "Draft the Q3 report")
    with sqlite3.connect(os.environ["LOCAL_DB_PATH"]) as connection:
        ensure_user_row(connection, user_id=USER, timestamp="2026-10-08T00:00:00Z")
    starts = [
        LlmToolCall(
            call_id=f"s{n}",
            name="start_action",
            arguments={"relay": [], "note": "Draft the Q3 report"},
        )
        for n in range(2)
    ]
    plan = plan_chat_turn(user_id=USER, retry_of=None)
    assert plan is not None
    model = _Model([_turn(*starts), _reply_call("On it.")])

    await run_chat_turn(
        plan, send=model.send, tools=chat_tools(plan), window=ChatWindow.fresh()
    )

    with sqlite3.connect(os.environ["LOCAL_DB_PATH"]) as connection:
        actions = connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()
    assert actions == (1,)
    # The second is answered as not run, so the model sees it before resending.
    assert "Not run" in str(model.requests[1].conversation[-1])

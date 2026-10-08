"""What the shared conversation loop sends, runs and answers, turn after turn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.loop import (
    ENDING_REJECTED_ERROR_CODE,
    NOT_RUN_ERROR_CODE,
    REPAIR_MAX_ATTEMPTS,
    Continue,
    ConversationEntry,
    ConversationOutputInvalid,
    ConversationRequest,
    ConversationRun,
    ConversationTurnsExhausted,
    Finish,
    IdleTurn,
    RecordedTurn,
    run_conversation,
)
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    ToolTurnPlacement,
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
    LlmTurnToolResultItem,
    LlmTurnUserItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_LLM_TOOL_CALL_INVALID,
    LlmProxyExecutionError,
)

_FINISH = LlmToolDefinition(
    name="finish", description="End.", parameters={"type": "object"}
)
type _Step = _Reply | BaseException
type _Tools = Sequence[tuple[str, ToolTurnPlacement]]


@pytest.fixture(autouse=True)
def _cloud_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cloud route names a provider-turn identity with no stored connection.
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "cloud")


@dataclass(frozen=True, slots=True)
class _Reply:
    response: LlmActionTurnResponse
    provider_turn: LlmProviderTurn | None


def _call(call_id: str, name: str, **arguments: str) -> LlmToolCall:
    return LlmToolCall(call_id=call_id, name=name, arguments=dict(arguments))


def _end(call_id: str, answer: str = "done") -> LlmToolCall:
    return _call(call_id, "finish", answer=answer)


def _reply(*calls: LlmToolCall, text: str = "", dropped: Sequence[str] = ()) -> _Reply:
    return _Reply(
        response=LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(phase="commentary", source_message_id="m", text=text)
            ]
            if text
            else [],
            calls=list(calls),
            dropped_call_names=list(dropped),
        ),
        provider_turn=OpenAiProviderTurn(
            provider="openai",
            items=[
                {"type": "reasoning", "id": "rs", "encrypted_content": "x"},
                *(
                    {"type": "function_call", "call_id": c.call_id, "name": c.name}
                    for c in calls
                ),
            ],
        ),
    )


def _refused(*, stop: bool = False) -> LlmProxyExecutionError:
    return LlmProxyExecutionError(
        error_code=PROXY_LLM_TOOL_CALL_INVALID,
        error_message="Action commentary outside the commentary character limit.",
        retryable=False,
        recovery="stop" if stop else "repair_next_turn",
        tool_call_violation_reason="invalid_response",
    )


def _user(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


def _text(item: LlmTurnItem) -> str:
    assert isinstance(item, LlmTurnUserItem)
    block = item.content[0]
    assert isinstance(block, LlmInputTextBlock)
    return block.text


def _answer_on_end(turn: IdleTurn) -> Finish[str] | Continue:
    if turn.ending_call is not None:
        return Finish(str(turn.ending_call.arguments["answer"]))
    return Continue("Call finish to end the run.")


class _Pause(Exception):
    pass


def _tool(
    name: str, placement: ToolTurnPlacement, log: list[str]
) -> ReactToolDefinition:
    async def execute(_call: ReactToolCall, _step: int) -> ReactToolResult:
        log.append(f"start {name}")
        await asyncio.sleep(0)
        if name == "boom":
            raise _Pause
        if name == "slow":
            await asyncio.Event().wait()
        log.append(f"end {name}")
        return ReactToolResult(tool_name=name, status="success", output={"ran": name})

    schema: dict[str, JSONValue] = {
        "required": ["ran"],
        "additionalProperties": False,
        "properties": {"ran": {}},
    }
    return ReactToolDefinition(
        name=name,
        description=name,
        request_schema={"type": "object"},
        response_schema=react_tool_response_schema(success_schema=schema),
        execute=execute,
        concurrency=ToolConcurrency(placement),
    )


@dataclass
class _Run:
    """A scripted model and a caller that keeps what the loop reports."""

    script: Sequence[_Step]
    decide: Callable[[IdleTurn], Finish[str] | Continue] = _answer_on_end
    tools: _Tools = (("a", "parallel"),)
    log: list[str] = field(default_factory=list)
    history: list[ConversationEntry] = field(
        default_factory=lambda: [ConversationEntry(_user("Do the task."))]
    )
    arriving: list[list[LlmTurnItem]] = field(default_factory=list)
    hold: bool = False  # recording c1 waits for the gate
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    store: ProviderTurnStore = field(default_factory=lambda: ProviderTurnStore(None))
    requests: list[ConversationRequest] = field(default_factory=list)
    turns: list[RecordedTurn] = field(default_factory=list)
    results: dict[str, LlmTurnToolResultItem] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    idle: list[IdleTurn] = field(default_factory=list)

    async def send(self, request: ConversationRequest) -> _Reply:
        # What the real client builds: a call answered twice fails here.
        LlmActionTurnRequest(
            mode="action_turn",
            tools=list(request.tools),
            max_parallel_tool_calls=request.max_parallel_tool_calls,
            conversation=request.conversation,
        )
        self.requests.append(request)
        step = self.script[len(self.requests) - 1]
        if isinstance(step, BaseException):
            raise step
        return step

    async def before_send(self) -> list[LlmTurnItem]:
        return self.arriving.pop(0) if self.arriving else []

    async def on_turn(self, turn: RecordedTurn) -> None:
        self.turns.append(turn)

    async def on_result(
        self, call: LlmToolCall, result: ReactToolResult
    ) -> LlmTurnToolResultItem:
        assert call.call_id not in self.results, "answered twice"
        if self.hold and call.call_id == "c1":
            self.log.append("recording c1")
            await self.gate.wait()
        ids = call.model_dump(include={"call_id", "name"})
        item = LlmTurnToolResultItem(type="tool_result", output=result.output, **ids)
        self.results[call.call_id] = item
        return item

    async def on_notice(self, item: LlmTurnUserItem) -> None:
        self.notices.append(_text(item))

    def judge(self, turn: IdleTurn) -> Finish[str] | Continue:
        self.idle.append(turn)
        return self.decide(turn)

    async def __call__(self, *, max_turns: int = 8) -> str:
        return await run_conversation(
            ConversationRun(
                prompt="head",
                system_instruction="system",
                tools=tuple(_tool(name, kind, self.log) for name, kind in self.tools),
                ending_tools=(_FINISH,),
                history=self.history,
                provider_turns=self.store,
                inference_profile="profile-a",
                max_turns=max_turns,
                max_tool_calls=10,
                max_parallel_tool_calls=4,
                send=self.send,
                before_send=self.before_send,
                on_turn=self.on_turn,
                on_result=self.on_result,
                on_notice=self.on_notice,
                decide=self.judge,
            )
        )

    def code(self, call_id: str) -> JSONValue:
        output = self.results[call_id].output
        assert isinstance(output, dict)
        return output.get("error_code")


async def test_a_turn_without_calls_carries_on_or_ends_as_the_caller_says() -> None:
    def decide(turn: IdleTurn) -> Finish[str] | Continue:
        if turn.response.messages[0].text == "Thinking.":
            return Continue("Call finish to end the run.")
        return Finish("accepted")

    run = _Run([_reply(text="Thinking."), _reply(text="All done.")], decide=decide)

    assert await run() == "accepted"
    assert [turn.ending_call for turn in run.idle] == [None, None]
    assert run.turns[0].entry.item == LlmTurnAssistantItem(
        type="assistant", text=["Thinking."], calls=[]
    )
    # The notice is kept, so the next request does not end on the model's turn.
    assert run.notices == ["Call finish to end the run."]
    assert _text(run.requests[1].conversation[-1]) == run.notices[0]


async def test_calls_the_plan_holds_back_are_answered_as_not_run() -> None:
    tools: _Tools = [("solo", "solo_turn"), ("a", "parallel")]
    # A solo call runs alone, and an ending call among others never ends.
    first = _reply(_call("c1", "solo"), _call("c2", "a"), _end("c3"), dropped=["b"])
    run = _Run([first, _reply(_end("c4"))], tools=tools)

    await run()

    assert run.log == ["start solo", "end solo"]
    assert [run.code(call_id) for call_id in ("c2", "c3")] == [NOT_RUN_ERROR_CODE] * 2
    # A call the provider dropped has no id to answer, so a notice names it.
    assert run.notices[0].startswith("# System Notice\nNot run this turn: b (")
    assert _text(run.requests[1].conversation[-1]) == run.notices[0]
    # A turn that ran a tool is not the caller's to judge.
    assert len(run.idle) == 1


async def test_a_refused_output_is_resent_as_an_append_and_not_kept() -> None:
    script: list[_Step] = [_refused(), _reply(_call("c1", "a")), _reply(_end("c2"))]
    run = _Run(script)

    await run()

    refused, repaired, after = run.requests
    assert repaired.conversation[:-1] == refused.conversation
    assert "commentary character limit" in _text(repaired.conversation[-1])
    # The notice closes that send alone, and the turn produced behind it was
    # kept with the notice in its prefix, so it never goes back.
    assistant = after.conversation[1]
    assert isinstance(assistant, LlmTurnAssistantItem)
    assert assistant.provider_turn is None


async def test_a_turn_refused_on_every_send_fails_the_run_with_nothing_run() -> None:
    # A reused call id is refused like an output the provider refused.
    reused = _reply(_call("c1", "a"), _call("c1", "a"))
    run = _Run([_refused(), *[reused] * (REPAIR_MAX_ATTEMPTS - 1)])

    with pytest.raises(ConversationOutputInvalid) as raised:
        await run()

    assert len(raised.value.args) == len(run.requests) == REPAIR_MAX_ATTEMPTS
    assert "'c1' is already taken" in raised.value.args[-1]
    assert run.log == []


async def test_the_last_turn_keeps_the_tools_and_runs_only_the_end() -> None:
    last = _reply(_call("c2", "a"), _end("c3"))
    run = _Run([_reply(_call("c1", "a")), last])

    assert await run(max_turns=2) == "done"

    assert run.requests[1].tools == run.requests[0].tools
    assert "This is the last turn" in _text(run.requests[1].conversation[-1])
    assert run.log == ["start a", "end a"]
    assert run.code("c2") == NOT_RUN_ERROR_CODE
    assert run.idle[0].final


async def test_a_refusal_on_the_last_turn_is_answered_before_the_run_fails() -> None:
    first = _Run([_reply(_end("c1", "draft"))], decide=lambda _: Continue("Not yet."))

    with pytest.raises(ConversationTurnsExhausted):
        await first(max_turns=1)

    assert first.code("c1") == ENDING_REJECTED_ERROR_CODE
    # What the hooks were given rebuilds the history, and a next run sends it.
    stored = [
        *first.history,
        first.turns[0].entry,
        ConversationEntry(first.results["c1"]),
    ]
    resumed = _Run([_reply(_end("c2"))], history=stored, store=first.store)
    assert await resumed() == "done"
    assert resumed.requests[0].conversation == [entry.item for entry in stored]


async def test_items_that_arrive_mid_run_go_before_the_next_send() -> None:
    run = _Run(
        [_reply(_call("c1", "a")), _reply(_end("c2"))],
        arriving=[[], [_user("Also check b.")]],
    )

    await run()

    *_, result, arrived = run.requests[1].conversation
    assert (result, _text(arrived)) == (run.results["c1"], "Also check b.")


async def test_every_send_extends_the_last_and_hands_its_turn_back() -> None:
    script = [_reply(_call("c1", "a")), _reply(_call("c2", "a")), _reply(_end("c3"))]
    run = _Run(script)

    await run()

    for earlier, later in zip(run.requests, run.requests[1:], strict=False):
        assert later.conversation[: len(earlier.conversation)] == earlier.conversation
    # Each turn is kept with the prefix it was produced behind, and goes back
    # only because every later request still lays that prefix out the same.
    for turn, request in zip(run.turns, run.requests, strict=True):
        assert turn.provider_turn is not None
        assert turn.provider_turn.fingerprint == request.fingerprint
    assert [
        item.provider_turn
        for item in run.requests[-1].conversation
        if isinstance(item, LlmTurnAssistantItem)
    ] == [turn.reply.provider_turn for turn in run.turns[:2]]


async def test_a_refused_replay_is_resent_once_without_the_turns() -> None:
    refusal = LlmProxyExecutionError(
        error_code=PROXY_INVALID_INPUT, error_message="bad", retryable=False
    )
    script: list[_Step] = [_reply(_call("c1", "a")), refusal, _reply(_end("c2"))]
    run = _Run(script)

    await run()

    sent = [request.conversation[1] for request in run.requests[1:]]
    assert [
        isinstance(item, LlmTurnAssistantItem) and item.provider_turn is not None
        for item in sent
    ] == [True, False]
    # Only the turn produced after the refusal is kept to go back.
    assert list(run.store.turns) == [run.turns[1].entry.turn_id]


async def test_a_stop_from_the_model_propagates_as_raised() -> None:
    stopped = _refused(stop=True)
    run = _Run([stopped])
    with pytest.raises(LlmProxyExecutionError) as raised:
        await run()
    assert raised.value is stopped
    assert len(run.requests) == 1


@pytest.mark.parametrize(
    ("kind", "stopper", "answers"),
    # Per call c1..c4: R ran, N answered as not run, - no result.
    [
        ("parallel", "boom", "R-RN"),
        ("sequential", "boom", "R-NN"),
        ("parallel", "slow", "RNRN"),
        ("sequential", "slow", "RNNN"),
        ("parallel", "rec", "RRRN"),
        ("sequential", "rec", "RNNN"),
    ],
)
async def test_a_stopped_batch_answers_every_call_that_did_not_finish(
    kind: ToolTurnPlacement, stopper: str, answers: str
) -> None:
    calls = (_call("c1", "a"), _call("c2", stopper), _call("c3", "b"), _end("c4"))
    tools: _Tools = [("a", kind), (stopper, kind), ("b", kind)]
    run = _Run([_reply(*calls)], tools=tools, hold=stopper == "rec")
    task = asyncio.create_task(run())
    if stopper != "boom":
        # The user stops the run, twice, while the slow call runs or while c1
        # is being recorded.
        mark = "start slow" if stopper == "slow" else "recording c1"
        parallel_slow = (stopper, kind) == ("slow", "parallel")
        while mark not in run.log or (parallel_slow and "end b" not in run.log):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        run.gate.set()

    with pytest.raises(_Pause if stopper == "boom" else asyncio.CancelledError):
        await task

    # A parallel batch ran b beside the slow call; a sequential one never
    # started it. A call that raised by itself gets no result (a paused one is
    # the resumed run's to settle); every other call is answered once.
    assert {call_id: run.code(call_id) for call_id in run.results} == {
        f"c{n}": None if answer == "R" else NOT_RUN_ERROR_CODE
        for n, answer in enumerate(answers, start=1)
        if answer != "-"
    }

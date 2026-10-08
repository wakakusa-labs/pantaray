"""What a child's history rows rebuild, and how a resumed run carries on from them."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.tasks.test_action_subagent_job import _running_child

from pantaray_agents.conversation import provider_turns
from pantaray_agents.conversation.loop import (
    NOT_RUN_ERROR_CODE,
    Continue,
    ConversationEntry,
    ConversationRequest,
    ConversationRun,
    Finish,
    IdleTurn,
    RecordedTurn,
    run_conversation,
)
from pantaray_agents.conversation.provider_turns import ProviderTurnStore
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ActionSubagentMessageRequest,
    send_action_subagent_message,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_history import (
    SubagentHistory,
    SubagentHistoryWriter,
    load_subagent_history,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmProviderTurn,
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

_TASK = "# Assigned Task\nInspect the boundary"
_REPORT = LlmToolDefinition(
    name="submit_subagent_report", description="End.", parameters={"type": "object"}
)


@dataclass(frozen=True, slots=True)
class _Reply:
    response: LlmActionTurnResponse
    provider_turn: LlmProviderTurn | None


@dataclass(frozen=True, slots=True)
class _Totals:
    prompt_tokens: int
    fields: dict[str, int]


class _Stop(Exception):
    """The run stopping mid-send, before it recorded the turn."""


def _reply(*calls: LlmToolCall, text: str = "") -> _Reply:
    return _Reply(
        response=LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(phase="commentary", source_message_id="m", text=text)
            ]
            if text
            else [],
            calls=list(calls),
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


def _look() -> ReactToolDefinition:
    async def execute(call: ReactToolCall, _step: int) -> ReactToolResult:
        return ReactToolResult(
            tool_name="look", status="success", output={"saw": call.tool_args["path"]}
        )

    return ReactToolDefinition(
        name="look",
        description="Look at a path.",
        request_schema={"type": "object"},
        response_schema=react_tool_response_schema(
            success_schema={"type": "object", "required": ["saw"]}
        ),
        execute=execute,
        concurrency=ToolConcurrency("parallel"),
    )


def _message(db_path: Path, message_id: str, content: str) -> None:
    send_action_subagent_message(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ActionSubagentMessageRequest(
            "user-1",
            "action-1",
            "parent-process",
            "parent-job",
            "child-process",
            message_id,
            content,
        ),
    )


def _load(db_path: Path) -> SubagentHistory:
    return load_subagent_history(
        db_path=db_path,
        busy_timeout_ms=1_000,
        process_id="child-process",
        assigned_task=_TASK,
        window_tokens=1_000_000,
    )


async def _run(
    writer: SubagentHistoryWriter,
    history: SubagentHistory,
    script: Sequence[_Reply | Exception],
    *,
    sent: list[ConversationRequest],
    recorded: list[RecordedTurn],
    on_send: dict[int, str],
    db_path: Path,
) -> str:
    async def send(request: ConversationRequest) -> _Reply:
        sent.append(request)
        if len(sent) in on_send:
            # A parent message that lands while this turn is in flight.
            _message(db_path, f"m{len(sent)}", on_send[len(sent)])
        step = script[len(sent) - 1]
        if isinstance(step, Exception):
            raise step
        return step

    async def on_turn(turn: RecordedTurn) -> None:
        recorded.append(turn)
        await writer.on_turn(turn)

    def decide(turn: IdleTurn) -> Finish[str] | Continue:
        if turn.ending_call is None:
            return Continue("Call submit_subagent_report alone to end.")
        return Finish(str(turn.ending_call.arguments["report"]))

    identity, _ = provider_turns.read_provider_turn_target(inference_profile="p")
    return await run_conversation(
        ConversationRun(
            prompt="parent context",
            system_instruction="system",
            tools=(_look(),),
            ending_tools=(_REPORT,),
            history=await writer.answer_unanswered(history),
            provider_turns=ProviderTurnStore(identity, dict(history.turns)),
            inference_profile="p",
            max_turns=8,
            max_tool_calls=8,
            max_parallel_tool_calls=4,
            window=history.window,
            # Every send reads more input, which the window calibrates on.
            usage=lambda: _Totals(1_000 * len(sent), {}),
            send=send,
            before_send=writer.before_send,
            on_turn=on_turn,
            on_result=writer.on_result,
            on_notice=writer.on_notice,
            decide=decide,
        )
    )


@pytest.fixture
def child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ActionSubagentJobPayload]:
    # The cloud route names a provider-turn identity with no stored connection.
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "cloud")
    return _running_child(tmp_path, profile_id=SUBAGENT_MODEL_SETTINGS[0].profile_id)


async def test_a_run_resumed_from_its_rows_sends_the_same_request_and_goes_on(
    child: tuple[Path, ActionSubagentJobPayload],
) -> None:
    db_path, payload = child
    writer = SubagentHistoryWriter(
        db_path=db_path, busy_timeout_ms=1_000, payload=payload
    )
    _message(db_path, "m0", "Before the first turn")
    look = [
        LlmToolCall(call_id=f"c{n}", name="look", arguments={"path": f"p{n}"})
        for n in (1, 2)
    ]
    first: list[ConversationRequest] = []
    recorded: list[RecordedTurn] = []
    with pytest.raises(_Stop):
        await _run(
            writer,
            _load(db_path),
            [_reply(*look), _reply(text="Thinking."), _Stop()],
            sent=first,
            recorded=recorded,
            on_send={2: "Mid-turn correction"},
            db_path=db_path,
        )

    history = _load(db_path)
    assert history.unanswered == ()
    assert history.window == recorded[-1].window
    resumed: list[ConversationRequest] = []
    report = LlmToolCall(
        call_id="c3", name="submit_subagent_report", arguments={"report": "done"}
    )
    assert (
        await _run(
            writer,
            history,
            [_reply(report)],
            sent=resumed,
            recorded=[],
            on_send={},
            db_path=db_path,
        )
        == "done"
    )

    # The send the stop cut short, rebuilt from the rows alone: the same items,
    # every provider turn back in its place, behind the same prefix.
    assert resumed[0].conversation == first[-1].conversation
    assert resumed[0].fingerprint == first[-1].fingerprint
    texts = [
        item.model_dump_json(exclude={"provider_turn"})
        for item in resumed[0].conversation
    ]
    # The message that landed mid-turn went in behind that turn's notice.
    assert [n for n, text in enumerate(texts) if "Mid-turn correction" in text] == [
        len(texts) - 1
    ]
    assert "Before the first turn" in texts[1]
    assert all(
        item.provider_turn is not None
        for item in resumed[0].conversation
        if isinstance(item, LlmTurnAssistantItem)
    )


async def test_calls_a_stopped_turn_left_are_answered_before_the_run_goes_on(
    child: tuple[Path, ActionSubagentJobPayload],
) -> None:
    db_path, payload = child
    writer = SubagentHistoryWriter(
        db_path=db_path, busy_timeout_ms=1_000, payload=payload
    )
    calls = [
        LlmToolCall(call_id="a1", name="look", arguments={"path": "a"}),
        LlmToolCall(call_id="e1", name="edit", arguments={"path": "b"}),
        LlmToolCall(call_id="e2", name="edit", arguments={"path": "c"}),
    ]
    item = LlmTurnAssistantItem(type="assistant", text=[], calls=calls)
    await writer.on_turn(
        RecordedTurn(
            entry=ConversationEntry(item, turn_id="t1"),
            reply=_reply(*calls),
            provider_turn=None,
            window=_load(db_path).window,
        )
    )
    done = ReactToolResult(tool_name="look", status="success", output={"ok": True})
    await writer.on_result(calls[0], done)
    # An answer written outside the loop names no call: it answers the first
    # call of its tool still waiting, the one that was running.
    writer.answer_waiting_call(
        "edit", ReactToolResult(tool_name="edit", status="success", output="patched")
    )

    history = _load(db_path)
    assert history.unanswered == (calls[2],)
    entries = await writer.answer_unanswered(history)

    answers = {
        entry.item.call_id: json.dumps(entry.item.output)
        for entry in entries
        if isinstance(entry.item, LlmTurnToolResultItem)
    }
    assert list(answers) == ["a1", "e1", "e2"]
    assert "patched" in answers["e1"]
    assert NOT_RUN_ERROR_CODE in answers["e2"] and "interrupted" in answers["e2"]
    # The answer is stored too, so the next rebuild has nothing left waiting.
    rebuilt = _load(db_path)
    assert rebuilt.unanswered == ()
    assert list(rebuilt.entries) == entries

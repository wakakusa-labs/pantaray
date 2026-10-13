"""The memory update harness on the shared conversation loop."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import ActionTurnReply
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink
from pantaray_agents.agents.memory_agent.agent import saved_results_for_run
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.conversation.loop import ConversationRequest, SendTurn
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolDefinition, ReactToolResult
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

_CALL_IDS = itertools.count()


def _reply(*calls: tuple[str, dict[str, JSONValue]], text: str = "") -> ActionTurnReply:
    return ActionTurnReply(
        response=LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(phase="commentary", source_message_id="m", text=text)
            ]
            if text
            else [],
            calls=[
                LlmToolCall(
                    call_id=f"call-{next(_CALL_IDS)}", name=name, arguments=args
                )
                for name, args in calls
            ],
        ),
        provider_turn=None,
    )


def _read_tool(text: str = "memory") -> ReactToolDefinition:
    async def execute(_call, _step_number: int) -> ReactToolResult:
        return ReactToolResult(
            tool_name="read_memory",
            status="success",
            output={"status": "success", "text": text},
        )

    return ReactToolDefinition(
        name="read_memory",
        description="Read memory.",
        request_schema={"type": "object"},
        response_schema={"type": "object"},
        execute=execute,
    )


def _run_input(
    tmp_path: Path,
    send: SendTurn,
    *,
    tools: tuple[ReactToolDefinition, ...] = (),
    memory_request_ids: tuple[str, ...] = (),
) -> MemoryFileEditorRunInput:
    tool_results_path = tmp_path / "tool-results"
    tool_results_path.mkdir(mode=0o700, exist_ok=True)
    save_result, read_saved_results = saved_results_for_run(
        tool_results_path=tool_results_path, db_path=tmp_path / "runtime.sqlite3"
    )
    return MemoryFileEditorRunInput(
        run_id="run-1",
        save_result=save_result,
        read_saved_results=read_saved_results,
        tool_definitions=tools,
        prompt="memory context",
        task="Update the draft.",
        system_instruction="system",
        send=send,
        usage=lambda: CountingSink().delta,
        max_turns=8,
        max_tool_calls=8,
        memory_request_ids=memory_request_ids,
    )


def _scripted(
    *steps: ActionTurnReply | Callable[[ConversationRequest], ActionTurnReply],
) -> tuple[SendTurn, list[ConversationRequest]]:
    queue = list(steps)
    requests: list[ConversationRequest] = []

    async def send(request: ConversationRequest) -> ActionTurnReply:
        requests.append(request)
        step = queue.pop(0)
        return step if isinstance(step, ActionTurnReply) else step(request)

    return send, requests


def _last_result(request: ConversationRequest) -> LlmTurnToolResultItem:
    item = request.conversation[-1]
    assert isinstance(item, LlmTurnToolResultItem)
    return item


@pytest.mark.asyncio
async def test_a_large_text_is_saved_and_read_on_from_its_hint(tmp_path: Path) -> None:
    large_text = "".join(
        f"line {number:05d} raw-tool-result-日本語🌊\n" for number in range(1_200)
    )
    read_back: list[str] = []

    def read_next(request: ConversationRequest) -> ActionTurnReply:
        result = _last_result(request).output
        assert isinstance(result, dict)
        if result.get("kind") == "file":
            read_back.append(str(result["content"]))
            offset, column = result["next_offset"], result["next_column"]
            path = result["path"]
        else:
            # The saved text keeps its place in the result, which says how to go on.
            assert result["status"] == "success"
            moved = result["text"]
            assert isinstance(moved, dict)
            read_back.append(str(moved["preview"]) + "\n")
            hint = str(moved["retry_hint"])
            path = hint.split("path=")[1].split(" ")[0]
            offset, column = int(hint.split("offset=")[1].split(",")[0]), 1
        if offset is None:
            return _reply(("completed", {}))
        return _reply(("read", {"path": path, "offset": offset, "column": column}))

    send, requests = _scripted(
        _reply(("read_memory", {"path": "facts.md"})), *([read_next] * 8)
    )

    applied = await run_memory_file_editor(
        _run_input(tmp_path, send, tools=(_read_tool(large_text),))
    )

    assert applied == ()
    assert "".join(read_back) == large_text
    assert all(large_text not in str(request.conversation) for request in requests)
    tool_names = {tool.name for tool in requests[0].tools}
    assert "read" in tool_names


@pytest.mark.asyncio
async def test_read_reaches_only_the_saved_results(tmp_path: Path) -> None:
    outside = tmp_path / "facts.md"
    outside.write_text("private", encoding="utf-8")
    send, requests = _scripted(
        _reply(("read", {"path": str(outside)})), _reply(("completed", {}))
    )

    await run_memory_file_editor(_run_input(tmp_path, send))

    refused = _last_result(requests[1]).output
    assert isinstance(refused, dict)
    assert refused["status"] == "error"
    assert "private" not in str(refused)


@pytest.mark.asyncio
async def test_completed_reports_the_requests_applied_after_a_rejected_report(
    tmp_path: Path,
) -> None:
    send, requests = _scripted(
        _reply(text="I updated the facts."),
        _reply(("completed", {"applied_memory_requests": "R2"})),
        _reply(("completed", {"applied_memory_requests": ["R2"]})),
    )

    applied = await run_memory_file_editor(
        _run_input(tmp_path, send, memory_request_ids=("R1", "R2"))
    )

    assert applied == ("R2",)
    completed = next(tool for tool in requests[0].tools if tool.name == "completed")
    assert completed.parameters["required"] == ["applied_memory_requests"]
    asked = requests[1].conversation[-1]
    assert isinstance(asked, LlmTurnUserItem)
    block = asked.content[0]
    assert isinstance(block, LlmInputTextBlock)
    assert "call `completed` alone" in block.text
    rejected = _last_result(requests[2]).output
    assert isinstance(rejected, dict)
    assert rejected["error_code"] == "COMPLETION_PRECONDITION_FAILED"

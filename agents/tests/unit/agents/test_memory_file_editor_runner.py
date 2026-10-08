"""The memory update harness on the shared conversation loop."""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import ActionTurnReply
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink
from pantaray_agents.agents.memory_file_editor import runner as memory_runner
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.agents.memory_file_editor.tool_result_store import (
    TOOL_RESULT_FETCH_TOOL_NAME,
)
from pantaray_agents.conversation.loop import ConversationRequest, SendTurn
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import ReactToolDefinition, ReactToolResult
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

_OPEN_TEST_DIRECTORY_FDS: list[int] = []
_CALL_IDS = itertools.count()


@pytest.fixture(autouse=True)
def _close_test_directory_fds() -> Iterator[None]:
    try:
        yield
    finally:
        while _OPEN_TEST_DIRECTORY_FDS:
            os.close(_OPEN_TEST_DIRECTORY_FDS.pop())


def _tool_result_directory(tmp_path: Path) -> int:
    path = tmp_path / "tool-results"
    path.mkdir(mode=0o700)
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _OPEN_TEST_DIRECTORY_FDS.append(directory_fd)
    return directory_fd


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
    directory_fd: int | None = None,
) -> MemoryFileEditorRunInput:
    return MemoryFileEditorRunInput(
        run_id="run-1",
        tool_result_directory_fd=(
            _tool_result_directory(tmp_path) if directory_fd is None else directory_fd
        ),
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
async def test_a_large_result_is_spilled_and_read_back_in_bounded_pages(
    tmp_path: Path,
) -> None:
    large_text = "raw-tool-result-日本語🌊\n" * 1_200
    fetched: list[str] = []

    def fetch_next(request: ConversationRequest) -> ActionTurnReply:
        result = _last_result(request).output
        assert isinstance(result, dict)
        if result.get("storage") == "run_tool_result_file":
            offset: JSONValue = 0
        else:
            fetched.append(str(result["content"]))
            offset = result["next_offset"]
        if not isinstance(offset, int):
            return _reply(("completed", {}))
        return _reply(
            (
                TOOL_RESULT_FETCH_TOOL_NAME,
                {"result_ref": result["result_ref"], "offset": offset},
            )
        )

    send, requests = _scripted(
        _reply(("read_memory", {"path": "facts.md"})), *([fetch_next] * 7)
    )

    applied = await run_memory_file_editor(
        _run_input(tmp_path, send, tools=(_read_tool(large_text),))
    )

    assert applied == ()
    assert "".join(fetched) == serialize_json_tool_output(
        {"output": {"status": "success", "text": large_text}, "error_message": None}
    )
    assert all(large_text not in str(request.conversation) for request in requests)
    assert TOOL_RESULT_FETCH_TOOL_NAME in {tool.name for tool in requests[0].tools}


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


@pytest.mark.asyncio
async def test_closing_the_store_never_deletes_through_a_replaced_workspace(
    tmp_path: Path,
) -> None:
    outside_user = tmp_path / "outside-user"
    with MemoryRunWorkspaceScope() as workspace_scope:
        workspace = workspace_scope.create(
            artifact_root=tmp_path / "artifacts", user_id="user-1", run_id="run-1"
        )
        original_user = workspace.root_path.parents[1]
        renamed_user = original_user.with_name("user-1-original")
        original_user.rename(renamed_user)
        original_user.symlink_to(outside_user, target_is_directory=True)
        victim = (
            outside_user
            / "workspaces"
            / workspace.root_path.name
            / "tool-results"
            / "victim"
        )
        victim.mkdir(parents=True)
        send, _ = _scripted(_reply(("completed", {})))

        await run_memory_file_editor(
            _run_input(
                tmp_path,
                send,
                directory_fd=workspace_scope.require_tool_results_fd(),
            )
        )

        assert victim.is_dir()

    assert victim.is_dir()
    assert tuple((renamed_user / "workspaces").iterdir()) == ()


@pytest.mark.asyncio
async def test_a_stopped_run_closes_the_store_only_after_its_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    projection_started = threading.Event()
    release_projection = threading.Event()
    projection_finished = threading.Event()
    close_finished = threading.Event()
    original_close = memory_runner.RunToolResultStore.close

    def blocking_projection(
        *, result: ReactToolResult, store: memory_runner.RunToolResultStore
    ) -> ReactToolResult:
        del store
        projection_started.set()
        assert release_projection.wait(timeout=5)
        projection_finished.set()
        return result

    def tracked_close(store: memory_runner.RunToolResultStore) -> None:
        assert projection_finished.is_set()
        original_close(store)
        close_finished.set()

    monkeypatch.setattr(memory_runner, "project_tool_result", blocking_projection)
    monkeypatch.setattr(memory_runner.RunToolResultStore, "close", tracked_close)
    send, _ = _scripted(_reply(("read_memory", {"path": "facts.md"})))

    run = asyncio.create_task(
        run_memory_file_editor(_run_input(tmp_path, send, tools=(_read_tool(),)))
    )
    assert await asyncio.to_thread(projection_started.wait, 5)
    run.cancel()
    await asyncio.sleep(0)
    assert not run.done()
    release_projection.set()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert close_finished.is_set()


@pytest.mark.asyncio
async def test_a_run_error_keeps_its_type_when_closing_the_store_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RunFailure(RuntimeError):
        pass

    def fail_close(_store: memory_runner.RunToolResultStore) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(memory_runner.RunToolResultStore, "close", fail_close)

    async def send(_request: ConversationRequest) -> ActionTurnReply:
        raise RunFailure("run failed")

    with pytest.raises(RunFailure) as exc_info:
        await run_memory_file_editor(_run_input(tmp_path, send))

    assert exc_info.value.__notes__ == [
        "run tool-result store close failed: RuntimeError: close failed"
    ]

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.agents.artifact_react import (
    ReactLoopPolicy,
    ReactLoopStep,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
)
from pantaray_agents.agents.artifact_react.transcript import (
    build_prompt_with_transcript,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import LlmToolCallTurn
from pantaray_agents.agents.memory_file_editor import runner as memory_runner
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.agents.memory_file_editor.tool_result_store import (
    TOOL_RESULT_FETCH_TOOL_NAME,
)
from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
    OpenAiToolContinuation,
)
from pantaray_llm.errors import LlmProxyExecutionError

_OPEN_TEST_DIRECTORY_FDS: list[int] = []


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
    directory_fd = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    _OPEN_TEST_DIRECTORY_FDS.append(directory_fd)
    return directory_fd


def _turn(
    name: str,
    arguments: dict,
    *,
    call_id: str,
    with_continuation: bool,
) -> LlmToolCallTurn:
    return LlmToolCallTurn(
        calls=(LlmToolCall(call_id=call_id, name=name, arguments=arguments),),
        continuation=(
            OpenAiToolContinuation(
                provider="openai",
                history_items=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "prompt"}],
                    }
                ],
            )
            if with_continuation
            else None
        ),
    )


async def _execute_read(_call, _step_number: int) -> ReactToolResult:
    return ReactToolResult(
        tool_name="read_memory",
        status="success",
        output={"status": "success", "text": "memory"},
    )


def _read_tool() -> ReactToolDefinition:
    return ReactToolDefinition(
        name="read_memory",
        description="Read memory.",
        request_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "properties": {
                    "status": {"const": "success"},
                    "text": {"type": "string"},
                },
                "required": ["status", "text"],
            }
        ),
        execute=_execute_read,
    )


def _large_read_tool(text: str) -> ReactToolDefinition:
    async def execute(_call, _step_number: int) -> ReactToolResult:
        return ReactToolResult(
            tool_name="read_memory",
            status="success",
            output={"status": "success", "text": text},
        )

    definition = _read_tool()
    return ReactToolDefinition(
        name=definition.name,
        description=definition.description,
        request_schema=definition.request_schema,
        response_schema=definition.response_schema,
        execute=execute,
    )


def _invalid_tool_call_error() -> LlmProxyExecutionError:
    return LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="Expected exactly one tool call, but received 2.",
        retryable=False,
        recovery="repair_next_turn",
        tool_call_violation_reason="multiple_calls",
        actual_tool_call_count=2,
    )


def _blocked_tool_call_error() -> LlmProxyExecutionError:
    return LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="Model response was blocked.",
        retryable=False,
        recovery="stop",
        tool_call_violation_reason="response_blocked",
    )


async def test_memory_editor_uses_native_continuation_until_completed(
    tmp_path: Path,
) -> None:
    recorded: list[ReactLoopStep] = []
    llm_inputs: list[tuple[object, LlmToolResult | None]] = []

    async def call_llm(_prompt, tools, continuation, tool_result):
        llm_inputs.append((continuation, tool_result))
        assert {tool.name for tool in tools} == {
            "completed",
            "read_memory",
            TOOL_RESULT_FETCH_TOOL_NAME,
        }
        if len(llm_inputs) == 1:
            return _turn(
                "read_memory",
                {"path": "facts.md"},
                call_id="call-read",
                with_continuation=True,
            )
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    async def record_step(step: ReactLoopStep) -> None:
        recorded.append(step)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-1",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(_read_tool(),),
            build_prompt=lambda _results, _error: "prompt",
            call_llm=call_llm,
            record_step=record_step,
            policy=ReactLoopPolicy(max_llm_turns=3, max_tool_calls=2),
        )
    )

    assert result.loop_result.status == "success"
    assert result.loop_result.completion_reason == "completed"
    assert llm_inputs[0] == (None, None)
    continuation, tool_result = llm_inputs[1]
    assert continuation is not None
    assert tool_result is not None
    assert tool_result.call_id == "call-read"
    assert tool_result.name == "read_memory"
    assert tool_result.output == {"status": "success", "text": "memory"}
    assert [step.step_kind for step in recorded] == ["llm", "tool", "tool", "llm"]
    assert recorded[-1].prompt_text is None


@pytest.mark.asyncio
async def test_memory_editor_spills_large_result_and_fetches_bounded_pages(
    tmp_path: Path,
) -> None:
    large_text = "raw-tool-result-日本語🌊\n" * 1_200
    expected = serialize_json_tool_output(
        {
            "output": {"status": "success", "text": large_text},
            "error_message": None,
        }
    )
    prompts: list[str] = []
    recorded: list[ReactLoopStep] = []
    fetched_parts: list[str] = []
    call_count = 0
    tool_result_directory_path = tmp_path / "tool-results"
    tool_result_directory = _tool_result_directory(tmp_path)

    async def call_llm(prompt, tools, continuation, tool_result):
        nonlocal call_count
        call_count += 1
        prompts.append(prompt)
        assert TOOL_RESULT_FETCH_TOOL_NAME in {tool.name for tool in tools}
        if call_count == 1:
            return _turn(
                "read_memory",
                {"path": "facts.md"},
                call_id="call-read-large",
                with_continuation=True,
            )
        assert continuation is not None
        assert tool_result is not None
        if tool_result.name == "read_memory":
            assert isinstance(tool_result.output, dict)
            assert tool_result.output["storage"] == "run_tool_result_file"
            assert large_text not in prompt
            return _turn(
                TOOL_RESULT_FETCH_TOOL_NAME,
                {
                    "result_ref": tool_result.output["result_ref"],
                    "offset": 0,
                },
                call_id="call-fetch-0",
                with_continuation=True,
            )
        assert tool_result.name == TOOL_RESULT_FETCH_TOOL_NAME
        assert isinstance(tool_result.output, dict)
        content = tool_result.output["content"]
        assert isinstance(content, str)
        fetched_parts.append(content)
        next_offset = tool_result.output["next_offset"]
        if isinstance(next_offset, int):
            return _turn(
                TOOL_RESULT_FETCH_TOOL_NAME,
                {
                    "result_ref": tool_result.output["result_ref"],
                    "offset": next_offset,
                },
                call_id=f"call-fetch-{next_offset}",
                with_continuation=True,
            )
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-large-result",
            tool_result_directory_fd=tool_result_directory,
            tool_definitions=(_large_read_tool(large_text),),
            build_prompt=lambda results, error: build_prompt_with_transcript(
                initial_prompt="prompt",
                tool_results=results,
                last_error=error,
            ),
            call_llm=call_llm,
            record_step=lambda step: _record_step(recorded, step),
            policy=ReactLoopPolicy(max_llm_turns=8, max_tool_calls=7),
        )
    )

    assert result.loop_result.status == "success"
    assert tool_result_directory_path.is_dir()
    assert len(tuple(tool_result_directory_path.iterdir())) == 1
    assert "".join(fetched_parts) == expected
    assert all(large_text not in prompt for prompt in prompts)
    final_tool_steps = [
        step
        for step in recorded
        if step.step_kind == "tool" and step.status != "processing"
    ]
    assert final_tool_steps[0].tool_output == result.loop_result.steps[1].tool_output
    assert isinstance(final_tool_steps[0].tool_output, dict)
    assert final_tool_steps[0].tool_output == {
        "status": "success",
        "text": large_text,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("read_prefix", [False, True])
@pytest.mark.parametrize("completion_error", [None, "draft is invalid"])
async def test_memory_editor_completion_validates_draft_with_unread_result_pages(
    tmp_path: Path, read_prefix: bool, completion_error: str | None
) -> None:
    large_text = "unread-tool-result" * 50_000
    calls = 0

    async def call_llm(prompt, _tools, _continuation, tool_result):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _turn(
                "read_memory",
                {"path": "facts.md"},
                call_id="call-read",
                with_continuation=True,
            )
        if calls == 2 and read_prefix:
            assert tool_result is not None
            return _turn(
                TOOL_RESULT_FETCH_TOOL_NAME,
                {"result_ref": tool_result.output["result_ref"], "offset": 0},
                call_id="call-fetch-prefix",
                with_continuation=True,
            )
        assert "run_tool_result_file" in prompt
        return _turn("completed", {}, call_id="call-completed", with_continuation=True)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-unread-result",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(_large_read_tool(large_text),),
            build_prompt=lambda results, error: build_prompt_with_transcript(
                initial_prompt="prompt", tool_results=results, last_error=error
            ),
            call_llm=call_llm,
            record_step=lambda _step: _completed_awaitable(),
            policy=ReactLoopPolicy(
                max_llm_turns=2 + int(read_prefix), max_tool_calls=3
            ),
            validate_completion=lambda: completion_error,
        )
    )

    assert result.loop_result.status == (
        "success" if completion_error is None else "error"
    )
    assert result.loop_result.last_error == completion_error


@pytest.mark.asyncio
async def test_runner_never_deletes_through_replaced_workspace_ancestor(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    outside_user = tmp_path / "outside-user"

    with MemoryRunWorkspaceScope() as workspace_scope:
        workspace = workspace_scope.create(
            artifact_root=artifact_root,
            user_id="user-1",
            run_id="run-1",
        )
        original_user = workspace.root_path.parents[1]
        renamed_user = original_user.with_name("user-1-original")
        original_user.rename(renamed_user)
        original_user.symlink_to(outside_user, target_is_directory=True)
        outside_tool_results = (
            outside_user / "workspaces" / workspace.root_path.name / "tool-results"
        )
        victim = outside_tool_results / "victim"
        victim.mkdir(parents=True)

        async def call_llm(_prompt, _tools, _continuation, _tool_result):
            return _turn(
                "completed",
                {},
                call_id="call-completed",
                with_continuation=False,
            )

        result = await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-1",
                tool_result_directory_fd=workspace_scope.require_tool_results_fd(),
                tool_definitions=(),
                build_prompt=lambda _results, _error: "prompt",
                call_llm=call_llm,
                record_step=lambda _step: _completed_awaitable(),
                policy=ReactLoopPolicy(max_llm_turns=1, max_tool_calls=1),
            )
        )

        assert result.loop_result.status == "success"
        assert victim.is_dir()

    assert victim.is_dir()
    assert tuple((renamed_user / "workspaces").iterdir()) == ()


@pytest.mark.asyncio
async def test_memory_editor_completes_at_tool_budget_with_spilled_result(
    tmp_path: Path,
) -> None:
    large_text = "force-terminal-raw" * 2_000
    prompts: list[str] = []
    llm_calls = 0

    async def call_llm(prompt, tools, continuation, tool_result):
        nonlocal llm_calls
        llm_calls += 1
        prompts.append(prompt)
        if llm_calls == 1:
            return _turn(
                "read_memory",
                {"path": "facts.md"},
                call_id="call-read",
                with_continuation=True,
            )
        assert {tool.name for tool in tools} == {"completed"}
        assert continuation is None
        assert tool_result is None
        assert "run_tool_result_file" in prompt
        assert large_text not in prompt
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-force-terminal",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(_large_read_tool(large_text),),
            build_prompt=lambda results, error: build_prompt_with_transcript(
                initial_prompt="prompt",
                tool_results=results,
                last_error=error,
            ),
            call_llm=call_llm,
            record_step=lambda step: _completed_awaitable(),
            policy=ReactLoopPolicy(max_llm_turns=3, max_tool_calls=1),
        )
    )

    assert result.loop_result.status == "success"
    assert result.loop_result.completion_reason == "completed"
    assert len(prompts) == 2


@pytest.mark.asyncio
async def test_memory_editor_projects_rejected_completion_for_inference_only(
    tmp_path: Path,
) -> None:
    large_error = "completion-precondition-raw" * 1_000
    recorded: list[ReactLoopStep] = []
    ready = False
    llm_calls = 0
    result_ref = ""

    async def call_llm(prompt, _tools, continuation, tool_result):
        nonlocal ready, llm_calls, result_ref
        llm_calls += 1
        if llm_calls == 1:
            return _turn(
                "completed", {}, call_id="call-too-early", with_continuation=True
            )
        assert continuation is not None
        assert tool_result is not None
        assert tool_result.name == "completed"
        assert isinstance(tool_result.output, dict)
        assert tool_result.output["storage"] == "run_tool_result_file"
        assert large_error not in prompt
        result_ref = str(tool_result.output["result_ref"])
        return _turn(
            TOOL_RESULT_FETCH_TOOL_NAME,
            {"result_ref": result_ref, "offset": 0},
            call_id="call-fetch-0",
            with_continuation=True,
        )

    async def call_llm_with_fetch(prompt, tools, continuation, tool_result):
        nonlocal ready
        if result_ref:
            assert continuation is not None
            assert tool_result is not None
            if tool_result.name == TOOL_RESULT_FETCH_TOOL_NAME:
                next_offset = tool_result.output["next_offset"]
                if isinstance(next_offset, int):
                    return _turn(
                        TOOL_RESULT_FETCH_TOOL_NAME,
                        {"result_ref": result_ref, "offset": next_offset},
                        call_id=f"call-fetch-{next_offset}",
                        with_continuation=True,
                    )
                ready = True
                return _turn(
                    "completed",
                    {},
                    call_id="call-completed",
                    with_continuation=False,
                )
        return await call_llm(prompt, tools, continuation, tool_result)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-large-completion-error",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(),
            build_prompt=lambda results, error: build_prompt_with_transcript(
                initial_prompt="prompt",
                tool_results=results,
                last_error=error,
            ),
            call_llm=call_llm_with_fetch,
            record_step=lambda step: _record_step(recorded, step),
            policy=ReactLoopPolicy(max_llm_turns=6, max_tool_calls=5),
            validate_completion=lambda: None if ready else large_error,
        )
    )

    assert result.loop_result.status == "success"
    rejected_step = next(
        step
        for step in recorded
        if step.tool_name == "completed" and step.status == "error"
    )
    assert isinstance(rejected_step.tool_output, dict)
    assert rejected_step.tool_output["message"] == large_error
    assert rejected_step.error_message == large_error


@pytest.mark.asyncio
async def test_memory_editor_returns_rejected_completion_to_same_inference(
    tmp_path: Path,
) -> None:
    recorded: list[ReactLoopStep] = []
    llm_inputs: list[LlmToolResult | None] = []
    ready = False

    async def call_llm(
        _prompt: str,
        _tools: tuple[LlmToolDefinition, ...],
        _continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        nonlocal ready
        llm_inputs.append(tool_result)
        if len(llm_inputs) == 1:
            return _turn(
                "completed",
                {},
                call_id="call-too-early",
                with_continuation=True,
            )
        assert tool_result is not None
        assert tool_result.name == "completed"
        assert tool_result.output == {
            "status": "error",
            "error_code": "COMPLETION_PRECONDITION_FAILED",
            "message": "draft is missing",
        }
        ready = True
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-completion-guard",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(),
            build_prompt=lambda _results, _error: "prompt",
            call_llm=call_llm,
            record_step=lambda step: _record_step(recorded, step),
            policy=ReactLoopPolicy(max_llm_turns=3, max_tool_calls=2),
            validate_completion=lambda: None if ready else "draft is missing",
        )
    )

    assert result.loop_result.status == "success"
    assert [step.step_kind for step in recorded] == ["llm", "tool", "tool", "llm"]
    assert [step.status for step in recorded[1:3]] == ["processing", "error"]
    assert recorded[2].tool_call_envelope == {
        "tool_id": "completed",
        "reason": None,
        "args": {},
    }


@pytest.mark.asyncio
async def test_memory_editor_repairs_contract_error_and_completes(
    tmp_path: Path,
) -> None:
    recorded: list[ReactLoopStep] = []
    prompts: list[str] = []

    async def call_llm(prompt, _tools, _continuation, _tool_result):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise _invalid_tool_call_error()
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    async def record_step(step: ReactLoopStep) -> None:
        recorded.append(step)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-repair",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(_read_tool(),),
            build_prompt=lambda _results, error: f"prompt\n{error or ''}",
            call_llm=call_llm,
            record_step=record_step,
            policy=ReactLoopPolicy(max_llm_turns=6, max_tool_calls=2),
        )
    )

    assert result.loop_result.status == "success"
    assert len(prompts) == 2
    assert "multiple_calls" in prompts[1]
    assert [step.status for step in recorded] == ["error", "success"]


@pytest.mark.asyncio
async def test_memory_editor_stops_after_five_consecutive_contract_errors(
    tmp_path: Path,
) -> None:
    recorded: list[ReactLoopStep] = []
    calls = 0

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        nonlocal calls
        calls += 1
        raise _invalid_tool_call_error()

    async def record_step(step: ReactLoopStep) -> None:
        recorded.append(step)

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-limit",
                tool_result_directory_fd=_tool_result_directory(tmp_path),
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, error: f"prompt\n{error or ''}",
                call_llm=call_llm,
                record_step=record_step,
                policy=ReactLoopPolicy(max_llm_turns=10, max_tool_calls=2),
            )
        )

    assert exc_info.value.tool_call_violation_reason == "multiple_calls"
    assert calls == 5
    assert len(recorded) == 5


@pytest.mark.asyncio
async def test_memory_editor_honors_stop_recovery_without_reinference(
    tmp_path: Path,
) -> None:
    recorded: list[ReactLoopStep] = []
    calls = 0

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        nonlocal calls
        calls += 1
        raise _blocked_tool_call_error()

    async def record_step(step: ReactLoopStep) -> None:
        recorded.append(step)

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-blocked",
                tool_result_directory_fd=_tool_result_directory(tmp_path),
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, error: f"prompt\n{error or ''}",
                call_llm=call_llm,
                record_step=record_step,
                policy=ReactLoopPolicy(max_llm_turns=10, max_tool_calls=2),
            )
        )

    assert exc_info.value.recovery == "stop"
    assert calls == 1
    assert len(recorded) == 1


@pytest.mark.asyncio
async def test_memory_editor_resets_contract_error_streak_after_valid_call(
    tmp_path: Path,
) -> None:
    calls = 0

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        nonlocal calls
        calls += 1
        if calls in {1, 2, 3, 4, 6, 7, 8, 9}:
            raise _invalid_tool_call_error()
        if calls == 5:
            return _turn(
                "read_memory",
                {"path": "facts.md"},
                call_id="call-read",
                with_continuation=True,
            )
        return _turn("completed", {}, call_id="call-completed", with_continuation=False)

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-reset",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(_read_tool(),),
            build_prompt=lambda _results, error: f"prompt\n{error or ''}",
            call_llm=call_llm,
            record_step=lambda _step: _completed_awaitable(),
            policy=ReactLoopPolicy(max_llm_turns=12, max_tool_calls=2),
        )
    )

    assert result.loop_result.status == "success"
    assert calls == 10


@pytest.mark.asyncio
async def test_projection_finishes_before_store_close_after_run_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    projection_started = threading.Event()
    release_projection = threading.Event()
    projection_finished = threading.Event()
    close_finished = threading.Event()
    tool_result_directory_path = tmp_path / "tool-results"
    tool_result_directory = _tool_result_directory(tmp_path)
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
    monkeypatch.setattr(
        memory_runner.RunToolResultStore,
        "close",
        tracked_close,
    )

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        return _turn(
            "read_memory",
            {"path": "facts.md"},
            call_id="call-cancelled-projection",
            with_continuation=True,
        )

    run = asyncio.create_task(
        run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-cancelled-projection",
                tool_result_directory_fd=tool_result_directory,
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, _error: "prompt",
                call_llm=call_llm,
                record_step=lambda _step: _completed_awaitable(),
                policy=ReactLoopPolicy(max_llm_turns=2, max_tool_calls=1),
            )
        )
    )
    assert await asyncio.to_thread(projection_started.wait, 5)

    run.cancel()
    await asyncio.sleep(0)
    assert not run.done()
    release_projection.set()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert projection_finished.is_set()
    assert close_finished.is_set()
    assert tool_result_directory_path.is_dir()


@pytest.mark.asyncio
async def test_projection_failure_does_not_reclassify_successful_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ProjectionFailure(RuntimeError):
        pass

    recorded: list[ReactLoopStep] = []

    def fail_projection(
        *, result: ReactToolResult, store: memory_runner.RunToolResultStore
    ) -> ReactToolResult:
        del result, store
        raise ProjectionFailure("projection failed")

    monkeypatch.setattr(memory_runner, "project_tool_result", fail_projection)

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        return _turn(
            "read_memory",
            {"path": "facts.md"},
            call_id="call-projection-failure",
            with_continuation=True,
        )

    with pytest.raises(ProjectionFailure, match="projection failed"):
        await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-projection-failure",
                tool_result_directory_fd=_tool_result_directory(tmp_path),
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, _error: "prompt",
                call_llm=call_llm,
                record_step=lambda step: _record_step(recorded, step),
                policy=ReactLoopPolicy(max_llm_turns=2, max_tool_calls=1),
            )
        )

    assert [step.status for step in recorded] == ["success", "processing", "success"]
    assert recorded[-1].tool_output == {"status": "success", "text": "memory"}


@pytest.mark.asyncio
async def test_projection_failure_after_cancellation_keeps_cancellation_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ProjectionFailure(RuntimeError):
        pass

    projection_started = threading.Event()
    release_projection = threading.Event()
    projection_finished = threading.Event()
    close_finished = threading.Event()
    original_close = memory_runner.RunToolResultStore.close

    def failing_projection(
        *, result: ReactToolResult, store: memory_runner.RunToolResultStore
    ) -> ReactToolResult:
        del result, store
        projection_started.set()
        assert release_projection.wait(timeout=5)
        projection_finished.set()
        raise ProjectionFailure("projection failed")

    def tracked_close(store: memory_runner.RunToolResultStore) -> None:
        assert projection_finished.is_set()
        original_close(store)
        close_finished.set()

    monkeypatch.setattr(memory_runner, "project_tool_result", failing_projection)
    monkeypatch.setattr(
        memory_runner.RunToolResultStore,
        "close",
        tracked_close,
    )

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        return _turn(
            "read_memory",
            {"path": "facts.md"},
            call_id="call-cancelled-projection-failure",
            with_continuation=True,
        )

    run = asyncio.create_task(
        run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-cancelled-projection-failure",
                tool_result_directory_fd=_tool_result_directory(tmp_path),
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, _error: "prompt",
                call_llm=call_llm,
                record_step=lambda _step: _completed_awaitable(),
                policy=ReactLoopPolicy(max_llm_turns=2, max_tool_calls=1),
            )
        )
    )
    assert await asyncio.to_thread(projection_started.wait, 5)

    run.cancel()
    await asyncio.sleep(0)
    assert not run.done()
    release_projection.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await run
    assert exc_info.value.__notes__ == [
        "background operation also failed after cancellation: "
        "ProjectionFailure: projection failed"
    ]
    assert close_finished.is_set()


@pytest.mark.asyncio
async def test_run_error_type_is_preserved_when_store_close_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RunFailure(RuntimeError):
        pass

    class CloseFailure(RuntimeError):
        pass

    def fail_close(_store: memory_runner.RunToolResultStore) -> None:
        raise CloseFailure("close failed")

    monkeypatch.setattr(
        memory_runner.RunToolResultStore,
        "close",
        fail_close,
    )

    async def call_llm(_prompt, _tools, _continuation, _tool_result):
        raise RunFailure("run failed")

    with pytest.raises(RunFailure) as exc_info:
        await run_memory_file_editor(
            MemoryFileEditorRunInput(
                run_id="run-and-close-fail",
                tool_result_directory_fd=_tool_result_directory(tmp_path),
                tool_definitions=(_read_tool(),),
                build_prompt=lambda _results, _error: "prompt",
                call_llm=call_llm,
                record_step=lambda _step: _completed_awaitable(),
                policy=ReactLoopPolicy(max_llm_turns=2, max_tool_calls=1),
            )
        )

    assert exc_info.value.__notes__ == [
        "run tool-result store close failed: CloseFailure: close failed"
    ]


async def _completed_awaitable() -> None:
    return None


async def _record_step(recorded: list[ReactLoopStep], step: ReactLoopStep) -> None:
    recorded.append(step)


@pytest.mark.asyncio
async def test_memory_editor_returns_the_requests_completed_reports_applied(
    tmp_path: Path,
) -> None:
    async def call_llm(
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        continuation: LlmToolContinuation | None,
        tool_result: LlmToolResult | None,
    ) -> LlmToolCallTurn:
        del prompt, continuation, tool_result
        completed = next(tool for tool in tools if tool.name == "completed")
        assert completed.parameters["required"] == ["applied_memory_requests"]
        return _turn(
            "completed",
            {"applied_memory_requests": ["R2"]},
            call_id="call-completed",
            with_continuation=False,
        )

    result = await run_memory_file_editor(
        MemoryFileEditorRunInput(
            run_id="run-applied-requests",
            tool_result_directory_fd=_tool_result_directory(tmp_path),
            tool_definitions=(),
            build_prompt=lambda _results, _error: "prompt",
            call_llm=call_llm,
            record_step=lambda _step: _completed_awaitable(),
            policy=ReactLoopPolicy(max_llm_turns=2, max_tool_calls=1),
            memory_request_ids=("R1", "R2"),
        )
    )

    assert result.loop_result.status == "success"
    assert result.applied_memory_request_ids == ("R2",)

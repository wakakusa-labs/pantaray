from __future__ import annotations

import pytest

from pantaray_agents.agents.artifact_document import (
    ReActAgentRunInput,
    ReActAgentRunner,
    ReActAgentRunResult,
)
from pantaray_agents.agents.artifact_react import (
    PatchCommitResult,
    ReactLoopStep,
    build_artifact_react_response_format,
    parse_artifact_document_react_output,
    run_react_loop,
)
from pantaray_agents.agents.artifact_react.artifact_patch_contract import (
    artifact_patch_request_schema,
)
from pantaray_agents.local_runtime.llm_proxy.generate_config import serialize_for_json
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.tools.contract import ReactToolCall, ReactToolResult
from pantaray_agents.utils.prompt_loader import load_config
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.errors.error_contract import PROXY_REQUEST_FAILED

from .runner_helpers import (
    DummyDefinition,
    DummyRequest,
    DummyResponse,
    FatalToolError,
    _completed_tool_call,
    _noop,
    _patch_args,
    _react_tool_call,
    _record,
    _schema_has_no_unsupported_gemini_features,
    _unused_commit_patch,
    _unused_tool_result,
    echo_tool_definition,
    reserved_tool_definition,
)


def test_memory_update_prompt_requires_native_tool_calls() -> None:
    config = load_config("memory_update")

    instruction = config.system_instruction or ""
    normalized_instruction = " ".join(instruction.split())
    assert "Call exactly one provided tool per turn" in normalized_instruction
    assert "Call `completed` when" in normalized_instruction
    assert "{tools_definitions}" not in config.prompt
    assert "JSON tool request" not in instruction
    assert "Response schema:" not in instruction
    assert "Response schema:" not in config.prompt


def test_memory_update_prompt_defines_artifact_editing_boundaries() -> None:
    config = load_config("memory_update")

    instruction = config.system_instruction or ""
    normalized_instruction = " ".join(instruction.split())
    assert "## Tool protocol" in instruction
    assert "Before patching an existing file" in normalized_instruction
    assert "build patch context from that raw result" in normalized_instruction
    assert "Registered workspaces are read-only" in instruction
    assert "Do not perform unrelated cleanup" in instruction
    assert "all memory, all Activity" in instruction
    assert "<memory_file_manifest>" in config.prompt


def test_artifact_react_response_schema_is_strict_without_extra_tools() -> None:
    response_format = build_artifact_react_response_format()
    schema = serialize_for_json(response_format)

    assert _schema_has_no_unsupported_gemini_features(schema)
    assert schema["properties"]["tool_id"]["enum"] == ["artifact_patch", "completed"]
    assert "reason" in schema["properties"]
    assert schema["properties"]["args"]["additionalProperties"] is False
    assert "chunks" in schema["properties"]["args"]["properties"]
    assert "reason" not in schema["properties"]["args"]["properties"]


def test_artifact_patch_request_schema_documents_all_args() -> None:
    schema = artifact_patch_request_schema(logical_path="long_term.md")

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["chunks"]

    properties = schema["properties"]
    assert set(properties) == {"chunks"}
    assert properties["chunks"]["description"]
    assert "Current Document" in properties["chunks"]["description"]
    assert (
        "Markdown bullet markers belong in text" in properties["chunks"]["description"]
    )
    assert "Use add for new wording" in properties["chunks"]["description"]
    assert "<OMITTED>" in properties["chunks"]["description"]
    assert properties["chunks"]["items"]["properties"]["lines"]["contains"]


def test_artifact_react_response_schema_is_extensible_with_extra_tools() -> None:
    response_format = build_artifact_react_response_format(
        tool_definitions=(echo_tool_definition(),)
    )
    schema = serialize_for_json(response_format)

    assert _schema_has_no_unsupported_gemini_features(schema)
    assert schema["properties"]["tool_id"]["enum"] == [
        "artifact_patch",
        "completed",
        "echo",
    ]
    assert schema["properties"]["args"] == {"type": "object"}
    validated_payload = response_format.model_validate(
        {"tool_id": "echo", "args": {"message": "hello"}}
    )
    parsed = parse_artifact_document_react_output(validated_payload)

    assert isinstance(parsed, ReactToolCall)
    assert parsed.tool_name == "echo"
    assert parsed.tool_args == {"message": "hello"}
    assert parsed.tool_call_envelope.to_json() == {
        "tool_id": "echo",
        "reason": None,
        "args": {"message": "hello"},
    }


def test_artifact_react_response_schema_rejects_reserved_extra_tool_names() -> None:
    with pytest.raises(ValueError, match="reserved names: completed"):
        _ = build_artifact_react_response_format(
            tool_definitions=(reserved_tool_definition("completed"),)
        )


def test_react_tool_definition_rejects_whitespace_padded_name() -> None:
    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        _ = reserved_tool_definition(" completed ")


@pytest.mark.asyncio
async def test_runner_commits_patch_and_builds_success_response() -> None:
    responses = iter(
        (
            {
                "tool_id": "artifact_patch",
                "args": _patch_args(),
            },
            {"tool_id": "completed", "reason": "done", "args": {}},
        )
    )
    completed_calls: list[str] = []
    prompts: list[str] = []

    async def call_llm(prompt: str) -> object:
        prompts.append(prompt)
        return next(responses)

    async def commit_patch(
        _base_text: str,
        updated_text: str,
        base_sha256: str,
        call: ReactToolCall,
        _step_number: int,
    ) -> PatchCommitResult:
        assert call.tool_args == _patch_args()
        assert call.tool_call_envelope.to_json() == {
            "tool_id": "artifact_patch",
            "reason": None,
            "args": _patch_args(),
        }
        return PatchCommitResult(
            committed_text=updated_text,
            storage_path="dummy.md",
            sha256="updated-sha",
            base_sha256=base_sha256,
        )

    async def commit_completed(base_text: str) -> None:
        completed_calls.append(base_text)

    async def build_success_response(
        _request: DummyRequest, result: ReActAgentRunResult
    ) -> DummyResponse:
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="success",
            error=None,
            text=result.final_text,
        )

    async def build_error_response(
        _request: DummyRequest, error: AgentError
    ) -> DummyResponse:
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="error",
            error=error,
            text="",
        )

    run_input = ReActAgentRunInput(
        run_id="run-1",
        logical_path="dummy.md",
        base_text="base",
        build_prompt=lambda current_text, _last_error: f"document:{current_text}",
        call_llm=call_llm,
        apply_patch=lambda _base_text, _patch_text: "updated",
        commit_patch=commit_patch,
        record_step=lambda _step: _noop(),
        build_success_response=build_success_response,
        build_error_response=build_error_response,
        commit_completed_without_patch=commit_completed,
    )

    response = await ReActAgentRunner().run(
        definition=DummyDefinition(run_input), request=DummyRequest(run_id="run-1")
    )

    assert response.status == "success"
    assert response.text == "updated"
    assert completed_calls == []
    assert prompts[0] == "document:base"
    assert "document:updated" in prompts[1]


@pytest.mark.asyncio
async def test_runner_commits_completed_without_patch_when_no_tool_succeeds() -> None:
    completed_calls: list[str] = []

    async def build_success_response(
        _request: DummyRequest, result: ReActAgentRunResult
    ) -> DummyResponse:
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="success",
            error=None,
            text=result.final_text,
        )

    async def build_error_response(
        _request: DummyRequest, error: AgentError
    ) -> DummyResponse:
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="error",
            error=error,
            text="",
        )

    async def commit_completed(base_text: str) -> None:
        completed_calls.append(base_text)

    run_input = ReActAgentRunInput(
        run_id="run-2",
        logical_path="dummy.md",
        base_text="base",
        build_prompt=lambda _current_text, _last_error: "prompt",
        call_llm=lambda _prompt: _completed_tool_call(),
        apply_patch=lambda _base_text, _patch_text: "unused",
        commit_patch=_unused_commit_patch,
        record_step=lambda _step: _noop(),
        build_success_response=build_success_response,
        build_error_response=build_error_response,
        commit_completed_without_patch=commit_completed,
    )

    response = await ReActAgentRunner().run(
        definition=DummyDefinition(run_input), request=DummyRequest(run_id="run-2")
    )

    assert response.status == "success"
    assert response.text == "base"
    assert completed_calls == ["base"]


@pytest.mark.asyncio
async def test_react_loop_terminalizes_fatal_tool_error_before_reraising() -> None:
    recorded_steps: list[ReactLoopStep] = []

    async def call_llm(_prompt: str) -> object:
        return "tool call"

    def parse_output(_output: object) -> ReactToolCall:
        return _react_tool_call(
            "artifact_patch",
            _patch_args(),
        )

    async def execute_tool(_call: ReactToolCall, _step_number: int) -> ReactToolResult:
        raise FatalToolError("secret path /tmp/should-not-persist")

    async def record_step(step: ReactLoopStep) -> None:
        recorded_steps.append(step)

    with pytest.raises(FatalToolError):
        await run_react_loop(
            run_id="run-fatal",
            initial_prompt="prompt",
            call_llm=call_llm,
            parse_output=parse_output,
            execute_tool=execute_tool,
            record_step=record_step,
        )

    assert [step.status for step in recorded_steps] == [
        "success",
        "processing",
        "error",
    ]
    assert recorded_steps[1].step_number == recorded_steps[2].step_number
    assert recorded_steps[2].tool_output == {"exception_type": "FatalToolError"}
    assert recorded_steps[2].error_message == "fatal tool error"
    assert "/tmp/should-not-persist" not in str(recorded_steps[2].tool_output)
    assert "/tmp/should-not-persist" not in str(recorded_steps[2].error_message)


@pytest.mark.asyncio
async def test_react_loop_persists_safe_llm_error_message() -> None:
    recorded_steps: list[ReactLoopStep] = []

    async def call_llm(_prompt: str) -> object:
        raise LlmProxyExecutionError(
            error_code=PROXY_REQUEST_FAILED,
            error_message="provider failed token=secret https://provider.example/error",
            retryable=False,
        )

    result = await run_react_loop(
        run_id="run-llm-error",
        initial_prompt="prompt",
        call_llm=call_llm,
        parse_output=lambda _output: _react_tool_call("unused", {}),
        execute_tool=lambda _call, _step_number: _unused_tool_result(),
        record_step=lambda step: _record(recorded_steps, step),
    )

    assert result.status == "error"
    assert recorded_steps[0].error_message == "LLM provider request failed."
    assert "token=secret" not in str(recorded_steps[0].error_message)
    assert "provider.example" not in str(recorded_steps[0].error_message)
    assert result.last_error == "LLM provider request failed after retries."


@pytest.mark.asyncio
async def test_react_loop_stops_on_retryable_llm_proxy_error() -> None:
    recorded_steps: list[ReactLoopStep] = []
    call_count = 0

    async def call_llm(_prompt: str) -> object:
        nonlocal call_count
        call_count += 1
        raise LlmProxyExecutionError(
            error_code=PROXY_REQUEST_FAILED,
            error_message="provider failed",
            retryable=True,
        )

    result = await run_react_loop(
        run_id="run-retryable-llm-error",
        initial_prompt="prompt",
        call_llm=call_llm,
        parse_output=lambda _output: _react_tool_call("unused", {}),
        execute_tool=lambda _call, _step_number: _unused_tool_result(),
        record_step=lambda step: _record(recorded_steps, step),
    )

    assert result.status == "error"
    assert call_count == 1
    assert len(recorded_steps) == 1
    assert recorded_steps[0].error_message == "LLM provider request failed."

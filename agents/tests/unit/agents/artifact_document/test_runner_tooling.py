from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.agents.artifact_react import (
    PatchCommitResult,
    ReactLoopStep,
    run_artifact_update_react_loop,
    run_react_loop,
)
from pantaray_agents.agents.artifact_react.artifact_patch_tool import (
    PATCH_APPLY_MESSAGE,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolRegistry,
    ReactToolResult,
    ToolResponseValidationError,
)
from pantaray_agents.utils.log_redaction import RedactingFormatter
from pantaray_agents.utils.structured_logging import fingerprint_text
from pantaray_agents.utils.trace_context import TraceContextManager

from .runner_helpers import (
    FatalToolError,
    StepRecordError,
    _completed_tool_call_sync,
    _patch_args,
    _patch_tool_call,
    _raise_patch_error,
    _raise_value_error,
    _react_tool_call,
    _record,
    _unused_commit_patch,
    _unused_tool_executor,
    _unused_tool_result,
    echo_tool_definition,
    reserved_tool_definition,
)


@pytest.mark.asyncio
async def test_artifact_runtime_propagates_patch_applier_value_error_as_fatal() -> None:
    recorded_steps: list[ReactLoopStep] = []

    async def commit_patch(
        _base_text: str,
        _updated_text: str,
        _base_sha256: str,
        _call: ReactToolCall,
        _step_number: int,
    ) -> PatchCommitResult:
        raise AssertionError("commit_patch should not be called")

    async def call_llm(_prompt: str) -> object:
        return _patch_tool_call()

    with pytest.raises(ValueError, match="programmer bug"):
        await run_artifact_update_react_loop(
            run_id="run-patch-value-error",
            base_text="base",
            build_prompt=lambda current_text, _last_error: current_text,
            call_llm=call_llm,
            apply_patch=lambda _base_text, _patch_text: _raise_value_error(),
            commit_patch=commit_patch,
            record_step=lambda step: _record(recorded_steps, step),
            logical_path="dummy.md",
        )

    assert [step.status for step in recorded_steps] == [
        "success",
        "processing",
        "error",
    ]
    assert recorded_steps[2].error_message == "fatal tool error"
    assert recorded_steps[2].tool_output == {"exception_type": "ValueError"}


@pytest.mark.asyncio
async def test_react_loop_preserves_fatal_tool_error_when_error_step_record_fails() -> (
    None
):
    async def call_llm(_prompt: str) -> object:
        return "tool call"

    def parse_output(_output: object) -> ReactToolCall:
        return _react_tool_call(
            "artifact_patch",
            _patch_args(),
        )

    async def execute_tool(_call: ReactToolCall, _step_number: int) -> ReactToolResult:
        raise FatalToolError("original typed phase")

    async def record_step(step: ReactLoopStep) -> None:
        if step.status == "error":
            raise StepRecordError("step db unavailable")

    with pytest.raises(FatalToolError) as exc_info:
        await run_react_loop(
            run_id="run-fatal-record-fails",
            initial_prompt="prompt",
            call_llm=call_llm,
            parse_output=parse_output,
            execute_tool=execute_tool,
            record_step=record_step,
        )

    assert "original typed phase" in str(exc_info.value)
    assert "failed to record fatal tool error step: StepRecordError" in (
        "\n".join(exc_info.value.__notes__)
    )


@pytest.mark.asyncio
async def test_artifact_runtime_logs_patch_apply_tool_response() -> None:
    recorded_steps: list[ReactLoopStep] = []
    prompts: list[str] = []
    responses = iter((_patch_tool_call(), _completed_tool_call_sync()))

    async def call_llm(prompt: str) -> object:
        prompts.append(prompt)
        return next(responses)

    execution = await run_artifact_update_react_loop(
        run_id="run-recoverable-patch",
        base_text="base",
        build_prompt=lambda current_text, last_error: f"{current_text}:{last_error}",
        call_llm=call_llm,
        apply_patch=lambda _base_text, _patch_text: _raise_patch_error(),
        commit_patch=_unused_commit_patch,
        record_step=lambda step: _record(recorded_steps, step),
        logical_path="dummy.md",
    )

    tool_error_step = recorded_steps[2]
    assert execution.loop_result.status == "success"
    assert tool_error_step.status == "error"
    assert tool_error_step.error_message == PATCH_APPLY_MESSAGE
    assert tool_error_step.tool_output == {
        "status": "error",
        "error_code": "PATCH_APPLY_FAILED",
        "message": PATCH_APPLY_MESSAGE,
        "details": {"reason": "secret patch detail"},
    }
    assert "secret patch detail" in prompts[1]


@pytest.mark.asyncio
async def test_artifact_runtime_logs_structured_patch_parse_error_as_tool_response() -> (
    None
):
    recorded_steps: list[ReactLoopStep] = []
    responses = iter(
        (
            {
                "tool_id": "artifact_patch",
                "args": {
                    "chunks": [
                        {
                            "lines": [
                                {"op": "add", "text": "two\nlines"},
                            ]
                        }
                    ]
                },
            },
            _completed_tool_call_sync(),
        )
    )

    async def call_llm(_prompt: str) -> object:
        return next(responses)

    execution = await run_artifact_update_react_loop(
        run_id="run-structured-parse-error",
        base_text="",
        build_prompt=lambda current_text, _last_error: current_text,
        call_llm=call_llm,
        apply_patch=lambda _base_text, _patch: "unused",
        commit_patch=_unused_commit_patch,
        record_step=lambda step: _record(recorded_steps, step),
        logical_path="dummy.md",
    )

    tool_error_step = recorded_steps[2]
    assert execution.loop_result.status == "success"
    assert tool_error_step.status == "error"
    assert tool_error_step.tool_output["error_code"] == "PATCH_APPLY_FAILED"
    assert tool_error_step.tool_output["details"]["line_index"] == 0
    assert "text" not in tool_error_step.tool_output["details"]
    assert tool_error_step.tool_output["details"]["text_omitted"] is True


@pytest.mark.asyncio
async def test_artifact_runtime_records_schema_invalid_patch_args_as_tool_error() -> (
    None
):
    recorded_steps: list[ReactLoopStep] = []
    responses = iter(
        (
            {
                "tool_id": "artifact_patch",
                "reason": "try an invalid patch",
                "args": {"reason": "wrong field"},
            },
            _completed_tool_call_sync(),
        )
    )

    async def call_llm(_prompt: str) -> object:
        return next(responses)

    execution = await run_artifact_update_react_loop(
        run_id="run-invalid-patch-args",
        base_text="base",
        build_prompt=lambda current_text, _last_error: current_text,
        call_llm=call_llm,
        apply_patch=lambda _base_text, _patch_text: "unused",
        commit_patch=_unused_commit_patch,
        record_step=lambda step: _record(recorded_steps, step),
        logical_path="dummy.md",
    )

    assert execution.loop_result.status == "success"
    assert recorded_steps[1].step_kind == "tool"
    assert recorded_steps[1].status == "processing"
    assert recorded_steps[1].tool_call_envelope == {
        "tool_id": "artifact_patch",
        "reason": "try an invalid patch",
        "args": {"reason": "wrong field"},
    }
    assert recorded_steps[2].status == "error"
    assert recorded_steps[2].tool_output["error_code"] == (
        "TOOL_REQUEST_VALIDATION_ERROR"
    )


@pytest.mark.asyncio
async def test_react_tool_registry_dispatches_extra_tools_without_runtime_switch() -> (
    None
):
    result = await ReactToolRegistry((echo_tool_definition(),)).execute(
        _react_tool_call("echo", {"message": "hello"}),
        1,
    )

    assert result.status == "success"
    assert result.output == {"status": "success", "message": "hello"}


@pytest.mark.asyncio
async def test_react_tool_registry_returns_unsupported_tool_response() -> None:
    result = await ReactToolRegistry(()).execute(
        _react_tool_call("unknown_tool", {"token": "secret"}),
        1,
    )

    assert result.status == "error"
    assert result.error_message == "The requested tool is not available."
    assert result.output == {
        "status": "error",
        "error_code": "UNSUPPORTED_TOOL",
        "message": "The requested tool is not available.",
    }


@pytest.mark.asyncio
async def test_artifact_runtime_executes_extra_tool_contract_end_to_end() -> None:
    recorded_steps: list[ReactLoopStep] = []
    prompts: list[str] = []
    responses = iter(
        (
            {"tool_id": "echo", "args": {"message": "hello"}},
            _completed_tool_call_sync(),
        )
    )

    async def call_llm(prompt: str) -> object:
        prompts.append(prompt)
        return next(responses)

    execution = await run_artifact_update_react_loop(
        run_id="run-extra-tool",
        base_text="base",
        build_prompt=lambda current_text, _last_error: f"document:{current_text}",
        call_llm=call_llm,
        apply_patch=lambda _base_text, _patch_text: "unused",
        commit_patch=_unused_commit_patch,
        record_step=lambda step: _record(recorded_steps, step),
        logical_path="dummy.md",
        tools=(echo_tool_definition(),),
    )

    assert execution.loop_result.status == "success"
    assert recorded_steps[1].tool_args == {"message": "hello"}
    assert recorded_steps[1].tool_call_envelope == {
        "tool_id": "echo",
        "reason": None,
        "args": {"message": "hello"},
    }
    assert recorded_steps[2].tool_output == {"status": "success", "message": "hello"}
    assert '"message": "hello"' in prompts[1]


@pytest.mark.asyncio
async def test_react_tool_registry_validates_extra_tool_request_schema() -> None:
    result = await ReactToolRegistry((echo_tool_definition(),)).execute(
        _react_tool_call("echo", {}),
        1,
    )

    assert result.status == "error"
    assert result.output["error_code"] == "TOOL_REQUEST_VALIDATION_ERROR"
    assert result.output["message"] == (
        "Tool request did not match the tool request schema."
    )


def test_react_tool_definition_requires_common_error_response_schema() -> None:
    with pytest.raises(ValueError, match="common tool error response"):
        ReactToolDefinition(
            name="bad_echo",
            description="Invalid tool schema.",
            request_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            response_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["success"]}},
            },
            execute=_unused_tool_executor,
        )


@pytest.mark.asyncio
async def test_artifact_runtime_rejects_reserved_extra_tool_names() -> None:
    with pytest.raises(ValueError, match="reserved names: completed"):
        _ = await run_artifact_update_react_loop(
            run_id="run-reserved-tool",
            base_text="base",
            build_prompt=lambda current_text, _last_error: f"document:{current_text}",
            call_llm=lambda _prompt: _unused_tool_result(),
            apply_patch=lambda _base_text, _patch_text: "unused",
            commit_patch=_unused_commit_patch,
            record_step=lambda step: _record([], step),
            logical_path="dummy.md",
            tools=(reserved_tool_definition("completed"),),
        )


def test_patch_commit_result_tool_output_is_canonical_success_response() -> None:
    assert PatchCommitResult(
        committed_text="updated",
        storage_path="dummy.md",
        sha256="updated-sha",
        base_sha256="base-sha",
    ).to_tool_output() == {
        "status": "success",
        "storage_path": "dummy.md",
        "sha256": "updated-sha",
        "base_sha256": "base-sha",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["invalid_response", "database", "private_exception"]
)
async def test_fatal_tool_retains_diagnostics_in_file_when_step_storage_fails(
    tmp_path: Path, failure: str
) -> None:
    private_text = "private-document-body"
    prompts: list[str] = []

    async def call_llm(prompt: str) -> object:
        prompts.append(prompt)
        return "tool call"

    async def execute_tool(_call: ReactToolCall, _step: int) -> ReactToolResult:
        if failure == "invalid_response":
            return ReactToolResult(
                tool_name="echo", status="success", output={"status": private_text}
            )
        if failure == "private_exception":
            raise ValueError(str(_call.tool_args["message"]))
        try:
            # A real DB error represents infrastructure the LLM cannot repair.
            with sqlite3.connect(":memory:") as connection:
                connection.execute("SELECT * FROM absent_index")
        except sqlite3.OperationalError as error:
            raise RuntimeError("index query failed token=credential-value") from error
        raise AssertionError("database query unexpectedly succeeded")

    async def record_step(step: ReactLoopStep) -> None:
        if step.status == "error":
            raise StepRecordError("step db unavailable")

    registry = ReactToolRegistry(
        (replace(echo_tool_definition(), execute=execute_tool),)
    )
    log_file = tmp_path / "backend.log"
    handler = logging.FileHandler(log_file)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("pantaray_agents.agents.artifact_react.runner")
    logger.addHandler(handler)
    try:
        expected = (
            ToolResponseValidationError
            if failure == "invalid_response"
            else ValueError
            if failure == "private_exception"
            else RuntimeError
        )
        with (
            TraceContextManager(local_job_id="private-job-id"),
            pytest.raises(expected) as caught,
        ):
            await run_react_loop(
                run_id="private-run-id",
                initial_prompt=private_text,
                call_llm=call_llm,
                parse_output=lambda _output: _react_tool_call(
                    "echo", {"message": private_text}
                ),
                execute_tool=registry.execute,
                record_step=record_step,
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert len(prompts) == 1  # Fatal failures must not be offered to another LLM turn.
    assert "StepRecordError" in "\n".join(caught.value.__notes__)
    content = log_file.read_text()
    assert private_text not in content
    assert "private-run-id" not in content
    assert "private-job-id" not in content
    assert "credential-value" not in content
    payload = json.loads(content)
    assert payload["evt"] == "REACT_TOOL_FAILED"
    assert payload["tool_name"] == "echo"
    assert payload["run_id_fp"]
    assert payload["job_id_fp"] == fingerprint_text("private-job-id")
    assert payload["step_number"] == 2
    chain = payload["exception_chain"]
    if failure == "invalid_response":
        assert chain[0]["error_class"] == "ToolResponseValidationError"
        assert "omitted" in chain[0]["message"]
        assert "enum at" in chain[1]["message"]
        assert "status" in chain[1]["message"]
        assert chain[1]["error_class"] == "ValidationError"
        assert "_validate_tool_result" in content
    elif failure == "private_exception":
        assert chain[0]["error_class"] == "ValueError"
        assert "omitted" in chain[0]["message"]
    else:
        assert chain[0]["error_class"] == "RuntimeError"
        assert "omitted" in chain[0]["message"]
        assert chain[1]["error_class"] == "OperationalError"
        assert chain[1]["message"] == "SQLite error: SQLITE_ERROR"
        assert any(frame["function"] == "execute_tool" for frame in chain[1]["stack"])
    assert all(frame["line"] > 0 for error in chain for frame in error["stack"])

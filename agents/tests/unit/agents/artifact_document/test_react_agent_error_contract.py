from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

import pytest

from pantaray_agents.agents.artifact_document import ReActAgent, ReActAgentHost
from pantaray_agents.agents.artifact_react import (
    PatchCommitResult,
    ReactLoopResult,
    ReactLoopStep,
)
from pantaray_agents.agents.core import TokenSink
from pantaray_agents.agents.core.error_contract import (
    ERROR_SPEC_BY_PHASE,
    AgentErrorPhase,
)
from pantaray_agents.schema.agent import AgentRequest, AgentResponse
from pantaray_agents.schema.agent.base import AgentError, JSONValue
from pantaray_agents.tools.contract import ReactToolCall
from pantaray_agents.utils.artifact_patch.errors import ArtifactPatchConflictError
from pantaray_agents.utils.artifact_text_patch import ArtifactTextPatchError
from pantaray_agents.utils.structured_artifact_patch import StructuredArtifactPatch


class DummyRequest(AgentRequest):
    user_id: str
    run_id: str = "run-1"


class DummyResponse(AgentResponse):
    text: str


class DummyHost(ReActAgentHost):
    DEFAULT_SYSTEM_INSTRUCTION = "system"

    def __init__(self, responses: Iterable[object]) -> None:
        self._responses = iter(responses)
        self._current_language: str | None = None

    def _compose_system_instruction(
        self, *, base_instruction: str, language: str | None
    ) -> str:
        _ = language
        return base_instruction

    async def _generate_llm_response(
        self,
        prompt: str,
        *,
        sink: TokenSink,
        system_instruction: str | None = None,
        file_inputs: list[object] | None = None,
        stage: str | None = None,
        response_schema: object | None = None,
    ) -> str | object:
        _ = sink, prompt, system_instruction, file_inputs, stage
        _ = response_schema
        return next(self._responses)


FailureMode = Literal[
    "none",
    "context_fetch",
    "run_start",
    "commit",
    "commit_noop",
    "conflict",
    "patch",
    "step_record",
    "response_build",
]


class DummyReActAgent(ReActAgent[DummyRequest, DummyResponse, dict[str, JSONValue]]):
    agent_name = "dummy"
    prompt_name = "dummy"
    prompt_version = "1.0"
    logical_path = "structured_facts.md"
    patch_loop_stage = "dummy_patch_loop"
    error_code_prefix = "DUMMY"

    def __init__(
        self,
        *,
        failure_mode: FailureMode = "none",
        responses: Iterable[object] = (
            {"tool_id": "completed", "reason": "done", "args": {}},
        ),
        error_handler_fails: bool = False,
    ) -> None:
        self.host = DummyHost(responses)
        self.failure_mode = failure_mode
        self.error_handler_fails = error_handler_fails

    def validate_request(self, request: AgentRequest) -> DummyRequest:
        if isinstance(request, DummyRequest):
            return request
        return DummyRequest.model_validate(request)

    def response_params_from_request(self, request: AgentRequest) -> dict[str, object]:
        return {"text": "", "user_id": getattr(request, "user_id", "")}

    async def persist_error(
        self, error: AgentError, response_params: dict[str, object]
    ) -> None:
        _ = error, response_params
        if self.error_handler_fails:
            raise RuntimeError("failed to persist error")

    async def build_error_response(
        self, request: DummyRequest, error: AgentError
    ) -> DummyResponse:
        _ = request
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="error",
            error=error,
            text="",
        )

    async def load_context(self, request: DummyRequest) -> dict[str, JSONValue]:
        _ = request
        if self.failure_mode == "context_fetch":
            raise ValueError("local path /tmp/secret should not be persisted")
        return {"base": "base"}

    def base_text_from_context(self, context_data: dict[str, JSONValue]) -> str:
        return str(context_data["base"])

    def run_id_from_context(
        self, *, request: DummyRequest, context_data: dict[str, JSONValue]
    ) -> str:
        _ = context_data
        return request.run_id

    async def start_run(
        self,
        *,
        request: DummyRequest,
        context_data: dict[str, JSONValue],
        base_text: str,
    ) -> None:
        _ = request, context_data, base_text
        if self.failure_mode == "run_start":
            raise ValueError("db start failed")

    def build_domain_prompt(
        self,
        *,
        context_data: dict[str, JSONValue],
        current_text: str,
        tools_definitions: str,
    ) -> str:
        _ = context_data, tools_definitions
        return current_text

    def patch_loop_base_instruction(self) -> str:
        return "system"

    def apply_patch(self, base_text: str, patch: StructuredArtifactPatch) -> str:
        _ = patch
        if self.failure_mode == "patch":
            raise ArtifactTextPatchError("patch failed")
        return "updated" if base_text == "base" else base_text

    async def commit_domain_patch(
        self,
        *,
        request: DummyRequest,
        context_data: dict[str, JSONValue],
        tool_base_text: str,
        updated_text: str,
        base_sha256: str,
        call: ReactToolCall,
        step_number: int,
    ) -> PatchCommitResult:
        _ = request, context_data, tool_base_text, base_sha256, call, step_number
        if self.failure_mode == "commit":
            raise RuntimeError("db commit failed")
        if self.failure_mode == "conflict":
            raise ArtifactPatchConflictError("structured facts conflict detected")
        return PatchCommitResult(
            committed_text=updated_text,
            storage_path="structured_facts.md",
            sha256="sha",
            base_sha256=base_sha256,
        )

    async def record_step(
        self,
        *,
        request: DummyRequest,
        context_data: dict[str, JSONValue],
        step: ReactLoopStep,
    ) -> None:
        _ = request, context_data, step
        if self.failure_mode == "step_record":
            raise RuntimeError("step record failed")

    async def commit_completed_without_patch(
        self,
        *,
        request: DummyRequest,
        context_data: dict[str, JSONValue],
        base_text: str,
    ) -> None:
        _ = request, context_data, base_text
        if self.failure_mode == "commit_noop":
            raise RuntimeError("completed save failed")

    async def build_success_response(
        self, request: DummyRequest, result
    ) -> DummyResponse:
        _ = request
        if self.failure_mode == "response_build":
            raise ValueError("missing response field")
        return DummyResponse(
            created_at="2026-05-04T00:00:00Z",
            status="success",
            error=None,
            text=result.final_text,
        )


def test_error_contract_covers_all_declared_phases() -> None:
    assert set(ERROR_SPEC_BY_PHASE) == set(AgentErrorPhase)


@pytest.mark.parametrize(
    ("failure_mode", "expected_code", "expected_phase"),
    (
        ("context_fetch", "DUMMY_FETCH_CONTEXT_ERROR", AgentErrorPhase.CONTEXT_FETCH),
        ("run_start", "DUMMY_RUN_START_ERROR", AgentErrorPhase.RUN_START),
        ("commit_noop", "DUMMY_COMMIT_ERROR", AgentErrorPhase.COMMIT),
        ("step_record", "DUMMY_STEP_RECORD_ERROR", AgentErrorPhase.STEP_RECORD),
        (
            "response_build",
            "DUMMY_RESPONSE_BUILD_ERROR",
            AgentErrorPhase.RESPONSE_BUILD,
        ),
    ),
)
@pytest.mark.asyncio
async def test_react_agent_maps_process_phase_errors(
    failure_mode: FailureMode,
    expected_code: str,
    expected_phase: AgentErrorPhase,
) -> None:
    response = await DummyReActAgent(failure_mode=failure_mode).process(
        DummyRequest(user_id="user-1")
    )

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == expected_code
    assert response.error.metadata == {
        "phase": expected_phase.value,
        "exception_type": "RuntimeError"
        if failure_mode in ("commit_noop", "step_record")
        else "ValueError",
    }
    assert "/tmp/secret" not in (response.error.error_message or "")


@pytest.mark.asyncio
async def test_react_agent_maps_patch_commit_loop_error_to_commit() -> None:
    responses = (
        {
            "tool_id": "artifact_patch",
            "args": _patch_args(),
        },
    )

    response = await DummyReActAgent(
        failure_mode="commit",
        responses=responses,
    ).process(DummyRequest(user_id="user-1"))

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == "DUMMY_COMMIT_ERROR"
    assert response.error.metadata == {
        "phase": AgentErrorPhase.COMMIT.value,
        "exception_type": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_react_agent_maps_patch_conflict_to_conflict() -> None:
    response = await DummyReActAgent(
        failure_mode="conflict",
        responses=(_patch_tool_call(),),
    ).process(DummyRequest(user_id="user-1"))

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == "DUMMY_CONFLICT"
    assert response.error.metadata == {
        "phase": AgentErrorPhase.CONFLICT.value,
        "exception_type": "ArtifactPatchConflictError",
    }


def test_react_agent_maps_recoverable_patch_error_to_patch() -> None:
    error = DummyReActAgent().error_from_loop_result(
        ReactLoopResult(
            status="error",
            final_text="",
            steps=(
                ReactLoopStep(
                    run_id="run-1",
                    step_number=1,
                    step_kind="tool",
                    status="error",
                    tool_name="artifact_patch",
                    error_message="patch failed",
                ),
            ),
            last_error="patch failed",
        )
    )

    assert error.error_code == "DUMMY_PATCH_ERROR"
    assert error.metadata == {"phase": AgentErrorPhase.PATCH.value}


@pytest.mark.asyncio
async def test_react_agent_maps_common_user_id_failure_to_validation() -> None:
    response = await DummyReActAgent().process(DummyRequest(user_id=""))

    assert response.status == "error"
    assert response.error is not None
    assert response.error.error_code == "DUMMY_VALIDATION_ERROR"
    assert response.error.metadata == {
        "phase": AgentErrorPhase.VALIDATION.value,
        "exception_type": "ValueError",
    }


@pytest.mark.asyncio
async def test_react_agent_surfaces_error_handler_failure() -> None:
    with pytest.raises(RuntimeError, match="failed to persist error") as exc_info:
        await DummyReActAgent(
            failure_mode="context_fetch",
            error_handler_fails=True,
        ).process(DummyRequest(user_id="user-1"))

    assert isinstance(exc_info.value.__cause__, ValueError)


def _patch_tool_call() -> object:
    return {
        "tool_id": "artifact_patch",
        "args": _patch_args(),
    }


def _patch_args() -> dict[str, object]:
    return {
        "chunks": [
            {
                "lines": [
                    {"op": "remove", "text": "base"},
                    {"op": "add", "text": "updated"},
                ]
            }
        ]
    }

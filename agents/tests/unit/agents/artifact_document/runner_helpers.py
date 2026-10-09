from __future__ import annotations

from dataclasses import dataclass

from pantaray_agents.agents.artifact_document import ReActAgentRunInput
from pantaray_agents.agents.artifact_react import (
    PatchCommitResult,
    ReactLoopResult,
    ReactLoopStep,
)
from pantaray_agents.schema.agent import AgentRequest, AgentResponse
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolCallEnvelope,
    react_tool_response_schema,
)
from pantaray_agents.utils.artifact_text_patch import ArtifactTextPatchError


class DummyRequest(AgentRequest):
    run_id: str


class DummyResponse(AgentResponse):
    text: str


@dataclass
class DummyDefinition:
    run_input: ReActAgentRunInput[DummyRequest, DummyResponse]

    agent_name = "dummy"
    prompt_name = "dummy_prompt"
    prompt_version = "1.0"
    logical_path = "dummy.md"

    async def prepare(
        self, _request: DummyRequest
    ) -> ReActAgentRunInput[DummyRequest, DummyResponse]:
        return self.run_input

    def error_from_loop_result(self, loop_result: ReactLoopResult) -> AgentError:
        return AgentError(
            error_type="internal_error",
            error_code="DUMMY_LOOP_ERROR",
            error_message=loop_result.last_error,
            severity="error",
        )


async def _noop() -> None:
    return None


async def _unused_commit_patch(
    _base_text: str,
    _updated_text: str,
    _base_sha256: str,
    _call,
    _step_number: int,
) -> PatchCommitResult:
    raise AssertionError("commit_patch should not be called")


async def _completed_tool_call() -> object:
    return {"tool_id": "completed", "reason": "done", "args": {}}


class FatalToolError(RuntimeError):
    pass


class StepRecordError(RuntimeError):
    pass


async def _unused_tool_result() -> ReactToolResult:
    raise AssertionError("execute_tool should not be called")


def _react_tool_call(
    tool_name: str, tool_args: dict[str, object], reason: str | None = None
) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=tool_args,
        tool_call_envelope=ToolCallEnvelope(
            tool_id=tool_name,
            reason=reason,
            args=tool_args,
        ),
    )


async def _unused_tool_executor(
    _call: ReactToolCall, _step_number: int
) -> ReactToolResult:
    raise AssertionError("tool should not be executed")


async def _record(steps: list[ReactLoopStep], step: ReactLoopStep) -> None:
    steps.append(step)


def _raise_value_error() -> str:
    raise ValueError("programmer bug should not become a patch retry")


def _raise_patch_error() -> str:
    raise ArtifactTextPatchError("secret patch detail")


def _completed_tool_call_sync() -> object:
    return {"tool_id": "completed", "reason": "done", "args": {}}


def _patch_tool_call() -> object:
    return {
        "tool_id": "artifact_patch",
        "reason": "update the artifact",
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


def echo_tool_definition() -> ReactToolDefinition:
    async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
        if not isinstance(call.tool_args, dict):
            raise RuntimeError("validated echo request must be an object")
        message = call.tool_args["message"]
        if not isinstance(message, str):
            raise RuntimeError("validated echo request must include message")
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={"status": "success", "message": message},
        )

    return ReactToolDefinition(
        name="echo",
        description="Echo a message back to the next LLM turn.",
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "message"],
                "properties": {
                    "status": {"type": "string", "enum": ["success"]},
                    "message": {"type": "string"},
                },
            },
        ),
        execute=execute,
    )


def reserved_tool_definition(name: str) -> ReactToolDefinition:
    async def execute(_call: ReactToolCall, _step_number: int) -> ReactToolResult:
        return ReactToolResult(
            tool_name=name,
            status="success",
            output={"status": "success"},
        )

    return ReactToolDefinition(
        name=name,
        description="Reserved tool name test helper.",
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["success"]}},
            },
        ),
        execute=execute,
    )


def _schema_has_no_unsupported_gemini_features(schema: object) -> bool:
    serialized_schema = str(schema)
    return all(
        unsupported not in serialized_schema
        for unsupported in ("oneOf", "discriminator", "nullable")
    )

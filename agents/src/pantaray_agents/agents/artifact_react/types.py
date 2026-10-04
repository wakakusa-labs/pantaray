from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.providers.openai_responses.retry_policy import (
    LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS,
)

type ReactStepKind = Literal["llm", "tool"]
type ReactStepStatus = Literal["processing", "success", "error"]
type ReactToolResultStatus = Literal["success", "error"]
type ReactRunStatus = Literal["success", "error"]


@dataclass(frozen=True)
class ReactLoopPolicy:
    max_llm_turns: int = 100
    max_tool_calls: int = 100
    max_consecutive_llm_errors: int = LLM_TOOL_CALL_MAX_CONSECUTIVE_ERRORS
    # UTF-8 bytes of the conversation a turn may resend. Past it the run starts a
    # fresh turn from the prompt transcript with the oldest tool outputs omitted.
    max_input_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.max_llm_turns < 1:
            raise ValueError("max_llm_turns must be >= 1")
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        if self.max_consecutive_llm_errors < 1:
            raise ValueError("max_consecutive_llm_errors must be >= 1")
        if self.max_input_bytes is not None and self.max_input_bytes < 1:
            raise ValueError("max_input_bytes must be >= 1")


@dataclass(frozen=True)
class ReactFinish:
    final_text: str
    reason: str | None = None


@dataclass(frozen=True)
class ToolCallEnvelope:
    tool_id: str
    reason: str | None
    args: dict[str, JSONValue]

    def __post_init__(self) -> None:
        if not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("tool_call reason must not be blank")

    def to_json(self) -> dict[str, JSONValue]:
        return {
            "tool_id": self.tool_id,
            "reason": self.reason,
            "args": self.args,
        }


@dataclass(frozen=True)
class ReactToolCall:
    tool_name: str
    tool_args: JSONValue
    tool_call_envelope: ToolCallEnvelope

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if self.tool_call_envelope.tool_id != self.tool_name:
            raise ValueError("tool_call_envelope.tool_id must match tool_name")


@dataclass(frozen=True)
class ReactToolResult:
    tool_name: str
    status: ReactToolResultStatus
    output: JSONValue
    error_message: str | None = None
    final_step_recorded: bool = False

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must not be empty")
        if self.status not in ("success", "error"):
            raise ValueError("tool result status must be success or error")
        if self.status == "error" and not self.error_message:
            raise ValueError("error tool result requires error_message")


@dataclass(frozen=True)
class ReactLoopStep:
    run_id: str
    step_number: int
    step_kind: ReactStepKind
    status: ReactStepStatus
    prompt_text: str | None = None
    response_text: str | None = None
    thinking: str | None = None
    tool_name: str | None = None
    tool_args: JSONValue = None
    tool_call_envelope: JSONValue = None
    tool_output: JSONValue = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if self.step_number < 1:
            raise ValueError("step_number must be >= 1")


@dataclass(frozen=True)
class ReactLoopResult:
    status: ReactRunStatus
    final_text: str
    steps: tuple[ReactLoopStep, ...]
    completion_reason: str | None = None
    last_error: str | None = None


class ReactParseError(ValueError):
    """LLM response could not be parsed into a ReAct decision."""


class LlmUpstreamError(RuntimeError):
    """Transient LLM upstream failure."""


def stringify_llm_output(output: str | object) -> str:
    if isinstance(output, str):
        return output
    if hasattr(output, "model_dump_json"):
        dumped = output.model_dump_json()
        return dumped if isinstance(dumped, str) else str(dumped)
    if isinstance(output, Mapping):
        return json.dumps(output, ensure_ascii=False, default=str)
    return json.dumps(output, ensure_ascii=False, default=str)

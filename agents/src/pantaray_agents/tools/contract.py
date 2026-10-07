"""Agent-independent tool contract: definitions, calls, results, and errors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from jsonschema import Draft7Validator, ValidationError  # type: ignore[import-untyped]

from pantaray_agents.schema.agent.base import JSONValue

type ReactToolResultStatus = Literal["success", "error"]


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


type ReactToolExecutor = Callable[[ReactToolCall, int], Awaitable[ReactToolResult]]
type JsonSchema = Mapping[str, JSONValue]

UNSUPPORTED_TOOL_MESSAGE = "The requested tool is not available."
REQUEST_VALIDATION_MESSAGE = "Tool request did not match the tool request schema."
TOOL_ERROR_RESPONSE_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "error_code", "message"],
    "properties": {
        "status": {"type": "string", "enum": ["error"]},
        "error_code": {"type": "string", "minLength": 1},
        "message": {"type": "string", "minLength": 1},
        "details": {"type": "object"},
    },
}


class BrokerPolicyError(RuntimeError):
    """A tool call refused by policy, with optional LLM repair guidance.

    The file tools and the Action broker both raise it, so one handler turns
    every refusal into the same coded error the model can act on.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "BROKER_POLICY_REJECTED",
        fix_hint: str | None = None,
        examples: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fix_hint = fix_hint
        self.examples = examples


class ToolResponseValidationError(RuntimeError):
    """An implementation violated its result contract; abort instead of retrying."""


@dataclass(frozen=True)
class ReactToolDefinition:
    name: str
    description: str
    request_schema: JsonSchema
    response_schema: JsonSchema
    execute: ReactToolExecutor

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if self.name != self.name.strip():
            raise ValueError(
                "tool name must not include leading or trailing whitespace"
            )
        validation_error = validate_json_value(
            schema=self.response_schema,
            value=tool_error_output(
                error_code="TOOL_REQUEST_VALIDATION_ERROR",
                message=REQUEST_VALIDATION_MESSAGE,
                details={"path": [], "message": "schema check"},
            ),
        )
        if validation_error is not None:
            raise ValueError(
                f"{self.name} response_schema must accept the common tool error "
                f"response: {validation_error.message}"
            )


@dataclass(frozen=True)
class ReactToolRegistry:
    tools: tuple[ReactToolDefinition, ...]

    def __post_init__(self) -> None:
        names = tuple(tool.name for tool in self.tools)
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")

    async def execute(self, call: ReactToolCall, step_number: int) -> ReactToolResult:
        tool = self.find(call.tool_name)
        if tool is None:
            return _validate_tool_result(
                tool_name=call.tool_name,
                schema=TOOL_ERROR_RESPONSE_SCHEMA,
                result=tool_error_response(
                    tool_name=call.tool_name,
                    error_code="UNSUPPORTED_TOOL",
                    message=UNSUPPORTED_TOOL_MESSAGE,
                ),
            )
        validation_error = validate_json_value(
            schema=tool.request_schema,
            value=call.tool_args,
        )
        if validation_error is not None:
            return _validate_tool_result(
                tool_name=call.tool_name,
                schema=tool.response_schema,
                result=tool_error_response(
                    tool_name=tool.name,
                    error_code="TOOL_REQUEST_VALIDATION_ERROR",
                    message=REQUEST_VALIDATION_MESSAGE,
                    details={
                        "path": list(validation_error.path),
                        "message": validation_error.message,
                    },
                ),
            )
        result = await tool.execute(call, step_number)
        return _validate_tool_result(
            tool_name=tool.name,
            schema=tool.response_schema,
            result=result,
        )

    def find(self, tool_name: str) -> ReactToolDefinition | None:
        for tool in self.tools:
            if tool.name == tool_name:
                return tool
        return None


def resolve_react_tool_definitions(
    *,
    definitions: tuple[ReactToolDefinition, ...],
    tool_ids: tuple[str, ...],
) -> tuple[ReactToolDefinition, ...]:
    """Select one Agent's tools from run-bound common definitions."""

    registry = ReactToolRegistry(definitions)
    if len(set(tool_ids)) != len(tool_ids):
        raise ValueError("tool ids must be unique")
    selected: list[ReactToolDefinition] = []
    for tool_id in tool_ids:
        definition = registry.find(tool_id)
        if definition is None:
            raise ValueError(f"unknown React tool id: {tool_id}")
        selected.append(definition)
    return tuple(selected)


def validate_json_value(
    *,
    schema: JsonSchema,
    value: JSONValue,
) -> ValidationError | None:
    validator = Draft7Validator(dict(schema))
    try:
        validator.validate(value)
    except ValidationError as exc:
        return exc
    return None


def tool_error_response(
    *,
    tool_name: str,
    error_code: str,
    message: str,
    details: JSONValue = None,
) -> ReactToolResult:
    """Return a rejected operation the caller can address on its next turn.

    Infrastructure failures and implementation defects propagate as exceptions;
    they must not be converted into feedback for the LLM to repair.
    """
    return ReactToolResult(
        tool_name=tool_name,
        status="error",
        output=tool_error_output(
            error_code=error_code,
            message=message,
            details=details,
        ),
        error_message=message,
    )


def tool_error_output(
    *,
    error_code: str,
    message: str,
    details: JSONValue = None,
) -> dict[str, JSONValue]:
    response: dict[str, JSONValue] = {
        "status": "error",
        "error_code": error_code,
        "message": message,
    }
    if details is not None:
        response["details"] = details
    return response


def react_tool_response_schema(*, success_schema: JsonSchema) -> JsonSchema:
    return {
        "oneOf": [
            dict(success_schema),
            dict(TOOL_ERROR_RESPONSE_SCHEMA),
        ]
    }


def _validate_tool_result(
    *,
    tool_name: str,
    schema: JsonSchema,
    result: ReactToolResult,
) -> ReactToolResult:
    response_validation_error = validate_json_value(
        schema=schema,
        value=result.output,
    )
    if response_validation_error is not None:
        violations = response_validation_error.context or (response_validation_error,)
        # Schema locations identify the violated contract without echoing result values.
        summary = "; ".join(
            f"{error.validator} at {list(error.absolute_schema_path)}"
            for error in violations
        )
        raise ToolResponseValidationError(
            f"{tool_name} returned an invalid tool response: {summary}"
        ) from response_validation_error
    return result

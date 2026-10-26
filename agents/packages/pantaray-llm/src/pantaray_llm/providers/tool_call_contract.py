from __future__ import annotations

from dataclasses import dataclass

from jsonschema import (  # type: ignore[import-untyped]
    Draft7Validator,
    FormatChecker,
)

from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.tool_use import LlmToolDefinition
from pantaray_llm.errors import (
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_LLM_TOOL_CALL_INVALID,
    LlmProvider,
    ProviderError,
    ToolCallViolationReason,
)
from pantaray_llm.providers.schema_compiler import (
    schema_error_path,
    schema_error_property_names,
)

_FORMAT_CHECKER = FormatChecker()


@dataclass(frozen=True, slots=True)
class ToolCallErrorContext:
    provider: LlmProvider
    local_job_id: str
    profile_id: str
    upstream_request_id: str | None = None

    def details(self) -> dict[str, JSONValue]:
        details: dict[str, JSONValue] = {
            "local_job_id": self.local_job_id,
            "upstream_provider": self.provider,
            "profile_id": self.profile_id,
        }
        if self.upstream_request_id is not None:
            details["upstream_request_id"] = self.upstream_request_id
        return details


@dataclass(frozen=True, slots=True)
class ToolArgumentsValidationFailure:
    argument_path: str
    schema_keyword: str
    property_names: tuple[str, ...]


def find_declared_tool(
    *, name: str, tools: list[LlmToolDefinition]
) -> LlmToolDefinition | None:
    return next((tool for tool in tools if tool.name == name), None)


def validate_tool_arguments(
    *, arguments: dict[str, JSONValue], tool: LlmToolDefinition
) -> ToolArgumentsValidationFailure | None:
    validator = Draft7Validator(tool.parameters, format_checker=_FORMAT_CHECKER)
    error = next(validator.iter_errors(arguments), None)
    if error is None:
        return None
    return ToolArgumentsValidationFailure(
        argument_path=schema_error_path(error),
        schema_keyword=str(error.validator or "unknown"),
        property_names=schema_error_property_names(error),
    )


def build_tool_call_contract_error(
    *,
    reason: ToolCallViolationReason,
    context: ToolCallErrorContext,
    actual_call_count: int | None = None,
    tool_name: str | None = None,
    response_status: str | None = None,
    argument_path: str | None = None,
    schema_keyword: str | None = None,
    property_names: tuple[str, ...] = (),
) -> ProviderError:
    details = context.details()
    details["tool_call_violation_reason"] = reason
    if actual_call_count is not None:
        details["actual_tool_call_count"] = actual_call_count
    if tool_name is not None:
        details["tool_name"] = tool_name
    if response_status is not None:
        details["response_status"] = response_status
    if argument_path is not None:
        details["argument_path"] = argument_path
    if schema_keyword is not None:
        details["schema_keyword"] = schema_keyword
    return ProviderError(
        status_code=502,
        code=PROXY_LLM_TOOL_CALL_INVALID,
        message=_violation_message(
            reason=reason,
            actual_call_count=actual_call_count,
            tool_name=tool_name,
            response_status=response_status,
            argument_path=argument_path,
            schema_keyword=schema_keyword,
            property_names=property_names,
        ),
        details=details,
    )


def build_invalid_tool_provider_response(
    *,
    context: ToolCallErrorContext,
    message: str,
    response_status: str | None = None,
) -> ProviderError:
    details = context.details()
    if response_status is not None:
        details["response_status"] = response_status
    return ProviderError(
        status_code=502,
        code=PROXY_INVALID_UPSTREAM_RESPONSE,
        message=message,
        details=details,
    )


def _violation_message(
    *,
    reason: ToolCallViolationReason,
    actual_call_count: int | None,
    tool_name: str | None,
    response_status: str | None,
    argument_path: str | None,
    schema_keyword: str | None,
    property_names: tuple[str, ...],
) -> str:
    # Every adapter shares these messages, and they reach the model through
    # `model_error.message` on the next turn, so they name no provider.
    if reason == "missing_call":
        return "The model provider returned no tool call; exactly one is required."
    if reason == "multiple_calls":
        count = actual_call_count if actual_call_count is not None else "multiple"
        return (
            f"The model provider returned {count} tool calls; exactly one is required."
        )
    if reason == "undeclared_tool":
        name = tool_name or "<missing>"
        return f"The model provider called an undeclared tool: {name}."
    if reason == "invalid_arguments":
        return "The model provider returned tool arguments that are not valid JSON."
    if reason == "arguments_not_object":
        return "The model provider returned tool arguments that are not an object."
    if reason == "arguments_schema_mismatch":
        # Names and schema keywords only: argument values can be long or private.
        call = f"The {tool_name or '<missing>'} call"
        path = argument_path or "$"
        location = "" if path == "$" else f" at {path}"
        names = ", ".join(property_names)
        if schema_keyword == "required":
            return f"{call} is missing required argument(s){location}: {names}."
        if schema_keyword == "additionalProperties":
            return f"{call} has unexpected argument(s){location}: {names}."
        return (
            f"{call} has arguments that do not match the declared schema at "
            f"{path} ({schema_keyword or 'unknown'})."
        )
    if reason == "response_incomplete":
        status = response_status or "incomplete"
        return f"The model provider returned an incomplete tool response: {status}."
    if reason == "call_incomplete":
        status = response_status or "incomplete"
        return f"The model provider returned an incomplete tool call: {status}."
    if reason == "response_blocked":
        status = response_status or "blocked"
        return f"The model provider blocked the tool response: {status}."
    if reason == "malformed_call":
        return "The model provider reported a malformed tool call."
    return "The model provider returned an invalid native tool response."


__all__ = [
    "ToolArgumentsValidationFailure",
    "ToolCallErrorContext",
    "build_tool_call_contract_error",
    "build_invalid_tool_provider_response",
    "find_declared_tool",
    "validate_tool_arguments",
]

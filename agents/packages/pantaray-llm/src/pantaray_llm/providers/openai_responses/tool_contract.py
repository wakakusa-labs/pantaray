from __future__ import annotations

import json
from typing import cast

from openai.types.responses.response_function_tool_call import (
    ResponseFunctionToolCall,
)
from openai.types.responses.response_input_item_param import ResponseInputItemParam
from openai.types.responses.response_output_item import ResponseOutputItem
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from openai.types.responses.tool_param import ToolParam
from pydantic import ValidationError

from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
    LlmCommentary,
)
from pantaray_llm.contracts.conversation import OpenAiProviderTurn
from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolUseRequest,
    LlmToolUseResponse,
    OpenAiContinuationMediaSlot,
    OpenAiToolContinuation,
)
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_LLM_TOOL_CALL_INVALID,
    ProviderError,
)
from pantaray_llm.providers.openai_responses.input_items import (
    is_replayable_output_item,
)
from pantaray_llm.providers.openai_responses.response_error import (
    build_openai_failed_response_error,
)
from pantaray_llm.providers.openai_responses.tool_use import serialize_openai_history
from pantaray_llm.providers.schema_compiler import (
    CompiledProviderSchema,
    ProviderSchemaCompilationError,
    ProviderSchemaDecodeError,
    SchemaUsage,
    compile_provider_schema,
)
from pantaray_llm.providers.tool_call_contract import (
    ToolCallErrorContext,
    build_invalid_tool_provider_response,
    build_tool_call_contract_error,
    find_declared_tool,
    validate_tool_arguments,
)

_OPENAI_COMPLETED_STATUS = "completed"


def build_openai_tools(
    tool_use: LlmToolUseRequest | LlmActionTurnRequest,
) -> list[ToolParam]:
    tools: list[ToolParam] = []
    for tool in tool_use.tools:
        schema = _compile_openai_schema(
            tool.parameters,
            usage="tool_parameters",
        ).wire_schema
        tools.append(
            cast(
                ToolParam,
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                    "strict": True,
                },
            )
        )
    return tools


def extract_openai_native_response(
    *,
    response: object,
    input_items: list[ResponseInputItemParam],
    media_slots: list[OpenAiContinuationMediaSlot],
    tool_use: LlmToolUseRequest | LlmActionTurnRequest,
    error_context: ToolCallErrorContext,
) -> LlmToolUseResponse | LlmActionTurnResponse:
    response_status = getattr(response, "status", None)
    if response_status != _OPENAI_COMPLETED_STATUS:
        status_detail = _openai_response_status_detail(response)
        failed_response_error = build_openai_failed_response_error(
            response=response,
            details=error_context.details(),
        )
        if failed_response_error is not None:
            raise failed_response_error
        if response_status != "incomplete":
            raise build_invalid_tool_provider_response(
                context=error_context,
                message="OpenAI returned a failed or unknown response status.",
                response_status=status_detail,
            )
        raise build_tool_call_contract_error(
            reason="response_incomplete",
            context=error_context,
            response_status=status_detail,
        )
    output = getattr(response, "output", None)
    if not isinstance(output, list):
        raise build_invalid_tool_provider_response(
            context=error_context,
            message="OpenAI response output was not a list.",
            response_status=response_status,
        )
    calls = [item for item in output if isinstance(item, ResponseFunctionToolCall)]
    if not calls and isinstance(tool_use, LlmToolUseRequest):
        raise build_tool_call_contract_error(
            reason="missing_call",
            context=error_context,
            actual_call_count=0,
        )
    max_calls = tool_use.max_parallel_tool_calls
    dropped_calls: list[ResponseFunctionToolCall] = []
    if len(calls) > max_calls:
        if max_calls == 1:
            raise build_tool_call_contract_error(
                reason="multiple_calls",
                context=error_context,
                actual_call_count=len(calls),
            )
        dropped_calls = calls[max_calls:]
        calls = calls[:max_calls]
    tool_calls = [
        _extract_openai_tool_call(
            call=call,
            tool_use=tool_use,
            error_context=error_context,
        )
        for call in calls
    ]
    if isinstance(tool_use, LlmActionTurnRequest):
        messages = _extract_action_messages(output=output, error_context=error_context)
        if not messages and not tool_calls:
            reasoning_items = sum(
                isinstance(item, ResponseReasoningItem) for item in output
            )
            raise _invalid_action_turn(
                "OpenAI returned no commentary or function calls "
                f"(output items: {len(output)}, reasoning items: {reasoning_items}).",
                error_context,
            )
        return LlmActionTurnResponse(
            mode="action_turn",
            messages=messages,
            calls=tool_calls,
            dropped_call_names=[call.name for call in dropped_calls],
        )
    continuation = None
    if tool_use.continuation_mode == "stateless":
        serialized_output = [
            dumped
            for dumped in (
                item.model_dump(mode="json", exclude_none=True) for item in output
            )
            if is_replayable_output_item(dumped)
        ]
        continuation = OpenAiToolContinuation(
            provider="openai",
            history_items=serialize_openai_history(
                items=[*input_items, *serialized_output],
                media_slots=media_slots,
            ),
            media_slots=media_slots,
        )
    return LlmToolUseResponse(
        calls=tool_calls,
        dropped_call_names=[call.name for call in dropped_calls],
        continuation=continuation,
    )


def build_openai_provider_turn(
    *,
    output: list[ResponseOutputItem],
    calls: list[LlmToolCall],
) -> OpenAiProviderTurn | None:
    """Keep the turn's output items for the next turn's conversation.

    The turn records what this adapter reported: a call the batch limit dropped
    is never executed and never answered, so replaying it would leave the model
    reading a call whose result never arrives. Items that cannot be replayed at
    all go the same way, and a turn left holding nothing is no turn.
    """

    accepted_call_ids = {call.call_id for call in calls}
    items = [
        dumped
        for dumped in (
            item.model_dump(mode="json", exclude_none=True) for item in output
        )
        if is_replayable_output_item(dumped)
        and (
            dumped.get("type") != "function_call"
            or dumped.get("call_id") in accepted_call_ids
        )
    ]
    if not items:
        return None
    return OpenAiProviderTurn(provider="openai", items=items)


def _extract_action_messages(
    *, output: list[object], error_context: ToolCallErrorContext
) -> list[LlmCommentary]:
    messages: list[LlmCommentary] = []
    for item in output:
        if isinstance(item, ResponseFunctionToolCall | ResponseReasoningItem):
            continue
        if not isinstance(item, ResponseOutputMessage):
            raise _invalid_action_turn(
                "OpenAI returned an unsupported Action output item.", error_context
            )
        if item.status != _OPENAI_COMPLETED_STATUS:
            raise _invalid_action_turn(
                "OpenAI returned an unfinished Action message.", error_context
            )
        if item.phase == "final_answer":
            # The model answered in text; every answer here is a tool call.
            raise _invalid_action_turn(
                "The output was a final answer in plain text, outside any tool call.",
                error_context,
            )
        try:
            message = LlmCommentary.model_validate(
                {
                    "phase": item.phase,
                    "source_message_id": item.id,
                    "text": "".join(
                        content.text
                        for content in item.content
                        if isinstance(content, ResponseOutputText)
                    ),
                }
            )
        except ValidationError as exc:
            raise _invalid_action_turn(
                "OpenAI Action messages must have phase commentary, a source ID, "
                "and non-blank text within the commentary character limit.",
                error_context,
            ) from exc
        messages.append(message)
    return messages


def _invalid_action_turn(
    message: str, error_context: ToolCallErrorContext
) -> ProviderError:
    return ProviderError(
        status_code=502,
        code=PROXY_LLM_TOOL_CALL_INVALID,
        message=message,
        details={
            **error_context.details(),
            "tool_call_violation_reason": "invalid_response",
        },
    )


def _extract_openai_tool_call(
    *,
    call: ResponseFunctionToolCall,
    tool_use: LlmToolUseRequest | LlmActionTurnRequest,
    error_context: ToolCallErrorContext,
) -> LlmToolCall:
    if call.status != _OPENAI_COMPLETED_STATUS:
        if call.status not in {"incomplete", "in_progress"}:
            raise build_invalid_tool_provider_response(
                context=error_context,
                message="OpenAI returned a failed or unknown function-call status.",
                response_status=call.status or "missing_call_status",
            )
        raise build_tool_call_contract_error(
            reason="call_incomplete",
            context=error_context,
            tool_name=call.name,
            response_status=call.status,
        )
    declared_tool = find_declared_tool(name=call.name, tools=tool_use.tools)
    if declared_tool is None:
        raise build_tool_call_contract_error(
            reason="undeclared_tool",
            context=error_context,
            tool_name=call.name,
        )
    try:
        arguments = json.loads(call.arguments)
    except (json.JSONDecodeError, TypeError) as exc:
        raise build_tool_call_contract_error(
            reason="invalid_arguments",
            context=error_context,
            tool_name=call.name,
        ) from exc
    if not isinstance(arguments, dict):
        raise build_tool_call_contract_error(
            reason="arguments_not_object",
            context=error_context,
            tool_name=call.name,
        )
    compiled_schema = _compile_openai_schema(
        declared_tool.parameters,
        usage="tool_parameters",
    )
    try:
        restored_arguments = compiled_schema.decode(arguments)
    except ProviderSchemaDecodeError as exc:
        raise build_tool_call_contract_error(
            reason="arguments_schema_mismatch",
            context=error_context,
            tool_name=call.name,
            argument_path=exc.path,
            schema_keyword=exc.keyword,
        ) from exc
    if not isinstance(restored_arguments, dict):
        raise AssertionError("object tool arguments must remain an object")
    validation_failure = validate_tool_arguments(
        arguments=restored_arguments,
        tool=declared_tool,
    )
    if validation_failure is not None:
        raise build_tool_call_contract_error(
            reason="arguments_schema_mismatch",
            context=error_context,
            tool_name=call.name,
            argument_path=validation_failure.argument_path,
            schema_keyword=validation_failure.schema_keyword,
        )
    return LlmToolCall(
        call_id=call.call_id,
        name=call.name,
        arguments=restored_arguments,
    )


def _openai_response_status_detail(response: object) -> str:
    status = getattr(response, "status", None)
    status_text = status if isinstance(status, str) and status else "missing_status"
    incomplete_details = getattr(response, "incomplete_details", None)
    incomplete_reason = getattr(incomplete_details, "reason", None)
    if isinstance(incomplete_reason, str) and incomplete_reason:
        return f"{status_text}:{incomplete_reason}"
    return status_text


def _compile_openai_schema(
    schema: dict[str, JSONValue],
    *,
    usage: SchemaUsage,
) -> CompiledProviderSchema:
    try:
        return compile_provider_schema(
            schema=schema,
            usage=usage,
        )
    except ProviderSchemaCompilationError as exc:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=exc.args[0],
            details={"schema_path": exc.path, "schema_keyword": exc.keyword},
        ) from exc

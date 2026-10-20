"""共有契約の tool use と Anthropic Messages API の tool ブロックの相互変換。"""

from __future__ import annotations

import json

from pydantic import ValidationError

from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
    LlmCommentary,
)
from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.tool_use import (
    AnthropicToolContinuation,
    LlmToolCall,
    LlmToolContinuation,
    LlmToolResult,
    LlmToolUseRequest,
    LlmToolUseResponse,
)
from pantaray_llm.errors import (
    PROXY_CONTINUATION_PROVIDER_MISMATCH,
    PROXY_LLM_TOOL_CALL_INVALID,
    ProviderError,
)
from pantaray_llm.providers.anthropic.wire import (
    AnthropicContentBlock,
    AnthropicMessageResponse,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
    AnthropicToolUseBlock,
)
from pantaray_llm.providers.tool_call_contract import (
    ToolCallErrorContext,
    build_tool_call_contract_error,
    find_declared_tool,
    validate_tool_arguments,
)

type AnthropicToolRequest = LlmToolUseRequest | LlmActionTurnRequest

# The exact text of a progress block that stands in for unfinished work when a
# response stops soon after a tool call or result.
_INTERRUPTED_PROGRESS_UPDATE_TEXT = (
    "This part of the response was interrupted before it finished."
)


def build_anthropic_tools(tool_use: AnthropicToolRequest) -> list[JSONValue]:
    # Messages API は Draft-7 の input_schema をそのまま受け取る。`strict` は
    # 送らないので、引数の検証は応答側で宣言スキーマに照らして行う。
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
        for tool in tool_use.tools
    ]


def build_anthropic_tool_choice(tool_use: AnthropicToolRequest) -> JSONValue:
    # Opus 5.5, Sonnet 5.5 and Fable 5.1 reject a forced choice ("any" or
    # "tool") with a 400, so a tool use request asks with "auto" too. The
    # provider asks again after a reply without a call, and the response
    # contract rejects one that still has none.
    # https://platform.claude.com/docs/en/models/opus-5-5/migration-guide
    choice: dict[str, JSONValue] = {"type": "auto"}
    if tool_use.max_parallel_tool_calls == 1:
        choice["disable_parallel_tool_use"] = True
    return choice


def resume_anthropic_messages(
    *, continuation: LlmToolContinuation, tool_result: LlmToolResult
) -> list[dict[str, JSONValue]]:
    """Replay the stored turns and answer the tool call they end on.

    Messages API requires the tool_result blocks in the user message directly
    after the assistant turn that asked for them, so the answer opens that
    message. The contract keeps a stateless continuation single-call
    (`max_parallel_tool_calls` above 1 requires `disabled`), so the assistant
    turn holds one tool_use block and this one block answers it.
    """

    if not isinstance(continuation, AnthropicToolContinuation):
        raise ProviderError(
            status_code=400,
            code=PROXY_CONTINUATION_PROVIDER_MISMATCH,
            message="Anthropic profiles require an Anthropic tool continuation.",
        )
    answer: dict[str, JSONValue] = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_result.call_id,
                # The same text the OpenAI adapter puts in function_call_output,
                # so the model reads one tool result on either provider.
                "content": json.dumps(tool_result.output, ensure_ascii=False),
            }
        ],
    }
    return [*continuation.messages, answer]


def extract_anthropic_tool_response(
    *,
    message: AnthropicMessageResponse,
    tool_use: AnthropicToolRequest,
    sent_messages: list[dict[str, JSONValue]],
    progress_updates: bool,
    error_context: ToolCallErrorContext,
) -> LlmToolUseResponse | LlmActionTurnResponse:
    blocks = [
        block for block in message.content if isinstance(block, AnthropicToolUseBlock)
    ]
    if not blocks and isinstance(tool_use, LlmToolUseRequest):
        raise build_tool_call_contract_error(
            reason="missing_call", context=error_context, actual_call_count=0
        )
    max_calls = tool_use.max_parallel_tool_calls
    dropped_blocks: list[AnthropicToolUseBlock] = []
    if len(blocks) > max_calls:
        if max_calls == 1:
            raise build_tool_call_contract_error(
                reason="multiple_calls",
                context=error_context,
                actual_call_count=len(blocks),
            )
        dropped_blocks = blocks[max_calls:]
        blocks = blocks[:max_calls]
    calls = [
        _extract_tool_call(block=block, tool_use=tool_use, error_context=error_context)
        for block in blocks
    ]
    dropped_call_names = [block.name for block in dropped_blocks]
    if isinstance(tool_use, LlmActionTurnRequest):
        messages = _extract_action_messages(
            message=message,
            progress_updates=progress_updates,
        )
        if not messages and not calls:
            raise _invalid_action_turn(
                "The model provider returned no commentary or tool calls.",
                error_context,
            )
        try:
            return LlmActionTurnResponse(
                mode="action_turn",
                messages=messages,
                calls=calls,
                dropped_call_names=dropped_call_names,
            )
        except ValidationError as exc:
            raise _invalid_action_turn(
                "The model provider returned Action commentary beside tool calls "
                "over the commentary character limit.",
                error_context,
            ) from exc
    continuation = None
    if tool_use.continuation_mode == "stateless":
        continuation = AnthropicToolContinuation(
            provider="anthropic",
            # The assistant turn goes back exactly as it arrived: thinking and
            # redacted_thinking blocks keep their signature and data, and the
            # block order is the one the model signed.
            messages=[
                *sent_messages,
                {"role": "assistant", "content": message.raw_content},
            ],
        )
    return LlmToolUseResponse(
        calls=calls,
        dropped_call_names=dropped_call_names,
        continuation=continuation,
    )


def _extract_tool_call(
    *,
    block: AnthropicToolUseBlock,
    tool_use: AnthropicToolRequest,
    error_context: ToolCallErrorContext,
) -> LlmToolCall:
    declared_tool = find_declared_tool(name=block.name, tools=tool_use.tools)
    if declared_tool is None:
        raise build_tool_call_contract_error(
            reason="undeclared_tool", context=error_context, tool_name=block.name
        )
    validation_failure = validate_tool_arguments(
        arguments=block.input, tool=declared_tool
    )
    if validation_failure is not None:
        raise build_tool_call_contract_error(
            reason="arguments_schema_mismatch",
            context=error_context,
            tool_name=block.name,
            argument_path=validation_failure.argument_path,
            schema_keyword=validation_failure.schema_keyword,
        )
    return LlmToolCall(call_id=block.id, name=block.name, arguments=block.input)


def _extract_action_messages(
    *,
    message: AnthropicMessageResponse,
    progress_updates: bool,
) -> list[LlmCommentary]:
    messages: list[LlmCommentary] = []
    for index, block in enumerate(message.content):
        text = _spoken_text(block, progress_updates=progress_updates)
        if text is None:
            continue
        messages.append(
            LlmCommentary(
                phase="commentary",
                # Messages API は text ブロックに ID を付けない。呼び出し元は
                # この値からターン内で一意な発話 ID を導くので、位置で識別する。
                source_message_id=f"{message.id}:{index}",
                text=text,
            )
        )
    return messages


def _spoken_text(block: AnthropicContentBlock, *, progress_updates: bool) -> str | None:
    """What a block says to the user, if anything.

    With progress updates requested, a thinking block with text is a note the
    model wrote between tool calls, and the reasoning blocks come back empty;
    without them a thinking block's text could be reasoning and stays private.
    """

    if isinstance(block, AnthropicTextBlock):
        text = block.text
    elif progress_updates and isinstance(block, AnthropicThinkingBlock):
        text = block.thinking
        # Stands in for work a response cut short, not something to tell anyone.
        if text == _INTERRUPTED_PROGRESS_UPDATE_TEXT:
            return None
    else:
        return None
    return text if text.strip() else None


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


__all__ = [
    "AnthropicToolRequest",
    "build_anthropic_tool_choice",
    "build_anthropic_tools",
    "extract_anthropic_tool_response",
    "resume_anthropic_messages",
]

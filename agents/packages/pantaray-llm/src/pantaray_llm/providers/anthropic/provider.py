"""Anthropic Messages API アダプター（httpx 直接・非ストリーミング）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx

from pantaray_llm.contracts.action_turn import LlmActionTurnRequest
from pantaray_llm.contracts.conversation import AnthropicProviderTurn
from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.request import (
    LlmInputTextBlock,
    LlmMessageRole,
    LlmOutputMessage,
    LlmOutputTextBlock,
    LlmProxyResponse,
    LlmProxyResultMeta,
    LlmRequest,
    LlmUsagePayload,
)
from pantaray_llm.contracts.tool_use import LlmToolUseRequest
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_MODEL_OUTPUT_REJECTED,
    ProviderError,
)
from pantaray_llm.providers.anthropic.messages import (
    anthropic_invalid_input,
    build_anthropic_messages,
)
from pantaray_llm.providers.anthropic.response_error import (
    anthropic_request_details,
    anthropic_stop_reason_error,
    build_anthropic_status_error,
    invalid_anthropic_response,
)
from pantaray_llm.providers.anthropic.settings import (
    AnthropicAdaptiveThinking,
    AnthropicLlmProfile,
)
from pantaray_llm.providers.anthropic.tool_use import (
    build_anthropic_tool_choice,
    build_anthropic_tools,
    extract_anthropic_tool_response,
)
from pantaray_llm.providers.anthropic.transport import send_anthropic_message
from pantaray_llm.providers.anthropic.wire import (
    AnthropicMessageResponse,
    AnthropicToolUseBlock,
    anthropic_usage_payload,
)
from pantaray_llm.providers.model_output import model_output_error_from_proxy_error
from pantaray_llm.providers.response_error import (
    enrich_provider_error,
    transport_failure_error,
)
from pantaray_llm.providers.schema_compiler import (
    CompiledProviderSchema,
    ProviderSchemaCompilationError,
    ProviderSchemaDecodeError,
    compile_provider_schema,
)
from pantaray_llm.providers.tool_call_contract import ToolCallErrorContext
from pantaray_llm.providers.uploaded_content import reject_unreferenced_uploads

SYSTEM_ROLE: LlmMessageRole = "system"
# Requests after a text-only reply to a tool use request, which promises at
# least one call. The response contract still rejects a reply that has none.
MISSING_CALL_REPAIR_LIMIT = 2
MISSING_CALL_REPAIR_TEXT = (
    "Your reply did not call a tool. Respond by calling one of the provided tools."
)


async def execute_anthropic_request(
    *,
    request: LlmRequest,
    profile: AnthropicLlmProfile,
    uploaded_blobs: Mapping[str, UploadedBlob],
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
) -> LlmProxyResponse:
    tool_use = request.tool_use
    instructions = _require_system_instructions(request)
    messages, referenced_blob_refs = build_anthropic_messages(
        request=request,
        profile=profile,
        uploaded_blobs=uploaded_blobs,
        tool_use=tool_use,
    )
    reject_unreferenced_uploads(
        uploaded_blobs=uploaded_blobs, referenced_blob_refs=referenced_blob_refs
    )
    compiled_schema = _compile_response_schema(request)
    # prompt_cache_key は OpenAI 系だけに送る契約なので、ここでは本文に入れない。
    body: dict[str, JSONValue] = {
        "model": profile.model,
        "max_tokens": profile.max_output_tokens,
        "messages": cast(JSONValue, messages),
        # Automatic caching puts the breakpoint on the last cacheable block, so
        # it moves forward as a conversation is appended to and each turn reads
        # the previous one. It stays out of the messages themselves, which a
        # continuation stores and replays.
        # https://platform.claude.com/docs/en/build-with-claude/prompt-caching
        "cache_control": {"type": "ephemeral"},
    }
    if instructions is not None:
        # A second breakpoint closes the tools and system prefix, which requests
        # with the same tools and instructions share even when their messages
        # differ.
        body["system"] = [
            {
                "type": "text",
                "text": instructions,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    output_config: dict[str, JSONValue] = {}
    if isinstance(profile.thinking, AnthropicAdaptiveThinking):
        body["thinking"] = {"type": "adaptive"}
        output_config["effort"] = profile.thinking.effort
    if compiled_schema is not None:
        schema = cast(JSONValue, compiled_schema.wire_schema)
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        body["output_config"] = output_config
    if tool_use is not None:
        body["tools"] = build_anthropic_tools(tool_use)
        body["tool_choice"] = build_anthropic_tool_choice(tool_use)

    exchange = await _send_message(
        body=body, request=request, api_key=api_key, http_client=http_client
    )
    if isinstance(tool_use, LlmToolUseRequest):
        for _ in range(MISSING_CALL_REPAIR_LIMIT):
            if any(
                isinstance(block, AnthropicToolUseBlock)
                for block in exchange.message.content
            ):
                break
            # Without a forced tool_choice the model may answer in text alone.
            # The reply stays in the conversation and the request asks again,
            # so the resent prefix is unchanged: it reads the cache, and the
            # reply's thinking blocks stay valid.
            messages = [*messages, *_missing_call_repair(exchange.message)]
            body = {**body, "messages": cast(JSONValue, messages)}
            exchange = await _send_message(
                body=body,
                request=request,
                api_key=api_key,
                http_client=http_client,
                prior_usage=exchange.usage,
            )
    message = exchange.message
    usage = exchange.usage
    upstream_request_id = exchange.upstream_request_id
    details = exchange.details
    text = message.text()
    if tool_use is None and not text:
        empty_output = invalid_anthropic_response(
            message="The model provider returned a response without output.",
            details=details,
        )
        raise enrich_provider_error(
            empty_output, upstream_request_id, usage
        ) from empty_output
    if compiled_schema is not None:
        try:
            text = _decode_structured_output(
                text=text, compiled_schema=compiled_schema, details=details
            )
        except ProviderError as exc:
            raise enrich_provider_error(exc, upstream_request_id, usage) from exc
    tool_response = None
    if tool_use is not None:
        try:
            tool_response = extract_anthropic_tool_response(
                message=message,
                tool_use=tool_use,
                sent_messages=messages,
                error_context=ToolCallErrorContext(
                    provider="anthropic",
                    local_job_id=request.trace.local_job_id,
                    profile_id=request.purpose,
                    upstream_request_id=upstream_request_id,
                ),
            )
        except ProviderError as exc:
            return _reject_model_output(
                exc,
                request=request,
                profile=profile,
                message=message,
                usage=usage,
                upstream_request_id=upstream_request_id,
            )
    return LlmProxyResponse(
        id=message.id,
        model=profile.model,
        # Action のターンでは発話は commentary として返す。出力本文に重ねない。
        output=(
            [LlmOutputMessage(content=[LlmOutputTextBlock(text=text)])]
            if text and not isinstance(tool_use, LlmActionTurnRequest)
            else []
        ),
        tool_use=tool_response,
        provider_turn=_action_provider_turn(tool_use=tool_use, message=message),
        thinking=message.thinking(),
        finish_reason=message.stop_reason,
        usage=usage,
        meta=LlmProxyResultMeta(
            local_job_id=request.trace.local_job_id,
            upstream_provider="anthropic",
            profile_id=request.purpose,
            outcome=PROXY_OUTCOME_COMPLETE,
            upstream_request_id=upstream_request_id,
            resolved_model=message.model,
        ),
    )


@dataclass(frozen=True, slots=True)
class _Exchange:
    message: AnthropicMessageResponse
    # Every request this call has sent so far, so a repaired turn is billed whole.
    usage: LlmUsagePayload
    upstream_request_id: str | None
    details: dict[str, JSONValue]


async def _send_message(
    *,
    body: dict[str, JSONValue],
    request: LlmRequest,
    api_key: str,
    http_client: httpx.AsyncClient | None,
    prior_usage: LlmUsagePayload | None = None,
) -> _Exchange:
    try:
        http_response = await send_anthropic_message(
            api_key=api_key, body=body, client=http_client
        )
    except UnicodeEncodeError as exc:
        # httpx は本文を UTF-8 で符号化する。孤立サロゲートは JSON 入力として
        # 契約を通ってしまうので、送信前の符号化失敗は入力の誤りとして返す。
        raise anthropic_invalid_input(
            "The request contains text that cannot be encoded as UTF-8."
        ) from exc
    except httpx.TransportError as exc:
        error = transport_failure_error(
            exc, details=anthropic_request_details(request=request)
        )
        raise _with_prior_usage(error, prior_usage) from exc
    status_error = build_anthropic_status_error(response=http_response, request=request)
    if status_error is not None:
        raise _with_prior_usage(status_error, prior_usage)

    upstream_request_id = http_response.headers.get("request-id")
    details = anthropic_request_details(
        request=request, upstream_request_id=upstream_request_id
    )
    try:
        # JSON の復号失敗も pydantic の検証失敗も ValueError として届く。
        message = AnthropicMessageResponse.model_validate(http_response.json())
    except ValueError as exc:
        error = invalid_anthropic_response(
            message="The model provider returned an unreadable Messages response.",
            details=details,
        )
        raise _with_prior_usage(error, prior_usage) from exc
    usage = anthropic_usage_payload(message.usage)
    if prior_usage is not None:
        usage = _sum_usage(prior_usage, usage)
    stop_reason_error = anthropic_stop_reason_error(
        stop_reason=message.stop_reason, details=details
    )
    if stop_reason_error is not None:
        raise enrich_provider_error(
            stop_reason_error, upstream_request_id, usage
        ) from stop_reason_error
    return _Exchange(
        message=message,
        usage=usage,
        upstream_request_id=upstream_request_id,
        details=details,
    )


def _missing_call_repair(
    message: AnthropicMessageResponse,
) -> list[dict[str, JSONValue]]:
    request: dict[str, JSONValue] = {
        "role": "user",
        "content": [{"type": "text", "text": MISSING_CALL_REPAIR_TEXT}],
    }
    if not message.raw_content:
        # Messages rejects an empty assistant turn; the request alone asks again.
        return [request]
    return [{"role": "assistant", "content": message.raw_content}, request]


def _with_prior_usage(
    error: ProviderError, prior_usage: LlmUsagePayload | None
) -> ProviderError:
    # A failed repair request still leaves the earlier requests billed.
    if prior_usage is None:
        return error
    return enrich_provider_error(error, None, prior_usage)


def _sum_usage(first: LlmUsagePayload, second: LlmUsagePayload) -> LlmUsagePayload:
    return LlmUsagePayload(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        cached_prompt_tokens=first.cached_prompt_tokens + second.cached_prompt_tokens,
        cache_write_prompt_tokens=(
            first.cache_write_prompt_tokens + second.cache_write_prompt_tokens
        ),
        completion_tokens=first.completion_tokens + second.completion_tokens,
        reasoning_tokens=first.reasoning_tokens + second.reasoning_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
    )


def _reject_model_output(
    error: ProviderError,
    *,
    request: LlmRequest,
    profile: AnthropicLlmProfile,
    message: AnthropicMessageResponse,
    usage: LlmUsagePayload,
    upstream_request_id: str | None,
) -> LlmProxyResponse:
    """モデルが tool 契約を破った応答を、次ターンで直せる拒否として返す。"""

    model_error = model_output_error_from_proxy_error(error)
    if model_error is None:
        raise enrich_provider_error(error, upstream_request_id, usage) from error
    return LlmProxyResponse(
        id=message.id,
        model=profile.model,
        output=[],
        finish_reason=message.stop_reason,
        usage=usage,
        meta=LlmProxyResultMeta(
            local_job_id=request.trace.local_job_id,
            upstream_provider="anthropic",
            profile_id=request.purpose,
            outcome=PROXY_OUTCOME_MODEL_OUTPUT_REJECTED,
            upstream_request_id=upstream_request_id,
            resolved_model=message.model,
        ),
        model_error=model_error,
    )


def _action_provider_turn(
    *,
    tool_use: LlmToolUseRequest | LlmActionTurnRequest | None,
    message: AnthropicMessageResponse,
) -> AnthropicProviderTurn | None:
    """Hand this turn's blocks back, for the caller to replay on the next one.

    Only a caller that already sends a conversation can place them, so a turn
    without one gets the response it got before this existed.
    """

    if not isinstance(tool_use, LlmActionTurnRequest) or tool_use.conversation is None:
        return None
    # Every element of a parsed content array is an object: the typed view above
    # it rejects anything else before this reads the same blocks untouched.
    blocks = cast(list[dict[str, JSONValue]], message.raw_content)
    return AnthropicProviderTurn(provider="anthropic", blocks=blocks)


def _require_system_instructions(request: LlmRequest) -> str | None:
    messages = [message for message in request.messages if message.role == SYSTEM_ROLE]
    if len(messages) > 1:
        raise anthropic_invalid_input("LLM proxy accepts at most one system message.")
    if not messages:
        return None
    parts: list[str] = []
    for block in messages[0].content:
        if not isinstance(block, LlmInputTextBlock):
            raise anthropic_invalid_input(
                "Anthropic system messages accept only text content blocks."
            )
        parts.append(block.text)
    return "\n".join(parts)


def _compile_response_schema(
    request: LlmRequest,
) -> CompiledProviderSchema | None:
    if request.response_format is None:
        return None
    try:
        return compile_provider_schema(
            schema=request.response_format.json_schema, usage="structured_output"
        )
    except ProviderSchemaCompilationError as exc:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=exc.args[0],
            details={"schema_path": exc.path, "schema_keyword": exc.keyword},
        ) from exc


def _decode_structured_output(
    *,
    text: str,
    compiled_schema: CompiledProviderSchema,
    details: dict[str, JSONValue],
) -> str:
    try:
        decoded = compiled_schema.decode(json.loads(text))
    except (json.JSONDecodeError, ProviderSchemaDecodeError) as exc:
        raise invalid_anthropic_response(
            message="The model provider structured output did not match the schema.",
            details=details,
        ) from exc
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))

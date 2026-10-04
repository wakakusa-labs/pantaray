from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import cast

import httpx
from openai import AsyncOpenAI, OpenAIError
from openai.types.responses.response import Response
from openai.types.responses.response_create_params import (
    ResponseCreateParamsBase,
    ResponseCreateParamsNonStreaming,
)
from openai.types.responses.response_format_text_json_schema_config_param import (
    ResponseFormatTextJSONSchemaConfigParam,
)
from openai.types.responses.response_includable import ResponseIncludable
from openai.types.responses.response_input_item_param import ResponseInputItemParam
from openai.types.responses.response_output_item import ResponseOutputItem
from openai.types.responses.response_output_item_done_event import (
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response_text_config_param import ResponseTextConfigParam

from pantaray_llm.contracts.action_turn import LlmActionTurnRequest
from pantaray_llm.contracts.request import (
    LlmInputTextBlock,
    LlmMessage,
    LlmMessageRole,
    LlmOutputMessage,
    LlmOutputTextBlock,
    LlmProxyResponse,
    LlmProxyResultMeta,
    LlmRequest,
    LlmUsagePayload,
)
from pantaray_llm.contracts.tool_use import (
    LlmToolUseRequest,
    OpenAiContinuationMediaSlot,
)
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_MODEL_OUTPUT_REJECTED,
    LlmProvider,
    ProviderError,
)
from pantaray_llm.providers.model_output import (
    model_output_error_from_proxy_error,
)
from pantaray_llm.providers.openai_responses.input_items import (
    build_openai_conversation_items,
    build_openai_message_content,
)
from pantaray_llm.providers.openai_responses.input_policy import (
    require_openai_input_within_limit,
)
from pantaray_llm.providers.openai_responses.request_budget import (
    prepare_openai_request,
)
from pantaray_llm.providers.openai_responses.response_error import (
    build_openai_response_error,
    map_openai_exception,
    openai_request_details,
    plain_text_status_error,
)
from pantaray_llm.providers.openai_responses.retry_policy import OPENAI_SDK_MAX_RETRIES
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile
from pantaray_llm.providers.openai_responses.tool_contract import (
    build_openai_provider_turn,
    build_openai_tools,
    extract_openai_native_response,
)
from pantaray_llm.providers.openai_responses.tool_use import (
    build_openai_tool_input,
)
from pantaray_llm.providers.openai_responses.transport import (
    OpenAiResponsesTransport,
)
from pantaray_llm.providers.response_error import (
    enrich_provider_error,
    transport_failure_error,
)
from pantaray_llm.providers.schema_compiler import (
    ProviderSchemaCompilationError,
    ProviderSchemaDecodeError,
    compile_provider_schema,
)
from pantaray_llm.providers.tool_call_contract import ToolCallErrorContext
from pantaray_llm.providers.uploaded_content import (
    reject_oversized_media_references,
    reject_unreferenced_uploads,
)

OPENAI_RESPONSE_FORMAT_NAME = "pantaray_llm_response"
ENCRYPTED_REASONING_INCLUDE: ResponseIncludable = "reasoning.encrypted_content"
SYSTEM_ROLE: LlmMessageRole = "system"
USER_ROLE: LlmMessageRole = "user"


@lru_cache(maxsize=4)
def build_openai_client(transport: OpenAiResponsesTransport) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=transport.api_key,
        base_url=transport.base_url,
        default_headers=dict(transport.extra_headers),
        max_retries=OPENAI_SDK_MAX_RETRIES,
    )


async def _create_response(
    *,
    client: AsyncOpenAI,
    create_kwargs: ResponseCreateParamsNonStreaming,
    stream: bool,
) -> Response:
    if not stream:
        return await client.responses.create(**create_kwargs)
    stream_kwargs = cast(
        ResponseCreateParamsBase,
        {key: value for key, value in create_kwargs.items() if key != "stream"},
    )
    async with client.responses.stream(**stream_kwargs) as response_stream:
        completed_items: dict[int, ResponseOutputItem] = {}
        async for event in response_stream:
            if isinstance(event, ResponseOutputItemDoneEvent):
                completed_items[event.output_index] = event.item
        response = await response_stream.get_final_response()
        # Codex can leave the terminal response.output empty after streaming
        # completed items. Keep the terminal response's status and usage.
        if response.status == "completed" and not response.output and completed_items:
            return response.model_copy(
                update={"output": [completed_items[i] for i in sorted(completed_items)]}
            )
        return response


def _require_system_instructions(request: LlmRequest) -> str | None:
    role = SYSTEM_ROLE
    messages = [message for message in request.messages if message.role == role]
    if len(messages) > 1:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="LLM proxy accepts at most one system message.",
        )
    if not messages:
        return None
    parts: list[str] = []
    for block in messages[0].content:
        if not isinstance(block, LlmInputTextBlock):
            raise ProviderError(
                status_code=400,
                code=PROXY_INVALID_INPUT,
                message="OpenAI LLM profiles accept only text content blocks.",
            )
        parts.append(block.text)
    return "\n".join(parts)


def _require_single_user_message(request: LlmRequest) -> LlmMessage:
    messages = [message for message in request.messages if message.role == USER_ROLE]
    if len(messages) != 1:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="LLM proxy requires exactly one user message.",
        )
    return messages[0]


def _build_openai_initial_input(
    *,
    request: LlmRequest,
    profile: OpenAiLlmProfile,
    uploaded_blobs: Mapping[str, UploadedBlob],
) -> tuple[
    list[ResponseInputItemParam],
    list[OpenAiContinuationMediaSlot],
]:
    content, media_slots = build_openai_message_content(
        blocks=_require_single_user_message(request).content,
        profile=profile,
        uploaded_blobs=uploaded_blobs,
        item_index=0,
    )
    return (
        [cast(ResponseInputItemParam, {"role": "user", "content": content})],
        media_slots,
    )


def _build_text_config(
    request: LlmRequest,
) -> ResponseTextConfigParam | None:
    if request.response_format is None:
        return None
    try:
        schema = compile_provider_schema(
            schema=request.response_format.json_schema,
            usage="structured_output",
        ).wire_schema
    except ProviderSchemaCompilationError as exc:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=exc.args[0],
            details={"schema_path": exc.path, "schema_keyword": exc.keyword},
        ) from exc
    response_format: ResponseFormatTextJSONSchemaConfigParam = {
        "type": "json_schema",
        "name": OPENAI_RESPONSE_FORMAT_NAME,
        "schema": cast(dict[str, object], schema),
        "strict": True,
    }
    return {"format": response_format}


def _extract_usage_payload(response: object) -> LlmUsagePayload | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_details = usage.input_tokens_details
    output_details = usage.output_tokens_details
    return LlmUsagePayload(
        prompt_tokens=usage.input_tokens,
        cached_prompt_tokens=(
            input_details.cached_tokens if input_details is not None else 0
        ),
        cache_write_prompt_tokens=(
            input_details.cache_write_tokens if input_details is not None else 0
        ),
        completion_tokens=usage.output_tokens,
        reasoning_tokens=(
            output_details.reasoning_tokens if output_details is not None else 0
        ),
        total_tokens=usage.total_tokens,
    )


async def execute_openai_request(
    *,
    request: LlmRequest,
    profile: OpenAiLlmProfile,
    uploaded_blobs: Mapping[str, UploadedBlob],
    transport: OpenAiResponsesTransport,
    client: AsyncOpenAI | None = None,
) -> LlmProxyResponse:
    provider = transport.provider
    tool_use = request.tool_use
    action_request = tool_use if isinstance(tool_use, LlmActionTurnRequest) else None
    tool_use_request = tool_use if isinstance(tool_use, LlmToolUseRequest) else None
    action_turn = action_request is not None
    conversation = None if tool_use is None else tool_use.conversation
    reject_oversized_media_references(
        messages=request.messages, conversation=conversation
    )
    instructions = _require_system_instructions(request)
    initial_input, initial_media_slots = _build_openai_initial_input(
        request=request,
        profile=profile,
        uploaded_blobs=uploaded_blobs,
    )
    (
        input_value,
        media_slots,
        referenced_blob_refs,
        history_media_slot_count,
    ) = build_openai_tool_input(
        initial_input=initial_input,
        initial_media_slots=initial_media_slots,
        tool_use=tool_use_request,
    )
    if conversation is not None:
        conversation_items, conversation_slots = build_openai_conversation_items(
            conversation=conversation,
            profile=profile,
            uploaded_blobs=uploaded_blobs,
            first_item_index=len(input_value),
        )
        input_value = [*input_value, *conversation_items]
        # The conversation's media is the history side of the byte budget: it is
        # what earlier turns attached, and it gives way before the media this
        # turn is asking about, so its slots lead the list.
        media_slots = [*conversation_slots, *media_slots]
        history_media_slot_count = len(conversation_slots)
        referenced_blob_refs |= {
            slot.projection.source.blob_ref for slot in conversation_slots
        }
    reject_unreferenced_uploads(
        uploaded_blobs=uploaded_blobs,
        referenced_blob_refs=referenced_blob_refs,
    )
    create_kwargs: ResponseCreateParamsNonStreaming = {
        "model": profile.model,
        "input": input_value,
        "store": False,
        "stream": False,
    }
    if profile.max_output_tokens is not None:
        create_kwargs["max_output_tokens"] = profile.max_output_tokens
    if profile.reasoning_effort is not None:
        create_kwargs["reasoning"] = {"effort": profile.reasoning_effort}
    if instructions is not None:
        create_kwargs["instructions"] = instructions
    if request.prompt_cache_key is not None:
        create_kwargs["prompt_cache_key"] = request.prompt_cache_key
    text_config = _build_text_config(request)
    if text_config is not None:
        create_kwargs["text"] = text_config
    if tool_use is not None:
        create_kwargs["tools"] = build_openai_tools(tool_use)
        create_kwargs["tool_choice"] = "auto" if action_turn else "required"
        create_kwargs["parallel_tool_calls"] = tool_use.max_parallel_tool_calls > 1
        # Only a turn that replays the output items has a reader for the
        # encrypted reasoning they carry: a stateless tool use through its
        # continuation, an Action turn through the conversation it was sent. A
        # tool use that sends a conversation instead keeps no provider turn, so
        # it would pay for reasoning it never hands back.
        replays_output_items = (
            tool_use_request.continuation_mode == "stateless"
            if tool_use_request is not None
            else conversation is not None
        )
        if replays_output_items:
            create_kwargs["include"] = [ENCRYPTED_REASONING_INCLUDE]
    if transport.always_include_encrypted_reasoning:
        create_kwargs["include"] = [ENCRYPTED_REASONING_INCLUDE]
    prepared_request = await prepare_openai_request(
        create_kwargs=create_kwargs,
        media_slots=media_slots,
        uploaded_blobs=uploaded_blobs,
        history_media_slot_count=history_media_slot_count,
    )
    try:
        if client is None:
            client = build_openai_client(transport)
        if transport.supports_input_token_count:
            await require_openai_input_within_limit(
                client=client,
                create_kwargs=prepared_request.create_kwargs,
            )
        response = await _create_response(
            client=client,
            create_kwargs=prepared_request.create_kwargs,
            stream=transport.stream,
        )
    except (OpenAIError, ValueError, RuntimeError) as exc:
        # The SDK raises RuntimeError for missing or out-of-order SSE events.
        raise map_openai_exception(request=request, provider=provider, exc=exc) from exc
    except httpx.TransportError as exc:
        # The SDK wraps transport failures only until the response head arrives.
        # A stream that stalls or drops after that raises httpx's own exception
        # here, before any of its events has reached the caller.
        raise transport_failure_error(
            exc, details=openai_request_details(request=request, provider=provider)
        ) from exc

    response_id = response.id if isinstance(response.id, str) else None
    usage_payload = _extract_usage_payload(response)
    response_text: str | None = (
        response.output_text if isinstance(response.output_text, str) else None
    )
    response_error = build_openai_response_error(
        response=response,
        response_text=response_text,
        tool_call_expected=tool_use is not None,
        details=openai_request_details(request=request, provider=provider),
        defer_empty_output_validation=action_turn,
    )
    if response_error is not None:
        raise enrich_provider_error(
            response_error,
            response_id,
            usage_payload,
        ) from response_error
    if tool_use is None:
        status_error = plain_text_status_error(
            response=response, request=request, provider=provider
        )
        if status_error is not None:
            raise enrich_provider_error(
                status_error, response_id, usage_payload
            ) from status_error
    try:
        response_text = _decode_openai_structured_output(
            response_text=response_text,
            request=request,
            provider=provider,
        )
    except ProviderError as exc:
        raise enrich_provider_error(exc, response_id, usage_payload) from exc
    try:
        tool_response = (
            extract_openai_native_response(
                response=response,
                input_items=cast(
                    list[ResponseInputItemParam],
                    prepared_request.create_kwargs["input"],
                ),
                media_slots=prepared_request.media_slots,
                tool_use=tool_use,
                error_context=ToolCallErrorContext(
                    provider=provider,
                    local_job_id=request.trace.local_job_id,
                    profile_id=request.purpose,
                    upstream_request_id=response_id,
                ),
            )
            if tool_use is not None
            else None
        )
    except ProviderError as exc:
        model_error = model_output_error_from_proxy_error(exc)
        if model_error is None:
            raise enrich_provider_error(exc, response_id, usage_payload) from exc
        return LlmProxyResponse(
            id=response_id,
            model=profile.model,
            output=[],
            finish_reason=response.status,
            usage=usage_payload,
            meta=LlmProxyResultMeta(
                local_job_id=request.trace.local_job_id,
                upstream_provider=provider,
                profile_id=request.purpose,
                outcome=PROXY_OUTCOME_MODEL_OUTPUT_REJECTED,
                upstream_request_id=response_id,
                resolved_model=response.model,
            ),
            model_error=model_error,
        )
    return LlmProxyResponse(
        id=response_id,
        model=profile.model,
        output=(
            [LlmOutputMessage(content=[LlmOutputTextBlock(text=response_text)])]
            if response_text and not action_turn
            else []
        ),
        tool_use=tool_response,
        # Only an Action turn puts a provider turn back on the conversation it
        # sends, so only it is handed one.
        provider_turn=(
            build_openai_provider_turn(
                output=response.output, calls=tool_response.calls
            )
            if action_turn and conversation is not None and tool_response is not None
            else None
        ),
        finish_reason=response.status,
        usage=usage_payload,
        meta=LlmProxyResultMeta(
            local_job_id=request.trace.local_job_id,
            upstream_provider=provider,
            profile_id=request.purpose,
            outcome=PROXY_OUTCOME_COMPLETE,
            upstream_request_id=response_id,
            resolved_model=response.model,
        ),
    )


def _decode_openai_structured_output(
    *,
    response_text: str | None,
    request: LlmRequest,
    provider: LlmProvider,
) -> str | None:
    if response_text is None or request.response_format is None:
        return response_text
    try:
        value = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="OpenAI structured output was not valid JSON.",
            details=openai_request_details(request=request, provider=provider),
        ) from exc
    try:
        compiled = compile_provider_schema(
            schema=request.response_format.json_schema,
            usage="structured_output",
        )
        decoded = compiled.decode(value)
    except (ProviderSchemaCompilationError, ProviderSchemaDecodeError) as exc:
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="OpenAI structured output could not be decoded.",
            details=openai_request_details(request=request, provider=provider),
        ) from exc
    return json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))

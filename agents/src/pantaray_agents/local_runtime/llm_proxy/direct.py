"""Send one LLM request to the user's own provider (design 6.4 step 4, 6.5).

This module owns the mapping from a stored connection to a provider adapter and
back into the caller's response contract; the route decision, the request build
and the stop barrier stay with their own owners. A capability the model is known
to lack fails before any socket opens, every send is lent a client bound to the
caller's source lifetime, and the typed result crosses into ``ProxyResponse``
field by field with the meanings ``client.py`` parses out of the cloud's JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

import httpx
from openai import AsyncOpenAI

from pantaray_agents.local_runtime.context.source_transport import source_http_client
from pantaray_agents.local_runtime.llm_proxy.response_parsing import (
    coerce_non_empty_string,
    extract_error_usage,
    parse_response_text,
    read_llm_provider,
    read_optional_int,
    read_optional_string,
)
from pantaray_agents.local_runtime.llm_proxy.transport import (
    LLM_CONNECT_TIMEOUT_SECONDS,
    LLM_IDLE_TIMEOUT_SECONDS,
)
from pantaray_agents.local_runtime.llm_proxy.types import (
    LocalLlmProxyResultMeta,
    ProxyResponse,
)
from pantaray_agents.local_runtime.runtime.connection_store import (
    ApiKeyConnection,
    ChatGptConnection,
    LlmConnection,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.request import (
    LlmModelOutputError,
    LlmProxyResponse,
    LlmProxyResultMeta,
    LlmRequest,
    LlmUsagePayload,
)
from pantaray_llm.contracts.tool_use import LlmToolUseResponse
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_LLM_TOOL_CALL_INVALID,
    LlmProvider,
    LlmProxyExecutionError,
    ProviderError,
    ProxySuggestedAction,
    coerce_proxy_error_code,
    coerce_proxy_suggested_action,
    coerce_tool_call_violation_reason,
    is_retryable_proxy_error_code,
    resolve_proxy_recovery,
)
from pantaray_llm.profiles.direct import resolve_direct_profile
from pantaray_llm.providers.anthropic.provider import (
    MISSING_CALL_REPAIR_LIMIT,
    execute_anthropic_request,
)
from pantaray_llm.providers.openai_responses.provider import execute_openai_request
from pantaray_llm.providers.openai_responses.retry_policy import OPENAI_SDK_MAX_RETRIES
from pantaray_llm.providers.openai_responses.transport import (
    OpenAiResponsesTransport,
    chatgpt_codex_transport,
    fireworks_transport,
    openai_api_transport,
)

_PROVIDER_TIMEOUT = httpx.Timeout(
    LLM_IDLE_TIMEOUT_SECONDS, connect=LLM_CONNECT_TIMEOUT_SECONDS
)


async def execute_direct_llm_request(
    *,
    connection: LlmConnection,
    request: LlmRequest,
    uploaded_blobs: Mapping[str, UploadedBlob],
    user_id: str,
    response_schema: object | None,
) -> ProxyResponse:
    """Run one inference on the configured connection and return its response.

    ``request`` and ``response_schema`` come from one ``build_llm_request``
    result, and ``uploaded_blobs`` holds its media payloads keyed by ``blob_ref``.
    ``response_schema`` is passed because the wire schema in
    ``request.response_format`` cannot restore the caller's type.

    The caller passes a connection whose credential is live
    (``connection_store.llm_connection_can_send``); refusing a lapsed one sends
    nothing and reads nothing, so the route decision owns it.
    """

    try:
        response = await _dispatch(
            connection=connection,
            request=request,
            uploaded_blobs=uploaded_blobs,
            user_id=user_id,
        )
    except ProviderError as exc:
        raise direct_execution_error(
            exc, credential_repair=_credential_repair(connection)
        ) from exc
    return build_proxy_response(response, response_schema=response_schema)


def _credential_repair(connection: LlmConnection) -> ProxySuggestedAction:
    """How the user repairs this connection's credential when it is rejected.

    The shared contract defaults ``PROXY_AUTHENTICATION_FAILED`` to ``abort``
    because the repair depends on the credential: a lapsed OAuth login is signed
    in again, a wrong API key is re-entered. Only this side knows which it is.
    """

    return (
        "reauthenticate"
        if isinstance(connection, ChatGptConnection)
        else "configure_connection"
    )


async def _dispatch(
    *,
    connection: LlmConnection,
    request: LlmRequest,
    uploaded_blobs: Mapping[str, UploadedBlob],
    user_id: str,
) -> LlmProxyResponse:
    if isinstance(connection, ApiKeyConnection) and connection.provider == "anthropic":
        anthropic_profile = resolve_direct_profile(
            provider="anthropic", model=connection.model, request=request
        )
        async with source_http_client(
            user_id,
            timeout=_PROVIDER_TIMEOUT,
            # A tool use reply without a call is asked again on the same client.
            max_requests=1 + MISSING_CALL_REPAIR_LIMIT,
        ) as http_client:
            return await execute_anthropic_request(
                request=request,
                profile=anthropic_profile,
                uploaded_blobs=uploaded_blobs,
                api_key=connection.api_key,
                http_client=http_client,
            )
    # The other three connections speak OpenAI Responses and differ only in
    # endpoint, credential and request policy (design 6.5).
    transport = _openai_transport(connection)
    profile = resolve_direct_profile(
        provider=transport.provider, model=connection.model, request=request
    )
    async with source_http_client(
        user_id,
        timeout=_PROVIDER_TIMEOUT,
        # api.openai.com prices an input-token preflight and sends it as a
        # second POST ahead of the inference request; the others send once.
        max_requests=2 if transport.supports_input_token_count else 1,
    ) as http_client:
        # `build_openai_client` keeps its clients for the process lifetime; the
        # user's own credential must not outlive the request it was lent for.
        return await execute_openai_request(
            request=request,
            profile=profile,
            uploaded_blobs=uploaded_blobs,
            transport=transport,
            client=AsyncOpenAI(
                api_key=transport.api_key,
                base_url=transport.base_url,
                default_headers=dict(transport.extra_headers),
                http_client=http_client,
                max_retries=OPENAI_SDK_MAX_RETRIES,
            ),
        )


def _openai_transport(connection: LlmConnection) -> OpenAiResponsesTransport:
    match connection:
        case ChatGptConnection(credential=credential):
            return chatgpt_codex_transport(
                access_token=credential.access_token,
                account_id=credential.account_id,
            )
        case ApiKeyConnection(provider="openai", api_key=api_key):
            return openai_api_transport(api_key=api_key)
        case ApiKeyConnection(provider="fireworks", api_key=api_key):
            return fireworks_transport(api_key=api_key)
        case _:
            # The connection union and its provider literal are closed; a new
            # member must land here rather than reach the wrong endpoint.
            raise AssertionError(
                f"connection does not speak OpenAI Responses: {connection.kind}"
            )


def build_proxy_response(
    response: LlmProxyResponse, *, response_schema: object | None
) -> ProxyResponse:
    """Raise the model-output rejection, or return the caller's response."""

    if response.model_error is not None:
        raise _model_output_error(response, response.model_error)
    tool_use = response.tool_use
    tool_call_turn = tool_use if isinstance(tool_use, LlmToolUseResponse) else None
    thinking = response.thinking
    try:
        # The cloud route trims every text block and refuses a blank one
        # (`response_parsing.extract_text`), so a blank answer is retried there.
        text = "".join(
            coerce_non_empty_string(block.text, field_name="output.content.text")
            for message in response.output
            for block in message.content
        )
    except RuntimeError as exc:
        raise LlmProxyExecutionError(
            error_code=PROXY_INVALID_UPSTREAM_RESPONSE,
            error_message="LLM proxy returned an invalid success response.",
            retryable=True,
            recovery="retry_same_request",
            upstream_status_code=200,
            upstream_code="invalid_success_response",
            **_response_identity(response),
        ) from exc
    return ProxyResponse(
        text=text,
        usage_metadata=_usage_metadata(response.usage),
        parsed=parse_response_text(text=text, response_schema=response_schema),
        meta=_result_meta(response.meta),
        thinking=thinking if thinking is not None and thinking.strip() else None,
        tool_calls=tuple(tool_call_turn.calls) if tool_call_turn is not None else (),
        dropped_tool_call_names=(
            tuple(tool_call_turn.dropped_call_names)
            if tool_call_turn is not None
            else ()
        ),
        tool_continuation=(
            tool_call_turn.continuation if tool_call_turn is not None else None
        ),
        action_turn=tool_use if isinstance(tool_use, LlmActionTurnResponse) else None,
        provider_turn=response.provider_turn,
    )


def direct_execution_error(
    error: ProviderError, *, credential_repair: ProxySuggestedAction
) -> LlmProxyExecutionError:
    """Map a provider-boundary failure onto the caller's execution error.

    The cloud proxy serializes the same ``ProviderError`` into its HTTP error body
    and ``client.py::_parse_proxy_error`` rebuilds these fields from it. Only the
    known non-secret detail keys are read, so a credential cannot reach the caller
    even if a future adapter puts one in ``details``.

    An adapter that named the repair itself keeps it; a rejected credential it
    said nothing about takes ``credential_repair``.
    """

    details = error.details
    error_code = coerce_proxy_error_code(error.code)
    violation_reason = coerce_tool_call_violation_reason(
        details.get("tool_call_violation_reason") if details is not None else None
    )
    suggested_action = coerce_proxy_suggested_action(
        details.get("suggested_action") if details is not None else None
    )
    return LlmProxyExecutionError(
        error_code=error_code,
        error_message=error.message,
        retryable=is_retryable_proxy_error_code(error_code),
        recovery=resolve_proxy_recovery(
            error_code=error_code, tool_call_violation_reason=violation_reason
        ),
        suggested_action=(
            suggested_action
            if suggested_action is not None or error_code != PROXY_AUTHENTICATION_FAILED
            else credential_repair
        ),
        local_job_id=read_optional_string(details, "local_job_id"),
        upstream_provider=read_llm_provider(details),
        upstream_request_id=read_optional_string(details, "upstream_request_id"),
        profile_id=read_optional_string(details, "profile_id"),
        upstream_status_code=read_optional_int(details, "upstream_status_code"),
        upstream_code=read_optional_string(details, "upstream_code"),
        usage_metadata=extract_error_usage(details),
        tool_call_violation_reason=violation_reason,
        actual_tool_call_count=read_optional_int(details, "actual_tool_call_count"),
        tool_name=read_optional_string(details, "tool_name"),
        response_status=read_optional_string(details, "response_status"),
        argument_path=read_optional_string(details, "argument_path"),
        schema_keyword=read_optional_string(details, "schema_keyword"),
        media_failure_reason=read_optional_string(details, "reason"),
    )


def _model_output_error(
    response: LlmProxyResponse, model_error: LlmModelOutputError
) -> LlmProxyExecutionError:
    return LlmProxyExecutionError(
        error_code=PROXY_LLM_TOOL_CALL_INVALID,
        error_message=model_error.message,
        retryable=False,
        recovery=model_error.recovery,
        **_response_identity(response),
        tool_call_violation_reason=model_error.violation_reason,
        actual_tool_call_count=model_error.actual_tool_call_count,
        tool_name=model_error.tool_name,
        response_status=model_error.response_status,
        argument_path=model_error.argument_path,
        schema_keyword=model_error.schema_keyword,
    )


class _ResponseIdentity(TypedDict):
    local_job_id: str
    upstream_provider: LlmProvider
    upstream_request_id: str | None
    profile_id: str
    usage_metadata: dict[str, int] | None


def _response_identity(response: LlmProxyResponse) -> _ResponseIdentity:
    # What a failure on a delivered response still says about that response.
    meta = response.meta
    return {
        "local_job_id": meta.local_job_id,
        "upstream_provider": meta.upstream_provider,
        "upstream_request_id": meta.upstream_request_id,
        "profile_id": meta.profile_id,
        "usage_metadata": _usage_metadata(response.usage),
    }


def _usage_metadata(usage: LlmUsagePayload | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "cached_prompt_tokens": usage.cached_prompt_tokens,
        "cache_write_prompt_tokens": usage.cache_write_prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
    }


def _result_meta(meta: LlmProxyResultMeta) -> LocalLlmProxyResultMeta:
    payload: LocalLlmProxyResultMeta = {
        "local_job_id": meta.local_job_id,
        "upstream_provider": meta.upstream_provider,
        "profile_id": meta.profile_id,
        "outcome": meta.outcome,
    }
    if meta.upstream_request_id is not None:
        payload["upstream_request_id"] = meta.upstream_request_id
    return payload


__all__ = ["execute_direct_llm_request"]

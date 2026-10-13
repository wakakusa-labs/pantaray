from __future__ import annotations

from collections.abc import Mapping

from openai import APIConnectionError, APIStatusError
from openai.types.responses.response_function_tool_call import (
    ResponseFunctionToolCall,
)
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal

from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.request import LlmRequest
from pantaray_llm.errors import (
    MODEL_REFUSAL_RESPONSE_STATUS,
    PROXY_AUTHENTICATION_FAILED,
    PROXY_INSUFFICIENT_BALANCE,
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_LLM_TOOL_CALL_INVALID,
    PROXY_REQUEST_FAILED,
    PROXY_UPSTREAM_FORBIDDEN,
    PROXY_UPSTREAM_INTERNAL_ERROR,
    PROXY_UPSTREAM_NOT_FOUND,
    PROXY_UPSTREAM_RATE_LIMITED,
    PROXY_UPSTREAM_UNAVAILABLE,
    LlmProvider,
    ProviderError,
)

UPSTREAM_PROVIDER_OPENAI: LlmProvider = "openai"

# A 429 is not always a wait-and-retry limit: the ChatGPT backend also answers it
# when the signed-in plan does not include this model, and api.openai.com when the
# account's quota is spent. Codex reads the same `error.type` to stop retrying.
# https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/api_bridge.rs
_EXHAUSTED_USAGE_ERROR_TYPES = frozenset({"usage_not_included", "insufficient_quota"})


def build_openai_response_error(
    *,
    response: object,
    response_text: str | None,
    tool_call_expected: bool,
    details: Mapping[str, JSONValue],
    defer_empty_output_validation: bool = False,
) -> ProviderError | None:
    response_status = getattr(response, "status", None)
    incomplete_details = getattr(response, "incomplete_details", None)
    incomplete_reason = getattr(incomplete_details, "reason", None)
    if response_status == "incomplete" and incomplete_reason == "content_filter":
        error_details = dict(details)
        error_details["response_status"] = MODEL_REFUSAL_RESPONSE_STATUS
        error_details["upstream_code"] = incomplete_reason
        return ProviderError(
            403,
            PROXY_UPSTREAM_FORBIDDEN,
            "OpenAI refused the request.",
            error_details,
        )
    if response_status != "completed":
        return None

    output = getattr(response, "output", None)
    output_items = output if isinstance(output, list) else []
    has_refusal = any(
        isinstance(item, ResponseOutputMessage)
        and any(isinstance(content, ResponseOutputRefusal) for content in item.content)
        for item in output_items
    )
    if has_refusal:
        error_details = dict(details)
        error_details["response_status"] = MODEL_REFUSAL_RESPONSE_STATUS
        return ProviderError(
            403,
            PROXY_UPSTREAM_FORBIDDEN,
            "OpenAI refused the request.",
            error_details,
        )

    has_expected_tool_call = tool_call_expected and any(
        isinstance(item, ResponseFunctionToolCall) for item in output_items
    )
    if response_text or has_expected_tool_call or defer_empty_output_validation:
        return None
    error_details = dict(details)
    error_details["response_status"] = "completed"
    return ProviderError(
        502,
        PROXY_INVALID_UPSTREAM_RESPONSE,
        "OpenAI returned a completed response without output.",
        error_details,
    )


def build_openai_failed_response_error(
    *,
    response: object,
    details: Mapping[str, JSONValue],
) -> ProviderError | None:
    if getattr(response, "status", None) != "failed":
        return None
    provider_error = getattr(response, "error", None)
    upstream_code = getattr(provider_error, "code", None)
    if not isinstance(upstream_code, str):
        return None

    error_details = dict(details)
    error_details["response_status"] = "failed"
    error_details["upstream_code"] = upstream_code
    if upstream_code == "server_error":
        return ProviderError(
            500, PROXY_UPSTREAM_INTERNAL_ERROR, "OpenAI failed.", error_details
        )
    if upstream_code == "rate_limit_exceeded":
        return ProviderError(
            429, PROXY_UPSTREAM_RATE_LIMITED, "OpenAI is rate-limited.", error_details
        )
    if upstream_code == "vector_store_timeout":
        return ProviderError(
            502, PROXY_UPSTREAM_UNAVAILABLE, "OpenAI timed out.", error_details
        )
    if upstream_code in {"bio_policy", "image_content_policy_violation"}:
        error_details["response_status"] = MODEL_REFUSAL_RESPONSE_STATUS
        return ProviderError(
            403, PROXY_UPSTREAM_FORBIDDEN, "OpenAI refused the request.", error_details
        )
    return ProviderError(
        400, PROXY_INVALID_INPUT, "OpenAI rejected the input.", error_details
    )


def openai_request_details(
    *,
    request: LlmRequest,
    provider: LlmProvider,
    upstream_status_code: int | None = None,
    upstream_request_id: str | None = None,
) -> dict[str, JSONValue]:
    details: dict[str, JSONValue] = {
        "local_job_id": request.trace.local_job_id,
        "upstream_provider": provider,
        "profile_id": request.purpose,
    }
    if upstream_status_code is not None:
        details["upstream_status_code"] = upstream_status_code
    if upstream_request_id is not None:
        details["upstream_request_id"] = upstream_request_id
    return details


def _upstream_error_type(body: object) -> str | None:
    """The `error.type` the provider named, for classification rather than display.

    `APIStatusError.body` is already the response body's `error` object.
    """

    if not isinstance(body, Mapping):
        return None
    error_type = body.get("type")
    return error_type if isinstance(error_type, str) and error_type else None


def map_openai_exception(
    *, request: LlmRequest, provider: LlmProvider, exc: Exception
) -> ProviderError:
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        request_id = exc.request_id
        upstream_code = _upstream_error_type(exc.body) if status == 429 else None
        input_rejection = (400, PROXY_INVALID_INPUT, "Provider input rejected.")
        mappings = {
            400: input_rejection,
            408: (
                408,
                PROXY_UPSTREAM_UNAVAILABLE,
                "The model provider request timed out.",
            ),
            409: (409, PROXY_UPSTREAM_UNAVAILABLE, "Temporary provider conflict."),
            401: (
                502,
                PROXY_AUTHENTICATION_FAILED,
                "The model provider authentication failed.",
            ),
            403: (
                403,
                PROXY_UPSTREAM_FORBIDDEN,
                "The model provider refused the request.",
            ),
            404: (
                404,
                PROXY_UPSTREAM_NOT_FOUND,
                "The requested model resource was not found.",
            ),
            429: (
                429,
                PROXY_UPSTREAM_RATE_LIMITED,
                "The model provider is rate-limited.",
            ),
            500: (
                500,
                PROXY_UPSTREAM_INTERNAL_ERROR,
                "The model provider returned an internal server error.",
            ),
        }
        if upstream_code in _EXHAUSTED_USAGE_ERROR_TYPES:
            response_status, code, message = (
                402,
                PROXY_INSUFFICIENT_BALANCE,
                "The model provider account has no usage left for this model.",
            )
        elif status in mappings:
            response_status, code, message = mappings[status]
        elif 400 <= status < 500:
            response_status, code, message = input_rejection
        else:
            response_status, code, message = (
                status if status in {503, 504} else 502,
                PROXY_UPSTREAM_UNAVAILABLE,
                "The model provider is temporarily unavailable.",
            )
        details = openai_request_details(
            request=request,
            provider=provider,
            upstream_status_code=status,
            upstream_request_id=request_id,
        )
        if upstream_code is not None:
            details["upstream_code"] = upstream_code
        return ProviderError(
            status_code=response_status,
            code=code,
            message=message,
            details=details,
        )
    if isinstance(exc, APIConnectionError):
        return ProviderError(
            status_code=502,
            code=PROXY_UPSTREAM_UNAVAILABLE,
            message="The model provider is temporarily unavailable.",
            details=openai_request_details(request=request, provider=provider),
        )
    return ProviderError(
        status_code=502,
        code=PROXY_REQUEST_FAILED,
        message="The model provider request failed before a valid response was returned.",
        details=openai_request_details(request=request, provider=provider),
    )


def plain_text_status_error(
    *, response: object, request: LlmRequest, provider: LlmProvider
) -> ProviderError | None:
    status = getattr(response, "status", None)
    if status == "completed":
        return None
    incomplete_details = getattr(response, "incomplete_details", None)
    incomplete_reason = getattr(incomplete_details, "reason", None)
    status_detail = status if isinstance(status, str) and status else "missing_status"
    if isinstance(incomplete_reason, str) and incomplete_reason:
        status_detail = f"{status_detail}:{incomplete_reason}"
    details = openai_request_details(request=request, provider=provider)
    details["response_status"] = status_detail
    if status == "incomplete":
        details["tool_call_violation_reason"] = "response_incomplete"
        return ProviderError(
            status_code=502,
            code=PROXY_LLM_TOOL_CALL_INVALID,
            message=f"OpenAI returned an incomplete response: {status_detail}.",
            details=details,
        )
    failed_response_error = build_openai_failed_response_error(
        response=response,
        details=details,
    )
    if failed_response_error is None:
        return ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="OpenAI returned a failed or unknown response status.",
            details=details,
        )
    return failed_response_error

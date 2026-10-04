from __future__ import annotations

import httpx

from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.request import LlmUsagePayload
from pantaray_llm.errors import PROXY_UPSTREAM_UNAVAILABLE, ProviderError

PROMPT_TOKENS_DETAIL = "prompt_tokens"
CACHED_PROMPT_TOKENS_DETAIL = "cached_prompt_tokens"
CACHE_WRITE_PROMPT_TOKENS_DETAIL = "cache_write_prompt_tokens"
COMPLETION_TOKENS_DETAIL = "completion_tokens"
TOTAL_TOKENS_DETAIL = "total_tokens"


def enrich_provider_error(
    error: ProviderError,
    upstream_request_id: str | None,
    usage: LlmUsagePayload | None,
) -> ProviderError:
    """Preserve correlation and billable usage after a provider responded."""

    details: dict[str, JSONValue] = dict(error.details or {})
    if upstream_request_id is not None:
        details["upstream_request_id"] = upstream_request_id
    if usage is not None:
        details.update(
            {
                PROMPT_TOKENS_DETAIL: usage.prompt_tokens,
                CACHED_PROMPT_TOKENS_DETAIL: usage.cached_prompt_tokens,
                CACHE_WRITE_PROMPT_TOKENS_DETAIL: usage.cache_write_prompt_tokens,
                COMPLETION_TOKENS_DETAIL: usage.completion_tokens,
                TOTAL_TOKENS_DETAIL: usage.total_tokens,
            }
        )
    return ProviderError(
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=details,
    )


def transport_failure_error(
    exc: httpx.TransportError, *, details: dict[str, JSONValue]
) -> ProviderError:
    """A response the provider never finished: no connection, a dropped one, or
    silence past the read timeout. Nothing was delivered, so the same request may
    be sent again.
    """

    timed_out = isinstance(exc, httpx.TimeoutException)
    return ProviderError(
        status_code=408 if timed_out else 502,
        code=PROXY_UPSTREAM_UNAVAILABLE,
        message=(
            "The model provider request timed out."
            if timed_out
            else "The model provider is temporarily unavailable."
        ),
        details=details,
    )

from __future__ import annotations

from typing import Literal, Required, TypedDict, cast, get_args

type ProxySurfaceId = Literal["web_search", "web_extract", "web_crawl", "llm"]
type LlmProvider = Literal[
    "openai",
    "openai_codex",
    "fireworks",
    "anthropic",
]
# Runtime membership check for values decoded from the wire.
LLM_PROVIDERS: frozenset[str] = frozenset(get_args(LlmProvider.__value__))
type ProxyProvider = Literal["tavily", "openai"]
type ProxyErrorCode = Literal[
    "PROXY_CONNECTION_NOT_CONFIGURED",
    "PROXY_CONTINUATION_PROVIDER_MISMATCH",
    "PROXY_MODEL_CAPABILITY_UNSUPPORTED",
    "PROXY_INVALID_INPUT",
    "PROXY_EMBEDDING_REJECTED",
    "PROXY_AUTHENTICATION_FAILED",
    "PROXY_INSUFFICIENT_BALANCE",
    "PROXY_WALLET_CHECK_FAILED",
    "PROXY_UPSTREAM_RATE_LIMITED",
    "PROXY_UPSTREAM_INTERNAL_ERROR",
    "PROXY_UPSTREAM_UNAVAILABLE",
    "PROXY_UPSTREAM_FORBIDDEN",
    "PROXY_UPSTREAM_NOT_FOUND",
    "PROXY_LLM_TOOL_CALL_INVALID",
    "PROXY_INVALID_UPSTREAM_RESPONSE",
    "PROXY_REQUEST_FAILED",
]
type ToolCallViolationReason = Literal[
    "missing_call",
    "multiple_calls",
    "undeclared_tool",
    "invalid_arguments",
    "arguments_not_object",
    "arguments_schema_mismatch",
    "response_incomplete",
    "call_incomplete",
    "response_blocked",
    "malformed_call",
    "invalid_response",
]
type ProxySuggestedAction = Literal[
    "configure_connection",
    "reauthenticate",
    "refine_query",
    "change_url",
    "narrow_scope",
    "retry_later",
    "switch_tool",
    "abort",
    "adjust_input",
]
type ProxyResultOutcome = Literal[
    "complete",
    "no_results",
    "partial_results",
    "model_output_rejected",
]
type LlmRecoveryStrategy = Literal[
    "retry_same_request",
    "repair_next_turn",
    "stop",
]

PROXY_CONNECTION_NOT_CONFIGURED: ProxyErrorCode = "PROXY_CONNECTION_NOT_CONFIGURED"
PROXY_CONTINUATION_PROVIDER_MISMATCH: ProxyErrorCode = (
    "PROXY_CONTINUATION_PROVIDER_MISMATCH"
)
PROXY_MODEL_CAPABILITY_UNSUPPORTED: ProxyErrorCode = (
    "PROXY_MODEL_CAPABILITY_UNSUPPORTED"
)
PROXY_INVALID_INPUT: ProxyErrorCode = "PROXY_INVALID_INPUT"
PROXY_EMBEDDING_REJECTED: ProxyErrorCode = "PROXY_EMBEDDING_REJECTED"
PROXY_AUTHENTICATION_FAILED: ProxyErrorCode = "PROXY_AUTHENTICATION_FAILED"
PROXY_INSUFFICIENT_BALANCE: ProxyErrorCode = "PROXY_INSUFFICIENT_BALANCE"
PROXY_WALLET_CHECK_FAILED: ProxyErrorCode = "PROXY_WALLET_CHECK_FAILED"
PROXY_UPSTREAM_RATE_LIMITED: ProxyErrorCode = "PROXY_UPSTREAM_RATE_LIMITED"
PROXY_UPSTREAM_INTERNAL_ERROR: ProxyErrorCode = "PROXY_UPSTREAM_INTERNAL_ERROR"
PROXY_UPSTREAM_UNAVAILABLE: ProxyErrorCode = "PROXY_UPSTREAM_UNAVAILABLE"
PROXY_UPSTREAM_FORBIDDEN: ProxyErrorCode = "PROXY_UPSTREAM_FORBIDDEN"
PROXY_UPSTREAM_NOT_FOUND: ProxyErrorCode = "PROXY_UPSTREAM_NOT_FOUND"
PROXY_LLM_TOOL_CALL_INVALID: ProxyErrorCode = "PROXY_LLM_TOOL_CALL_INVALID"
PROXY_INVALID_UPSTREAM_RESPONSE: ProxyErrorCode = "PROXY_INVALID_UPSTREAM_RESPONSE"
PROXY_REQUEST_FAILED: ProxyErrorCode = "PROXY_REQUEST_FAILED"

PROXY_SUGGESTED_ACTION_REFINE_QUERY: ProxySuggestedAction = "refine_query"
PROXY_SUGGESTED_ACTION_CHANGE_URL: ProxySuggestedAction = "change_url"
PROXY_SUGGESTED_ACTION_NARROW_SCOPE: ProxySuggestedAction = "narrow_scope"
PROXY_SUGGESTED_ACTION_RETRY_LATER: ProxySuggestedAction = "retry_later"
PROXY_SUGGESTED_ACTION_SWITCH_TOOL: ProxySuggestedAction = "switch_tool"
PROXY_SUGGESTED_ACTION_ABORT: ProxySuggestedAction = "abort"
PROXY_SUGGESTED_ACTION_ADJUST_INPUT: ProxySuggestedAction = "adjust_input"

PROXY_OUTCOME_COMPLETE: ProxyResultOutcome = "complete"
PROXY_OUTCOME_NO_RESULTS: ProxyResultOutcome = "no_results"
PROXY_OUTCOME_PARTIAL_RESULTS: ProxyResultOutcome = "partial_results"
PROXY_OUTCOME_MODEL_OUTPUT_REJECTED: ProxyResultOutcome = "model_output_rejected"

PROXY_ERROR_SEVERITY = "error"

# The model declined to answer: PROXY_UPSTREAM_FORBIDDEN with this
# `response_status`. A FORBIDDEN without it is an access denial (an HTTP 403),
# which the user cannot fix by changing the model or starting over.
MODEL_REFUSAL_RESPONSE_STATUS = "refusal"

_VALID_ERROR_CODES: frozenset[str] = frozenset(get_args(ProxyErrorCode.__value__))
_VALID_SUGGESTED_ACTIONS: frozenset[str] = frozenset(
    get_args(ProxySuggestedAction.__value__)
)

_VALID_TOOL_CALL_VIOLATION_REASONS: frozenset[str] = frozenset(
    {
        "missing_call",
        "multiple_calls",
        "undeclared_tool",
        "invalid_arguments",
        "arguments_not_object",
        "arguments_schema_mismatch",
        "response_incomplete",
        "call_incomplete",
        "response_blocked",
        "malformed_call",
        "invalid_response",
    }
)


class ProxyErrorDetails(TypedDict, total=False):
    surface_id: Required[str]
    retryable: Required[bool]
    recovery: Required[LlmRecoveryStrategy]
    suggested_action: Required[ProxySuggestedAction]
    request_id: str
    local_job_id: str
    upstream_provider: LlmProvider | ProxyProvider
    upstream_request_id: str
    upstream_status_code: int
    upstream_code: str
    profile_id: str
    tool_call_violation_reason: ToolCallViolationReason
    actual_tool_call_count: int
    tool_name: str
    response_status: str
    argument_path: str
    schema_keyword: str


class ProxyAgentErrorPayload(TypedDict):
    error_type: str
    error_code: str
    error_message: str
    error_details: ProxyErrorDetails
    severity: str
    metadata: None


class ProxyToolResultMeta(TypedDict, total=False):
    """Metadata for tool/proxy result payloads; not for LLM proxy responses."""

    request_id: Required[str]
    upstream_provider: Required[ProxyProvider]
    profile_id: Required[str]
    outcome: Required[ProxyResultOutcome]
    upstream_request_id: str
    suggested_action: ProxySuggestedAction


def is_retryable_proxy_error_code(error_code: ProxyErrorCode) -> bool:
    return error_code in {
        PROXY_INVALID_UPSTREAM_RESPONSE,
        PROXY_WALLET_CHECK_FAILED,
        PROXY_UPSTREAM_RATE_LIMITED,
        PROXY_UPSTREAM_INTERNAL_ERROR,
        PROXY_UPSTREAM_UNAVAILABLE,
        PROXY_REQUEST_FAILED,
    }


def coerce_proxy_error_code(
    value: object,
    *,
    default_code: ProxyErrorCode = PROXY_REQUEST_FAILED,
) -> ProxyErrorCode:
    if isinstance(value, str) and value in _VALID_ERROR_CODES:
        return cast(ProxyErrorCode, value)
    return default_code


def coerce_proxy_suggested_action(value: object) -> ProxySuggestedAction | None:
    if isinstance(value, str) and value in _VALID_SUGGESTED_ACTIONS:
        return cast(ProxySuggestedAction, value)
    return None


def coerce_tool_call_violation_reason(
    value: object,
) -> ToolCallViolationReason | None:
    if isinstance(value, str) and value in _VALID_TOOL_CALL_VIOLATION_REASONS:
        return cast(ToolCallViolationReason, value)
    return None


def build_proxy_agent_error(
    *,
    surface_id: ProxySurfaceId,
    error_code: ProxyErrorCode,
    request_id: str | None = None,
    local_job_id: str | None = None,
    upstream_provider: LlmProvider | ProxyProvider | None = None,
    upstream_request_id: str | None = None,
    upstream_status_code: int | None = None,
    upstream_code: str | None = None,
    profile_id: str | None = None,
    tool_call_violation_reason: ToolCallViolationReason | None = None,
    actual_tool_call_count: int | None = None,
    tool_name: str | None = None,
    response_status: str | None = None,
    argument_path: str | None = None,
    schema_keyword: str | None = None,
    recovery: LlmRecoveryStrategy | None = None,
    suggested_action: ProxySuggestedAction | None = None,
) -> ProxyAgentErrorPayload:
    default_recovery = resolve_proxy_recovery(
        error_code=error_code,
        tool_call_violation_reason=tool_call_violation_reason,
    )
    recovery = recovery or default_recovery
    if suggested_action is None:
        # An explicit boundary stop supersedes generic retry/repair guidance.
        suggested_action = (
            PROXY_SUGGESTED_ACTION_ABORT
            if recovery == "stop" and default_recovery != "stop"
            else _resolve_suggested_action(surface_id=surface_id, error_code=error_code)
        )
    error_details: ProxyErrorDetails = {
        "surface_id": surface_id,
        "retryable": recovery == "retry_same_request",
        "recovery": recovery,
        "suggested_action": suggested_action,
    }
    if request_id is not None:
        error_details["request_id"] = request_id
    if local_job_id is not None:
        error_details["local_job_id"] = local_job_id
    if upstream_provider is not None:
        error_details["upstream_provider"] = upstream_provider
    if upstream_request_id is not None:
        error_details["upstream_request_id"] = upstream_request_id
    if upstream_status_code is not None:
        error_details["upstream_status_code"] = upstream_status_code
    if upstream_code is not None:
        error_details["upstream_code"] = upstream_code
    if profile_id is not None:
        error_details["profile_id"] = profile_id
    if tool_call_violation_reason is not None:
        error_details["tool_call_violation_reason"] = tool_call_violation_reason
    if actual_tool_call_count is not None:
        error_details["actual_tool_call_count"] = actual_tool_call_count
    if tool_name is not None:
        error_details["tool_name"] = tool_name
    if response_status is not None:
        error_details["response_status"] = response_status
    if argument_path is not None:
        error_details["argument_path"] = argument_path
    if schema_keyword is not None:
        error_details["schema_keyword"] = schema_keyword
    return {
        "error_type": _resolve_error_type(error_code),
        "error_code": error_code,
        "error_message": _resolve_error_message(
            surface_id=surface_id,
            error_code=error_code,
        ),
        "error_details": error_details,
        "severity": PROXY_ERROR_SEVERITY,
        "metadata": None,
    }


def resolve_proxy_recovery(
    *,
    error_code: ProxyErrorCode,
    tool_call_violation_reason: ToolCallViolationReason | None,
) -> LlmRecoveryStrategy:
    if error_code == PROXY_LLM_TOOL_CALL_INVALID:
        return (
            "stop"
            if tool_call_violation_reason == "response_blocked"
            else "repair_next_turn"
        )
    return "retry_same_request" if is_retryable_proxy_error_code(error_code) else "stop"


def build_proxy_tool_result_meta(
    *,
    request_id: str,
    upstream_provider: ProxyProvider,
    profile_id: str,
    outcome: ProxyResultOutcome,
    upstream_request_id: str | None = None,
    suggested_action: ProxySuggestedAction | None = None,
) -> ProxyToolResultMeta:
    payload: ProxyToolResultMeta = {
        "request_id": request_id,
        "upstream_provider": upstream_provider,
        "profile_id": profile_id,
        "outcome": outcome,
    }
    if upstream_request_id is not None:
        payload["upstream_request_id"] = upstream_request_id
    if suggested_action is not None:
        payload["suggested_action"] = suggested_action
    return payload


def _resolve_error_type(error_code: ProxyErrorCode) -> str:
    if error_code == PROXY_INVALID_INPUT:
        return "validation_error"
    return "internal_error"


def _resolve_suggested_action(
    *,
    surface_id: ProxySurfaceId,
    error_code: ProxyErrorCode,
) -> ProxySuggestedAction:
    if error_code in {
        PROXY_CONNECTION_NOT_CONFIGURED,
        PROXY_MODEL_CAPABILITY_UNSUPPORTED,
    }:
        return "configure_connection"
    if error_code == PROXY_CONTINUATION_PROVIDER_MISMATCH:
        return PROXY_SUGGESTED_ACTION_ABORT
    if error_code == PROXY_AUTHENTICATION_FAILED:
        return PROXY_SUGGESTED_ACTION_ABORT
    if error_code == PROXY_INSUFFICIENT_BALANCE:
        return PROXY_SUGGESTED_ACTION_ABORT
    if error_code in {
        PROXY_WALLET_CHECK_FAILED,
        PROXY_UPSTREAM_RATE_LIMITED,
        PROXY_UPSTREAM_INTERNAL_ERROR,
        PROXY_UPSTREAM_UNAVAILABLE,
        PROXY_REQUEST_FAILED,
    }:
        return PROXY_SUGGESTED_ACTION_RETRY_LATER
    if error_code == PROXY_INVALID_UPSTREAM_RESPONSE:
        return (
            PROXY_SUGGESTED_ACTION_RETRY_LATER
            if surface_id == "llm"
            else PROXY_SUGGESTED_ACTION_SWITCH_TOOL
        )
    if error_code == PROXY_LLM_TOOL_CALL_INVALID:
        return PROXY_SUGGESTED_ACTION_ADJUST_INPUT
    if surface_id == "llm":
        return PROXY_SUGGESTED_ACTION_ADJUST_INPUT
    if surface_id == "web_search":
        return PROXY_SUGGESTED_ACTION_REFINE_QUERY
    if error_code == PROXY_INVALID_INPUT:
        return PROXY_SUGGESTED_ACTION_NARROW_SCOPE
    return PROXY_SUGGESTED_ACTION_CHANGE_URL


def _resolve_error_message(
    *,
    surface_id: ProxySurfaceId,
    error_code: ProxyErrorCode,
) -> str:
    if error_code == PROXY_CONNECTION_NOT_CONFIGURED:
        return "No connection is configured. Configure a connection before retrying."
    if error_code == PROXY_CONTINUATION_PROVIDER_MISMATCH:
        return "The connection changed. Restart the job without the old continuation."
    if error_code == PROXY_MODEL_CAPABILITY_UNSUPPORTED:
        return "The model lacks a required capability. Select a compatible model."
    if error_code == PROXY_AUTHENTICATION_FAILED:
        return (
            "The external provider authentication failed. Stop and surface the "
            "infrastructure issue."
        )
    if error_code == PROXY_INSUFFICIENT_BALANCE:
        return "Wallet balance is insufficient. Recharge before retrying."
    if error_code == PROXY_WALLET_CHECK_FAILED:
        return (
            "Wallet verification failed before inference started. Retry later or "
            "surface the infrastructure issue."
        )
    if error_code == PROXY_UPSTREAM_RATE_LIMITED:
        return (
            "The external provider is rate-limited. Retry later rather than "
            "changing the request."
        )
    if error_code == PROXY_UPSTREAM_INTERNAL_ERROR:
        return (
            "The external provider returned an internal server error. Retry later "
            "rather than changing the request."
        )
    if error_code == PROXY_UPSTREAM_UNAVAILABLE:
        return "The external provider is temporarily unavailable. Retry later."
    if error_code == PROXY_INVALID_UPSTREAM_RESPONSE:
        if surface_id == "llm":
            return (
                "The model provider returned an invalid response. Retry later or "
                "adjust the request."
            )
        return (
            "The provider returned an invalid response. Do not reuse this result; "
            "retry later or switch strategy."
        )
    if error_code == PROXY_LLM_TOOL_CALL_INVALID:
        return "The model returned an invalid native tool call."
    if error_code == PROXY_REQUEST_FAILED:
        return (
            "The request failed before a valid response was returned. Retry later "
            "if it is still needed."
        )
    if error_code == PROXY_INVALID_INPUT:
        return _invalid_input_message(surface_id)
    if error_code == PROXY_UPSTREAM_FORBIDDEN:
        if surface_id == "llm":
            return (
                "The model provider refused the request. Adjust the prompt or retry "
                "with a safer request."
            )
        if surface_id == "web_search":
            return (
                "The search request could not access relevant content. Refine the "
                "query or choose another source."
            )
        return (
            "The target content could not be accessed. Try a different URL or source."
        )
    if error_code == PROXY_UPSTREAM_NOT_FOUND:
        if surface_id == "llm":
            return (
                "The requested model resource was not found. Check the profile or "
                "provider configuration."
            )
        return (
            "The requested page was not found. Verify the URL or choose another source."
        )
    return "The proxy request failed."


def _invalid_input_message(surface_id: ProxySurfaceId) -> str:
    if surface_id == "web_search":
        return (
            "The web search arguments are invalid. Fix the query, topic, or "
            "country and try again."
        )
    if surface_id == "web_extract":
        return (
            "The content extraction arguments are invalid. Provide valid public "
            "URLs and try again."
        )
    if surface_id == "web_crawl":
        return (
            "The website crawl arguments are invalid. Provide a valid starting URL "
            "and narrower instructions if needed."
        )
    return (
        "The model request is invalid. Fix the prompt, profile, or response schema "
        "and try again."
    )


__all__ = [
    "MODEL_REFUSAL_RESPONSE_STATUS",
    "PROXY_CONNECTION_NOT_CONFIGURED",
    "PROXY_CONTINUATION_PROVIDER_MISMATCH",
    "PROXY_MODEL_CAPABILITY_UNSUPPORTED",
    "coerce_proxy_suggested_action",
    "PROXY_AUTHENTICATION_FAILED",
    "PROXY_ERROR_SEVERITY",
    "PROXY_EMBEDDING_REJECTED",
    "PROXY_INSUFFICIENT_BALANCE",
    "PROXY_INVALID_INPUT",
    "PROXY_INVALID_UPSTREAM_RESPONSE",
    "PROXY_LLM_TOOL_CALL_INVALID",
    "PROXY_OUTCOME_COMPLETE",
    "PROXY_OUTCOME_NO_RESULTS",
    "PROXY_OUTCOME_PARTIAL_RESULTS",
    "PROXY_REQUEST_FAILED",
    "PROXY_WALLET_CHECK_FAILED",
    "PROXY_SUGGESTED_ACTION_ABORT",
    "PROXY_SUGGESTED_ACTION_ADJUST_INPUT",
    "PROXY_SUGGESTED_ACTION_CHANGE_URL",
    "PROXY_SUGGESTED_ACTION_NARROW_SCOPE",
    "PROXY_SUGGESTED_ACTION_REFINE_QUERY",
    "PROXY_SUGGESTED_ACTION_RETRY_LATER",
    "PROXY_SUGGESTED_ACTION_SWITCH_TOOL",
    "PROXY_UPSTREAM_FORBIDDEN",
    "PROXY_UPSTREAM_INTERNAL_ERROR",
    "PROXY_UPSTREAM_NOT_FOUND",
    "PROXY_UPSTREAM_RATE_LIMITED",
    "PROXY_UPSTREAM_UNAVAILABLE",
    "ProxyAgentErrorPayload",
    "ProxyErrorCode",
    "ProxyErrorDetails",
    "ProxyProvider",
    "ProxyToolResultMeta",
    "ProxyResultOutcome",
    "ProxySuggestedAction",
    "ProxySurfaceId",
    "ToolCallViolationReason",
    "build_proxy_agent_error",
    "build_proxy_tool_result_meta",
    "coerce_proxy_error_code",
    "coerce_tool_call_violation_reason",
    "is_retryable_proxy_error_code",
    "resolve_proxy_recovery",
]

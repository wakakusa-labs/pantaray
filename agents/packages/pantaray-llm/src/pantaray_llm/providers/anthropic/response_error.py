"""Anthropic の失敗応答をプロキシのエラー契約へ写像する。"""

from __future__ import annotations

import httpx

from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.contracts.request import LlmRequest
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_INSUFFICIENT_BALANCE,
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_LLM_TOOL_CALL_INVALID,
    PROXY_UPSTREAM_FORBIDDEN,
    PROXY_UPSTREAM_INTERNAL_ERROR,
    PROXY_UPSTREAM_NOT_FOUND,
    PROXY_UPSTREAM_RATE_LIMITED,
    PROXY_UPSTREAM_UNAVAILABLE,
    ProviderError,
    ProxyErrorCode,
)

# 失敗本文は要求内容を含みうるので details には残さない。
_REJECTED_INPUT = (400, PROXY_INVALID_INPUT, "The model provider rejected the input.")
_UNAVAILABLE = (502, PROXY_UPSTREAM_UNAVAILABLE, "The model provider is unavailable.")
_STATUS_MAPPINGS: dict[int, tuple[int, ProxyErrorCode, str]] = {
    400: _REJECTED_INPUT,
    401: (502, PROXY_AUTHENTICATION_FAILED, "Provider authentication failed."),
    # 直 API の billing_error。要求内容ではなく接続先アカウントの支払いの問題。
    402: (402, PROXY_INSUFFICIENT_BALANCE, "The model provider account is unfunded."),
    # 安全分類器の拒否は HTTP 200 の stop_reason で届く。403 は資格情報の権限不足
    # （permission_error）なので、設定の問題として止める。
    403: (502, PROXY_AUTHENTICATION_FAILED, "The model provider denied access."),
    404: (404, PROXY_UPSTREAM_NOT_FOUND, "The model resource was not found."),
    408: (408, PROXY_UPSTREAM_UNAVAILABLE, "The model provider request timed out."),
    409: (409, PROXY_UPSTREAM_UNAVAILABLE, "Temporary provider conflict."),
    413: (400, PROXY_INVALID_INPUT, "The model provider rejected the request size."),
    429: (429, PROXY_UPSTREAM_RATE_LIMITED, "The model provider is rate-limited."),
    500: (500, PROXY_UPSTREAM_INTERNAL_ERROR, "Provider internal server error."),
}
# 出力が途中で打ち切られた印。完了として返すと部分出力が正解として扱われてしまう。
_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})


def anthropic_request_details(
    *,
    request: LlmRequest,
    upstream_status_code: int | None = None,
    upstream_request_id: str | None = None,
    upstream_code: str | None = None,
) -> dict[str, JSONValue]:
    details: dict[str, JSONValue] = {
        "local_job_id": request.trace.local_job_id,
        "upstream_provider": "anthropic",
        "profile_id": request.purpose,
    }
    if upstream_status_code is not None:
        details["upstream_status_code"] = upstream_status_code
    if upstream_request_id is not None:
        details["upstream_request_id"] = upstream_request_id
    if upstream_code is not None:
        details["upstream_code"] = upstream_code
    return details


def _upstream_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("type") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else None


def build_anthropic_status_error(
    *,
    response: httpx.Response,
    request: LlmRequest,
) -> ProviderError | None:
    status = response.status_code
    if status < 400:
        return None
    status_code, code, message = _STATUS_MAPPINGS.get(
        status, _REJECTED_INPUT if status < 500 else _UNAVAILABLE
    )
    return ProviderError(
        status_code=status_code,
        code=code,
        message=message,
        details=anthropic_request_details(
            request=request,
            upstream_status_code=status,
            upstream_request_id=response.headers.get("request-id"),
            upstream_code=_upstream_error_code(response),
        ),
    )


def anthropic_stop_reason_error(
    *, stop_reason: str | None, details: dict[str, JSONValue]
) -> ProviderError | None:
    """安全分類器の拒否と出力打ち切りは HTTP 200 で返るので、ここで分岐させる。"""

    if stop_reason == "refusal":
        return ProviderError(
            status_code=403,
            code=PROXY_UPSTREAM_FORBIDDEN,
            message="The model provider refused the request.",
            details={**details, "response_status": "refusal"},
        )
    if stop_reason in _TRUNCATED_STOP_REASONS:
        return ProviderError(
            status_code=502,
            code=PROXY_LLM_TOOL_CALL_INVALID,
            message=f"The model provider returned an incomplete response: {stop_reason}.",
            details={
                **details,
                "response_status": stop_reason,
                "tool_call_violation_reason": "response_incomplete",
            },
        )
    return None


def invalid_anthropic_response(
    *, message: str, details: dict[str, JSONValue]
) -> ProviderError:
    return ProviderError(
        status_code=502,
        code=PROXY_INVALID_UPSTREAM_RESPONSE,
        message=message,
        details=details,
    )

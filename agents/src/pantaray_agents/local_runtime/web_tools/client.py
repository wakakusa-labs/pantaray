"""Web tools の唯一の送信点。要求ごとに cloud / direct / local の経路を 1 回決める。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypedDict

import httpx2
from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.connection_store import (
    peek_web_search_credential,
    read_web_search_route,
)
from pantaray_agents.local_runtime.runtime.job_route_identity import (
    require_current_route_identity,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    AUTH_CONTEXT_EXPIRED_STATE,
    load_active_desktop_session,
    read_auth_context_state,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.env import read_required_env
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_REQUEST_FAILED,
    PROXY_UPSTREAM_FORBIDDEN,
    PROXY_UPSTREAM_NOT_FOUND,
    PROXY_UPSTREAM_RATE_LIMITED,
    PROXY_UPSTREAM_UNAVAILABLE,
    ProviderError,
    ProxyErrorCode,
    ProxyProvider,
    ProxySuggestedAction,
    coerce_proxy_error_code,
    coerce_proxy_suggested_action,
    is_retryable_proxy_error_code,
)
from pantaray_llm.web_tools.schemas import WebToolsProxyRequest
from pantaray_llm.web_tools.tavily import execute_web_tools_proxy_request

from .contract import (
    WebContentExecutionError,
    WebContentInvalidResponseError,
    WebToolsWrapperContext,
    WebToolsWrapperResponse,
)
from .local_fetch import extract_pages_locally

WEB_TOOLS_PROXY_URL_ENV = "WEB_TOOLS_PROXY_URL"
HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_UNAUTHORIZED = 401
HTTP_STATUS_FORBIDDEN = 403
HTTP_STATUS_NOT_FOUND = 404
HTTP_STATUS_TOO_MANY_REQUESTS = 429
HTTP_STATUS_BAD_GATEWAY = 502
HTTP_STATUS_SERVICE_UNAVAILABLE = 503
HTTP_STATUS_GATEWAY_TIMEOUT = 504


class _ProxyErrorBody(TypedDict, total=False):
    code: str
    message: str
    details: dict[str, JSONValue]


def read_required_web_tools_proxy_url() -> str:
    """Cloud web tools wrapper endpoint を fail-closed で取得する。"""

    return read_required_env(WEB_TOOLS_PROXY_URL_ENV)


def require_non_empty_string(value: object, *, field_name: str) -> str:
    """非空文字列を厳格に要求する。"""

    if not isinstance(value, str):
        raise WebContentInvalidResponseError(field_name)
    normalized = value.strip()
    if not normalized:
        raise WebContentInvalidResponseError(field_name)
    return normalized


def require_non_empty_text(value: object, *, field_name: str) -> str:
    """本文などの文字列を改変せずに非空であることだけを要求する。"""

    if not isinstance(value, str):
        raise WebContentInvalidResponseError(field_name)
    if not value.strip():
        raise WebContentInvalidResponseError(field_name)
    return value


def require_optional_integer(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise WebContentInvalidResponseError(field_name)
    return value


def build_web_tools_wrapper_context(
    *,
    user_id: object,
    action_id: object,
) -> WebToolsWrapperContext:
    context: WebToolsWrapperContext = {
        "user_id": require_non_empty_string(user_id, field_name="user_id"),
        "request_id": str(uuid.uuid4()),
    }
    if isinstance(action_id, str) and action_id.strip():
        context["action_id"] = action_id.strip()
    return context


def _coerce_json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_coerce_json_value(item) for item in value]
    if isinstance(value, dict):
        payload: dict[str, JSONValue] = {}
        for key, item in value.items():
            payload[str(key)] = _coerce_json_value(item)
        return payload
    raise WebContentInvalidResponseError("json_value")


def _coerce_wrapper_response(value: object) -> WebToolsWrapperResponse:
    if not isinstance(value, dict):
        raise WebContentInvalidResponseError("wrapper_response")
    tool_id = require_non_empty_string(value.get("tool_id"), field_name="tool_id")
    status = require_non_empty_string(value.get("status"), field_name="status")
    request_id = require_non_empty_string(
        value.get("request_id"), field_name="request_id"
    )
    response: WebToolsWrapperResponse = {
        "tool_id": tool_id,
        "status": status,
        "request_id": request_id,
    }
    response_time_ms = require_optional_integer(
        value.get("response_time_ms"),
        field_name="response_time_ms",
    )
    if response_time_ms is not None:
        response["response_time_ms"] = response_time_ms
    result = value.get("result")
    if result is not None:
        if not isinstance(result, dict):
            raise WebContentInvalidResponseError("result")
        response["result"] = {
            str(key): _coerce_json_value(item) for key, item in result.items()
        }
    error = value.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise WebContentInvalidResponseError("error")
        response["error"] = {
            str(key): _coerce_json_value(item) for key, item in error.items()
        }
    return response


def _coerce_proxy_error_body(value: object) -> _ProxyErrorBody:
    """Read whichever proxy error fields an error response actually carries."""

    payload: _ProxyErrorBody = {}
    if not isinstance(value, dict):
        return payload
    error = value.get("error")
    if not isinstance(error, dict):
        return payload
    code = error.get("code")
    if isinstance(code, str) and code.strip():
        payload["code"] = code.strip()
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        payload["message"] = message.strip()
    details = error.get("details")
    if isinstance(details, dict):
        payload["details"] = {
            str(key): _coerce_json_value(item) for key, item in details.items()
        }
    return payload


def _fallback_web_tool_error_code(http_status_code: int) -> ProxyErrorCode:
    if http_status_code == HTTP_STATUS_BAD_REQUEST:
        return PROXY_INVALID_INPUT
    if http_status_code == HTTP_STATUS_UNAUTHORIZED:
        return PROXY_AUTHENTICATION_FAILED
    if http_status_code == HTTP_STATUS_FORBIDDEN:
        return PROXY_UPSTREAM_FORBIDDEN
    if http_status_code == HTTP_STATUS_NOT_FOUND:
        return PROXY_UPSTREAM_NOT_FOUND
    if http_status_code == HTTP_STATUS_TOO_MANY_REQUESTS:
        return PROXY_UPSTREAM_RATE_LIMITED
    if http_status_code in {
        HTTP_STATUS_SERVICE_UNAVAILABLE,
        HTTP_STATUS_GATEWAY_TIMEOUT,
    }:
        return PROXY_UPSTREAM_UNAVAILABLE
    if http_status_code == HTTP_STATUS_BAD_GATEWAY:
        return PROXY_INVALID_UPSTREAM_RESPONSE
    return PROXY_REQUEST_FAILED


def _fallback_web_tool_error_message(http_status_code: int) -> str:
    if http_status_code == HTTP_STATUS_BAD_REQUEST:
        return "The web tool request is invalid."
    if http_status_code == HTTP_STATUS_UNAUTHORIZED:
        return "The web tool request could not be authenticated."
    if http_status_code == HTTP_STATUS_FORBIDDEN:
        return "The target content could not be accessed."
    if http_status_code == HTTP_STATUS_NOT_FOUND:
        return "The requested content was not found."
    if http_status_code == HTTP_STATUS_TOO_MANY_REQUESTS:
        return "The external provider is rate-limited."
    if http_status_code in {
        HTTP_STATUS_SERVICE_UNAVAILABLE,
        HTTP_STATUS_GATEWAY_TIMEOUT,
    }:
        return "The external provider is temporarily unavailable."
    if http_status_code == HTTP_STATUS_BAD_GATEWAY:
        return "The external provider returned an invalid response."
    return "The web tool request failed."


def _build_web_tool_execution_error(
    *,
    http_status_code: int,
    code: str | None,
    message: str | None,
    details: dict[str, JSONValue] | None,
    request_id: str,
    profile_id: str,
    suggested_action: ProxySuggestedAction | None = None,
) -> WebContentExecutionError:
    """Map one proxy error contract failure, whichever route produced it.

    The cloud route decodes the fields from the error response body and the
    direct route takes them from ``ProviderError``, so the same provider
    failure yields the same code, retryability, and identifiers on both.
    ``suggested_action`` is what the calling route knows about its own
    connection; without it the failure carries whatever guidance the producer
    stated in ``details``.
    """
    error_code = coerce_proxy_error_code(
        code,
        default_code=_fallback_web_tool_error_code(http_status_code),
    )
    return WebContentExecutionError(
        error_code=error_code,
        error_message=message or _fallback_web_tool_error_message(http_status_code),
        retryable=is_retryable_proxy_error_code(error_code),
        suggested_action=suggested_action
        or coerce_proxy_suggested_action(
            details.get("suggested_action") if details is not None else None
        ),
        request_id=_read_optional_string(details, field_name="request_id")
        or request_id,
        upstream_provider=_read_optional_provider(details),
        upstream_request_id=_read_optional_string(
            details,
            field_name="upstream_request_id",
        ),
        profile_id=_read_optional_string(details, field_name="profile_id")
        or profile_id,
        upstream_status_code=http_status_code,
        upstream_code=code,
    )


def _read_optional_string(
    details: dict[str, JSONValue] | None,
    *,
    field_name: str,
) -> str | None:
    if details is None:
        return None
    value = details.get(field_name)
    return value if isinstance(value, str) and value.strip() else None


def _read_optional_provider(
    details: dict[str, JSONValue] | None,
) -> ProxyProvider | None:
    provider = _read_optional_string(details, field_name="upstream_provider")
    if provider == "tavily":
        return "tavily"
    return None


def _connection_not_configured_error(
    *,
    request_id: str,
    profile_id: str,
) -> WebContentExecutionError:
    return WebContentExecutionError(
        error_code=PROXY_CONNECTION_NOT_CONFIGURED,
        error_message="Web search is not configured.",
        retryable=False,
        request_id=request_id,
        profile_id=profile_id,
    )


async def _invoke_with_retries(
    attempt: Callable[[], Awaitable[WebToolsWrapperResponse]],
    *,
    max_retries: int,
) -> WebToolsWrapperResponse:
    """Retry transport failures and retryable proxy codes on either route."""

    for attempt_index in range(max_retries):
        try:
            return await attempt()
        except WebContentExecutionError as error:
            if not error.retryable or attempt_index >= max_retries - 1:
                raise
    raise RuntimeError("unreachable")


async def _invoke_cloud_proxy(
    *,
    tool_id: str,
    web_tool_profile: str,
    args: dict[str, JSONValue],
    context: WebToolsWrapperContext,
    max_retries: int,
) -> WebToolsWrapperResponse:
    proxy_url = read_required_web_tools_proxy_url()
    desktop_session = load_active_desktop_session(
        db_path=SESSION_STORE_UNUSED_DB_PATH,
        busy_timeout_ms=SESSION_STORE_CALLER_TIMEOUT_MS,
        user_id=context["user_id"],
    )
    request_payload = {
        "tool_id": tool_id,
        "web_tool_profile": web_tool_profile,
        "args": args,
        "request_context": context,
    }
    timeout = httpx2.Timeout(30.0)

    async def attempt() -> WebToolsWrapperResponse:
        started_at = time.perf_counter()
        try:
            async with httpx2.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    proxy_url,
                    json=request_payload,
                    headers={
                        "Authorization": (
                            f"Bearer {desktop_session.desktop_access_token}"
                        ),
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
        except httpx2.HTTPError as exc:
            raise WebContentExecutionError(
                error_code=PROXY_REQUEST_FAILED,
                error_message="The web tool request failed before a response was returned.",
                retryable=True,
                request_id=context["request_id"],
                profile_id=web_tool_profile,
            ) from exc

        if not response.is_success:
            error_payload: object
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = None
            parsed_error = _coerce_proxy_error_body(error_payload)
            raise _build_web_tool_execution_error(
                http_status_code=response.status_code,
                code=parsed_error.get("code"),
                message=parsed_error.get("message"),
                details=parsed_error.get("details"),
                request_id=context["request_id"],
                profile_id=web_tool_profile,
            )

        try:
            parsed = _coerce_wrapper_response(response.json())
            if parsed["tool_id"] != tool_id:
                raise WebContentInvalidResponseError("tool_id")
            if parsed["request_id"] != context["request_id"]:
                raise WebContentInvalidResponseError("request_id")
        except (ValueError, TypeError) as exc:
            raise WebContentExecutionError(
                error_code=PROXY_INVALID_UPSTREAM_RESPONSE,
                error_message="The external provider returned an invalid response.",
                retryable=is_retryable_proxy_error_code(
                    PROXY_INVALID_UPSTREAM_RESPONSE
                ),
                request_id=context["request_id"],
                profile_id=web_tool_profile,
                upstream_status_code=response.status_code,
            ) from exc
        if "response_time_ms" not in parsed:
            parsed["response_time_ms"] = int((time.perf_counter() - started_at) * 1000)
        return parsed

    return await _invoke_with_retries(attempt, max_retries=max_retries)


async def _invoke_tavily_directly(
    *,
    tool_id: str,
    web_tool_profile: str,
    args: dict[str, JSONValue],
    context: WebToolsWrapperContext,
    api_key: str,
    max_retries: int,
) -> WebToolsWrapperResponse:
    try:
        request = WebToolsProxyRequest.model_validate(
            {
                "tool_id": tool_id,
                "web_tool_profile": web_tool_profile,
                "args": args,
                "request_context": context,
            }
        )
    except ValidationError as exc:
        # Tool arguments are schema-checked before they get here, so this only
        # guards the shared contract; the message never quotes the arguments back.
        raise WebContentExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message=f"The {tool_id} request is invalid.",
            retryable=False,
            request_id=context["request_id"],
            profile_id=web_tool_profile,
        ) from exc

    async def attempt() -> WebToolsWrapperResponse:
        try:
            response = await execute_web_tools_proxy_request(request, api_key=api_key)
        except ProviderError as exc:
            raise _build_web_tool_execution_error(
                http_status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
                details=exc.details,
                request_id=context["request_id"],
                profile_id=web_tool_profile,
                # The key Tavily rejected is the owner's own stored key, so
                # signing in again cannot fix it; only a new key can.
                suggested_action=(
                    "configure_connection"
                    if exc.code == PROXY_AUTHENTICATION_FAILED
                    else None
                ),
            ) from exc
        parsed: WebToolsWrapperResponse = {
            "tool_id": response.tool_id,
            "status": response.status,
            "request_id": response.request_id,
            "response_time_ms": response.response_time_ms,
        }
        if response.result is not None:
            parsed["result"] = response.result
        return parsed

    return await _invoke_with_retries(attempt, max_retries=max_retries)


async def invoke_web_tools_wrapper(
    *,
    tool_id: str,
    web_tool_profile: str,
    args: dict[str, JSONValue],
    context: WebToolsWrapperContext,
    max_retries: int,
) -> WebToolsWrapperResponse:
    """Send one web tool request on the route this request resolves to (6.7).

    The route is decided once, here, and a failure on one route is never
    retried on another. A job that started on a different route stops before
    the route is read, so nothing it searches for reaches the new account
    (design 6.2, 7.3).
    """

    await require_current_route_identity()
    match read_web_search_route():
        # ``read_web_search_route`` folds an expired cloud session into
        # ``cloud`` so a lapsed session asks for re-authentication instead of
        # falling back to a stored Tavily key. Nothing is sent upstream.
        case "cloud" if read_auth_context_state() == AUTH_CONTEXT_EXPIRED_STATE:
            raise WebContentExecutionError(
                error_code=PROXY_AUTHENTICATION_FAILED,
                error_message="The Pantaray session expired. Sign in again.",
                retryable=False,
                suggested_action="reauthenticate",
                request_id=context["request_id"],
                profile_id=web_tool_profile,
            )
        case "cloud":
            return await _invoke_cloud_proxy(
                tool_id=tool_id,
                web_tool_profile=web_tool_profile,
                args=args,
                context=context,
                max_retries=max_retries,
            )
        case "direct":
            credential = peek_web_search_credential()
            if credential is None:
                # Cleared between the route read and this one.
                raise _connection_not_configured_error(
                    request_id=context["request_id"],
                    profile_id=web_tool_profile,
                )
            return await _invoke_tavily_directly(
                tool_id=tool_id,
                web_tool_profile=web_tool_profile,
                args=args,
                context=context,
                api_key=credential.api_key,
                max_retries=max_retries,
            )
        # Without either connection a page is still read, from this computer;
        # search and crawl have nothing to fall back to.
        case "unconfigured" if tool_id == "web_extract":
            return await extract_pages_locally(
                args=args, context=context, web_tool_profile=web_tool_profile
            )
        case _:
            raise _connection_not_configured_error(
                request_id=context["request_id"],
                profile_id=web_tool_profile,
            )


SESSION_STORE_CALLER_TIMEOUT_MS = 1
SESSION_STORE_UNUSED_DB_PATH = Path("/dev/null")

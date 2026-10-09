from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from tavily.errors import InvalidAPIKeyError, UsageLimitExceededError

from pantaray_agents.agents.action_agent.runtime.handlers.web_tool_error_mapper import (
    map_web_tool_error,
)
from pantaray_agents.local_runtime.runtime.connection_store import (
    WebSearchCredential,
    set_web_search_credential,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
    restore_expired_cloud_identity,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.web_tools.client import (
    WebContentExecutionError,
    WebToolsWrapperContext,
    WebToolsWrapperResponse,
    invoke_web_tools_wrapper,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_agents.tools.web.session import (
    _WEB_SEARCH_KEY_PLACE,
    WebResearchToolSession,
)
from pantaray_llm.errors import ProviderError
from pantaray_llm.profiles import WEB_EXTRACT_PROFILE_ID, WEB_SEARCH_PROFILE_ID
from pantaray_llm.web_tools.schemas import WebToolsProxyRequest
from pantaray_llm.web_tools.tavily import execute_web_tools_proxy_request

from .migrated_db import prepare_test_database

TAVILY_KEY = "tvly-secret-under-test"
OPERATOR_KEY = "operator-key"
PROXY_URL = "https://proxy.example.test/v1/web-tools"
REQUEST_CONTEXT: WebToolsWrapperContext = {
    "user_id": "user-1",
    "request_id": "request-1",
}
TAVILY_SEARCH_PAYLOAD: dict[str, object] = {
    "results": [
        {
            "title": "Pantaray",
            "url": "https://example.test/a",
            "content": "body",
            "score": 0.9,
        }
    ],
    "images": [],
    "request_id": "tavily-request-1",
}
TAVILY_EXTRACT_PAYLOAD: dict[str, object] = {
    "results": [{"url": "https://example.test/a", "raw_content": "body"}],
    "failed_results": [],
    "response_time": 0.5,
    "request_id": "tavily-request-2",
}


class _Response:
    def __init__(self, *, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _MalformedJsonResponse(_Response):
    def __init__(self) -> None:
        super().__init__(status_code=200, payload=None)

    def json(self) -> object:
        raise ValueError("truncated JSON")


class _RecordingAsyncClient:
    """Records every POST the cloud route sends to the Pantaray proxy."""

    responses: tuple[_Response, ...] = ()
    post_count = 0

    def __init__(self, **_kwargs: object) -> None: ...

    async def __aenter__(self) -> _RecordingAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        _url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> _Response:
        type(self).post_count += 1
        return type(self).responses[type(self).post_count - 1]


class _RecordingTavilyClient:
    """Records every call the direct route makes into the Tavily SDK."""

    def __init__(
        self,
        *,
        result: object = None,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[str] = []

    def _respond(self, name: str) -> object:
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        return self.result

    def search(self, **_kwargs: object) -> object:
        return self._respond("search")

    def extract(self, **_kwargs: object) -> object:
        return self._respond("extract")

    def crawl(self, **_kwargs: object) -> object:
        return self._respond("crawl")


def _successful_response() -> _Response:
    return _Response(
        status_code=200,
        payload={
            "tool_id": "web_search",
            "status": "success",
            "request_id": "request-1",
            "result": {"answer": "Pantaray"},
        },
    )


def _migrated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    return db_path


def _sign_in(tmp_path: Path) -> None:
    """Apply a ``configure`` whose cloud session is present."""

    import_desktop_session(
        db_path=_migrated_db(tmp_path),
        busy_timeout_ms=1_000,
        user_id=REQUEST_CONTEXT["user_id"],
        desktop_access_token="desktop-token",
        expires_at="2099-01-01T00:00:00Z",
        session_version="1",
    )
    mark_configured()


def _expire_session(tmp_path: Path) -> None:
    restore_expired_cloud_identity(
        db_path=_migrated_db(tmp_path),
        busy_timeout_ms=1_000,
        user_id=REQUEST_CONTEXT["user_id"],
        session_version="1",
    )
    mark_configured()


def _configure_tavily_key() -> None:
    """Apply a ``configure`` with no cloud session and a stored Tavily key."""

    mark_configured()
    set_web_search_credential(WebSearchCredential(api_key=TAVILY_KEY))


def _patch_proxy_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: tuple[_Response, ...] = (),
) -> type[_RecordingAsyncClient]:
    class _Client(_RecordingAsyncClient):
        pass

    _Client.responses = responses
    _Client.post_count = 0
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.web_tools.client.read_required_web_tools_proxy_url",
        lambda: PROXY_URL,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.web_tools.client.httpx2.AsyncClient",
        _Client,
    )
    return _Client


def _patch_tavily(
    monkeypatch: pytest.MonkeyPatch,
    tavily: _RecordingTavilyClient,
) -> list[str]:
    """Hand the shared executor a recording client and capture the key it is given."""

    keys: list[str] = []

    def build(api_key: str) -> _RecordingTavilyClient:
        keys.append(api_key)
        return tavily

    monkeypatch.setattr("pantaray_llm.web_tools.tavily.TavilyClient", build)
    return keys


async def _invoke(
    *,
    tool_id: str = "web_search",
    profile_id: str = WEB_SEARCH_PROFILE_ID,
    args: dict[str, object] | None = None,
    max_retries: int = 1,
) -> WebToolsWrapperResponse:
    return await invoke_web_tools_wrapper(
        tool_id=tool_id,
        web_tool_profile=profile_id,
        args=args if args is not None else {"query": "Pantaray"},
        context=REQUEST_CONTEXT,
        max_retries=max_retries,
    )


def _error_fields(error: WebContentExecutionError) -> dict[str, object]:
    return {
        "error_code": error.error_code,
        "error_message": error.error_message,
        "retryable": error.retryable,
        "suggested_action": error.suggested_action,
        "request_id": error.request_id,
        "upstream_provider": error.upstream_provider,
        "upstream_request_id": error.upstream_request_id,
        "profile_id": error.profile_id,
        "upstream_status_code": error.upstream_status_code,
        "upstream_code": error.upstream_code,
    }


def _without_timing(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "response_time_ms"}


def _proxy_request(
    *,
    tool_id: str = "web_search",
    profile_id: str = WEB_SEARCH_PROFILE_ID,
    args: dict[str, object] | None = None,
) -> WebToolsProxyRequest:
    return WebToolsProxyRequest.model_validate(
        {
            "tool_id": tool_id,
            "web_tool_profile": profile_id,
            "args": args if args is not None else {"query": "Pantaray"},
            "request_context": REQUEST_CONTEXT,
        }
    )


async def _cloud_success_body(
    *,
    tool_id: str,
    profile_id: str,
    args: dict[str, object],
) -> dict[str, object]:
    """The body Pantaray Cloud returns for the same upstream payload."""

    response = await execute_web_tools_proxy_request(
        _proxy_request(tool_id=tool_id, profile_id=profile_id, args=args),
        api_key=OPERATOR_KEY,
    )
    return response.model_dump(exclude_none=True)


async def _cloud_error_body() -> tuple[int, dict[str, object]]:
    """The status and body Pantaray Cloud returns for a shared-provider failure."""

    try:
        await execute_web_tools_proxy_request(_proxy_request(), api_key=OPERATOR_KEY)
    except ProviderError as exc:
        return exc.status_code, {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        }
    raise AssertionError("the shared executor did not fail")


@pytest.mark.asyncio
async def test_non_retryable_error_is_raised_after_one_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _sign_in(tmp_path)
    proxy = _patch_proxy_transport(
        monkeypatch,
        responses=(
            _Response(
                status_code=400,
                payload={
                    "error": {
                        "code": "PROXY_INVALID_INPUT",
                        "message": "Invalid request",
                    }
                },
            ),
        ),
    )

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke(profile_id="research.default", max_retries=3)

    assert exc_info.value.error_code == "PROXY_INVALID_INPUT"
    assert exc_info.value.retryable is False
    assert proxy.post_count == 1


@pytest.mark.asyncio
async def test_retryable_error_is_retried_through_remaining_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _sign_in(tmp_path)
    proxy = _patch_proxy_transport(
        monkeypatch,
        responses=3
        * (
            _Response(
                status_code=500,
                payload={
                    "error": {
                        "code": "PROXY_REQUEST_FAILED",
                        "message": "Provider request failed",
                    }
                },
            ),
        ),
    )

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke(profile_id="research.default", max_retries=3)

    assert exc_info.value.error_code == "PROXY_REQUEST_FAILED"
    assert exc_info.value.retryable is True
    assert proxy.post_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_response",
    [
        _MalformedJsonResponse(),
        _Response(status_code=200, payload={}),
        _Response(
            status_code=200,
            payload={
                "tool_id": "web_extract",
                "status": "success",
                "request_id": "request-1",
            },
        ),
        _Response(
            status_code=200,
            payload={
                "tool_id": "web_search",
                "status": "success",
                "request_id": "request-2",
            },
        ),
    ],
    ids=["malformed-json", "malformed-envelope", "tool-id", "request-id"],
)
async def test_invalid_success_response_is_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_response: _Response,
) -> None:
    _sign_in(tmp_path)
    proxy = _patch_proxy_transport(
        monkeypatch,
        responses=(invalid_response, _successful_response()),
    )

    result = await _invoke(profile_id="research.default", max_retries=3)

    assert result["result"] == {"answer": "Pantaray"}
    assert proxy.post_count == 2


@pytest.mark.asyncio
async def test_invalid_success_response_is_raised_after_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _sign_in(tmp_path)
    proxy = _patch_proxy_transport(
        monkeypatch,
        responses=(
            _MalformedJsonResponse(),
            _MalformedJsonResponse(),
            _MalformedJsonResponse(),
        ),
    )

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke(profile_id="research.default", max_retries=3)

    assert exc_info.value.error_code == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert exc_info.value.retryable is True
    assert exc_info.value.request_id == "request-1"
    assert exc_info.value.profile_id == "research.default"
    assert exc_info.value.upstream_status_code == 200
    assert proxy.post_count == 3


@pytest.mark.asyncio
async def test_a_present_cloud_session_reaches_only_the_pantaray_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_tavily_key()
    _sign_in(tmp_path)
    proxy = _patch_proxy_transport(monkeypatch, responses=(_successful_response(),))
    tavily = _RecordingTavilyClient(result=TAVILY_SEARCH_PAYLOAD)
    _patch_tavily(monkeypatch, tavily)

    await _invoke(profile_id="research.default")

    assert proxy.post_count == 1
    assert tavily.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "profile_id", "args", "upstream_payload", "expected_call"),
    [
        (
            "web_search",
            WEB_SEARCH_PROFILE_ID,
            {"query": "Pantaray"},
            TAVILY_SEARCH_PAYLOAD,
            "search",
        ),
        (
            "web_extract",
            WEB_EXTRACT_PROFILE_ID,
            {"urls": ["https://example.test/a"]},
            TAVILY_EXTRACT_PAYLOAD,
            "extract",
        ),
    ],
    ids=["web_search", "web_extract"],
)
async def test_a_stored_key_reaches_tavily_with_the_cloud_result_shape(
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    profile_id: str,
    args: dict[str, object],
    upstream_payload: dict[str, object],
    expected_call: str,
) -> None:
    _configure_tavily_key()
    proxy = _patch_proxy_transport(monkeypatch)
    _patch_tavily(monkeypatch, _RecordingTavilyClient(result=upstream_payload))
    cloud_body = await _cloud_success_body(
        tool_id=tool_id,
        profile_id=profile_id,
        args=args,
    )

    tavily = _RecordingTavilyClient(result=upstream_payload)
    keys = _patch_tavily(monkeypatch, tavily)
    direct = await _invoke(tool_id=tool_id, profile_id=profile_id, args=args)

    assert proxy.post_count == 0
    assert tavily.calls == [expected_call]
    assert keys == [TAVILY_KEY]
    assert _without_timing(direct) == _without_timing(cloud_body)


@pytest.mark.asyncio
async def test_an_expired_cloud_session_sends_nothing_even_with_a_stored_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_tavily_key()
    _expire_session(tmp_path)
    proxy = _patch_proxy_transport(monkeypatch)
    tavily = _RecordingTavilyClient(result=TAVILY_SEARCH_PAYLOAD)
    _patch_tavily(monkeypatch, tavily)

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke()

    assert exc_info.value.error_code == "PROXY_AUTHENTICATION_FAILED"
    assert exc_info.value.retryable is False
    assert proxy.post_count == 0
    assert tavily.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False], ids=["no-key", "no-configure"])
async def test_an_unconfigured_web_search_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    if configured:
        mark_configured()
    proxy = _patch_proxy_transport(monkeypatch)
    tavily = _RecordingTavilyClient(result=TAVILY_SEARCH_PAYLOAD)
    _patch_tavily(monkeypatch, tavily)

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke()

    assert exc_info.value.error_code == "PROXY_CONNECTION_NOT_CONFIGURED"
    assert exc_info.value.retryable is False
    assert proxy.post_count == 0
    assert tavily.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (InvalidAPIKeyError("unauthorized"), "PROXY_AUTHENTICATION_FAILED"),
        (UsageLimitExceededError("rate limited"), "PROXY_UPSTREAM_RATE_LIMITED"),
        (RuntimeError("upstream returned 503"), "PROXY_REQUEST_FAILED"),
    ],
    ids=["invalid-key", "rate-limited", "upstream-failure"],
)
async def test_a_direct_tavily_failure_never_reaches_the_cloud_route(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    _configure_tavily_key()
    proxy = _patch_proxy_transport(monkeypatch)
    tavily = _RecordingTavilyClient(failure=failure)
    _patch_tavily(monkeypatch, tavily)

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke()

    assert exc_info.value.error_code == expected_code
    assert tavily.calls == ["search"]
    assert proxy.post_count == 0


@pytest.mark.asyncio
async def test_both_routes_map_the_same_provider_failure_identically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_tavily(
        monkeypatch,
        _RecordingTavilyClient(failure=UsageLimitExceededError("rate limited")),
    )
    _configure_tavily_key()
    _patch_proxy_transport(monkeypatch)
    with pytest.raises(WebContentExecutionError) as direct_error:
        await _invoke()

    status_code, body = await _cloud_error_body()
    _sign_in(tmp_path)
    _patch_proxy_transport(
        monkeypatch,
        responses=(_Response(status_code=status_code, payload=body),),
    )
    with pytest.raises(WebContentExecutionError) as cloud_error:
        await _invoke()

    assert _error_fields(direct_error.value) == _error_fields(cloud_error.value)


_Arrange = Callable[[pytest.MonkeyPatch, Path], None]


def _arrange_expired_cloud_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_tavily_key()
    _expire_session(tmp_path)
    _patch_proxy_transport(monkeypatch)
    _patch_tavily(monkeypatch, _RecordingTavilyClient(result=TAVILY_SEARCH_PAYLOAD))


def _arrange_rejected_tavily_key(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
) -> None:
    _configure_tavily_key()
    _patch_proxy_transport(monkeypatch)
    _patch_tavily(
        monkeypatch,
        _RecordingTavilyClient(failure=InvalidAPIKeyError("unauthorized")),
    )


def _arrange_unconfigured_web_search(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
) -> None:
    mark_configured()
    _patch_proxy_transport(monkeypatch)
    _patch_tavily(monkeypatch, _RecordingTavilyClient(result=TAVILY_SEARCH_PAYLOAD))


def _arrange_cloud_rejection(payload: object) -> _Arrange:
    """A signed-in owner whose request Pantaray Cloud rejects with 401."""

    def arrange(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _sign_in(tmp_path)
        _patch_proxy_transport(
            monkeypatch,
            responses=(_Response(status_code=401, payload=payload),),
        )

    return arrange


def _cloud_rejection_body(suggested_action: object) -> dict[str, object]:
    return {
        "error": {
            "code": "PROXY_AUTHENTICATION_FAILED",
            "message": "The Pantaray session is no longer valid.",
            "details": {"suggested_action": suggested_action},
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "expected_code", "expected_action"),
    [
        (
            _arrange_expired_cloud_session,
            "PROXY_AUTHENTICATION_FAILED",
            "reauthenticate",
        ),
        (
            _arrange_rejected_tavily_key,
            "PROXY_AUTHENTICATION_FAILED",
            "configure_connection",
        ),
        (
            _arrange_cloud_rejection(_cloud_rejection_body("reauthenticate")),
            "PROXY_AUTHENTICATION_FAILED",
            "reauthenticate",
        ),
        (
            _arrange_cloud_rejection(_cloud_rejection_body("please_sign_in")),
            "PROXY_AUTHENTICATION_FAILED",
            "abort",
        ),
        (
            _arrange_unconfigured_web_search,
            "PROXY_CONNECTION_NOT_CONFIGURED",
            "configure_connection",
        ),
    ],
    ids=[
        "expired-session",
        "rejected-tavily-key",
        "cloud-states-guidance",
        "cloud-states-unknown-guidance",
        "unconfigured",
    ],
)
async def test_a_failed_web_tool_tells_the_agent_how_this_connection_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arrange: _Arrange,
    expected_code: str,
    expected_action: str,
) -> None:
    arrange(monkeypatch, tmp_path)

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _invoke()

    error = map_web_tool_error(
        tool_id="web_search",
        exc=exc_info.value,
        request_id=REQUEST_CONTEXT["request_id"],
        profile_id=WEB_SEARCH_PROFILE_ID,
    )
    details = error["error_details"]
    assert error["error_code"] == expected_code
    assert details["suggested_action"] == expected_action
    assert details["recovery"] == "stop"
    assert details["retryable"] is False


def _arrange_rate_limited_tavily(
    monkeypatch: pytest.MonkeyPatch,
    _tmp_path: Path,
) -> None:
    _configure_tavily_key()
    _patch_proxy_transport(monkeypatch)
    _patch_tavily(
        monkeypatch,
        _RecordingTavilyClient(failure=UsageLimitExceededError("rate limited")),
    )


_UNAVAILABLE = "Web search is unavailable right now, so nothing was looked up."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "chat_message", "suggestion_message"),
    [
        (
            _arrange_unconfigured_web_search,
            "Web search is not set up, so nothing was looked up. Tell the user "
            "they can turn it on by saving the Tavily API key in Settings > AI "
            "connection > Web search (in Japanese: "
            '"Tavily API キー" in 設定 > AI 接続 > Web 検索).',
            "Web search is not set up, so nothing was looked up.",
        ),
        (
            _arrange_rejected_tavily_key,
            "The Tavily API key saved for web search was rejected, so nothing was "
            "looked up. Tell the user they can save a working key as the Tavily "
            "API key in Settings > AI connection > Web search (in Japanese: "
            '"Tavily API キー" in 設定 > AI 接続 > Web 検索).',
            "The Tavily API key saved for web search was rejected, so nothing was "
            "looked up.",
        ),
        (
            _arrange_expired_cloud_session,
            "The user's Pantaray sign-in has expired, so nothing was looked up. "
            "Tell the user web search works again once they sign in.",
            "The user's Pantaray sign-in has expired, so nothing was looked up.",
        ),
        (
            _arrange_cloud_rejection(_cloud_rejection_body("please_sign_in")),
            _UNAVAILABLE,
            _UNAVAILABLE,
        ),
        (_arrange_rate_limited_tavily, _UNAVAILABLE, _UNAVAILABLE),
    ],
    ids=[
        "unconfigured",
        "rejected-tavily-key",
        "expired-session",
        "cloud-states-unknown-guidance",
        "rate-limited",
    ],
)
@pytest.mark.parametrize("speaks_to_user", [True, False], ids=["chat", "suggestion"])
async def test_the_research_web_tool_names_only_a_fix_the_user_can_make(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arrange: _Arrange,
    chat_message: str,
    suggestion_message: str,
    speaks_to_user: bool,
) -> None:
    """The chat relays this result, so a system failure carries no code; only
    the chat, which answers the user, is asked to pass a fix on."""

    arrange(monkeypatch, tmp_path)
    args: dict[str, object] = {"query": "Pantaray", "offset": 1, "limit": 5}
    call = ReactToolCall(
        tool_name="web_search",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id="web_search",
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )
    registry = ReactToolRegistry(
        WebResearchToolSession(
            user_id=REQUEST_CONTEXT["user_id"], speaks_to_user=speaks_to_user
        ).definitions()
    )

    result = await registry.execute(call, 1)

    assert result.status == "error"
    assert result.output["message"] == (
        chat_message if speaks_to_user else suggestion_message
    )
    assert "PROXY_" not in str(result.output)


_UI_CATALOG = Path(__file__).resolve().parents[4] / "frontend/src/i18n/messageCatalog"


def _ui_labels(key: str) -> list[str]:
    """``key``'s label in every language the UI catalog defines it in."""

    pattern = re.compile(rf"'{re.escape(key)}': '([^']+)'")
    return [
        label
        for path in ("common.ts", "settings.ts")
        for label in pattern.findall((_UI_CATALOG / path).read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize(
    "key",
    [
        "nav.settings",
        "settings.aiConnection.title",
        "settings.aiConnection.webSearch.title",
        "settings.aiConnection.webSearch.keyLabel",
    ],
)
def test_the_fix_text_names_the_web_search_key_as_the_app_labels_it(key: str) -> None:
    labels = _ui_labels(key)

    assert len(labels) == 2  # English and Japanese
    assert all(label in _WEB_SEARCH_KEY_PLACE for label in labels)


@pytest.mark.asyncio
async def test_the_research_web_tool_keeps_a_provider_page_failure_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_tavily_key()
    _patch_proxy_transport(monkeypatch)
    provider_words = "upstream HTTP 503: service unavailable"
    _patch_tavily(
        monkeypatch,
        _RecordingTavilyClient(
            result={
                **TAVILY_EXTRACT_PAYLOAD,
                "failed_results": [
                    {"url": "https://example.test/b", "error": provider_words}
                ],
            }
        ),
    )
    args: dict[str, object] = {
        "urls": ["https://example.test/a", "https://example.test/b"],
        "query": None,
        "offset": 1,
        "limit": 100,
    }
    call = ReactToolCall(
        tool_name="web_extract",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id="web_extract",
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )
    registry = ReactToolRegistry(
        WebResearchToolSession(
            user_id=REQUEST_CONTEXT["user_id"], speaks_to_user=True
        ).definitions()
    )

    result = await registry.execute(call, 1)

    assert result.status == "success"
    assert result.output["failed_results"] == [
        {"url": "https://example.test/b", "error": "This page could not be read."}
    ]
    assert provider_words not in str(result.output)


@pytest.mark.asyncio
async def test_the_tavily_key_stays_out_of_errors_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_tavily_key()
    _patch_proxy_transport(monkeypatch)
    _patch_tavily(
        monkeypatch,
        _RecordingTavilyClient(failure=RuntimeError("upstream returned 503")),
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(WebContentExecutionError) as exc_info,
    ):
        await _invoke()

    error = exc_info.value
    assert TAVILY_KEY not in str(error)
    assert TAVILY_KEY not in repr(_error_fields(error))
    # The failing call logs, so an empty capture would make the next check vacuous.
    assert caplog.records
    assert TAVILY_KEY not in caplog.text

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.responses.response_completed_event import ResponseCompletedEvent

from pantaray_llm.contracts.request import LlmProxyRequest
from pantaray_llm.errors import ProviderError
from pantaray_llm.providers.openai_responses import provider as openai_provider
from pantaray_llm.providers.openai_responses.provider import execute_openai_request
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile
from pantaray_llm.providers.openai_responses.transport import (
    CHATGPT_CODEX_BASE_URL,
    OpenAiResponsesTransport,
    chatgpt_codex_transport,
    fireworks_transport,
)

_PROFILE = OpenAiLlmProfile(
    provider="openai",
    profile_id="test.profile",
    model="test-model",
    max_output_tokens=256,
    reasoning_effort="medium",
)


def _plain_text_request() -> LlmProxyRequest:
    return LlmProxyRequest.model_validate(
        {
            "inference_profile": "test.profile",
            "messages": [
                {"role": "system", "content": [{"type": "input_text", "text": "sys"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
            "metadata": {
                "request_kind": "llm_inference",
                "user_id": "user-1",
                "local_job_id": "job-1",
                "session_version": "1",
            },
        }
    )


def _completed_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_1",
        model="test-model-2026",
        status="completed",
        incomplete_details=None,
        error=None,
        output_text=text,
        output=[],
        usage=SimpleNamespace(
            input_tokens=10,
            input_tokens_details=None,
            output_tokens=2,
            output_tokens_details=None,
            total_tokens=12,
        ),
    )


class _NoTokenCount:
    async def count(self, **_kwargs: object) -> object:
        raise AssertionError("input token count must not be requested")


class _FakeEventStream:
    def __init__(self, response: object) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeEventStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[object]:
        yield ResponseCompletedEvent.model_construct(
            type="response.completed", response=self._response, sequence_number=0
        )


class _FakeResponses:
    def __init__(self, response: object) -> None:
        self.input_tokens = _NoTokenCount()
        self.kwargs: dict[str, object] | None = None
        self._response = response

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        if kwargs["stream"]:
            return _FakeEventStream(self._response)
        return self._response


def _install(monkeypatch: pytest.MonkeyPatch, responses: _FakeResponses) -> None:
    monkeypatch.setattr(
        openai_provider,
        "build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )


@pytest.mark.asyncio
async def test_chatgpt_transport_streams_without_a_token_precheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(_completed_response("hello"))
    _install(monkeypatch, responses)
    transport = chatgpt_codex_transport(access_token="token", account_id="acct-1")

    response = await execute_openai_request(
        request=_plain_text_request().to_request(),
        profile=_PROFILE,
        uploaded_blobs={},
        transport=transport,
    )

    assert transport.base_url == CHATGPT_CODEX_BASE_URL
    assert dict(transport.extra_headers) == {
        "chatgpt-account-id": "acct-1",
        "originator": "pantaray",
        "OpenAI-Beta": "responses=experimental",
        "Accept": "text/event-stream",
    }
    assert responses.kwargs is not None
    assert responses.kwargs["stream"] is True
    assert responses.kwargs["store"] is False
    assert responses.kwargs["instructions"] == "sys"
    assert responses.kwargs["include"] == ["reasoning.encrypted_content"]
    assert response.meta.upstream_provider == "openai_codex"
    assert response.output[0].content[0].text == "hello"
    assert response.meta.resolved_model == "test-model-2026"


_ECHO_CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "echo",
    "arguments": "{}",
    "status": "completed",
}


def _sse_client(*, items: list[dict[str, object]], terminal_output: list[object]):
    """An SDK client whose /responses answers one SSE stream of `items`."""

    response = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": "completed",
        "output": terminal_output,
        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    }
    events: list[dict[str, object]] = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}}
    ]
    for index, item in enumerate(items):
        events.append(
            {"type": "response.output_item.added", "output_index": index, "item": item}
        )
        events.append(
            {"type": "response.output_item.done", "output_index": index, "item": item}
        )
    events.append({"type": "response.completed", "response": response})
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    )
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        )
    )
    client = AsyncOpenAI(
        api_key="test-token",
        base_url="https://example.test/v1",
        http_client=http_client,
    )
    return http_client, client


@pytest.mark.asyncio
async def test_chatgpt_stream_recovers_done_items_from_empty_terminal() -> None:
    message = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }
    http_client, client = _sse_client(items=[message, _ECHO_CALL], terminal_output=[])
    async with http_client:
        result = await openai_provider._create_response(
            client=client,
            create_kwargs={
                "model": "test-model",
                "input": "hi",
                "store": False,
                "stream": False,
            },
            stream=True,
        )

    assert result.status == "completed"
    assert result.usage is not None and result.usage.total_tokens == 12
    assert [item.type for item in result.output] == ["message", "function_call"]
    assert result.output_text == "hello"


@pytest.mark.asyncio
async def test_chatgpt_stream_items_replay_without_sdk_only_fields() -> None:
    # Codex rejected the next turn with `Unknown parameter:
    # 'input[1].parsed_arguments'` once its terminal response carried output.
    http_client, client = _sse_client(items=[_ECHO_CALL], terminal_output=[_ECHO_CALL])
    async with http_client:
        result = await openai_provider._create_response(
            client=client,
            create_kwargs={
                "model": "test-model",
                "input": "hi",
                "store": False,
                "stream": False,
                "tools": [
                    {
                        "type": "function",
                        "name": "echo",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        "strict": True,
                    }
                ],
            },
            stream=True,
        )

    assert [
        item.model_dump(mode="json", exclude_none=True) for item in result.output
    ] == [_ECHO_CALL]


@pytest.mark.asyncio
async def test_non_streaming_transport_without_token_count_calls_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(_completed_response("hello"))
    _install(monkeypatch, responses)

    response = await execute_openai_request(
        request=_plain_text_request().to_request(),
        profile=_PROFILE,
        uploaded_blobs={},
        transport=fireworks_transport(api_key="key"),
    )

    assert responses.kwargs is not None
    assert responses.kwargs["stream"] is False
    assert "include" not in responses.kwargs
    assert response.meta.upstream_provider == "fireworks"


@pytest.mark.asyncio
async def test_transport_failures_report_their_own_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(RuntimeError("boom"))
    _install(monkeypatch, responses)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            request=_plain_text_request().to_request(),
            profile=_PROFILE,
            uploaded_blobs={},
            transport=fireworks_transport(api_key="key"),
        )

    assert exc_info.value.code == "PROXY_REQUEST_FAILED"
    assert exc_info.value.details is not None
    assert exc_info.value.details["upstream_provider"] == "fireworks"


def test_build_openai_client_applies_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def build_client(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace()

    openai_provider.build_openai_client.cache_clear()
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", build_client)
    transport: OpenAiResponsesTransport = chatgpt_codex_transport(
        access_token="token", account_id="account"
    )
    try:
        openai_provider.build_openai_client(transport)
    finally:
        openai_provider.build_openai_client.cache_clear()

    assert captured["base_url"] == CHATGPT_CODEX_BASE_URL
    assert captured["api_key"] == "token"
    assert captured["default_headers"]["chatgpt-account-id"] == "account"
    assert captured["max_retries"] == 0


@pytest.mark.parametrize("status_code", [200, 429])
@pytest.mark.asyncio
async def test_borrowed_sdk_client_preserves_request_and_caller_lifetime(
    status_code: int,
) -> None:
    import json

    import httpx
    from openai import AsyncOpenAI

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            json={
                "id": "resp_1",
                "created_at": 1,
                "model": "test-model",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "hello", "annotations": []}
                        ],
                    }
                ],
            },
        )

    transport = fireworks_transport(api_key="borrowed-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sdk = AsyncOpenAI(
            api_key=transport.api_key,
            base_url=transport.base_url,
            http_client=client,
            max_retries=0,
        )
        try:
            response = await execute_openai_request(
                request=_plain_text_request().to_request(),
                profile=_PROFILE,
                uploaded_blobs={},
                transport=transport,
                client=sdk,
            )
        except ProviderError as exc:
            assert status_code == 429
            assert exc.code == "PROXY_UPSTREAM_RATE_LIMITED"
            assert exc.details["local_job_id"] == "job-1"
        else:
            assert status_code == 200
            assert response.output[0].content[0].text == "hello"
            assert response.meta.local_job_id == "job-1"
        assert not sdk.is_closed()

    assert len(requests) == 1
    assert str(requests[0].url) == transport.base_url + "/responses"
    assert requests[0].headers["Authorization"] == "Bearer borrowed-key"
    body = json.loads(requests[0].content)
    assert body["model"] == "test-model"
    assert body["instructions"] == "sys"
    assert "metadata" not in body and "trace" not in body

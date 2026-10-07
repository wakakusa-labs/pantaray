import asyncio
import json
import platform
from contextlib import asynccontextmanager
from uuid import uuid4

import httpcore2
import httpx
import httpx2
import pytest
from httpcore2._backends.anyio import AnyIOBackend, AnyIOStream

from pantaray_agents.local_runtime.context import source_transport
from pantaray_agents.local_runtime.context.source_gate import (
    SourceGate,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.llm_proxy import client, transport
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.schema.memory_embeddings import MEMORY_EMBEDDING_PROFILE_ID
from pantaray_llm.errors import LlmProxyExecutionError

pytestmark = pytest.mark.asyncio

EMBEDDING_PAYLOAD = {
    "profile_id": MEMORY_EMBEDDING_PROFILE_ID,
    "model_id": "amazon.titan-embed-text-v2:0",
    "dimensions": 512,
    "normalized": True,
    "embeddings": [[0.25] * 512],
}
PROXY_ERRORS = (LlmProxyExecutionError,)


@pytest.fixture(autouse=True)
def isolated_transport_environment(monkeypatch):
    # Prime the SDK's lazy OS probe outside the server's network deadline.
    platform.platform()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@asynccontextmanager
async def server(
    *,
    wait_response=False,
    status=b"200 OK",
    payload=json.dumps(EMBEDDING_PAYLOAD).encode(),
    read_body=True,
):
    received = asyncio.Event()
    release = asyncio.Event()
    requests = []
    workers = set()

    async def handle(reader, writer):
        workers.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            body = await reader.readexactly(length) if read_body else b""
            requests.append((headers, body))
            received.set()
            if wait_response:
                await release.wait()
            response_payload = payload(headers) if callable(payload) else payload
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: "
                + str(len(response_payload)).encode()
                + b"\r\nConnection: close\r\nLocation: /\r\n\r\n"
                + response_payload
            )
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass  # Invalidation before headers closes an empty socket.
        finally:
            writer.close()
            await writer.wait_closed()
            workers.discard(asyncio.current_task())

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = listener.sockets[0].getsockname()[1]
        async with asyncio.timeout(5):
            yield f"http://127.0.0.1:{port}/", received, release, requests
    finally:
        release.set()
        listener.close()
        await listener.wait_closed()
        remaining = list(workers)
        for worker in remaining:
            worker.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


async def active():
    gate = SourceGate()
    async with gate.turn():
        gate.activate(
            SourceBinding(
                user_id="alice",
                epoch=uuid4(),
                policy_revision="policy",
                store_id="store",
                protocol_version=1,
            )
        )
        source = gate.current("alice")
    assert source is not None
    return gate, source


async def revoke(gate):
    async with gate.turn():
        gate.revoke("alice")


async def llm_post(url, user_id="alice"):
    return await transport.post_request(
        proxy_url=url,
        request_json={
            "metadata": {"local_job_id": "job", "user_id": user_id},
            "messages": ["raw"],
        },
        desktop_access_token="test-token",
        multipart_files=[],
    )


@pytest.fixture
def post():
    return llm_post


def assert_success(response):
    payload = (
        response.json()
        if isinstance(response, httpx2.Response)
        else response.model_dump()
    )
    assert payload == EMBEDDING_PAYLOAD


@asynccontextmanager
async def start(url, gate, source, post):
    with source_transport.source_scope(gate, source):
        task = asyncio.create_task(post(url))
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel_delivered", [True, False])
async def test_invalidation_before_header_grant_sends_no_request(
    post, monkeypatch, cancel_delivered
):
    gate, source = await active()
    reached, release = asyncio.Event(), asyncio.Event()
    if not cancel_delivered:
        monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)
    original = source_transport._HeaderTrace.__call__

    async def before_header(self, event, info):
        if event == "http11.send_request_headers.started":
            reached.set()
            await release.wait()
        await original(self, event, info)

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", before_header)
    async with server() as (url, _, _, requests):
        async with start(url, gate, source, post) as task:
            await reached.wait()
            await revoke(gate)
            release.set()
            with pytest.raises(
                asyncio.CancelledError if cancel_delivered else SourceInvalidated
            ):
                await task
            with (
                source_transport.source_scope(gate, source),
                pytest.raises(SourceInvalidated),
            ):
                await post(url)
            assert requests == []


async def test_same_store_reactivation_keeps_the_http_response_and_next_request(post):
    gate, source = await active()
    async with server(wait_response=True) as (url, received, release, requests):
        async with start(url, gate, source, post) as task:
            await received.wait()
            async with gate.turn():
                gate.activate(
                    source.binding.model_copy(
                        update={"epoch": uuid4(), "policy_revision": "new-policy"}
                    )
                )
            release.set()
            assert_success(await task)
            with source_transport.source_scope(gate, source):
                assert_success(await post(url))
            assert len(requests) == 2


async def test_response_wait_releases_gate_but_remains_cancellable(post):
    gate, source = await active()
    async with server(wait_response=True) as (url, received, _, requests):
        async with start(url, gate, source, post) as task:
            await received.wait()
            await revoke(gate)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(requests) == 1
            # Exiting raw scope restores normal/derived-only calls.
            async with server() as (other, _, _, normal):
                assert_success(await post(other))
                assert len(normal) == 1


@pytest.mark.parametrize("outcome", ["complete", "failure", "cancel"])
async def test_actual_header_write_holds_gate_and_always_releases(
    post, monkeypatch, outcome
):
    gate, source = await active()
    writing, release, queued, suspended = (asyncio.Event() for _ in range(4))
    original = AnyIOStream.write

    async def write(self, buffer, timeout=None):
        if buffer.startswith(b"POST "):
            writing.set()
            await release.wait()
            if outcome == "failure":
                raise httpcore2.WriteTimeout("injected header write failure")
        await original(self, buffer, timeout)

    monkeypatch.setattr(AnyIOStream, "write", write)

    async def suspend():
        queued.set()
        await revoke(gate)
        suspended.set()

    async with server(wait_response=True) as (url, _, _, requests):
        async with start(url, gate, source, post) as task:
            await writing.wait()
            waiter = asyncio.create_task(suspend())
            await queued.wait()
            assert not suspended.is_set()
            if outcome == "cancel":
                task.cancel()
            else:
                release.set()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except PROXY_ERRORS as exc:
                assert outcome == "failure"
                assert exc.retryable
            else:
                pytest.fail("revoked request returned a result")
            await waiter
            assert suspended.is_set()
            if outcome != "complete":
                assert requests == []
            async with gate.turn():
                pass


async def test_environment_proxy_connect_is_outside_post_gate(post, monkeypatch):
    gate, source = await active()
    writing = asyncio.Event()
    original = AnyIOStream.write

    async def write(self, buffer, timeout=None):
        if buffer.startswith(b"CONNECT "):
            writing.set()
            await asyncio.Event().wait()
        await original(self, buffer, timeout)

    monkeypatch.setattr(AnyIOStream, "write", write)
    async with server() as (proxy, _, _, requests):
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        async with start(
            "https://example.invalid/inference", gate, source, post
        ) as task:
            await writing.wait()
            await revoke(gate)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert requests == []


async def test_connect_completion_does_not_count_as_post(post, monkeypatch):
    gate, source = await active()
    async with server(status=b"403 Forbidden") as (proxy, _, _, requests):
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(PROXY_ERRORS) as error,
        ):
            await post("https://example.invalid/inference")
        assert error.value.retryable
        assert requests[0][0].startswith(b"CONNECT ")
        async with gate.guard(source):
            pass


async def test_old_response_rejected_even_when_cancel_delivery_is_delayed(
    post, monkeypatch
):
    gate, source = await active()
    original = httpx2.AsyncClient.__aexit__
    # Cancellation is dispatched to the caller loop; final permit validation
    # must work independently of delivery of that callback.
    monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)

    async def close(self, *args):
        await original(self, *args)
        await revoke(gate)

    monkeypatch.setattr(httpx2.AsyncClient, "__aexit__", close)
    async with server() as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(SourceInvalidated),
        ):
            await post(url)
        assert len(requests) == 1


@pytest.mark.parametrize("broken", ["missing", "http2"])
async def test_broken_trace_is_nonretryable(post, monkeypatch, broken):
    gate, source = await active()
    original = source_transport._HeaderTrace.__call__

    async def trace(self, event, info):
        if broken == "http2":
            await original(self, "http2.send_request_headers.started", info)
        # Fault-inject the dependency omitting all trace delivery.

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", trace)
    async with server() as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(PROXY_ERRORS) as error,
        ):
            await post(url)
        assert not error.value.retryable
        assert len(requests) == (1 if broken == "missing" else 0)


async def test_unscoped_http_error_mapping_and_no_redirect(post, monkeypatch):
    async with server(status=b"302 Found") as (url, _, _, requests):
        assert (await post(url)).status_code == 302
        assert len(requests) == 1
    connections = []
    original = AnyIOBackend.connect_tcp

    async def connect(self, *args, **kwargs):
        connections.append(args)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    with pytest.raises(PROXY_ERRORS) as error:
        await post(url)
    assert len(connections) == 1
    assert error.value.retryable and error.value.local_job_id == "job"


async def test_source_and_authenticated_request_subject_must_match(post):
    gate, source = await active()
    async with server() as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(SourceInvalidated),
        ):
            await post(url, user_id="bob")
        assert requests == []


async def test_scoped_success_uses_real_http1_and_restores_scope(post, monkeypatch):
    gate, source = await active()
    timeouts = []
    original = source_transport._HeaderTrace.__call__

    async def capture_timeout(self, event, info):
        if event == "http11.send_request_headers.started":
            timeouts.append(info["request"].extensions["timeout"])
        await original(self, event, info)

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", capture_timeout)
    async with server() as (url, _, _, requests):
        with source_transport.source_scope(gate, source):
            response = await post(url)
        assert_success(response)
        # An LLM may think silently for minutes; httpx applies `read` per socket
        # read, so it bounds that silence and never a response still arriving.
        expected = (
            {"connect": 10.0, "read": 250.0, "write": 250.0, "pool": 250.0}
            if post is llm_post
            else dict.fromkeys(("connect", "read", "write", "pool"), 30.0)
        )
        assert timeouts == [expected]
        assert requests[0][0].startswith(b"POST / HTTP/1.1\r\n")
        assert b"Authorization: Bearer test-token\r\n" in requests[0][0]
        await revoke(gate)
        assert_success(await post(url))
        assert len(requests) == 2


async def test_header_write_error_preserves_valid_http_error_response(
    post, monkeypatch
):
    gate, source = await active()
    original = AnyIOStream.write

    async def write(self, buffer, timeout=None):
        await original(self, buffer, timeout)
        if buffer.startswith(b"POST "):
            raise httpcore2.WriteError("peer rejected request after headers")

    monkeypatch.setattr(AnyIOStream, "write", write)
    payload = b'{"error":{"code":"PROXY_UPSTREAM_RATE_LIMITED","message":"rate"}}'
    async with server(
        status=b"429 Too Many Requests", payload=payload, read_body=False
    ) as (url, _, _, requests):
        with source_transport.source_scope(gate, source):
            with pytest.raises(PROXY_ERRORS) as error:
                await client.LocalLlmProxyClient(proxy_url=url)._post_request(
                    request_json={"metadata": {"user_id": "alice"}},
                    desktop_access_token="test-token",
                    response_schema=None,
                    multipart_files=[],
                )
        assert error.value.error_code == "PROXY_UPSTREAM_RATE_LIMITED"
        assert error.value.retryable
        assert len(requests) == 1
        async with gate.guard(source):
            pass


@pytest.mark.parametrize("failure", ["http", "http2_trace", "missing_trace"])
async def test_failed_request_rechecks_source_after_close(post, monkeypatch, failure):
    gate, source = await active()
    monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)
    original_close = httpx2.AsyncClient.__aexit__
    original_write = AnyIOStream.write
    original_trace = source_transport._HeaderTrace.__call__

    async def close(self, *args):
        await original_close(self, *args)
        await revoke(gate)

    async def write(self, buffer, timeout=None):
        if buffer.startswith(b"POST "):
            raise httpcore2.WriteTimeout("injected header write failure")
        await original_write(self, buffer, timeout)

    async def trace(self, event, info):
        if failure == "http2_trace":
            await original_trace(self, "http2.send_request_headers.started", info)
        # Missing trace delivery is the dependency failure tested above too.

    monkeypatch.setattr(httpx2.AsyncClient, "__aexit__", close)
    if failure == "http":
        monkeypatch.setattr(AnyIOStream, "write", write)
    else:
        monkeypatch.setattr(source_transport._HeaderTrace, "__call__", trace)
    async with server() as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(SourceInvalidated),
        ):
            await post(url)
        assert len(requests) == (1 if failure == "missing_trace" else 0)
        async with gate.turn():
            assert gate.current("alice") is None


def sdk_payload(headers):
    if b"/responses/input_tokens " in headers:
        return b'{"object":"response.input_tokens","input_tokens":5}'
    return json.dumps(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "model": "test-model",
            "status": "completed",
            "usage": None,
            "output": [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "SDK response",
                            "annotations": [],
                        }
                    ],
                }
            ],
        }
    ).encode()


async def sdk_post(url, *, max_requests=2):
    from openai import AsyncOpenAI

    from pantaray_llm.contracts.request import LlmRequest
    from pantaray_llm.providers.openai_responses import provider
    from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile
    from pantaray_llm.providers.openai_responses.transport import openai_api_transport

    request = LlmRequest.model_validate(
        {
            "purpose": "test.profile",
            "messages": [
                {"role": "user", "content": [{"type": "input_text", "text": "raw"}]}
            ],
            "trace": {"local_job_id": "job"},
        }
    )
    profile = OpenAiLlmProfile(
        provider="openai",
        profile_id="test.profile",
        model="test-model",
        max_output_tokens=256,
        reasoning_effort="medium",
    )
    async with source_transport.source_http_client(
        "alice", timeout=httpx.Timeout(180), max_requests=max_requests
    ) as http_client:
        sdk = AsyncOpenAI(
            api_key="test-key",
            base_url=url,
            http_client=http_client,
            max_retries=0,
        )
        return await provider.execute_openai_request(
            request=request,
            client=sdk,
            profile=profile,
            uploaded_blobs={},
            transport=openai_api_transport(api_key="test-key", base_url=url),
        )


async def test_sdk_checks_both_requests_and_keeps_input_token_preflight():
    gate, source = await active()
    async with server(payload=sdk_payload) as (url, _, _, requests):
        with source_transport.source_scope(gate, source):
            response = await sdk_post(url)
        assert response.output[0].content[0].text == "SDK response"
        assert [headers.split(b"\r\n", 1)[0] for headers, _ in requests] == [
            b"POST /responses/input_tokens HTTP/1.1",
            b"POST /responses HTTP/1.1",
        ]
        for headers, body in requests:
            assert b"Authorization: Bearer test-key\r\n" in headers
            assert b"raw" in body


async def test_sdk_cannot_exceed_the_source_request_limit():
    gate, source = await active()
    async with server(payload=sdk_payload) as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(source_transport.SourceTransportError),
        ):
            await sdk_post(url, max_requests=1)
        assert len(requests) == 1
        assert b"/responses/input_tokens " in requests[0][0]


@pytest.mark.parametrize("cancel_delivered", [True, False])
async def test_sdk_invalidation_before_second_header_sends_only_preflight(
    monkeypatch, cancel_delivered
):
    gate, source = await active()
    reached, release = asyncio.Event(), asyncio.Event()
    if not cancel_delivered:
        monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)
    original = source_transport._HeaderTrace.__call__

    async def before_second_header(self, event, info):
        if event == "http11.send_request_headers.started" and self.started == 1:
            reached.set()
            await release.wait()
        await original(self, event, info)

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", before_second_header)
    async with server(payload=sdk_payload) as (url, _, _, requests):
        async with start(url, gate, source, sdk_post) as task:
            await reached.wait()
            await revoke(gate)
            release.set()
            with pytest.raises(
                asyncio.CancelledError if cancel_delivered else SourceInvalidated
            ):
                await task
            assert len(requests) == 1


@pytest.mark.parametrize("request_number", [1, 2])
async def test_sdk_header_write_holds_the_source_gate(monkeypatch, request_number):
    from httpcore._backends.anyio import AnyIOStream as SdkStream

    gate, source = await active()
    writing, release, queued, revoked = (asyncio.Event() for _ in range(4))
    original = SdkStream.write
    sent = 0

    async def write(self, buffer, timeout=None):
        nonlocal sent
        if buffer.startswith(b"POST "):
            sent += 1
            if sent == request_number:
                writing.set()
                await release.wait()
        await original(self, buffer, timeout)

    async def invalidate():
        queued.set()
        await revoke(gate)
        revoked.set()

    monkeypatch.setattr(SdkStream, "write", write)
    async with server(payload=sdk_payload) as (url, _, _, _):
        async with start(url, gate, source, sdk_post) as task:
            await writing.wait()
            waiter = asyncio.create_task(invalidate())
            await queued.wait()
            assert not revoked.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await waiter
            assert revoked.is_set()


@pytest.mark.parametrize("failed", [False, True])
async def test_sdk_rechecks_source_after_client_close_on_success_and_provider_failure(
    monkeypatch, failed
):
    import httpx

    gate, source = await active()
    monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)
    original = httpx.AsyncClient.__aexit__

    async def close(self, *args):
        await original(self, *args)
        await revoke(gate)

    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", close)
    async with server(
        status=b"429 Too Many Requests" if failed else b"200 OK",
        payload=b'{"error":{"message":"rate limited","type":"rate_limit_error"}}'
        if failed
        else sdk_payload,
    ) as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(SourceInvalidated),
        ):
            await sdk_post(url)
        assert len(requests) == (1 if failed else 2)


@pytest.mark.parametrize("broken", ["missing", "http2"])
async def test_sdk_broken_trace_survives_provider_error_normalization(
    monkeypatch, broken
):
    gate, source = await active()
    original = source_transport._HeaderTrace.__call__

    async def trace(self, event, info):
        if broken == "http2":
            await original(self, "http2.send_request_headers.started", info)

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", trace)
    async with server(payload=sdk_payload) as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(source_transport.SourceTransportError),
        ):
            await sdk_post(url)
        assert len(requests) == (2 if broken == "missing" else 0)


ANTHROPIC_TOOL = {
    "name": "pick",
    "description": "Pick a candidate.",
    "parameters": {
        "type": "object",
        "properties": {"choice": {"type": "integer"}},
        "required": ["choice"],
        "additionalProperties": False,
    },
}


def anthropic_replies(*contents):
    """Answer the n-th Messages POST with the n-th content, repeating the last."""

    sent = 0

    def payload(_headers):
        nonlocal sent
        content = contents[min(sent, len(contents) - 1)]
        sent += 1
        return json.dumps(
            {
                "type": "message",
                "id": f"msg_{sent}",
                "model": "claude-opus-5-5",
                "role": "assistant",
                "content": content,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }
        ).encode()

    return payload


TEXT_ONLY = [{"type": "text", "text": "Candidate 1 fits best."}]
CALL = [{"type": "tool_use", "id": "toolu_1", "name": "pick", "input": {"choice": 1}}]


async def anthropic_tool_call(url, monkeypatch):
    """One single-shot tool use request through the direct Anthropic route."""

    from pantaray_agents.local_runtime.llm_proxy import direct
    from pantaray_agents.local_runtime.runtime.connection_store import (
        ApiKeyConnection,
    )
    from pantaray_llm.contracts.request import LlmRequest
    from pantaray_llm.providers.anthropic import transport as anthropic_transport

    monkeypatch.setattr(anthropic_transport, "ANTHROPIC_MESSAGES_URL", url)
    request = LlmRequest.model_validate(
        {
            "purpose": "suggestion",
            "trace": {"local_job_id": "job"},
            "messages": [
                {"role": "user", "content": [{"type": "input_text", "text": "raw"}]}
            ],
            "tool_use": {"tools": [ANTHROPIC_TOOL], "continuation_mode": "disabled"},
        }
    )
    return await direct.execute_direct_llm_request(
        connection=ApiKeyConnection(
            provider="anthropic", model="claude-opus-5-5", api_key="test-key"
        ),
        request=request,
        uploaded_blobs={},
        user_id="alice",
        response_schema=None,
    )


async def test_anthropic_asks_again_within_the_source_request_limit(monkeypatch):
    gate, source = await active()
    async with server(payload=anthropic_replies(TEXT_ONLY, CALL)) as (
        url,
        _,
        _,
        requests,
    ):
        with source_transport.source_scope(gate, source):
            response = await anthropic_tool_call(url, monkeypatch)
        assert len(requests) == 2
        assert all(b"raw" in body for _, body in requests)
    assert response.tool_calls[0].arguments == {"choice": 1}
    assert response.usage_metadata["total_tokens"] == 26


async def test_anthropic_stops_asking_at_the_repair_limit(monkeypatch):
    from pantaray_llm.providers.anthropic.provider import MISSING_CALL_REPAIR_LIMIT

    gate, source = await active()
    async with server(payload=anthropic_replies(TEXT_ONLY)) as (url, _, _, requests):
        with (
            source_transport.source_scope(gate, source),
            pytest.raises(LlmProxyExecutionError) as caught,
        ):
            await anthropic_tool_call(url, monkeypatch)
        assert len(requests) == 1 + MISSING_CALL_REPAIR_LIMIT
    # Still the repairable rejection, billed for every request it sent.
    assert caught.value.tool_call_violation_reason == "missing_call"
    assert caught.value.recovery == "repair_next_turn"
    assert caught.value.usage_metadata["total_tokens"] == 13 * (
        1 + MISSING_CALL_REPAIR_LIMIT
    )


@pytest.mark.parametrize("cancel_delivered", [True, False])
async def test_anthropic_revocation_before_the_repair_sends_nothing_more(
    monkeypatch, cancel_delivered
):
    gate, source = await active()
    reached, release = asyncio.Event(), asyncio.Event()
    if not cancel_delivered:
        monkeypatch.setattr(gate, "_cancel_tracked", lambda *_: None)
    original = source_transport._HeaderTrace.__call__

    async def before_second_header(self, event, info):
        if event == "http11.send_request_headers.started" and self.started == 1:
            reached.set()
            await release.wait()
        await original(self, event, info)

    monkeypatch.setattr(source_transport._HeaderTrace, "__call__", before_second_header)
    async with server(payload=anthropic_replies(TEXT_ONLY, CALL)) as (
        url,
        _,
        _,
        requests,
    ):

        async def call(url):
            return await anthropic_tool_call(url, monkeypatch)

        async with start(url, gate, source, call) as task:
            await reached.wait()
            await revoke(gate)
            release.set()
            # The revocation stops the job like any other; it is not a
            # repairable rejection that would let the caller ask again.
            with pytest.raises(
                asyncio.CancelledError if cancel_delivered else SourceInvalidated
            ):
                await task
            assert len(requests) == 1

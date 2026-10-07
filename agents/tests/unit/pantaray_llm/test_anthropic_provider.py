from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from pantaray_llm.contracts.request import LlmProxyRequest
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import ProviderError
from pantaray_llm.providers.anthropic import provider as anthropic_provider
from pantaray_llm.providers.anthropic import transport as anthropic_transport
from pantaray_llm.providers.anthropic.provider import execute_anthropic_request
from pantaray_llm.providers.anthropic.settings import (
    AnthropicAdaptiveThinking,
    AnthropicLlmProfile,
)

_PAYLOAD = b"\x89PNG-bytes"
_TOOL: dict[str, object] = {
    "name": "lookup",
    "description": "Look up a record.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}


def _profile(**overrides: object) -> AnthropicLlmProfile:
    fields: dict[str, object] = {
        "provider": "anthropic",
        "profile_id": "test.profile",
        "model": "claude-test-model",
        "max_output_tokens": 256,
        **overrides,
    }
    return AnthropicLlmProfile(**fields)  # type: ignore[arg-type]


def _request(**overrides: object) -> LlmProxyRequest:
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
            **overrides,
        }
    )


def _descriptor(mime_type: str, **extra: object) -> dict[str, object]:
    return {
        "blob_ref": "blob-1",
        "mime_type": mime_type,
        "byte_size": len(_PAYLOAD),
        "sha256": hashlib.sha256(_PAYLOAD).hexdigest(),
        **extra,
    }


def _blobs(mime_type: str) -> dict[str, UploadedBlob]:
    return {
        "blob-1": UploadedBlob(
            field_name="blob-1", payload=_PAYLOAD, mime_type=mime_type
        )
    }


def _message(**overrides: object) -> dict[str, object]:
    return {
        "type": "message",
        "id": "msg_1",
        "model": "claude-test-model-2026",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "step", "signature": "sig"},
            {"type": "text", "text": "hello"},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 2,
        },
        **overrides,
    }


def _tool_block(**overrides: object) -> dict[str, object]:
    return {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "lookup",
        "input": {"id": "a"},
        **overrides,
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    outcome: httpx.Response | Exception,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def send(
        *, api_key: str, body: dict[str, object], client: httpx.AsyncClient | None
    ) -> httpx.Response:
        calls.append({"api_key": api_key, "body": body})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(anthropic_provider, "send_anthropic_message", send)
    return calls


_CACHED_SYSTEM = [
    {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
]


def _ok(payload: dict[str, object], **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers=headers)


async def _execute(
    request: LlmProxyRequest,
    *,
    profile: AnthropicLlmProfile | None = None,
    uploaded_blobs: dict[str, UploadedBlob] | None = None,
) -> object:
    return await execute_anthropic_request(
        request=request.to_request(),
        profile=profile or _profile(),
        uploaded_blobs=uploaded_blobs or {},
        api_key="key",
    )


@pytest.mark.asyncio
async def test_plain_text_request_sends_the_messages_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _ok(_message(), **{"request-id": "req_1"}))

    response = await _execute(_request(), profile=_profile(max_output_tokens=4096))

    body = calls[0]["body"]
    assert body["model"] == "claude-test-model"
    assert body["max_tokens"] == 4096
    # One breakpoint closes the tools and system prefix; the automatic one rides
    # the end of the messages, which carry no cache markers of their own.
    assert body["system"] == _CACHED_SYSTEM
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in body
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ]
    assert response.output[0].content[0].text == "hello"
    assert response.thinking == "step"
    assert response.meta.upstream_provider == "anthropic"
    assert response.meta.upstream_request_id == "req_1"
    assert response.meta.resolved_model == "claude-test-model-2026"
    # Anthropic reports the three input counts disjointly.
    assert response.usage.prompt_tokens == 17
    assert response.usage.cached_prompt_tokens == 5
    assert response.usage.cache_write_prompt_tokens == 2
    assert response.usage.total_tokens == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [None, AnthropicAdaptiveThinking(effort="high")])
async def test_structured_output_is_requested_and_decoded(
    monkeypatch: pytest.MonkeyPatch,
    thinking: AnthropicAdaptiveThinking | None,
) -> None:
    content = [{"type": "text", "text": '{"answer": "yes"}'}]
    calls = _install(monkeypatch, _ok(_message(content=content)))
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    response = await _execute(
        _request(response_format={"type": "json_schema", "json_schema": schema}),
        profile=_profile(thinking=thinking),
    )

    body = calls[0]["body"]
    if thinking is None:
        assert "thinking" not in body
        assert "effort" not in body["output_config"]
    else:
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"]["effort"] == "high"
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["properties"] == schema["properties"]
    assert json.loads(response.output[0].content[0].text) == {"answer": "yes"}


@pytest.mark.asyncio
async def test_image_inputs_become_base64_source_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _ok(_message()))
    block = {"type": "input_image", "image": _descriptor("image/png")}

    await _execute(
        _request(messages=[{"role": "user", "content": [block]}]),
        profile=_profile(enable_image_inputs=True),
        uploaded_blobs=_blobs("image/png"),
    )

    content = calls[0]["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_repeated_media_references_are_rejected_before_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _ok(_message()))
    block = {
        "type": "input_image",
        "image": {**_descriptor("image/png"), "byte_size": 30_000_000},
    }

    with pytest.raises(ProviderError) as exc_info:
        await _execute(
            _request(messages=[{"role": "user", "content": [block, block]}]),
            profile=_profile(enable_image_inputs=True),
        )

    assert exc_info.value.code == "PROXY_INVALID_INPUT"
    assert calls == []


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "PROXY_AUTHENTICATION_FAILED"),
        (402, "PROXY_INSUFFICIENT_BALANCE"),
        (403, "PROXY_AUTHENTICATION_FAILED"),
        (429, "PROXY_UPSTREAM_RATE_LIMITED"),
        (529, "PROXY_UPSTREAM_UNAVAILABLE"),
    ],
)
@pytest.mark.asyncio
async def test_failed_statuses_map_to_the_proxy_error_contract(
    monkeypatch: pytest.MonkeyPatch, status_code: int, expected_code: str
) -> None:
    body = {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
    _install(
        monkeypatch,
        httpx.Response(status_code, json=body, headers={"request-id": "req_2"}),
    )

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    details = exc_info.value.details
    assert exc_info.value.code == expected_code
    assert details is not None
    assert details["upstream_status_code"] == status_code
    assert details["upstream_request_id"] == "req_2"
    assert details["upstream_code"] == "overloaded_error"
    assert details["upstream_provider"] == "anthropic"


@pytest.mark.asyncio
async def test_transport_timeouts_are_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, httpx.ConnectTimeout("timed out"))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    assert exc_info.value.code == "PROXY_UPSTREAM_UNAVAILABLE"
    assert exc_info.value.status_code == 408


@pytest.mark.asyncio
async def test_unencodable_text_is_reported_as_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # httpx が本文を UTF-8 へ符号化するときの失敗（孤立サロゲートなど）。
    failure = UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
    _install(monkeypatch, failure)

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    assert exc_info.value.code == "PROXY_INVALID_INPUT"


@pytest.mark.asyncio
async def test_refusals_are_reported_as_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _ok(_message(stop_reason="refusal", content=[])))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    assert exc_info.value.code == "PROXY_UPSTREAM_FORBIDDEN"
    assert exc_info.value.details is not None
    assert exc_info.value.details["response_status"] == "refusal"


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
@pytest.mark.asyncio
async def test_truncated_output_reports_the_incomplete_contract_with_usage(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str
) -> None:
    _install(monkeypatch, _ok(_message(stop_reason=stop_reason)))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    details = exc_info.value.details
    assert exc_info.value.code == "PROXY_LLM_TOOL_CALL_INVALID"
    assert details is not None
    assert details["tool_call_violation_reason"] == "response_incomplete"
    assert details["prompt_tokens"] == 17


@pytest.mark.asyncio
async def test_malformed_known_blocks_are_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [{"type": "text"}, {"type": "text", "text": "hello"}]
    _install(monkeypatch, _ok(_message(content=content)))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    assert exc_info.value.code == "PROXY_INVALID_UPSTREAM_RESPONSE"


@pytest.mark.parametrize(
    "content", [[], [{"type": "server_tool_use"}]], ids=["empty", "unknown_block"]
)
@pytest.mark.asyncio
async def test_unusable_success_responses_are_rejected(
    monkeypatch: pytest.MonkeyPatch, content: list[dict[str, object]]
) -> None:
    _install(monkeypatch, _ok(_message(content=content)))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(_request())

    assert exc_info.value.code == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert exc_info.value.details is not None
    assert exc_info.value.details["prompt_tokens"] == 17


@pytest.mark.asyncio
async def test_transport_sends_credentials_only_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok(_message())

    def client(*, timeout: float) -> httpx.AsyncClient:
        assert timeout == anthropic_transport.ANTHROPIC_REQUEST_TIMEOUT_SECONDS
        return real_client(transport=httpx.MockTransport(respond), timeout=timeout)

    monkeypatch.setattr(anthropic_transport.httpx, "AsyncClient", client)
    await _execute(_request())

    request = requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content)["model"] == "claude-test-model"


@pytest.mark.parametrize("status_code", [200, 429])
@pytest.mark.asyncio
async def test_borrowed_client_preserves_transport_and_caller_lifetime(
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=_message())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        try:
            response = await execute_anthropic_request(
                request=_request().to_request(),
                profile=_profile(),
                uploaded_blobs={},
                api_key="borrowed-key",
                http_client=client,
            )
        except ProviderError as exc:
            assert status_code == 429
            assert exc.code == "PROXY_UPSTREAM_RATE_LIMITED"
            assert exc.details["local_job_id"] == "job-1"
        else:
            assert status_code == 200
            assert response.output[0].content[0].text == "hello"
            assert response.meta.local_job_id == "job-1"
        assert not client.is_closed

    assert len(requests) == 1
    assert str(requests[0].url) == anthropic_transport.ANTHROPIC_MESSAGES_URL
    assert requests[0].headers["x-api-key"] == "borrowed-key"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"
    assert json.loads(requests[0].content) == {
        "model": "claude-test-model",
        "max_tokens": 256,
        "system": _CACHED_SYSTEM,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "cache_control": {"type": "ephemeral"},
    }


@pytest.mark.asyncio
async def test_adaptive_thinking_without_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _ok(_message()))
    await _execute(
        _request(), profile=_profile(thinking=AnthropicAdaptiveThinking(effort="low"))
    )
    body = calls[0]["body"]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_tool_use_requests_ask_for_one_call_without_forcing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [{"type": "text", "text": "Looking it up."}, _tool_block()]
    calls = _install(
        monkeypatch, _ok(_message(content=content, stop_reason="tool_use"))
    )

    # The Action subagent's shape: it rebuilds each prompt from its durable
    # transcript, so it asks for no continuation and gets none back.
    response = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "disabled"})
    )

    body = calls[0]["body"]
    assert body["tools"] == [
        {
            "name": "lookup",
            "description": "Look up a record.",
            "input_schema": _TOOL["parameters"],
        }
    ]
    # Current models reject a forced tool choice; the response side enforces it.
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    call = response.tool_use.calls[0]
    assert (call.call_id, call.name, call.arguments) == (
        "toolu_1",
        "lookup",
        {"id": "a"},
    )
    assert response.tool_use.continuation is None
    assert response.finish_reason == "tool_use"
    assert response.output[0].content[0].text == "Looking it up."
    assert response.meta.outcome == "complete"


def _install_sequence(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def send(
        *, api_key: str, body: dict[str, object], client: httpx.AsyncClient | None
    ) -> httpx.Response:
        calls.append({"api_key": api_key, "body": body})
        return responses[len(calls) - 1]

    monkeypatch.setattr(anthropic_provider, "send_anthropic_message", send)
    return calls


_TEXT_ONLY = [
    {"type": "thinking", "thinking": "", "signature": "sig-0"},
    {"type": "text", "text": "I will look it up."},
]
_REPAIR_REQUEST = {
    "role": "user",
    "content": [{"type": "text", "text": anthropic_provider.MISSING_CALL_REPAIR_TEXT}],
}


@pytest.mark.asyncio
async def test_a_text_only_reply_to_a_tool_use_request_is_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_sequence(
        monkeypatch,
        [
            _ok(_message(content=_TEXT_ONLY)),
            _ok(_message(content=[_tool_block()], stop_reason="tool_use")),
        ],
    )

    response = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "stateless"})
    )

    first, second = (call["body"]["messages"] for call in calls)
    assert first == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    # The reply stays in place, signed thinking included, so the second request
    # only appends to the first one's prefix.
    assert second == [
        *first,
        {"role": "assistant", "content": _TEXT_ONLY},
        _REPAIR_REQUEST,
    ]
    assert response.meta.outcome == "complete"
    assert response.tool_use.calls[0].call_id == "toolu_1"
    assert response.tool_use.continuation.messages == [
        *second,
        {"role": "assistant", "content": [_tool_block()]},
    ]
    # Both requests are billed.
    assert response.usage.prompt_tokens == 34
    assert response.usage.cached_prompt_tokens == 10
    assert response.usage.total_tokens == 40


@pytest.mark.asyncio
async def test_repeated_text_only_replies_stop_at_the_repair_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = anthropic_provider.MISSING_CALL_REPAIR_LIMIT
    calls = _install_sequence(
        monkeypatch, [_ok(_message(content=_TEXT_ONLY))] * (limit + 1)
    )

    response = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "disabled"})
    )

    assert len(calls) == limit + 1
    assert response.meta.outcome == "model_output_rejected"
    assert response.model_error.violation_reason == "missing_call"
    assert response.model_error.recovery == "repair_next_turn"
    assert response.usage.total_tokens == 20 * (limit + 1)


@pytest.mark.asyncio
async def test_action_turns_allow_parallel_calls_and_carry_commentary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [
        {"type": "text", "text": "Looking it up."},
        _tool_block(),
        {"type": "text", "text": "   "},
        _tool_block(id="toolu_2", input={"id": "b"}),
        _tool_block(id="toolu_3", input={"id": "c"}),
    ]
    calls = _install(
        monkeypatch, _ok(_message(content=content, stop_reason="tool_use"))
    )

    response = await _execute(
        _request(
            tool_use={
                "mode": "action_turn",
                "tools": [_TOOL],
                "max_parallel_tool_calls": 2,
            }
        )
    )

    assert calls[0]["body"]["tool_choice"] == {"type": "auto"}
    turn = response.tool_use
    assert [message.text for message in turn.messages] == ["Looking it up."]
    assert [call.call_id for call in turn.calls] == ["toolu_1", "toolu_2"]
    assert turn.dropped_call_names == ["lookup"]
    # Action の発話は commentary として返す。出力本文へ重ねて返さない。
    assert response.output == []


@pytest.mark.parametrize(
    ("content", "violation_reason"),
    [
        ([{"type": "text", "text": "I cannot."}], "missing_call"),
        ([_tool_block(name="unknown")], "undeclared_tool"),
        ([_tool_block(input={})], "arguments_schema_mismatch"),
        ([_tool_block(), _tool_block(id="toolu_2")], "multiple_calls"),
    ],
)
@pytest.mark.asyncio
async def test_tool_contract_violations_are_returned_as_rejected_output(
    monkeypatch: pytest.MonkeyPatch,
    content: list[dict[str, object]],
    violation_reason: str,
) -> None:
    _install(monkeypatch, _ok(_message(content=content, stop_reason="tool_use")))

    response = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "disabled"})
    )

    assert response.meta.outcome == "model_output_rejected"
    assert response.model_error.violation_reason == violation_reason
    assert response.model_error.recovery == "repair_next_turn"
    assert response.tool_use is None
    assert response.output == []


@pytest.mark.asyncio
async def test_overlong_action_commentary_is_repairable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [{"type": "text", "text": "x" * 4001}, _tool_block()]
    _install(monkeypatch, _ok(_message(content=content, stop_reason="tool_use")))

    response = await _execute(
        _request(tool_use={"mode": "action_turn", "tools": [_TOOL]})
    )

    assert response.meta.outcome == "model_output_rejected"
    assert response.model_error.violation_reason == "invalid_response"


@pytest.mark.asyncio
async def test_malformed_tool_use_blocks_are_not_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 壊れた tool_use を未知ブロックとして読み飛ばすと、道具を呼んだ応答が
    # 「呼び出しなし」に化ける。既知ブロックとして厳密に解く。
    content = [{"type": "tool_use", "id": "toolu_1", "name": "lookup"}]
    _install(monkeypatch, _ok(_message(content=content, stop_reason="tool_use")))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(
            _request(tool_use={"tools": [_TOOL], "continuation_mode": "disabled"})
        )

    assert exc_info.value.code == "PROXY_INVALID_UPSTREAM_RESPONSE"


_THINKING_BLOCKS: list[dict[str, object]] = [
    {"type": "thinking", "thinking": "step", "signature": "sig-1"},
    {"type": "redacted_thinking", "data": "encrypted-1"},
]


@pytest.mark.asyncio
async def test_stateless_first_turn_returns_the_replayable_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [*_THINKING_BLOCKS, _tool_block()]
    calls = _install(
        monkeypatch, _ok(_message(content=content, stop_reason="tool_use"))
    )

    response = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "stateless"})
    )

    assert calls[0]["body"]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ]
    continuation = response.tool_use.continuation
    assert continuation.provider == "anthropic"
    assert continuation.messages == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": content},
    ]


@pytest.mark.asyncio
async def test_tool_results_resume_the_conversation_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_content = [*_THINKING_BLOCKS, _tool_block()]
    second_content = [
        {"type": "thinking", "thinking": "more", "signature": "sig-2"},
        _tool_block(id="toolu_2", input={"id": "b"}),
    ]
    first_calls = _install(
        monkeypatch, _ok(_message(content=first_content, stop_reason="tool_use"))
    )
    first = await _execute(
        _request(tool_use={"tools": [_TOOL], "continuation_mode": "stateless"})
    )
    second_calls = _install(
        monkeypatch, _ok(_message(content=second_content, stop_reason="tool_use"))
    )

    second = await _execute(
        _request(
            tool_use={
                "tools": [_TOOL],
                "continuation_mode": "stateless",
                "continuation": first.tool_use.continuation.model_dump(mode="json"),
                "tool_result": {
                    "call_id": "toolu_1",
                    "name": "lookup",
                    "output": {"status": "error", "message": "not found"},
                },
            }
        )
    )

    assert len(first_calls) == 1
    # The signed assistant turn goes back byte for byte, and the tool result
    # opens the user message that directly follows it.
    assert second_calls[0]["body"]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": first_content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"status": "error", "message": "not found"}',
                }
            ],
        },
    ]
    # The system instruction and the tool declarations are resent every turn,
    # and the automatic breakpoint moves to the end of the longer conversation.
    assert second_calls[0]["body"]["system"] == _CACHED_SYSTEM
    assert second_calls[0]["body"]["cache_control"] == {"type": "ephemeral"}
    assert second_calls[0]["body"]["tools"][0]["name"] == "lookup"
    assert second.tool_use.calls[0].call_id == "toolu_2"
    assert second.tool_use.continuation.messages == [
        *second_calls[0]["body"]["messages"],
        {"role": "assistant", "content": second_content},
    ]


@pytest.mark.asyncio
async def test_a_continuation_from_another_provider_is_rejected_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, _ok(_message()))

    with pytest.raises(ProviderError) as exc_info:
        await _execute(
            _request(
                tool_use={
                    "tools": [_TOOL],
                    "continuation_mode": "stateless",
                    "continuation": {
                        "provider": "openai",
                        "history_items": [
                            {
                                "role": "user",
                                "content": [{"type": "input_text", "text": "hi"}],
                            }
                        ],
                    },
                    "tool_result": {
                        "call_id": "call_1",
                        "name": "lookup",
                        "output": {"ok": True},
                    },
                }
            )
        )

    assert exc_info.value.code == "PROXY_CONTINUATION_PROVIDER_MISMATCH"
    assert calls == []

"""Direct provider dispatch: send boundary, response parity, credential safety.

Every test records the requests leaving through the client the dispatcher lends
itself, so a request that must not reach a provider and a rejection that must not
spill onto a second host are both observable (acceptance A05 / A07).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel

from pantaray_agents.agents.core import CountingSink
from pantaray_agents.agents.core.mixins.llm_generation_mixin import (
    LLMGenerationMixin,
)
from pantaray_agents.local_runtime.llm_proxy import direct
from pantaray_agents.local_runtime.llm_proxy.response_parsing import (
    extract_meta,
    extract_provider_turn,
    extract_text,
    extract_thinking,
    extract_tool_use,
    extract_usage,
)
from pantaray_agents.local_runtime.llm_proxy.types import ProxyResponse
from pantaray_agents.local_runtime.runtime.connection_store import (
    ApiKeyConnection,
    ChatGptConnection,
    ChatGptCredential,
    LlmConnection,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_ACTION_JOB_TYPE,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE,
)
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.contracts.request import (
    LlmProxyResponse,
    LlmRequest,
    LlmRequestTrace,
)
from pantaray_llm.contracts.tool_use import LlmToolUseResponse
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import LlmProxyExecutionError, ProviderError
from pantaray_llm.profiles import ACTIVITY_SUMMARY_PROFILE_ID, MEMORY_UPDATE_PROFILE_ID
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS
from pantaray_llm.providers.anthropic.provider import MISSING_CALL_REPAIR_LIMIT
from pantaray_llm.providers.openai_responses.retry_policy import (
    LLM_TRANSPORT_MAX_ATTEMPTS,
)

OPENAI_KEY = "sk-test-openai-000000000000000000"
CHATGPT_TOKEN = "chatgpt-access-token-000000000000"
FIREWORKS_KEY = "fw-test-key-000000000000000000"
ANTHROPIC_KEY = "sk-ant-test-000000000000000000"
OWNER_ID = "owner-1"
BLOB_REF = "blob-1"
TEXT_BLOCK = {"type": "input_text", "text": "hi"}
LOCAL_JOB_ID = "job-1"
# A model no cloud purpose selects, so a request built from a purpose's own model
# rather than from the connection is visible in the recorded body.
USER_MODEL = "gpt-5.6-terra"

OPENAI_CONNECTION = ApiKeyConnection(
    provider="openai", model="gpt-6-luna", api_key=OPENAI_KEY
)
FIREWORKS_CONNECTION = ApiKeyConnection(
    provider="fireworks",
    model="accounts/fireworks/models/gpt-oss-120b",
    api_key=FIREWORKS_KEY,
)
ANTHROPIC_CONNECTION = ApiKeyConnection(
    provider="anthropic", model="claude-sonnet-5-5", api_key=ANTHROPIC_KEY
)
CHATGPT_CONNECTION = ChatGptConnection(
    model="gpt-5.6-sol",
    credential=ChatGptCredential(
        access_token=CHATGPT_TOKEN,
        expires_at="2099-01-01T00:00:00Z",
        account_id="acct-9",
    ),
)

ECHO_TOOL = {
    "name": "echo",
    "description": "Echo a value back.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
ANTHROPIC_MESSAGE = {
    "type": "message",
    "id": "msg_anthropic_1",
    "model": "claude-sonnet-5-5",
    "content": [
        {"type": "thinking", "thinking": "considering"},
        {"type": "text", "text": "hello"},
    ],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 1,
    },
}


class Answer(BaseModel):
    answer: str


def llm_request(
    *,
    purpose: str = ACTIVITY_SUMMARY_PROFILE_ID,
    content: list[dict[str, Any]] | None = None,
    tool_use: dict[str, Any] | None = None,
    response_format: dict[str, Any] | None = None,
) -> LlmRequest:
    payload: dict[str, Any] = {
        "purpose": purpose,
        "trace": {"local_job_id": LOCAL_JOB_ID},
        "messages": [
            {"role": "system", "content": [{"type": "input_text", "text": "sys"}]},
            {"role": "user", "content": content or [TEXT_BLOCK]},
        ],
    }
    if tool_use is not None:
        payload["tool_use"] = tool_use
    if response_format is not None:
        payload["response_format"] = response_format
    return LlmRequest.model_validate(payload)


def responses_payload(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "created_at": 1,
        "model": "test-model-2026",
        "object": "response",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 15,
        },
    }


def text_output(text: str, *, phase: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    if phase is not None:
        message["phase"] = phase
    return message


def function_call_output(*, name: str = "echo") -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": name,
        "arguments": '{"value":"hi"}',
        "status": "completed",
    }


def reasoning_output(item_id: str, *, encrypted: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "reasoning", "id": item_id, "summary": []}
    if encrypted is not None:
        item["encrypted_content"] = encrypted
    return item


def image_media(payload: bytes) -> dict[str, Any]:
    return {
        "mime_type": "image/png",
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def png_blobs(payload: bytes) -> dict[str, UploadedBlob]:
    return {
        BLOB_REF: UploadedBlob(
            field_name=BLOB_REF, payload=payload, mime_type="image/png"
        )
    }


def image_block(payload: bytes) -> dict[str, Any]:
    return {
        "type": "input_image",
        "image": {"blob_ref": BLOB_REF, **image_media(payload)},
    }


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class SendBoundary:
    """Lends the dispatcher a client whose every request is recorded."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.subjects: list[str] = []
        self.budgets: list[int] = []
        self.timeouts: list[httpx.Timeout] = []
        self.emitted: list[LlmProxyResponse] = []
        self._handler = handler

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def capture(self, execute: Callable[..., Any]) -> Callable[..., Any]:
        # Keep the typed result so it can be replayed through the cloud parsing.
        async def run(**kwargs: Any) -> LlmProxyResponse:
            response = await execute(**kwargs)
            self.emitted.append(response)
            return response

        return run

    @asynccontextmanager
    async def http_client(
        self, user_id: str, *, timeout: httpx.Timeout, max_requests: int = 1
    ) -> Any:
        self.subjects.append(user_id)
        self.budgets.append(max_requests)
        self.timeouts.append(timeout)
        async with httpx.AsyncClient(
            timeout=timeout, transport=httpx.MockTransport(self._respond)
        ) as client:
            yield client

    @property
    def targets(self) -> list[tuple[str, str]]:
        return [(request.url.host, request.url.path) for request in self.requests]


def sse_body(payload: dict[str, Any]) -> bytes:
    # The ChatGPT transport only accepts `stream: true`, so it reads back SSE.
    opened = {**payload, "status": "in_progress", "output": [], "usage": None}
    events = (("response.created", opened), ("response.completed", payload))
    return "".join(
        f"event: {name}\ndata: "
        + json.dumps({"type": name, "sequence_number": index, "response": response})
        + "\n\n"
        for index, (name, response) in enumerate(events)
    ).encode()


def responder(
    payload: dict[str, Any] | None = None, *, status_code: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    # Answer any of the four providers so a wrong endpoint is still recorded.
    body = payload if payload is not None else responses_payload([text_output("hello")])

    def respond(request: httpx.Request) -> httpx.Response:
        if status_code != 200:
            return httpx.Response(status_code, json={"error": {"message": "rejected"}})
        if request.url.path.endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100, "object": "response"})
        if request.url.path == "/v1/messages":
            return httpx.Response(200, json=ANTHROPIC_MESSAGE)
        if b'"stream":true' in (request.content or b""):
            return httpx.Response(
                200,
                content=sse_body(body),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=body)

    return respond


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Callable[..., SendBoundary]:
    def install(
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> SendBoundary:
        recorder = SendBoundary(handler or responder())
        monkeypatch.setattr(direct, "source_http_client", recorder.http_client)
        for adapter in ("execute_openai_request", "execute_anthropic_request"):
            monkeypatch.setattr(
                direct, adapter, recorder.capture(getattr(direct, adapter))
            )
        return recorder

    return install


async def dispatch(
    connection: LlmConnection,
    *,
    request: LlmRequest | None = None,
    uploaded_blobs: dict[str, UploadedBlob] | None = None,
    response_schema: object | None = None,
) -> ProxyResponse:
    return await direct.execute_direct_llm_request(
        connection=connection,
        request=request or llm_request(),
        uploaded_blobs=uploaded_blobs or {},
        user_id=OWNER_ID,
        response_schema=response_schema,
    )


def assert_matches_cloud_route(result: ProxyResponse, recorder: SendBoundary) -> None:
    """The same adapter result must reach the caller identically on both routes.

    The cloud proxy serializes the adapter's response and `client.py` reassembles
    a `ProxyResponse` from that JSON with the helpers below. `parsed` is excluded:
    the caller's own schema object never crosses the wire.
    """

    payload = json.loads(recorder.emitted[-1].model_dump_json())
    tool_use = extract_tool_use(payload)
    turn = tool_use if isinstance(tool_use, LlmToolUseResponse) else None
    assert dataclasses.replace(result, parsed=None) == ProxyResponse(
        text=extract_text(payload),
        usage_metadata=extract_usage(payload),
        parsed=None,
        meta=extract_meta(payload),
        thinking=extract_thinking(payload),
        tool_calls=tuple(turn.calls) if turn is not None else (),
        dropped_tool_call_names=tuple(turn.dropped_call_names) if turn else (),
        tool_continuation=turn.continuation if turn is not None else None,
        action_turn=None if turn is not None else tool_use,
        provider_turn=extract_provider_turn(payload),
    )


async def test_answer_text_is_trimmed_the_way_the_cloud_route_trims_it(
    boundary: Callable[..., SendBoundary],
) -> None:
    recorder = boundary(responder(payload=responses_payload([text_output("hello\n")])))

    result = await dispatch(OPENAI_CONNECTION)

    assert result.text == "hello"
    assert_matches_cloud_route(result, recorder)


async def test_a_blank_answer_is_retried_instead_of_adopted(
    boundary: Callable[..., SendBoundary],
) -> None:
    boundary(responder(payload=responses_payload([text_output("   ")])))

    with pytest.raises(LlmProxyExecutionError) as caught:
        await dispatch(OPENAI_CONNECTION)

    # The cloud route refuses the same response with the same retryable code.
    assert caught.value.error_code == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert caught.value.retryable is True
    assert caught.value.local_job_id == LOCAL_JOB_ID


async def test_an_openai_text_response_reaches_the_caller(
    boundary: Callable[..., SendBoundary],
) -> None:
    recorder = boundary()

    result = await dispatch(OPENAI_CONNECTION)

    assert recorder.targets == [
        ("api.openai.com", "/v1/responses/input_tokens"),
        ("api.openai.com", "/v1/responses"),
    ]
    # The preflight is the only reason a second send may leave for one request.
    assert recorder.budgets == [len(recorder.requests)]
    assert recorder.requests[-1].headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert result.text == "hello"
    assert result.usage_metadata == {
        "prompt_tokens": 12,
        "cached_prompt_tokens": 2,
        "cache_write_prompt_tokens": 1,
        "completion_tokens": 3,
        "reasoning_tokens": 1,
        "total_tokens": 15,
    }
    assert result.meta == {
        "local_job_id": LOCAL_JOB_ID,
        "upstream_provider": "openai",
        "profile_id": ACTIVITY_SUMMARY_PROFILE_ID,
        "outcome": "complete",
        "upstream_request_id": "resp_1",
    }


async def test_structured_output_is_decoded_with_the_caller_schema(
    boundary: Callable[..., SendBoundary],
) -> None:
    boundary(responder(responses_payload([text_output(json.dumps({"answer": "ok"}))])))

    result = await dispatch(
        OPENAI_CONNECTION,
        request=llm_request(
            response_format={"type": "json_schema", "json_schema": ANSWER_SCHEMA}
        ),
        response_schema=Answer,
    )

    assert result.parsed == Answer(answer="ok")
    assert json.loads(result.text) == {"answer": "ok"}


async def test_tool_use_carries_the_calls_and_the_continuation(
    boundary: Callable[..., SendBoundary],
) -> None:
    recorder = boundary(
        responder(responses_payload([text_output("hello"), function_call_output()]))
    )

    result = await dispatch(
        OPENAI_CONNECTION,
        request=llm_request(
            purpose=MEMORY_UPDATE_PROFILE_ID,
            tool_use={"tools": [ECHO_TOOL], "continuation_mode": "stateless"},
        ),
    )

    assert [call.name for call in result.tool_calls] == ["echo"]
    assert result.tool_calls[0].arguments == {"value": "hi"}
    assert result.dropped_tool_call_names == ()
    assert result.tool_continuation is not None
    assert result.tool_continuation.provider == "openai"
    assert result.action_turn is None
    assert_matches_cloud_route(result, recorder)


async def test_a_continuation_replays_only_reasoning_the_backend_can_resolve(
    boundary: Callable[..., SendBoundary],
) -> None:
    """Every request sends `store: false`, so the backend can only resolve a
    replayed reasoning item that carries its encrypted state. The bare one has to
    be dropped from the continuation rather than sent as an unknown reference.
    """

    recorder = boundary(
        responder(
            responses_payload(
                [
                    reasoning_output("rs_sealed", encrypted="sealed-state"),
                    reasoning_output("rs_bare", encrypted=None),
                    function_call_output(),
                ]
            )
        )
    )
    tool_use: dict[str, Any] = {
        "tools": [ECHO_TOOL],
        "continuation_mode": "stateless",
    }

    first = await dispatch(
        CHATGPT_CONNECTION,
        request=llm_request(purpose=MEMORY_UPDATE_PROFILE_ID, tool_use=tool_use),
    )

    assert first.tool_continuation is not None
    await dispatch(
        CHATGPT_CONNECTION,
        request=llm_request(
            purpose=MEMORY_UPDATE_PROFILE_ID,
            tool_use={
                **tool_use,
                "continuation": first.tool_continuation.model_dump(mode="json"),
                "tool_result": {"call_id": "call_1", "name": "echo", "output": "done"},
            },
        ),
    )

    replayed = json.loads(recorder.requests[-1].content)["input"]
    reasoning = [item for item in replayed if item.get("type") == "reasoning"]
    assert [item["id"] for item in reasoning] == ["rs_sealed"]
    assert reasoning[0]["encrypted_content"] == "sealed-state"
    # The call it answers still has to be replayed alongside its result.
    assert [item.get("call_id") for item in replayed if "call_id" in item] == [
        "call_1",
        "call_1",
    ]


async def test_an_action_turn_carries_its_commentary_and_calls(
    boundary: Callable[..., SendBoundary],
) -> None:
    turn = [text_output("on it", phase="commentary"), function_call_output()]
    boundary(responder(responses_payload(turn)))

    result = await dispatch(
        OPENAI_CONNECTION,
        request=llm_request(
            purpose=MEMORY_UPDATE_PROFILE_ID,
            tool_use={"mode": "action_turn", "tools": [ECHO_TOOL]},
        ),
    )

    assert result.action_turn is not None
    assert [message.text for message in result.action_turn.messages] == ["on it"]
    assert [call.name for call in result.action_turn.calls] == ["echo"]
    # An Action turn keeps its commentary inside the turn, never in `text`.
    assert result.text == ""
    assert result.tool_calls == ()


_INPUT_TEXT = {"type": "input_text", "text": "続けて"}


async def test_an_action_turns_own_output_items_reach_the_caller(
    boundary: Callable[..., SendBoundary],
) -> None:
    """会話つきの Action turn は、その応答の出力項目ごと呼び出し側へ返る。"""

    turn = [
        reasoning_output("rs_sealed", encrypted="sealed-state"),
        text_output("on it", phase="commentary"),
        function_call_output(),
    ]
    recorder = boundary(responder(responses_payload(turn)))

    result = await dispatch(
        OPENAI_CONNECTION,
        request=llm_request(
            purpose=MEMORY_UPDATE_PROFILE_ID,
            tool_use={
                "mode": "action_turn",
                "tools": [ECHO_TOOL],
                "conversation": [{"type": "user", "content": [_INPUT_TEXT]}],
            },
        ),
    )

    assert result.provider_turn is not None
    assert [item["type"] for item in result.provider_turn.items] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert result.provider_turn.items[0]["encrypted_content"] == "sealed-state"
    assert_matches_cloud_route(result, recorder)


@pytest.mark.parametrize(
    ("status_code", "error_code", "retryable"),
    [
        (401, "PROXY_AUTHENTICATION_FAILED", False),
        (429, "PROXY_UPSTREAM_RATE_LIMITED", True),
    ],
)
async def test_provider_rejections_map_to_the_proxy_error_contract(
    boundary: Callable[..., SendBoundary],
    status_code: int,
    error_code: str,
    retryable: bool,
) -> None:
    recorder = boundary(responder(status_code=status_code))

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(FIREWORKS_CONNECTION)

    assert recorder.targets == [("api.fireworks.ai", "/inference/v1/responses")]
    assert exc_info.value.error_code == error_code
    assert exc_info.value.retryable is retryable
    assert exc_info.value.upstream_provider == "fireworks"
    assert exc_info.value.upstream_status_code == status_code
    assert exc_info.value.local_job_id == LOCAL_JOB_ID


@pytest.mark.parametrize(
    ("error_type", "error_code", "retryable"),
    [
        ("usage_limit_reached", "PROXY_UPSTREAM_RATE_LIMITED", True),
        ("usage_not_included", "PROXY_INSUFFICIENT_BALANCE", False),
    ],
)
async def test_a_usage_rejection_is_classified_from_the_429_body(
    boundary: Callable[..., SendBoundary],
    error_type: str,
    error_code: str,
    retryable: bool,
) -> None:
    """A plan that never includes this model does not come back by waiting, so the
    body decides whether the caller may retry.
    """

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"type": error_type, "plan_type": "plus"}}
        )

    boundary(respond)

    with pytest.raises(LlmProxyExecutionError) as caught:
        await dispatch(CHATGPT_CONNECTION)

    assert caught.value.error_code == error_code
    assert caught.value.retryable is retryable
    assert caught.value.upstream_code == error_type
    assert caught.value.upstream_status_code == 429


async def test_a_known_capability_gap_fails_before_any_request(
    boundary: Callable[..., SendBoundary],
) -> None:
    recorder = boundary()
    payload = png_bytes()

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(
            FIREWORKS_CONNECTION,
            request=llm_request(content=[image_block(payload)]),
            uploaded_blobs=png_blobs(payload),
        )

    assert recorder.requests == []
    assert exc_info.value.error_code == "PROXY_MODEL_CAPABILITY_UNSUPPORTED"
    assert exc_info.value.retryable is False
    assert exc_info.value.profile_id == ACTIVITY_SUMMARY_PROFILE_ID
    assert exc_info.value.upstream_provider == "fireworks"
    cause = exc_info.value.__cause__
    assert isinstance(cause, ProviderError)
    assert cause.details == {
        "profile_id": ACTIVITY_SUMMARY_PROFILE_ID,
        "required_capability": "image_input",
        "upstream_provider": "fireworks",
    }


@pytest.mark.parametrize(
    ("connection", "host", "path", "budget", "headers"),
    [
        (
            CHATGPT_CONNECTION,
            "chatgpt.com",
            "/backend-api/codex/responses",
            1,
            {
                "authorization": f"Bearer {CHATGPT_TOKEN}",
                "chatgpt-account-id": "acct-9",
                "originator": "pantaray",
                "openai-beta": "responses=experimental",
                "accept": "text/event-stream",
            },
        ),
        (
            FIREWORKS_CONNECTION,
            "api.fireworks.ai",
            "/inference/v1/responses",
            1,
            {"authorization": f"Bearer {FIREWORKS_KEY}"},
        ),
        (
            ANTHROPIC_CONNECTION,
            "api.anthropic.com",
            "/v1/messages",
            # Room to ask again after a tool use reply without a call.
            1 + MISSING_CALL_REPAIR_LIMIT,
            {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        ),
    ],
    ids=["chatgpt", "fireworks", "anthropic"],
)
async def test_each_connection_reaches_only_its_own_endpoint(
    boundary: Callable[..., SendBoundary],
    connection: LlmConnection,
    host: str,
    path: str,
    budget: int,
    headers: dict[str, str],
) -> None:
    recorder = boundary()

    result = await dispatch(connection)

    assert len(recorder.requests) == 1
    assert recorder.budgets == [budget]
    assert recorder.subjects == [OWNER_ID]
    assert recorder.targets[-1] == (host, path)
    assert {reached for reached, _ in recorder.targets} == {host}
    sent = recorder.requests[-1].headers
    assert {name: sent.get(name) for name in headers} == headers
    assert result.text == "hello"
    assert_matches_cloud_route(result, recorder)


@pytest.mark.parametrize(
    "purpose", [settings.profile_id for settings in SUBAGENT_MODEL_SETTINGS]
)
@pytest.mark.parametrize(
    "connection",
    [
        ApiKeyConnection(provider="openai", model=USER_MODEL, api_key=OPENAI_KEY),
        ApiKeyConnection(
            provider="fireworks",
            model="accounts/fireworks/models/gpt-oss-120b",
            api_key=FIREWORKS_KEY,
        ),
        ApiKeyConnection(
            provider="anthropic", model="claude-opus-5-5", api_key=ANTHROPIC_KEY
        ),
        ChatGptConnection(model=USER_MODEL, credential=CHATGPT_CONNECTION.credential),
    ],
    ids=["openai", "fireworks", "anthropic", "chatgpt"],
)
async def test_a_subagent_purpose_runs_on_the_user_model_not_the_cloud_lineup(
    boundary: Callable[..., SendBoundary],
    connection: LlmConnection,
    purpose: str,
) -> None:
    """A subagent purpose names a cloud model; direct still sends the user's own.

    The Supervisor picks a child model out of the cloud lineup, and that choice
    reaches the child job as its purpose. Cloud maps that purpose onto the named
    OpenAI model; direct has one model, the configured one (design 6.4, D-11).
    None of these connections names a lineup model, so a request that resolved
    the purpose's model instead would be an OpenAI model ID sent to whichever
    provider this is. The purpose still declares `tool_use`, so each connection's
    catalog entry is checked for it before the model reaches the body; carrying
    an actual tool-use turn belongs to the adapter tests.
    """

    assert connection.model not in {
        settings.model for settings in SUBAGENT_MODEL_SETTINGS
    }
    recorder = boundary()

    result = await dispatch(connection, request=llm_request(purpose=purpose))

    assert json.loads(recorder.requests[-1].content)["model"] == connection.model
    assert result.meta["profile_id"] == purpose


async def test_an_uploaded_image_reaches_the_provider_payload(
    boundary: Callable[..., SendBoundary],
) -> None:
    payload = png_bytes()
    recorder = boundary()

    await dispatch(
        OPENAI_CONNECTION,
        request=llm_request(content=[image_block(payload)]),
        uploaded_blobs=png_blobs(payload),
    )

    block = json.loads(recorder.requests[-1].content)["input"][0]["content"][-1]
    assert base64.b64encode(payload).decode("ascii") in block["image_url"]


async def test_a_rejected_tool_call_becomes_the_model_output_error(
    boundary: Callable[..., SendBoundary],
) -> None:
    boundary(responder(responses_payload([function_call_output(name="not_declared")])))

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(
            OPENAI_CONNECTION,
            request=llm_request(
                purpose=MEMORY_UPDATE_PROFILE_ID,
                tool_use={"tools": [ECHO_TOOL], "continuation_mode": "stateless"},
            ),
        )

    assert exc_info.value.error_code == "PROXY_LLM_TOOL_CALL_INVALID"
    assert exc_info.value.retryable is False
    assert exc_info.value.recovery == "repair_next_turn"
    assert exc_info.value.tool_call_violation_reason == "undeclared_tool"
    assert exc_info.value.tool_name == "not_declared"
    assert exc_info.value.usage_metadata is not None
    assert exc_info.value.local_job_id == LOCAL_JOB_ID


async def test_a_chatgpt_conversation_keeps_one_cache_session_per_job(
    boundary: Callable[..., SendBoundary],
) -> None:
    """The backend keeps a session's cache together only by its session-id."""

    recorder = boundary()

    def job(job_id: str) -> LlmRequest:
        return llm_request().model_copy(
            update={
                "prompt_cache_key": "owner-key",
                "trace": LlmRequestTrace(local_job_id=job_id),
            }
        )

    for request in (job("job-a"), job("job-a"), job("job-b")):
        await dispatch(CHATGPT_CONNECTION, request=request)
    await dispatch(
        ApiKeyConnection(provider="openai", model=USER_MODEL, api_key=OPENAI_KEY),
        request=job("job-a"),
    )

    sent = [
        (request.headers.get("session-id"), json.loads(request.content))
        for request in recorder.requests
        if request.url.path.endswith("/responses")
    ]
    sessions = [session for session, _ in sent]
    # The key and the header name one conversation; the raw job id stays off.
    assert all(body["prompt_cache_key"] == session for session, body in sent[:3])
    assert sessions[0] == sessions[1] != sessions[2]
    assert "job-a" not in str(sessions[0])
    # Another provider keeps the key it was given and gets no session header.
    assert sent[3] == (None, {**sent[3][1], "prompt_cache_key": "owner-key"})


async def test_an_actions_runs_share_one_session_and_its_children_do_not(
    boundary: Callable[..., SendBoundary],
) -> None:
    """A follow-up run continues the Action's conversation; a child has its own."""

    recorder = boundary()

    async def run(job_id: str, job_type: str) -> str | None:
        request = llm_request().model_copy(
            update={"trace": LlmRequestTrace(local_job_id=job_id)}
        )
        # What the job executor binds for every job it runs.
        trace = {"action_id": "action-1", "extra": {"job_type": job_type}}
        with TraceContextManager(user_id=OWNER_ID, local_job_id=job_id, **trace):
            await dispatch(CHATGPT_CONNECTION, request=request)
        return recorder.requests[-1].headers.get("session-id")

    first = await run("job-1", LOCAL_ACTION_JOB_TYPE)
    follow_up = await run("job-2", LOCAL_ACTION_JOB_TYPE)
    child = await run("job-3", LOCAL_ACTION_SUBAGENT_JOB_TYPE)
    sibling = await run("job-4", LOCAL_ACTION_SUBAGENT_JOB_TYPE)

    assert first == follow_up
    assert len({first, child, sibling}) == 3
    assert "action-1" not in str(first)


async def test_a_credential_never_reaches_the_caller_or_the_log(
    boundary: Callable[..., SendBoundary], caplog: pytest.LogCaptureFixture
) -> None:
    boundary(responder(status_code=401))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(FIREWORKS_CONNECTION)

    exc = exc_info.value
    assert all(FIREWORKS_KEY not in str(value) for value in vars(exc).values())
    assert FIREWORKS_KEY not in repr(exc) and FIREWORKS_KEY not in repr(exc.__cause__)
    assert FIREWORKS_KEY not in caplog.text


async def test_a_provider_failure_crosses_the_source_lifetime_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`source_http_client`'s provider-failure handler re-validates the source and
    restores a broken send trace, so a `ProviderError` has to survive it intact.
    """

    async def reject(**_kwargs: Any) -> LlmProxyResponse:
        raise ProviderError(
            status_code=429,
            code="PROXY_UPSTREAM_RATE_LIMITED",
            message="The model provider is rate-limited.",
            details={"upstream_provider": "anthropic"},
        )

    monkeypatch.setattr(direct, "execute_anthropic_request", reject)

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(ANTHROPIC_CONNECTION)

    assert exc_info.value.error_code == "PROXY_UPSTREAM_RATE_LIMITED"
    assert exc_info.value.upstream_provider == "anthropic"


def stalled_stream(
    failure: type[httpx.TransportError],
) -> Callable[[httpx.Request], httpx.Response]:
    """The backend answers 200, sends its first event, then the stream breaks."""

    first_event = sse_body(responses_payload([text_output("hello")])).split(b"\n\n")[0]

    def respond(_request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield first_event + b"\n\n"
            # httpx gives a read timeout no message, as the reported stall did.
            raise failure("")

        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    return respond


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (httpx.ReadTimeout, "The model provider request timed out."),
        (httpx.RemoteProtocolError, "The model provider is temporarily unavailable."),
        (httpx.ReadError, "The model provider is temporarily unavailable."),
    ],
    ids=["stall", "cut-off", "reset"],
)
async def test_a_broken_stream_is_a_retryable_provider_failure(
    boundary: Callable[..., SendBoundary],
    failure: type[httpx.TransportError],
    message: str,
) -> None:
    boundary(stalled_stream(failure))

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await dispatch(CHATGPT_CONNECTION)

    assert exc_info.value.error_code == "PROXY_UPSTREAM_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert str(exc_info.value) == message
    assert exc_info.value.upstream_provider == "openai_codex"
    assert exc_info.value.local_job_id == LOCAL_JOB_ID


async def test_the_provider_client_bounds_silence_not_length(
    boundary: Callable[..., SendBoundary],
) -> None:
    """httpx applies `read` to every socket read, so a stream that keeps sending
    is never cut, while a provider that never accepts the connection fails fast.
    """

    recorder = boundary()

    await dispatch(CHATGPT_CONNECTION)

    assert recorder.timeouts == [httpx.Timeout(250.0, connect=10.0)]


class _Agent(LLMGenerationMixin):
    """The retry loop every agent's LLM call goes through, on the direct route."""

    LLM_INFERENCE_PROFILE_ID = ACTIVITY_SUMMARY_PROFILE_ID
    DEFAULT_SYSTEM_INSTRUCTION = "sys"

    def __init__(self) -> None:
        self.llm_config = {}
        self.client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=self._send))
        )

    async def _send(self, **_kwargs: object) -> ProxyResponse:
        return await dispatch(CHATGPT_CONNECTION)

    @classmethod
    async def _sleep_llm_retry_backoff(cls, attempt_index: int) -> None:
        return None


async def test_a_stalled_stream_is_sent_again_and_its_answer_adopted(
    boundary: Callable[..., SendBoundary],
) -> None:
    stall = stalled_stream(httpx.ReadTimeout)
    answer = responder()

    def respond(request: httpx.Request) -> httpx.Response:
        first = len(recorder.requests) == 1
        return stall(request) if first else answer(request)

    recorder = boundary(respond)

    text = await _Agent()._generate_llm_response("hi", sink=CountingSink())

    assert text == "hello"
    assert len(recorder.requests) == 2


async def test_a_stream_that_keeps_stalling_fails_with_a_named_cause(
    boundary: Callable[..., SendBoundary],
) -> None:
    recorder = boundary(stalled_stream(httpx.ReadTimeout))

    with pytest.raises(LlmProxyExecutionError) as exc_info:
        await _Agent()._generate_llm_response("hi", sink=CountingSink())

    assert len(recorder.requests) == LLM_TRANSPORT_MAX_ATTEMPTS
    assert str(exc_info.value) == "The model provider request timed out."

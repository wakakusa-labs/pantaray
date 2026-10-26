from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from openai import APIStatusError
from openai.types.responses.response_function_tool_call import (
    ResponseFunctionToolCall,
)
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from PIL import Image
from tests.unit.pantaray_llm.llm_profiles import get_llm_profile

from pantaray_llm.contracts.request import LlmProxyRequest
from pantaray_llm.contracts.tool_use import (
    AnthropicToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
    LlmToolUseRequest,
    OpenAiToolContinuation,
)
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import (
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_UPSTREAM_FORBIDDEN,
    ProviderError,
    is_retryable_proxy_error_code,
)
from pantaray_llm.profiles import (
    ACTION_EXECUTING_PROFILE_ID,
    ACTIVITY_SUMMARY_PROFILE_ID,
    INSIGHT_PROFILE_ID,
    MEMORY_UPDATE_PROFILE_ID,
    SUGGESTION_PROFILE_ID,
)
from pantaray_llm.profiles.direct import resolve_direct_profile
from pantaray_llm.providers import media_projection
from pantaray_llm.providers.media_projection import media_reference_text
from pantaray_llm.providers.openai_responses.input_policy import (
    OPENAI_MAX_INPUT_TOKENS,
)
from pantaray_llm.providers.openai_responses.provider import execute_openai_request
from pantaray_llm.providers.openai_responses.request_limits import (
    OPENAI_MAX_REQUEST_BYTES,
)
from pantaray_llm.providers.openai_responses.settings import OpenAiLlmProfile
from pantaray_llm.providers.openai_responses.transport import (
    openai_api_transport,
)

_TRANSPORT = openai_api_transport(api_key="test-key")


class _FakeInputTokens:
    def __init__(
        self,
        input_tokens: int = OPENAI_MAX_INPUT_TOKENS,
        error: Exception | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def count(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(input_tokens=self.input_tokens)


class _FakeResponses:
    def __init__(
        self,
        *,
        output_text: str = '{"decision":{"tool_id":"completed","reason":"done","args":{}}}',
        response_status: str = "completed",
        incomplete_reason: str = "max_output_tokens",
        response_error: object | None = None,
        output: list[object] | None = None,
    ) -> None:
        self.input_tokens = _FakeInputTokens()
        self.kwargs: dict[str, object] | None = None
        self.output_text = output_text
        self.response_status = response_status
        self.incomplete_reason = incomplete_reason
        self.response_error = response_error
        self.output = [] if output is None else output

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return SimpleNamespace(
            id="resp_openai_1",
            model="gpt-6-luna-2026-09-22",
            status=self.response_status,
            incomplete_details=(
                SimpleNamespace(reason=self.incomplete_reason)
                if self.response_status == "incomplete"
                else None
            ),
            error=self.response_error,
            output_text=self.output_text,
            output=self.output,
            usage=SimpleNamespace(
                input_tokens=120,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=20,
                    cache_write_tokens=30,
                ),
                output_tokens=12,
                output_tokens_details=SimpleNamespace(reasoning_tokens=7),
                total_tokens=132,
            ),
        )


class _FakeOpenAiClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


class _FakeToolResponses:
    def __init__(
        self,
        *,
        arguments: str = '{"reason":"done"}',
        response_status: str = "completed",
        call_status: str = "completed",
        call_count: int = 1,
        tool_name: str = "completed",
        call_names: tuple[str, ...] | None = None,
        encrypted_reasoning: str | None = None,
        response_error: object | None = None,
        output_text: str = "",
    ) -> None:
        self.input_tokens = _FakeInputTokens()
        self.calls: list[dict[str, object]] = []
        self.arguments = arguments
        self.response_status = response_status
        self.call_status = call_status
        self.call_count = call_count
        self.tool_name = tool_name
        self.call_names = call_names
        self.encrypted_reasoning = encrypted_reasoning
        self.response_error = response_error
        self.output_text = output_text

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        call_number = len(self.calls)
        emitted_names = (
            list(self.call_names)
            if self.call_names is not None
            else [self.tool_name] * self.call_count
        )
        return SimpleNamespace(
            id=f"resp_tool_{call_number}",
            model="gpt-6-luna-2026-09-22",
            status=self.response_status,
            error=self.response_error,
            incomplete_details=(
                SimpleNamespace(reason="max_output_tokens")
                if self.response_status == "incomplete"
                else None
            ),
            output_text=self.output_text,
            output=[
                *(
                    [
                        ResponseReasoningItem(
                            id=f"reasoning_{call_number}",
                            summary=[],
                            type="reasoning",
                            encrypted_content=self.encrypted_reasoning,
                            status="completed",
                        )
                    ]
                    if self.encrypted_reasoning is not None
                    else []
                ),
                *[
                    ResponseFunctionToolCall(
                        type="function_call",
                        call_id=(
                            f"call_{call_number}"
                            if len(emitted_names) == 1
                            else f"call_{call_number}_{index + 1}"
                        ),
                        name=name,
                        arguments=self.arguments,
                        status=self.call_status,
                    )
                    for index, name in enumerate(emitted_names)
                ],
            ],
            usage=SimpleNamespace(
                input_tokens=10,
                input_tokens_details=None,
                output_tokens=2,
                output_tokens_details=None,
                total_tokens=12,
            ),
        )


def _tool_definition(name: str) -> LlmToolDefinition:
    return LlmToolDefinition(
        name=name,
        description="Finish the task.",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def _tool_request(
    *,
    continuation=None,
    tool_result: LlmToolResult | None = None,
    inference_profile: str = MEMORY_UPDATE_PROFILE_ID,
    continuation_mode: Literal["disabled", "stateless"] = "stateless",
    max_parallel_tool_calls: int = 1,
    extra_tool_names: tuple[str, ...] = (),
) -> LlmProxyRequest:
    request = _request().model_dump(mode="json")
    request["inference_profile"] = inference_profile
    request.pop("response_format")
    request["tool_use"] = LlmToolUseRequest(
        tools=[
            _tool_definition("completed"),
            *[_tool_definition(name) for name in extra_tool_names],
        ],
        continuation_mode=continuation_mode,
        continuation=continuation,
        tool_result=tool_result,
        max_parallel_tool_calls=max_parallel_tool_calls,
    ).model_dump(mode="json")
    return LlmProxyRequest.model_validate(request)


def _tool_request_with_media(*, image_payload: bytes) -> LlmProxyRequest:
    request = _tool_request(inference_profile=ACTION_EXECUTING_PROFILE_ID).model_dump(
        mode="json"
    )
    request["messages"][1]["content"] = [
        {"type": "input_text", "text": "inspect attachments"},
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-1",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        },
    ]
    return LlmProxyRequest.model_validate(request)


def _uploaded_media(*, image_payload: bytes) -> dict[str, UploadedBlob]:
    return {
        "image-1": UploadedBlob(
            field_name="image-1",
            payload=image_payload,
            mime_type="image/png",
        ),
    }


def _noisy_png_bytes() -> bytes:
    image = Image.new("RGB", (140, 100))
    pixels = image.load()
    for y_pos in range(image.height):
        for x_pos in range(image.width):
            color = hashlib.sha256(f"{x_pos}:{y_pos}".encode()).digest()
            pixels[x_pos, y_pos] = (color[0], color[1], color[2])
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


class _StatusErrorResponses:
    def __init__(self, status_code: int) -> None:
        self.input_tokens = _FakeInputTokens()
        self._status_code = status_code

    async def create(self, **_kwargs: object) -> object:
        response = httpx.Response(
            status_code=self._status_code,
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        )
        raise APIStatusError(
            "provider error",
            response=response,
            body={"error": {"message": "provider error"}},
        )


def _request() -> LlmProxyRequest:
    return LlmProxyRequest.model_validate(
        {
            "inference_profile": MEMORY_UPDATE_PROFILE_ID,
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": "system"}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "prompt"}],
                },
            ],
            "metadata": {
                "request_kind": "llm_inference",
                "user_id": "user-1",
                "local_job_id": "job-1",
                "session_version": "1",
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision"],
                    "properties": {
                        "decision": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "tool_id": {"type": "string"},
                                "reason": {"type": "string", "minLength": 1},
                                "args": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            },
                        }
                    },
                },
            },
        }
    )


def _plain_text_request() -> LlmProxyRequest:
    request = _request().model_dump(mode="json")
    request["inference_profile"] = MEMORY_UPDATE_PROFILE_ID
    request.pop("response_format")
    return LlmProxyRequest.model_validate(request)


@pytest.mark.asyncio
async def test_openai_provider_uses_responses_with_profile_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert fake_client.responses.kwargs is not None
    assert fake_client.responses.kwargs["model"] == "gpt-6-luna"
    assert fake_client.responses.kwargs["reasoning"] == {"effort": "high"}
    assert fake_client.responses.kwargs["store"] is False
    assert fake_client.responses.kwargs["instructions"] == "system"
    assert fake_client.responses.kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "prompt"}]}
    ]
    text_config = fake_client.responses.kwargs["text"]
    assert isinstance(text_config, dict)
    response_format = text_config["format"]
    assert response_format["strict"] is True
    decision_schema = response_format["schema"]["properties"]["decision"]
    assert decision_schema["required"] == ["tool_id", "reason", "args"]
    assert decision_schema["properties"]["reason"]["anyOf"][0]["minLength"] == 1
    assert response.meta.upstream_provider == "openai"
    assert response.model == profile.model
    assert response.meta.resolved_model == "gpt-6-luna-2026-09-22"
    assert response.usage is not None
    assert response.usage.cached_prompt_tokens == 20
    assert response.usage.cache_write_prompt_tokens == 30
    assert response.usage.reasoning_tokens == 7


@pytest.mark.asyncio
async def test_openai_provider_forwards_the_prompt_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼び出し元が固定したキャッシュキーをそのまま provider へ渡す（決定事項 8）。"""

    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    request = LlmProxyRequest.model_validate(
        _request().model_dump(mode="json") | {"prompt_cache_key": "action-42"}
    )

    await execute_openai_request(
        transport=_TRANSPORT,
        request=request.to_request(),
        profile=get_llm_profile(MEMORY_UPDATE_PROFILE_ID),
        uploaded_blobs={},
    )

    assert fake_client.responses.kwargs is not None
    assert fake_client.responses.kwargs["prompt_cache_key"] == "action-42"


@pytest.mark.asyncio
async def test_openai_provider_omits_an_unset_prompt_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )

    await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(MEMORY_UPDATE_PROFILE_ID),
        uploaded_blobs={},
    )

    assert fake_client.responses.kwargs is not None
    assert "prompt_cache_key" not in fake_client.responses.kwargs


@pytest.mark.asyncio
async def test_openai_plain_text_incomplete_response_is_non_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(
        output_text="partial profile brief",
        response_status="incomplete",
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_plain_text_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_LLM_TOOL_CALL_INVALID"
    assert is_retryable_proxy_error_code(exc_info.value.code) is False
    assert exc_info.value.details is not None
    assert exc_info.value.details["response_status"] == ("incomplete:max_output_tokens")
    assert exc_info.value.details["upstream_request_id"] == "resp_openai_1"
    assert exc_info.value.details["total_tokens"] == 132


@pytest.mark.asyncio
async def test_openai_plain_text_failed_response_uses_provider_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(
        output_text="",
        response_status="failed",
        response_error=SimpleNamespace(code="server_error", message="provider failed"),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_plain_text_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_UPSTREAM_INTERNAL_ERROR"
    assert exc_info.value.details is not None
    assert exc_info.value.details["upstream_code"] == "server_error"
    assert exc_info.value.details["response_status"] == "failed"


@pytest.mark.asyncio
async def test_openai_unknown_failed_response_preserves_billable_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(
        output_text="",
        response_status="failed",
        response_error=SimpleNamespace(code="new_provider_error"),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_plain_text_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == PROXY_INVALID_INPUT
    assert exc_info.value.details is not None
    assert exc_info.value.details["upstream_request_id"] == "resp_openai_1"
    assert exc_info.value.details["upstream_code"] == "new_provider_error"
    assert exc_info.value.details["prompt_tokens"] == 120
    assert exc_info.value.details["cached_prompt_tokens"] == 20
    assert exc_info.value.details["cache_write_prompt_tokens"] == 30
    assert exc_info.value.details["completion_tokens"] == 12


@pytest.mark.asyncio
async def test_openai_plain_text_completed_response_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses(output_text="complete profile brief")
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_plain_text_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert response.meta.outcome == "complete"
    assert response.output[0].content[0].text == "complete profile brief"


@pytest.mark.asyncio
@pytest.mark.parametrize("output_contract", ("plain", "structured", "function"))
@pytest.mark.parametrize(
    ("response_kind", "response_status", "expected_status", "expected_code"),
    (
        ("refusal", "completed", 403, PROXY_UPSTREAM_FORBIDDEN),
        ("empty", "completed", 502, PROXY_INVALID_UPSTREAM_RESPONSE),
        ("content_filter", "incomplete", 403, PROXY_UPSTREAM_FORBIDDEN),
    ),
)
async def test_openai_response_failure_for_all_output_contracts(
    monkeypatch: pytest.MonkeyPatch,
    output_contract: str,
    response_kind: str,
    response_status: str,
    expected_status: int,
    expected_code: str,
) -> None:
    private_text = "private policy refusal details"
    responses = _FakeResponses(
        output_text=private_text if response_kind == "content_filter" else "",
        response_status=response_status,
        incomplete_reason="content_filter",
        output=(
            [
                ResponseOutputMessage(
                    id="msg_refusal",
                    content=[
                        ResponseOutputRefusal(type="refusal", refusal=private_text)
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ]
            if response_kind == "refusal"
            else []
        ),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    request = {
        "plain": _plain_text_request,
        "structured": _request,
        "function": _tool_request,
    }[output_contract]()
    profile = get_llm_profile(request.inference_profile)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=request.to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    error = exc_info.value
    assert error.status_code == expected_status
    assert error.code == expected_code
    assert error.details is not None
    if response_kind in {"refusal", "content_filter"}:
        assert private_text not in error.message
        assert private_text not in json.dumps(error.details)
    assert error.details["response_status"] == (
        "completed" if response_kind == "empty" else "refusal"
    )
    assert error.details["upstream_request_id"] == "resp_openai_1"
    assert error.details["total_tokens"] == 132


@pytest.mark.asyncio
@pytest.mark.parametrize("output_text", ["{", '{"unexpected":true}'])
async def test_openai_structured_decode_error_preserves_response_context(
    monkeypatch: pytest.MonkeyPatch,
    output_text: str,
) -> None:
    responses = _FakeResponses(output_text=output_text)
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert exc_info.value.details == {
        "local_job_id": "job-1",
        "upstream_provider": "openai",
        "profile_id": MEMORY_UPDATE_PROFILE_ID,
        "upstream_request_id": "resp_openai_1",
        "prompt_tokens": 120,
        "cached_prompt_tokens": 20,
        "cache_write_prompt_tokens": 30,
        "completion_tokens": 12,
        "total_tokens": 132,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_id",
    (
        SUGGESTION_PROFILE_ID,
        ACTIVITY_SUMMARY_PROFILE_ID,
        INSIGHT_PROFILE_ID,
    ),
)
async def test_migrated_openai_profiles_preserve_image_input(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    image_payload = _noisy_png_bytes()
    request_payload = _request().model_dump(mode="json")
    request_payload["inference_profile"] = profile_id
    request_payload["messages"][1]["content"] = [
        {"type": "input_text", "text": "inspect image"},
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-1",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        },
    ]
    profile = get_llm_profile(profile_id)
    assert isinstance(profile, OpenAiLlmProfile)

    await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(request_payload).to_request(),
        profile=profile,
        uploaded_blobs={
            "image-1": UploadedBlob(
                field_name="image-1",
                payload=image_payload,
                mime_type="image/png",
            )
        },
    )

    assert fake_client.responses.kwargs is not None
    content = fake_client.responses.kwargs["input"][0]["content"]
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "high"


@pytest.mark.asyncio
async def test_activity_summary_does_not_enable_provider_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = ACTIVITY_SUMMARY_PROFILE_ID
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    request_payload = _plain_text_request().model_dump(mode="json")
    request_payload["inference_profile"] = profile_id
    request_payload["messages"][1]["content"] = [
        {"type": "input_text", "text": "Read https://example.com/latest"}
    ]
    profile = get_llm_profile(profile_id)
    assert isinstance(profile, OpenAiLlmProfile)

    await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(request_payload).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert fake_client.responses.kwargs is not None
    assert "tools" not in fake_client.responses.kwargs
    assert "tool_choice" not in fake_client.responses.kwargs
    assert "tools" not in fake_client.responses.input_tokens.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("input_tokens", "should_create"),
    (
        (OPENAI_MAX_INPUT_TOKENS, True),
        (OPENAI_MAX_INPUT_TOKENS + 1, False),
    ),
)
async def test_openai_input_token_gate_enforces_exact_boundary_before_create(
    monkeypatch: pytest.MonkeyPatch,
    input_tokens: int,
    should_create: bool,
) -> None:
    responses = _FakeResponses()
    responses.input_tokens = _FakeInputTokens(input_tokens)
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    if should_create:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )
    else:
        with pytest.raises(ProviderError) as exc_info:
            await execute_openai_request(
                transport=_TRANSPORT,
                request=_request().to_request(),
                profile=profile,
                uploaded_blobs={},
            )
        assert exc_info.value.code == PROXY_INVALID_INPUT
        assert exc_info.value.details == {
            "input_tokens": OPENAI_MAX_INPUT_TOKENS + 1,
            "max_input_tokens": OPENAI_MAX_INPUT_TOKENS,
        }

    assert (responses.kwargs is not None) is should_create
    assert len(responses.input_tokens.calls) == 1
    count_kwargs = responses.input_tokens.calls[0]
    assert set(count_kwargs) == {"input", "instructions", "model", "reasoning", "text"}
    if responses.kwargs is not None:
        assert all(
            count_kwargs[field] == responses.kwargs[field] for field in count_kwargs
        )


@pytest.mark.asyncio
async def test_openai_input_token_count_failure_does_not_fall_back_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeResponses()
    responses.input_tokens = _FakeInputTokens(error=RuntimeError("count failed"))
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_REQUEST_FAILED"
    assert responses.kwargs is None


@pytest.mark.asyncio
async def test_action_profile_sends_image_inputs_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    image_payload = _noisy_png_bytes()
    request_payload = _request().model_dump(mode="json")
    request_payload["inference_profile"] = ACTION_EXECUTING_PROFILE_ID
    request_payload["messages"][1]["content"] = [
        {"type": "input_text", "text": "inspect attachments"},
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-1",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        },
    ]
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(request_payload).to_request(),
        profile=profile,
        uploaded_blobs={
            "image-1": UploadedBlob(
                field_name="image-1",
                payload=image_payload,
                mime_type="image/png",
            ),
        },
    )

    assert fake_client.responses.kwargs is not None
    assert fake_client.responses.kwargs["reasoning"] == {"effort": "high"}
    input_items = fake_client.responses.kwargs["input"]
    assert isinstance(input_items, list)
    content = input_items[0]["content"]
    assert content[0] == {"type": "input_text", "text": "inspect attachments"}
    assert content[1] == {
        "type": "input_image",
        "image_url": (
            "data:image/png;base64," + base64.b64encode(image_payload).decode("ascii")
        ),
        "detail": "high",
    }


@pytest.mark.asyncio
async def test_repeated_openai_media_is_rejected_before_payload_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_payload = b"x" * 1_048_576
    descriptor = {
        "blob_ref": "image-1",
        "mime_type": "image/png",
        "byte_size": len(image_payload),
        "sha256": hashlib.sha256(image_payload).hexdigest(),
    }
    repeat_count = OPENAI_MAX_REQUEST_BYTES // len(image_payload) + 1
    request_payload = _tool_request(
        inference_profile=ACTION_EXECUTING_PROFILE_ID
    ).model_dump(mode="json")
    request_payload["messages"][1]["content"] = [
        {"type": "input_image", "image": descriptor} for _ in range(repeat_count)
    ]

    def reject_payload_construction(**_kwargs: object) -> object:
        raise AssertionError("rejected request must not construct provider payload")

    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider._build_openai_initial_input",
        reject_payload_construction,
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=LlmProxyRequest.model_validate(request_payload).to_request(),
            profile=profile,
            uploaded_blobs={
                "image-1": UploadedBlob(
                    field_name="image-1",
                    payload=image_payload,
                    mime_type="image/png",
                )
            },
        )

    assert exc_info.value.code == PROXY_INVALID_INPUT
    assert exc_info.value.details == {
        "reason": "media_references_too_large",
        "request_size_bytes": len(image_payload) * repeat_count,
        "max_request_size_bytes": OPENAI_MAX_REQUEST_BYTES,
    }


@pytest.mark.asyncio
async def test_openai_provider_preserves_service_unavailable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(responses=_StatusErrorResponses(503))
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: client,
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "PROXY_UPSTREAM_UNAVAILABLE"
    assert exc_info.value.details is not None
    assert exc_info.value.details["upstream_provider"] == "openai"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "expected_code", "expected_retryable"),
    (
        (408, 408, "PROXY_UPSTREAM_UNAVAILABLE", True),
        (409, 409, "PROXY_UPSTREAM_UNAVAILABLE", True),
        (410, 400, PROXY_INVALID_INPUT, False),
        (413, 400, PROXY_INVALID_INPUT, False),
        (422, 400, PROXY_INVALID_INPUT, False),
        (429, 429, "PROXY_UPSTREAM_RATE_LIMITED", True),
    ),
)
async def test_openai_provider_classifies_client_errors(
    monkeypatch: pytest.MonkeyPatch,
    upstream_status: int,
    expected_status: int,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    client = SimpleNamespace(responses=_StatusErrorResponses(upstream_status))
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: client,
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.code == expected_code
    assert is_retryable_proxy_error_code(expected_code) is expected_retryable


@pytest.mark.asyncio
async def test_openai_provider_executes_stateless_native_tool_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            inference_profile=ACTION_EXECUTING_PROFILE_ID
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert first.output == []
    assert first.tool_use is not None
    assert first.tool_use.call.name == "completed"
    assert first.tool_use.call.arguments == {"reason": "done"}
    assert first.tool_use.continuation is not None
    first_kwargs = responses.calls[0]
    assert first_kwargs["reasoning"] == {"effort": "high"}
    assert first_kwargs["tool_choice"] == "required"
    assert first_kwargs["parallel_tool_calls"] is False
    assert first_kwargs["include"] == ["reasoning.encrypted_content"]
    tool = first_kwargs["tools"][0]
    assert tool["strict"] is True
    assert tool["parameters"]["required"] == ["reason"]
    assert tool["parameters"]["properties"]["reason"]["anyOf"][1] == {"type": "null"}

    second = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            continuation=first.tool_use.continuation,
            tool_result=LlmToolResult(
                call_id=first.tool_use.call.call_id,
                name=first.tool_use.call.name,
                output={"ok": True},
            ),
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert second.tool_use is not None
    second_input = responses.calls[1]["input"]
    assert second_input[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok": true}',
    }


@pytest.mark.asyncio
async def test_openai_stateless_continuation_externalizes_and_restores_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses(encrypted_reasoning="encrypted-reasoning")
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )

    assert first.tool_use is not None
    continuation = first.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    serialized_history = json.dumps(continuation.history_items)
    assert "data:image/png;base64" not in serialized_history
    assert len(continuation.media_slots) == 1
    assert continuation.media_slots[0].data_field == "image_url"
    first_content = continuation.history_items[0]["content"]
    assert isinstance(first_content, list)
    assert "image_url" not in first_content[1]
    assert any(
        item.get("encrypted_content") == "encrypted-reasoning"
        for item in continuation.history_items
    )
    decode_calls = 0
    decode = media_projection._decode_source_image

    def track_decode(payload: bytes) -> Image.Image:
        nonlocal decode_calls
        decode_calls += 1
        return decode(payload)

    monkeypatch.setattr(media_projection, "_decode_source_image", track_decode)

    second = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            continuation=continuation,
            tool_result=LlmToolResult(
                call_id=first.tool_use.call.call_id,
                name=first.tool_use.call.name,
                output={"ok": True},
            ),
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
        ).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )

    assert second.tool_use is not None
    second_input = responses.calls[1]["input"]
    assert isinstance(second_input, list)
    restored_content = second_input[0]["content"]
    assert restored_content[1]["image_url"] == (
        "data:image/png;base64," + base64.b64encode(image_payload).decode("ascii")
    )
    assert any(
        item.get("encrypted_content") == "encrypted-reasoning" for item in second_input
    )
    assert second_input[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok": true}',
    }
    assert decode_calls == 0


@pytest.mark.asyncio
async def test_openai_continuation_saves_exact_projected_request_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_llm.providers.openai_responses.request_budget import (  # noqa: PLC0415
        prepare_openai_request,
    )

    async def prepare_with_test_limit(**kwargs: object) -> object:
        return await prepare_openai_request(
            **kwargs,  # type: ignore[arg-type]
            max_request_bytes=4_000,
            min_long_edge_px=24,
        )

    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.prepare_openai_request",
        prepare_with_test_limit,
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )

    assert response.tool_use is not None
    continuation = response.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    projection = continuation.media_slots[0].projection
    assert projection.image_recipe is not None
    assert projection.materialized is not None
    sent_input = responses.calls[0]["input"]
    sent_url = sent_input[0]["content"][1]["image_url"]
    assert sent_url.startswith("data:image/webp;base64,")
    sent_payload = base64.b64decode(sent_url.split(",", 1)[1])
    assert projection.materialized.byte_size == len(sent_payload)
    assert projection.materialized.sha256 == hashlib.sha256(sent_payload).hexdigest()
    saved_content = continuation.history_items[0]["content"]
    assert "image_url" not in saved_content[1]
    decode_calls = 0
    decode = media_projection._decode_source_image

    def track_decode(payload: bytes) -> Image.Image:
        nonlocal decode_calls
        decode_calls += 1
        return decode(payload)

    monkeypatch.setattr(media_projection, "_decode_source_image", track_decode)

    await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            continuation=continuation,
            tool_result=LlmToolResult(
                call_id=response.tool_use.call.call_id,
                name=response.tool_use.call.name,
                output={"ok": True},
            ),
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
        ).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )

    assert decode_calls == 1


@pytest.mark.parametrize("tampered_field", ["sha256", "encoder"])
@pytest.mark.asyncio
async def test_openai_continuation_rejects_projection_integrity_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tampered_field: str,
) -> None:
    from pantaray_llm.providers.openai_responses.request_budget import (  # noqa: PLC0415
        prepare_openai_request,
    )

    async def prepare_with_test_limit(**kwargs: object) -> object:
        return await prepare_openai_request(
            **kwargs,  # type: ignore[arg-type]
            max_request_bytes=4_000,
            min_long_edge_px=24,
        )

    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.prepare_openai_request",
        prepare_with_test_limit,
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )
    assert first.tool_use is not None
    continuation = first.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    slot = continuation.media_slots[0]
    projection = slot.projection
    assert projection.materialized is not None
    assert projection.image_recipe is not None
    if tampered_field == "sha256":
        materialized = projection.materialized.model_copy(update={"sha256": "0" * 64})
        projection = projection.model_copy(update={"materialized": materialized})
    else:
        recipe = projection.image_recipe.model_copy(update={"encoder": "tampered"})
        projection = projection.model_copy(update={"image_recipe": recipe})
    continuation.media_slots[0] = slot.model_copy(update={"projection": projection})

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=continuation,
                tool_result=LlmToolResult(
                    call_id=first.tool_use.call.call_id,
                    name=first.tool_use.call.name,
                    output={"ok": True},
                ),
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
            ).to_request(),
            profile=profile,
            uploaded_blobs=_uploaded_media(image_payload=image_payload),
        )

    error = exc_info.value
    assert error.status_code == 400
    assert error.code == PROXY_INVALID_INPUT
    assert error.details == {"reason": "media_projection_integrity"}
    assert len(responses.calls) == 1


@pytest.mark.asyncio
async def test_openai_reference_projection_survives_following_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_llm.providers.openai_responses.request_budget import (  # noqa: PLC0415
        prepare_openai_request,
    )

    image_payload = _noisy_png_bytes()
    first_payload = _tool_request(
        inference_profile=ACTION_EXECUTING_PROFILE_ID
    ).model_dump(mode="json")
    first_payload["messages"][1]["content"].append(
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-1",
                "application_ref": "tool_attachment:image-1",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        }
    )
    uploaded_blobs = {
        "image-1": UploadedBlob(
            field_name="image-1",
            payload=image_payload,
            mime_type="image/png",
        )
    }
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(first_payload).to_request(),
        profile=profile,
        uploaded_blobs=uploaded_blobs,
    )
    assert first.tool_use is not None

    async def prepare_with_reference_limit(**kwargs: object) -> object:
        return await prepare_openai_request(
            **kwargs,  # type: ignore[arg-type]
            max_request_bytes=10_000,
            min_long_edge_px=640,
        )

    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.prepare_openai_request",
        prepare_with_reference_limit,
    )
    second = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            continuation=first.tool_use.continuation,
            tool_result=LlmToolResult(
                call_id=first.tool_use.call.call_id,
                name=first.tool_use.call.name,
                output={"ok": True},
            ),
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
        ).to_request(),
        profile=profile,
        uploaded_blobs=uploaded_blobs,
    )
    assert second.tool_use is not None
    continuation = second.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    projection = continuation.media_slots[0].projection
    assert projection.state == "reference"
    sent_reference = responses.calls[1]["input"][0]["content"][1]
    assert sent_reference == {
        "type": "input_text",
        "text": media_reference_text(projection),
    }

    third = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            continuation=continuation,
            tool_result=LlmToolResult(
                call_id=second.tool_use.call.call_id,
                name=second.tool_use.call.name,
                output={"ok": True},
            ),
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )
    replayed_reference = responses.calls[2]["input"][0]["content"][1]
    assert replayed_reference == sent_reference
    assert third.tool_use is not None

    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.prepare_openai_request",
        prepare_openai_request,
    )
    fourth_payload = _tool_request(
        continuation=third.tool_use.continuation,
        tool_result=LlmToolResult(
            call_id=third.tool_use.call.call_id,
            name=third.tool_use.call.name,
            output={"ok": True},
        ),
        inference_profile=ACTION_EXECUTING_PROFILE_ID,
    ).model_dump(mode="json")
    fourth_payload["messages"][1]["content"].append(
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-1",
                "application_ref": "tool_attachment:image-1",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        }
    )
    fourth = await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(fourth_payload).to_request(),
        profile=profile,
        uploaded_blobs=uploaded_blobs,
    )
    fourth_input = responses.calls[3]["input"]
    assert fourth_input[0]["content"][1] == sent_reference
    assert fourth_input[-1]["content"][0]["image_url"].startswith(
        "data:image/png;base64,"
    )
    assert fourth.tool_use is not None
    fourth_continuation = fourth.tool_use.continuation
    assert isinstance(fourth_continuation, OpenAiToolContinuation)
    assert [slot.projection.state for slot in fourth_continuation.media_slots] == [
        "reference",
        "inline",
    ]


@pytest.mark.asyncio
async def test_openai_continuation_appends_new_current_media_after_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            inference_profile=ACTION_EXECUTING_PROFILE_ID
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )
    assert first.tool_use is not None
    image_payload = _noisy_png_bytes()
    second_payload = _tool_request(
        continuation=first.tool_use.continuation,
        tool_result=LlmToolResult(
            call_id=first.tool_use.call.call_id,
            name=first.tool_use.call.name,
            output={"ok": True},
        ),
        inference_profile=ACTION_EXECUTING_PROFILE_ID,
    ).model_dump(mode="json")
    second_payload["messages"][1]["content"].append(
        {
            "type": "input_image",
            "image": {
                "blob_ref": "image-2",
                "mime_type": "image/png",
                "byte_size": len(image_payload),
                "sha256": hashlib.sha256(image_payload).hexdigest(),
            },
        }
    )

    second = await execute_openai_request(
        transport=_TRANSPORT,
        request=LlmProxyRequest.model_validate(second_payload).to_request(),
        profile=profile,
        uploaded_blobs={
            "image-2": UploadedBlob(
                field_name="image-2",
                payload=image_payload,
                mime_type="image/png",
            )
        },
    )

    second_input = responses.calls[1]["input"]
    assert second_input[-2]["type"] == "function_call_output"
    assert second_input[-1]["role"] == "user"
    assert second_input[-1]["content"][0]["image_url"].startswith(
        "data:image/png;base64,"
    )
    assert second.tool_use is not None
    continuation = second.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    assert continuation.media_slots[0].projection.source.blob_ref == "image-2"
    assert continuation.media_slots[0].item_index == len(second_input) - 1


@pytest.mark.asyncio
async def test_openai_stateless_continuation_rejects_missing_media_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )
    assert first.tool_use is not None

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=first.tool_use.continuation,
                tool_result=LlmToolResult(
                    call_id=first.tool_use.call.call_id,
                    name=first.tool_use.call.name,
                    output={"ok": True},
                ),
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
            ).to_request(),
            profile=profile,
            uploaded_blobs={},
        )
    assert exc_info.value.message == "Missing uploaded blob: image-1."


@pytest.mark.asyncio
async def test_openai_continuation_rejects_provider_file_id_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )
    assert first.tool_use is not None
    continuation = first.tool_use.continuation
    assert isinstance(continuation, OpenAiToolContinuation)
    content = continuation.history_items[0]["content"]
    assert isinstance(content, list)
    content[1]["file_id"] = "file_provider_bypass"

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=continuation,
                tool_result=LlmToolResult(
                    call_id=first.tool_use.call.call_id,
                    name=first.tool_use.call.name,
                    output={"ok": True},
                ),
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
            ).to_request(),
            profile=profile,
            uploaded_blobs=_uploaded_media(image_payload=image_payload),
        )

    assert "provider media outside" in exc_info.value.message
    assert len(responses.calls) == 1
    assert len(responses.calls) == 1


@pytest.mark.asyncio
async def test_openai_stateless_continuation_rejects_media_descriptor_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_payload = _noisy_png_bytes()
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request_with_media(image_payload=image_payload).to_request(),
        profile=profile,
        uploaded_blobs=_uploaded_media(image_payload=image_payload),
    )
    assert first.tool_use is not None
    mismatched_uploads = _uploaded_media(image_payload=b"\0" + image_payload[1:])

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=first.tool_use.continuation,
                tool_result=LlmToolResult(
                    call_id=first.tool_use.call.call_id,
                    name=first.tool_use.call.name,
                    output={"ok": True},
                ),
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
            ).to_request(),
            profile=profile,
            uploaded_blobs=mismatched_uploads,
        )
    assert exc_info.value.message == "Uploaded blob sha256 mismatch: image-1."
    assert len(responses.calls) == 1


@pytest.mark.asyncio
async def test_openai_stateless_continuation_rejects_unreferenced_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )
    assert first.tool_use is not None

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=first.tool_use.continuation,
                tool_result=LlmToolResult(
                    call_id=first.tool_use.call.call_id,
                    name=first.tool_use.call.name,
                    output={"ok": True},
                ),
            ).to_request(),
            profile=profile,
            uploaded_blobs={
                "extra": UploadedBlob(
                    field_name="extra",
                    payload=b"extra",
                    mime_type="image/png",
                )
            },
        )
    assert exc_info.value.message == "Unexpected uploaded blobs: extra."
    assert len(responses.calls) == 1


@pytest.mark.asyncio
async def test_openai_provider_rejects_tool_result_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)
    first = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )
    assert first.tool_use is not None
    request = _tool_request(
        continuation=first.tool_use.continuation,
        tool_result=LlmToolResult(
            call_id=first.tool_use.call.call_id,
            name=first.tool_use.call.name,
            output={"ok": True},
        ),
    )
    assert request.tool_use is not None
    request.tool_use.tools.append(
        LlmToolDefinition(
            name="other_tool",
            description="A different declared tool.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
    )
    request.tool_use.tool_result = LlmToolResult(
        call_id=first.tool_use.call.call_id,
        name="other_tool",
        output={"ok": True},
    )

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=request.to_request(),
            profile=profile,
            uploaded_blobs={},
        )
    assert "name does not match" in exc_info.value.message


@pytest.mark.asyncio
async def test_openai_provider_restores_optional_null_to_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses(arguments='{"reason":null}')
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert response.tool_use is not None
    assert response.tool_use.call.arguments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "expected_reason", "expected_message"),
    [
        (
            _FakeToolResponses(call_count=0, output_text="unexpected text"),
            "missing_call",
            "returned no tool call",
        ),
        (
            _FakeToolResponses(call_count=2),
            "multiple_calls",
            "returned 2 tool calls",
        ),
        (
            _FakeToolResponses(response_status="incomplete"),
            "response_incomplete",
            "incomplete:max_output_tokens",
        ),
        (
            _FakeToolResponses(call_status="incomplete"),
            "call_incomplete",
            "incomplete tool call",
        ),
        (
            _FakeToolResponses(arguments="not-json"),
            "invalid_arguments",
            "not valid JSON",
        ),
        (
            _FakeToolResponses(arguments="[]"),
            "arguments_not_object",
            "not an object",
        ),
        (
            _FakeToolResponses(arguments='{"reason":1}'),
            "arguments_schema_mismatch",
            "The completed call has arguments that do not match the declared "
            "schema at reason (type).",
        ),
        (
            _FakeToolResponses(arguments='{"note":"x"}'),
            "arguments_schema_mismatch",
            "The completed call has unexpected argument(s): note.",
        ),
        (
            _FakeToolResponses(tool_name="undeclared"),
            "undeclared_tool",
            "called an undeclared tool",
        ),
    ],
)
async def test_openai_provider_classifies_invalid_native_tool_response(
    monkeypatch: pytest.MonkeyPatch,
    responses: _FakeToolResponses,
    expected_reason: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request().to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert response.meta.outcome == "model_output_rejected"
    assert response.model == profile.model
    assert response.meta.resolved_model == "gpt-6-luna-2026-09-22"
    assert response.model_error is not None
    assert response.model_error.code == "PROXY_LLM_TOOL_CALL_INVALID"
    assert expected_message in response.model_error.message
    assert response.model_error.violation_reason == expected_reason
    assert response.model_error.recovery == "repair_next_turn"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 2
    assert len(responses.calls) == 1


@pytest.mark.asyncio
async def test_openai_provider_rejects_failed_provider_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses(response_status="failed")
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_INVALID_UPSTREAM_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_code", "expected_code", "expected_retryable", "expected_status"),
    (
        (
            "image_content_policy_violation",
            "PROXY_UPSTREAM_FORBIDDEN",
            False,
            "refusal",
        ),
        ("server_error", "PROXY_UPSTREAM_INTERNAL_ERROR", True, "failed"),
    ),
)
async def test_openai_provider_classifies_failed_native_tool_envelope(
    monkeypatch: pytest.MonkeyPatch,
    upstream_code: str,
    expected_code: str,
    expected_retryable: bool,
    expected_status: str,
) -> None:
    responses = _FakeToolResponses(
        response_status="failed",
        response_error=SimpleNamespace(code=upstream_code),
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request().to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == expected_code
    assert is_retryable_proxy_error_code(exc_info.value.code) is expected_retryable
    assert exc_info.value.details is not None
    assert exc_info.value.details["upstream_code"] == upstream_code
    assert exc_info.value.details["response_status"] == expected_status


@pytest.mark.asyncio
async def test_openai_provider_returns_batch_of_parallel_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses(call_names=("completed", "inspect"))
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
            continuation_mode="disabled",
            max_parallel_tool_calls=3,
            extra_tool_names=("inspect",),
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert responses.calls[0]["parallel_tool_calls"] is True
    assert response.tool_use is not None
    assert [call.name for call in response.tool_use.calls] == ["completed", "inspect"]
    assert [call.call_id for call in response.tool_use.calls] == [
        "call_1_1",
        "call_1_2",
    ]
    assert response.tool_use.dropped_call_names == []
    assert response.tool_use.call is response.tool_use.calls[0]
    assert response.tool_use.continuation is None


@pytest.mark.asyncio
async def test_openai_provider_truncates_tool_calls_beyond_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses(
        call_names=("completed", "inspect", "completed", "inspect")
    )
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    response = await execute_openai_request(
        transport=_TRANSPORT,
        request=_tool_request(
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
            continuation_mode="disabled",
            max_parallel_tool_calls=2,
            extra_tool_names=("inspect",),
        ).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert response.tool_use is not None
    assert [call.name for call in response.tool_use.calls] == ["completed", "inspect"]
    assert response.tool_use.dropped_call_names == ["completed", "inspect"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continuation_mode", "always_include", "expected"),
    [
        ("stateless", False, ["reasoning.encrypted_content"]),
        ("disabled", False, None),
        # The ChatGPT connection sets always_include_encrypted_reasoning because
        # its backend requires the include on every request it serves.
        ("disabled", True, ["reasoning.encrypted_content"]),
    ],
    ids=["stateless", "disabled", "disabled_on_chatgpt"],
)
async def test_encrypted_reasoning_is_requested_only_where_something_reads_it(
    monkeypatch: pytest.MonkeyPatch,
    continuation_mode: Literal["disabled", "stateless"],
    always_include: bool,
    expected: list[str] | None,
) -> None:
    """A disabled turn replays no output item, so the encrypted reasoning those
    items would carry has no reader and is not worth receiving."""

    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    await execute_openai_request(
        transport=replace(
            _TRANSPORT, always_include_encrypted_reasoning=always_include
        ),
        request=_tool_request(continuation_mode=continuation_mode).to_request(),
        profile=profile,
        uploaded_blobs={},
    )

    assert responses.calls[0].get("include") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "none", "high"])
async def test_optional_reasoning_is_distinct_from_explicit_none(
    monkeypatch: pytest.MonkeyPatch, effort: Literal["none", "high"] | None
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=replace(
            get_llm_profile(MEMORY_UPDATE_PROFILE_ID), reasoning_effort=effort
        ),
        uploaded_blobs={},
    )
    body = fake_client.responses.kwargs
    assert body is not None
    if effort is None:
        assert "reasoning" not in body
    else:
        assert body["reasoning"] == {"effort": effort}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model", "output_limit", "reasoning"),
    [
        ("openai", "custom-model", 8192, None),
        ("openai", "gpt-6-luna", 65536, {"effort": "high"}),
        ("openai_codex", "gpt-6-luna", None, {"effort": "high"}),
        ("fireworks", "accounts/fireworks/models/gpt-oss-120b", 8192, None),
    ],
)
async def test_direct_model_settings_reach_the_responses_wire(
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "openai_codex", "fireworks"],
    model: str,
    output_limit: int | None,
    reasoning: dict[str, str] | None,
) -> None:
    fake_client = _FakeOpenAiClient()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: fake_client,
    )
    request = _request().to_request()
    profile = resolve_direct_profile(provider=provider, model=model, request=request)
    assert isinstance(profile, OpenAiLlmProfile)
    await execute_openai_request(
        transport=replace(_TRANSPORT, provider=provider),
        request=request,
        profile=profile,
        uploaded_blobs={},
    )
    body = fake_client.responses.kwargs
    assert body is not None
    assert body["model"] == model
    if output_limit is None:
        assert "max_output_tokens" not in body
    else:
        assert body["max_output_tokens"] == output_limit
    if reasoning is None:
        assert "reasoning" not in body
    else:
        assert body["reasoning"] == reasoning


@pytest.mark.asyncio
async def test_openai_rejects_a_continuation_from_another_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _FakeToolResponses()
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )
    profile = get_llm_profile(MEMORY_UPDATE_PROFILE_ID)
    assert isinstance(profile, OpenAiLlmProfile)

    with pytest.raises(ProviderError) as exc_info:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_tool_request(
                continuation=AnthropicToolContinuation(
                    provider="anthropic",
                    messages=[{"role": "user", "content": [{"type": "text"}]}],
                ),
                tool_result=LlmToolResult(
                    call_id="call_1", name="completed", output={"ok": True}
                ),
            ).to_request(),
            profile=profile,
            uploaded_blobs={},
        )

    assert exc_info.value.code == "PROXY_CONTINUATION_PROVIDER_MISMATCH"
    assert responses.calls == []

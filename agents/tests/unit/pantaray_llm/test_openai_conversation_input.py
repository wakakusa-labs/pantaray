"""The Action conversation an OpenAI request carries, and the turn it gets back."""

from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from tests.unit.pantaray_llm.llm_profiles import get_llm_profile
from tests.unit.pantaray_llm.test_openai_provider import (
    _FakeResponses,
    _noisy_png_bytes,
    _tool_request,
)

from pantaray_llm.contracts.conversation import OpenAiProviderTurn
from pantaray_llm.contracts.media import LlmMediaProjection
from pantaray_llm.contracts.request import LlmProxyRequest, LlmRequest
from pantaray_llm.contracts.uploaded_blob import UploadedBlob
from pantaray_llm.errors import PROXY_INVALID_INPUT, ProviderError
from pantaray_llm.profiles import ACTION_EXECUTING_PROFILE_ID
from pantaray_llm.providers.media_projection import media_reference_text
from pantaray_llm.providers.openai_responses.provider import execute_openai_request
from pantaray_llm.providers.openai_responses.request_budget import (
    prepare_openai_request,
)
from pantaray_llm.providers.openai_responses.transport import openai_api_transport

_TRANSPORT = openai_api_transport(api_key="test-key")
_PROFILE = get_llm_profile(ACTION_EXECUTING_PROFILE_ID)
_ENCRYPTED_REASONING = "reasoning.encrypted_content"
_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"reason": {"type": "string"}},
    "required": ["reason"],
    "additionalProperties": False,
}
_USER_ITEM: dict[str, Any] = {
    "type": "user",
    "content": [{"type": "input_text", "text": "続けて"}],
}

_BASE_REQUEST: dict[str, Any] = {
    "inference_profile": ACTION_EXECUTING_PROFILE_ID,
    "metadata": {
        "request_kind": "llm_inference",
        "user_id": "user-1",
        "local_job_id": "job-1",
        "session_version": "v1",
    },
    "messages": [
        {"role": "system", "content": [{"type": "input_text", "text": "system rules"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "head prompt"}]},
    ],
    "prompt_cache_key": "action-1",
    "tool_use": {
        "mode": "action_turn",
        "max_parallel_tool_calls": 2,
        "tools": [
            {
                "name": "completed",
                "description": "finish",
                "parameters": _TOOL_PARAMETERS,
            }
        ],
    },
}

# Captured from this adapter before the conversation existed: an Action turn that
# carries no conversation must still send exactly this.
_BASELINE_CREATE_KWARGS: dict[str, Any] = {
    "model": "gpt-6-luna",
    "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "head prompt"}]}
    ],
    "store": False,
    "stream": False,
    "max_output_tokens": 65536,
    "reasoning": {"effort": "high"},
    "instructions": "system rules",
    "prompt_cache_key": "action-1",
    "tools": [
        {
            "type": "function",
            "name": "completed",
            "description": "finish",
            "parameters": _TOOL_PARAMETERS,
            "strict": True,
        }
    ],
    "tool_choice": "auto",
    "parallel_tool_calls": True,
}


def _request(conversation: list[dict[str, Any]] | None = None) -> LlmRequest:
    payload = copy.deepcopy(_BASE_REQUEST)
    if conversation is not None:
        payload["tool_use"]["conversation"] = conversation
    return LlmProxyRequest.model_validate(payload).to_request()


def _tool_use_request(conversation: list[dict[str, Any]]) -> LlmRequest:
    """The same conversation, sent by a ReAct loop instead of the Action agent."""

    payload = copy.deepcopy(_BASE_REQUEST)
    payload["tool_use"] = {
        "tools": payload["tool_use"]["tools"],
        "continuation_mode": "disabled",
        "conversation": conversation,
    }
    return LlmProxyRequest.model_validate(payload).to_request()


def _assistant(
    *,
    text: list[str] | None = None,
    calls: list[dict[str, Any]] | None = None,
    provider_turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "assistant",
        "text": text or [],
        "calls": calls or [],
    }
    if provider_turn is not None:
        item["provider_turn"] = provider_turn
    return item


def _call_item(call_id: str = "call_1") -> dict[str, Any]:
    return {"call_id": call_id, "name": "completed", "arguments": {"reason": "調べる"}}


def _reasoning_item(*, encrypted: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "reasoning", "id": "rs_1", "summary": []}
    if encrypted:
        item["encrypted_content"] = "opaque"
    return item


def _wire_call(call_id: str = "call_1", *, identity: str = "fc_1") -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": identity,
        "call_id": call_id,
        "name": "completed",
        "arguments": '{"reason":"調べる"}',
    }


def _tool_result(
    call_id: str = "call_1", *, content: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "tool_result",
        "call_id": call_id,
        "name": "completed",
        "output": {"ok": True},
    }
    if content is not None:
        item["content"] = content
    return item


def _image_block(
    *, byte_size: int, sha256: str, referenceable: bool = False
) -> dict[str, Any]:
    image: dict[str, Any] = {
        "blob_ref": "image-1",
        "mime_type": "image/png",
        "byte_size": byte_size,
        "sha256": sha256,
    }
    if referenceable:
        image["application_ref"] = "tool_attachment:image-1"
    return {"type": "input_image", "image": image}


def _response_call(call_id: str = "call_1") -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="fc_out",
        type="function_call",
        call_id=call_id,
        name="completed",
        arguments='{"reason":"done"}',
        status="completed",
    )


def _response_message() -> ResponseOutputMessage:
    text = ResponseOutputText(type="output_text", text="進めます。", annotations=[])
    return ResponseOutputMessage(
        id="msg_out",
        type="message",
        role="assistant",
        status="completed",
        phase="commentary",
        content=[text],
    )


def _install(monkeypatch: pytest.MonkeyPatch, responses: _FakeResponses) -> None:
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )


async def _send(
    monkeypatch: pytest.MonkeyPatch,
    request: LlmRequest,
    *,
    output: list[object] | None = None,
    uploaded_blobs: dict[str, UploadedBlob] | None = None,
) -> tuple[_FakeResponses, Any]:
    responses = _FakeResponses(
        output_text="", output=output if output is not None else [_response_call()]
    )
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=request,
        profile=_PROFILE,
        uploaded_blobs=uploaded_blobs or {},
    )
    return responses, result


@pytest.mark.asyncio
async def test_absent_conversation_sends_the_pre_conversation_request(
    monkeypatch,
) -> None:
    responses, result = await _send(monkeypatch, _request())

    assert responses.kwargs == _BASELINE_CREATE_KWARGS
    assert result.provider_turn is None


@pytest.mark.asyncio
async def test_conversation_follows_the_head_message_in_order(monkeypatch) -> None:
    responses, _ = await _send(
        monkeypatch,
        _request(
            [
                _assistant(
                    text=["調べます。"],
                    calls=[_call_item()],
                    provider_turn={
                        "provider": "openai",
                        "items": [_reasoning_item(), _wire_call()],
                    },
                ),
                _tool_result(),
                _USER_ITEM,
            ]
        ),
    )

    assert responses.kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "head prompt"}]},
        _reasoning_item(),
        _wire_call(),
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok": true}',
        },
        {"role": "user", "content": [{"type": "input_text", "text": "続けて"}]},
    ]
    assert responses.kwargs["include"] == [_ENCRYPTED_REASONING]


@pytest.mark.asyncio
async def test_a_tool_use_request_sends_its_conversation_the_same_way(
    monkeypatch,
) -> None:
    responses, result = await _send(
        monkeypatch,
        _tool_use_request(
            [
                _assistant(text=["調べます。"], calls=[_call_item()]),
                _tool_result(),
                _USER_ITEM,
            ]
        ),
    )

    assert responses.kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "head prompt"}]},
        {"role": "assistant", "content": "調べます。", "phase": "commentary"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "completed",
            "arguments": '{"reason": "調べる"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"ok": true}',
        },
        {"role": "user", "content": [{"type": "input_text", "text": "続けて"}]},
    ]
    # A tool use still has to call a tool, and a single one at that.
    assert responses.kwargs["tool_choice"] == "required"
    assert responses.kwargs["parallel_tool_calls"] is False
    # It replays no provider turn, so it neither asks for encrypted reasoning
    # nor is handed one it has nowhere to put.
    assert "include" not in responses.kwargs
    assert result.provider_turn is None


@pytest.mark.asyncio
async def test_parallel_calls_keep_every_call_and_its_result(monkeypatch) -> None:
    call_ids = ["call_1", "call_2", "call_3"]
    responses, _ = await _send(
        monkeypatch,
        _request(
            [
                _assistant(
                    calls=[_call_item(call_id) for call_id in call_ids],
                    provider_turn={
                        "provider": "openai",
                        "items": [
                            _reasoning_item(),
                            *(
                                _wire_call(call_id, identity=f"fc_{index}")
                                for index, call_id in enumerate(call_ids)
                            ),
                        ],
                    },
                ),
                *(_tool_result(call_id) for call_id in call_ids),
            ]
        ),
    )

    assert [item.get("type") for item in responses.kwargs["input"][1:]] == [
        "reasoning",
        *["function_call"] * 3,
        *["function_call_output"] * 3,
    ]
    assert [
        item["call_id"] for item in responses.kwargs["input"][2:] if "call_id" in item
    ] == [*call_ids, *call_ids]


@pytest.mark.asyncio
async def test_dropping_unreplayable_reasoning_also_drops_the_call_item_id(
    monkeypatch,
) -> None:
    responses, _ = await _send(
        monkeypatch,
        _request(
            [
                _assistant(
                    calls=[_call_item()],
                    provider_turn={
                        "provider": "openai",
                        "items": [
                            _reasoning_item(encrypted=False),
                            _wire_call(),
                            {"type": "message", "id": "msg_1", "content": []},
                        ],
                    },
                ),
                _tool_result(),
            ]
        ),
    )

    replayed = responses.kwargs["input"][1:3]
    assert replayed[0] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "completed",
        "arguments": '{"reason":"調べる"}',
    }
    # Only the call carries the reasoning pairing, so the message keeps its id.
    assert replayed[1]["id"] == "msg_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_turn",
    [None, {"provider": "anthropic", "blocks": [{"type": "thinking"}]}],
)
async def test_a_turn_this_adapter_cannot_replay_still_sends_the_structure(
    monkeypatch, provider_turn
) -> None:
    responses, _ = await _send(
        monkeypatch,
        _request(
            [
                _assistant(
                    text=["調べます。", "続けます。"],
                    calls=[_call_item()],
                    provider_turn=provider_turn,
                ),
                _tool_result(),
            ]
        ),
    )

    assert responses.kwargs["input"][1:4] == [
        {"role": "assistant", "content": "調べます。", "phase": "commentary"},
        {"role": "assistant", "content": "続けます。", "phase": "commentary"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "completed",
            "arguments": '{"reason": "調べる"}',
        },
    ]


@pytest.mark.asyncio
async def test_a_provider_turn_that_contradicts_its_calls_is_rejected(
    monkeypatch,
) -> None:
    with pytest.raises(ProviderError) as caught:
        await _send(
            monkeypatch,
            _request(
                [
                    _assistant(
                        calls=[_call_item("call_1")],
                        provider_turn={
                            "provider": "openai",
                            "items": [_wire_call("call_other")],
                        },
                    ),
                    _tool_result("call_1"),
                ]
            ),
        )

    assert caught.value.code == PROXY_INVALID_INPUT


@pytest.mark.asyncio
@pytest.mark.parametrize("max_request_bytes", [None, 10_000])
async def test_tool_result_attachments_ride_along_as_the_users_own_message(
    monkeypatch, max_request_bytes
) -> None:
    payload = _noisy_png_bytes()
    block = _image_block(
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        referenceable=True,
    )
    if max_request_bytes is not None:

        async def prepare_within_limit(**kwargs: object) -> object:
            return await prepare_openai_request(
                **kwargs,  # type: ignore[arg-type]
                max_request_bytes=max_request_bytes,
            )

        monkeypatch.setattr(
            "pantaray_llm.providers.openai_responses.provider.prepare_openai_request",
            prepare_within_limit,
        )
    responses, _ = await _send(
        monkeypatch,
        _request([_assistant(calls=[_call_item()]), _tool_result(content=[block])]),
        uploaded_blobs={
            "image-1": UploadedBlob(
                field_name="image-1", payload=payload, mime_type="image/png"
            )
        },
    )

    attachment = responses.kwargs["input"][3]
    assert attachment["role"] == "user"
    if max_request_bytes is None:
        assert attachment["content"][0]["image_url"].startswith(
            "data:image/png;base64,"
        )
        return
    # Under a budget only history media may fall back to a reference; the turn's
    # own media has nowhere to go and would have raised instead.
    assert attachment["content"][0] == {
        "type": "input_text",
        "text": media_reference_text(
            LlmMediaProjection.model_validate(
                {"state": "reference", "source": block["image"]}
            )
        ),
    }


@pytest.mark.asyncio
async def test_oversized_conversation_media_is_rejected_with_the_messages(
    monkeypatch,
) -> None:
    with pytest.raises(ProviderError) as caught:
        await _send(
            monkeypatch,
            _request(
                [
                    _assistant(calls=[_call_item()]),
                    _tool_result(
                        content=[_image_block(byte_size=512_000_001, sha256="0" * 64)]
                    ),
                ]
            ),
        )

    assert caught.value.details["reason"] == "media_references_too_large"


@pytest.mark.asyncio
async def test_the_response_turn_holds_the_items_the_adapter_reported(
    monkeypatch,
) -> None:
    conversation = [_assistant(calls=[_call_item()]), _tool_result()]
    _, result = await _send(
        monkeypatch,
        _request(conversation),
        output=[
            ResponseReasoningItem(
                id="rs_out", summary=[], type="reasoning", encrypted_content="opaque"
            ),
            ResponseReasoningItem(id="rs_plain", summary=[], type="reasoning"),
            _response_message(),
            _response_call("call_a"),
            _response_call("call_b"),
            # Over the batch limit of two, so it is dropped from the turn too.
            _response_call("call_c"),
        ],
    )

    turn = result.provider_turn
    assert isinstance(turn, OpenAiProviderTurn)
    assert [(item["type"], item.get("call_id")) for item in turn.items] == [
        ("reasoning", None),
        ("message", None),
        ("function_call", "call_a"),
        ("function_call", "call_b"),
    ]
    assert turn.items[0]["encrypted_content"] == "opaque"


@pytest.mark.asyncio
async def test_a_final_answer_turn_replays_with_its_own_phase(monkeypatch) -> None:
    # The API asks for each assistant message's phase back as it was emitted.
    final_answer = _response_message().model_copy(update={"phase": "final_answer"})
    _, result = await _send(
        monkeypatch,
        _request([_assistant(calls=[_call_item()]), _tool_result()]),
        output=[final_answer],
    )

    assert [message.text for message in result.tool_use.messages] == ["進めます。"]
    assert [item.get("phase") for item in result.provider_turn.items] == [
        "final_answer"
    ]


@pytest.mark.asyncio
async def test_a_rejected_turn_carries_no_provider_turn(monkeypatch) -> None:
    _, result = await _send(
        monkeypatch,
        _request([_assistant(calls=[_call_item()]), _tool_result()]),
        output=[_response_call().model_copy(update={"arguments": "not-json"})],
    )

    assert result.meta.outcome == "model_output_rejected"
    assert result.provider_turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_request", "expected"),
    [
        (lambda: _request(), False),
        (lambda: _request([_USER_ITEM]), True),
        (
            lambda: _tool_request(
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
                continuation_mode="stateless",
            ).to_request(),
            True,
        ),
        (
            lambda: _tool_request(
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
                continuation_mode="disabled",
            ).to_request(),
            False,
        ),
    ],
)
async def test_encrypted_reasoning_is_included_only_where_it_is_read(
    monkeypatch, build_request, expected
) -> None:
    responses, _ = await _send(monkeypatch, build_request())

    assert ("include" in responses.kwargs) is expected

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from tests.unit.pantaray_llm.llm_profiles import get_llm_profile
from tests.unit.pantaray_llm.test_openai_provider import _FakeResponses, _tool_request

from pantaray_llm.contracts.action_turn import (
    ACTION_COMMENTARY_MAX_CHARACTERS,
    LlmActionTurnResponse,
)
from pantaray_llm.contracts.request import LlmProxyRequest, LlmProxyResponse
from pantaray_llm.errors import ProviderError
from pantaray_llm.profiles import ACTION_EXECUTING_PROFILE_ID
from pantaray_llm.providers.openai_responses.provider import execute_openai_request
from pantaray_llm.providers.openai_responses.transport import (
    openai_api_transport,
)

_TRANSPORT = openai_api_transport(api_key="test-key")


def _message(
    text: str = "まず対象を確認します。", identity: str = "msg-1"
) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=identity,
        type="message",
        role="assistant",
        status="completed",
        phase="commentary",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _call(identity: str = "call-1") -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=identity,
        name="completed",
        arguments='{"reason":"done"}',
        status="completed",
    )


def _request(max_calls: int = 2) -> LlmProxyRequest:
    payload = _tool_request(
        inference_profile=ACTION_EXECUTING_PROFILE_ID,
        continuation_mode="disabled",
        max_parallel_tool_calls=max_calls,
    ).model_dump(mode="json")
    payload["tool_use"] = {
        "mode": "action_turn",
        "tools": payload["tool_use"]["tools"],
        "max_parallel_tool_calls": max_calls,
    }
    return LlmProxyRequest.model_validate(payload)


def _install(monkeypatch: pytest.MonkeyPatch, responses: _FakeResponses) -> None:
    monkeypatch.setattr(
        "pantaray_llm.providers.openai_responses.provider.build_openai_client",
        lambda _transport: SimpleNamespace(responses=responses),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [[_message()], [_call()], [_message(), _call()]])
async def test_action_turn_accepts_text_calls_and_both(monkeypatch, output) -> None:
    responses = _FakeResponses(output_text="途中説明", output=output)
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert isinstance(result.tool_use, LlmActionTurnResponse)
    assert [message.text for message in result.tool_use.messages] == [
        "まず対象を確認します。"
        for item in output
        if isinstance(item, ResponseOutputMessage)
    ]
    assert [call.call_id for call in result.tool_use.calls] == [
        "call-1" for item in output if isinstance(item, ResponseFunctionToolCall)
    ]
    assert result.output == []
    assert result.usage is not None
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (120, 12)
    assert responses.kwargs["tool_choice"] == "auto"
    assert responses.kwargs["parallel_tool_calls"] is True
    assert responses.kwargs["store"] is False
    assert responses.kwargs["stream"] is False
    assert "include" not in responses.kwargs
    assert "continuation" not in result.tool_use.model_dump()
    assert isinstance(
        LlmProxyResponse.model_validate_json(result.model_dump_json()).tool_use,
        LlmActionTurnResponse,
    )


@pytest.mark.asyncio
async def test_action_turn_preserves_message_and_call_order_without_reasoning(
    monkeypatch,
) -> None:
    first = _message("  調べます。\n")
    first.content.append(
        ResponseOutputText(
            type="output_text", text="本文を保持します。", annotations=[]
        )
    )
    responses = _FakeResponses(
        output_text="must not become output",
        output=[
            _call("call-1"),
            first,
            ResponseReasoningItem(
                id="reasoning-1",
                summary=[],
                type="reasoning",
                encrypted_content="private",
            ),
            _call("call-2"),
            _message("続けて確認します。", "msg-2"),
        ],
    )
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    turn = result.tool_use
    assert isinstance(turn, LlmActionTurnResponse)
    assert [
        (message.source_message_id, message.phase, message.text)
        for message in turn.messages
    ] == [
        ("msg-1", "commentary", "  調べます。\n本文を保持します。"),
        ("msg-2", "commentary", "続けて確認します。"),
    ]
    assert [call.call_id for call in turn.calls] == ["call-1", "call-2"]
    assert result.output == []
    assert result.thinking is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        [],
        [ResponseReasoningItem(id="reasoning-1", summary=[], type="reasoning")],
        [_message().model_copy(update={"phase": None}), _call()],
        [_message().model_copy(update={"phase": "final_answer"}), _call()],
        [_message().model_copy(update={"phase": "unknown"}), _call()],
        [_message(" \n\t"), _call()],
        [_message("x" * (ACTION_COMMENTARY_MAX_CHARACTERS + 1)), _call()],
        [_message(identity=""), _call()],
        [_message().model_copy(update={"status": "incomplete"}), _call()],
    ],
)
async def test_action_turn_rejects_invalid_messages_without_usable_output(
    monkeypatch, output
) -> None:
    responses = _FakeResponses(output_text="", output=output)
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert result.meta.outcome == "model_output_rejected"
    assert result.model_error is not None
    assert result.model_error.violation_reason == "invalid_response"
    assert result.model_error.recovery == "repair_next_turn"
    if not output or all(isinstance(item, ResponseReasoningItem) for item in output):
        assert result.model_error.message == (
            "OpenAI returned no commentary or function calls "
            f"(output items: {len(output)}, reasoning items: {len(output)})."
        )
    assert result.tool_use is None
    assert result.output == []
    assert result.usage is not None
    assert result.usage.total_tokens == 132


@pytest.mark.asyncio
async def test_an_answer_in_plain_text_is_refused_as_such(monkeypatch) -> None:
    # The repair has to say what was wrong: the model answered outside a call.
    output = [_message().model_copy(update={"phase": "final_answer"})]
    _install(monkeypatch, _FakeResponses(output_text="", output=output))

    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )

    assert result.model_error is not None
    assert result.model_error.recovery == "repair_next_turn"
    assert "final answer in plain text" in result.model_error.message


@pytest.mark.asyncio
async def test_action_turn_rejects_commentary_together_with_invalid_call(
    monkeypatch,
) -> None:
    responses = _FakeResponses(
        output_text="途中説明",
        output=[
            _message(),
            _call().model_copy(update={"arguments": "not-json"}),
        ],
    )
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert result.model_error.violation_reason == "invalid_arguments"
    assert result.tool_use is None
    assert result.output == []


@pytest.mark.asyncio
async def test_action_turn_keeps_existing_batch_limit(monkeypatch) -> None:
    responses = _FakeResponses(
        output_text="途中説明",
        output=[
            _message(),
            _call("call-1"),
            _call("call-2"),
            _call("call-3"),
        ],
    )
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert [call.call_id for call in result.tool_use.calls] == ["call-1", "call-2"]
    assert result.tool_use.dropped_call_names == ["completed"]
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request(max_calls=1).to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert result.model_error.violation_reason == "multiple_calls"
    assert result.tool_use is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "output", "expected_code"),
    [
        (
            "completed",
            None,
            [
                _message().model_copy(
                    update={
                        "content": [
                            ResponseOutputRefusal(type="refusal", refusal="refused")
                        ]
                    }
                ),
                _call(),
            ],
            "PROXY_UPSTREAM_FORBIDDEN",
        ),
        (
            "incomplete",
            "content_filter",
            [_message(), _call()],
            "PROXY_UPSTREAM_FORBIDDEN",
        ),
        (
            "failed",
            "server_error",
            [_message(), _call()],
            "PROXY_UPSTREAM_INTERNAL_ERROR",
        ),
    ],
)
async def test_action_turn_preserves_provider_failure_classification(
    monkeypatch, status, reason, output, expected_code
) -> None:
    responses = _FakeResponses(
        output_text="途中説明",
        output=output,
        response_status=status,
        incomplete_reason=reason,
        response_error=SimpleNamespace(code=reason),
    )
    _install(monkeypatch, responses)
    with pytest.raises(ProviderError) as caught:
        await execute_openai_request(
            transport=_TRANSPORT,
            request=_request().to_request(),
            profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
            uploaded_blobs={},
        )
    assert caught.value.code == expected_code
    assert caught.value.details["total_tokens"] == 132


@pytest.mark.asyncio
async def test_action_turn_repairs_incomplete_response(monkeypatch) -> None:
    responses = _FakeResponses(
        output_text="途中説明",
        output=[_message(), _call()],
        response_status="incomplete",
    )
    _install(monkeypatch, responses)
    result = await execute_openai_request(
        transport=_TRANSPORT,
        request=_request().to_request(),
        profile=get_llm_profile(ACTION_EXECUTING_PROFILE_ID),
        uploaded_blobs={},
    )
    assert result.model_error.violation_reason == "response_incomplete"
    assert result.tool_use is None
    assert result.output == []

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping

import pytest
from tests.unit.local_runtime.test_llm_proxy_client import (
    _build_client,
    _FakeHttpResponse,
)
from tests.unit.local_runtime.test_llm_proxy_client import (
    cloud_session as cloud_session,
)
from tests.unit.local_runtime.test_llm_proxy_client import (
    cloud_session_db as cloud_session_db,
)

from pantaray_agents.local_runtime.llm_proxy.request_builder import build_llm_request
from pantaray_agents.local_runtime.llm_proxy.types import ProxyResponse
from pantaray_agents.utils.llm_types import GenerateContentConfig
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.contracts.action_turn import LlmActionTurnRequest
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import (
    LlmImageDescriptor,
    LlmInputImageBlock,
    LlmInputTextBlock,
)
from pantaray_llm.contracts.request import LlmProxyRequest
from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolDefinition,
    LlmToolUseRequest,
)
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.profiles import ACTION_EXECUTING_PROFILE_ID


def _request(*, action: bool = True) -> LlmToolUseRequest | LlmActionTurnRequest:
    tools = [
        LlmToolDefinition(
            name="inspect",
            description="Inspect fixture.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
    ]
    if action:
        return LlmActionTurnRequest(mode="action_turn", tools=tools)
    return LlmToolUseRequest(tools=tools, continuation_mode="disabled")


def _turn() -> dict[str, object]:
    return {
        "mode": "action_turn",
        "messages": [
            {
                "phase": "commentary",
                "source_message_id": "msg-1",
                "text": "  対象を確認します。\n",
            }
        ],
        "calls": [],
    }


def _call() -> dict[str, object]:
    return {"call_id": "call-1", "name": "inspect", "arguments": {}}


def _payload(turn: object) -> dict[str, object]:
    # Raw proxy JSON also represents malformed responses in boundary tests.
    return {
        "model": "fixture-model",
        "output": [],
        "tool_use": turn,
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        "meta": {
            "local_job_id": "job-1",
            "upstream_provider": "openai",
            "profile_id": ACTION_EXECUTING_PROFILE_ID,
            "outcome": "complete",
        },
    }


def _install(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> list[Mapping[str, object]]:
    requests: list[Mapping[str, object]] = []

    async def post(**kwargs: object) -> _FakeHttpResponse:
        request = kwargs["request_json"]
        assert isinstance(request, Mapping)
        requests.append(request)
        return _FakeHttpResponse(status_code=200, payload=payload)

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.post_request", post
    )
    return requests


async def _generate(*, action: bool = True) -> ProxyResponse:
    with TraceContextManager(user_id="user-1", local_job_id="job-1"):
        return await _build_client().aio.models.generate_content(
            contents=["Inspect fixture"],
            config=GenerateContentConfig(
                inference_profile=ACTION_EXECUTING_PROFILE_ID,
                tool_use=_request(action=action),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_message", "with_call"), [(True, False), (False, True), (True, True)]
)
async def test_client_receives_action_turn_without_flattening_messages(
    monkeypatch, with_message, with_call
) -> None:
    turn = _turn()
    if not with_message:
        turn["messages"] = []
    if with_call:
        turn["calls"] = [_call()]
    requests = _install(monkeypatch, _payload(turn))
    response = await _generate()
    assert response.action_turn is not None
    assert response.action_turn.model_dump(exclude_defaults=True) == turn
    assert response.text == ""
    assert response.tool_calls == ()
    assert response.tool_continuation is None
    assert response.usage_metadata["total_tokens"] == 15
    assert len(requests) == 1
    # The Action turn envelope is the cloud proxy's wire contract, byte for byte,
    # and router.py parses it with this model before anything else runs.
    LlmProxyRequest.model_validate(requests[0])
    assert json.dumps(requests[0], ensure_ascii=False) == json.dumps(
        {
            "inference_profile": ACTION_EXECUTING_PROFILE_ID,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Inspect fixture"}],
                }
            ],
            "metadata": {
                "request_kind": "llm_inference",
                "user_id": "user-1",
                "local_job_id": "job-1",
                "session_version": "1",
            },
            "tool_use": _request().model_dump(mode="json"),
            "prompt_cache_key": hashlib.sha256(b"user-1").hexdigest(),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action_request", [True, False])
async def test_client_stops_on_native_variant_mismatch(
    monkeypatch, action_request
) -> None:
    turn = {"calls": [_call()], "continuation": None} if action_request else _turn()
    requests = _install(monkeypatch, _payload(turn))
    with pytest.raises(LlmProxyExecutionError) as caught:
        await _generate(action=action_request)
    assert caught.value.recovery == "stop"
    assert caught.value.retryable is False
    assert caught.value.usage_metadata["total_tokens"] == 15
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"messages": [], "calls": []},
        {
            "messages": [
                {"phase": "final_answer", "source_message_id": "msg-1", "text": "final"}
            ]
        },
        {"messages": [{"source_message_id": "msg-1", "text": "missing phase"}]},
        {
            "messages": [
                {"phase": "commentary", "source_message_id": "msg-1", "text": " \n"}
            ]
        },
    ],
)
async def test_client_rejects_invalid_action_contract_and_preserves_usage(
    monkeypatch, changes
) -> None:
    turn = {**_turn(), **changes}
    _install(monkeypatch, _payload(turn))
    with pytest.raises(LlmProxyExecutionError) as caught:
        await _generate()
    assert caught.value.error_code == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert caught.value.usage_metadata["total_tokens"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [True, False])
async def test_client_rejects_overflow_and_generic_action_text(
    monkeypatch, overflow
) -> None:
    turn = _turn()
    payload = _payload(turn)
    if overflow:
        turn["calls"] = [_call(), {**_call(), "call_id": "call-2"}]
    else:
        payload["output"] = [
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "must not be published"}],
            }
        ]
    _install(monkeypatch, payload)
    with pytest.raises(LlmProxyExecutionError) as caught:
        await _generate()
    assert caught.value.recovery == "stop"
    assert caught.value.usage_metadata["total_tokens"] == 15


@pytest.mark.asyncio
async def test_client_preserves_model_repair_for_invalid_action_output(
    monkeypatch,
) -> None:
    payload = _payload(None)
    payload["meta"]["outcome"] = "model_output_rejected"
    payload["model_error"] = {
        "code": "PROXY_LLM_TOOL_CALL_INVALID",
        "message": "Invalid commentary phase.",
        "recovery": "repair_next_turn",
        "violation_reason": "invalid_response",
    }
    requests = _install(monkeypatch, payload)
    with pytest.raises(LlmProxyExecutionError) as caught:
        await _generate()
    assert caught.value.recovery == "repair_next_turn"
    assert caught.value.retryable is False
    assert caught.value.usage_metadata["total_tokens"] == 15
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_client_waits_for_complete_response_before_returning_commentary(
    monkeypatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    requests: list[Mapping[str, object]] = []

    async def post(**kwargs: object) -> _FakeHttpResponse:
        requests.append(kwargs)
        entered.set()
        await release.wait()
        return _FakeHttpResponse(status_code=200, payload=_payload(_turn()))

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.post_request", post
    )
    task = asyncio.create_task(_generate())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result.action_turn.messages[0].text == "  対象を確認します。\n"
        assert len(requests) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_conversation_media_is_uploaded_once_without_repeating_it_in_the_message(
    tmp_path,
) -> None:
    """会話が置いた添付は、要求本体の user 1 通に二重で入らない。"""

    payload = b"screenshot-bytes"
    blob = tmp_path / "shot.png"
    blob.write_bytes(payload)
    descriptor = {
        "blob_ref": "attachment_blob_shot",
        "mime_type": "image/png",
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "application_ref": "tool_attachment:shot",
    }
    conversation = [
        LlmTurnAssistantItem(
            type="assistant",
            calls=[LlmToolCall(call_id="call_a", name="inspect", arguments={})],
        ),
        LlmTurnToolResultItem(
            type="tool_result",
            call_id="call_a",
            name="inspect",
            output={"result": "inspect: ok"},
            content=[
                LlmInputImageBlock(
                    type="input_image",
                    image=LlmImageDescriptor.model_validate(descriptor),
                )
            ],
        ),
        LlmTurnUserItem(
            type="user",
            content=[LlmInputTextBlock(type="input_text", text="tail")],
        ),
    ]
    action_request = _request()
    assert isinstance(action_request, LlmActionTurnRequest)
    built = build_llm_request(
        contents=[
            "Inspect fixture",
            {
                "file_data": {
                    **descriptor,
                    "source_kind": "workspace_file",
                    "workspace_root_path": str(tmp_path),
                    "workspace_relative_path": "shot.png",
                }
            },
        ],
        config=GenerateContentConfig(
            inference_profile=ACTION_EXECUTING_PROFILE_ID,
            tool_use=action_request.model_copy(update={"conversation": conversation}),
        ),
        user_id="user-1",
        local_job_id="job-1",
    )

    assert [block.type for block in built.request.messages[0].content] == ["input_text"]
    # The payload still has to travel: the adapter resolves the item's blob_ref
    # against the multipart uploads, and a missing one fails the request.
    assert [ref for ref, _ in built.multipart_files] == ["attachment_blob_shot"]
    assert built.multipart_files[0][1][1] == payload

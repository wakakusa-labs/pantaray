from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

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

from pantaray_agents.local_runtime.llm_proxy.content_files import FileInputAccessError
from pantaray_agents.local_runtime.llm_proxy.request_builder import (
    BuiltLlmRequest,
    build_llm_request,
)
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


def _legacy_workspace_turn(
    tmp_path: Path, *, conversation: bool
) -> tuple[dict[str, object], LlmActionTurnRequest]:
    """One image history named by its workspace file, as ``read`` once stored it."""

    payload = b"\x89PNG\r\n\x1a\nscreenshot"
    (tmp_path / "shot.png").write_bytes(payload)
    descriptor = {
        "blob_ref": "attachment_blob_shot",
        "mime_type": "image/png",
        "byte_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "application_ref": "tool_attachment:shot",
    }
    action_request = _request()
    assert isinstance(action_request, LlmActionTurnRequest)
    if conversation:
        action_request = action_request.model_copy(
            update={
                "conversation": [
                    LlmTurnAssistantItem(
                        type="assistant",
                        calls=[
                            LlmToolCall(call_id="call_a", name="inspect", arguments={})
                        ],
                    ),
                    LlmTurnToolResultItem(
                        type="tool_result",
                        call_id="call_a",
                        name="inspect",
                        output={"attached_files": ["tool_attachment:shot"]},
                        content=[
                            LlmInputImageBlock(
                                type="input_image",
                                image=LlmImageDescriptor.model_validate(descriptor),
                            )
                        ],
                    ),
                ]
            }
        )
    file_data = {
        **descriptor,
        "source_kind": "workspace_file",
        "workspace_root_path": str(tmp_path),
        "workspace_relative_path": "shot.png",
    }
    return file_data, action_request


def _build(
    file_data: dict[str, object], tool_use: LlmActionTurnRequest
) -> BuiltLlmRequest:
    return build_llm_request(
        contents=["See tool_attachment:shot", {"file_data": file_data}],
        config=GenerateContentConfig(
            inference_profile=ACTION_EXECUTING_PROFILE_ID, tool_use=tool_use
        ),
        user_id="user-1",
        local_job_id="job-1",
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda path: path.write_bytes(b"regenerated"), id="resized"),
        # Same size, other bytes: only the digest tells them apart.
        pytest.param(
            lambda path: path.write_bytes(b"\x89PNG\r\n\x1a\nSCREENSHOT"),
            id="same-size",
        ),
        pytest.param(lambda path: path.unlink(), id="removed"),
        pytest.param(
            lambda path: (path.unlink(), path.mkdir()), id="not-a-regular-file"
        ),
    ],
)
def test_an_image_whose_workspace_file_changed_is_sent_as_a_note(
    tmp_path: Path, change: Callable[[Path], object]
) -> None:
    """History from before ``read`` kept its copy must not fail every later turn."""

    file_data, tool_use = _legacy_workspace_turn(tmp_path, conversation=True)
    change(tmp_path / "shot.png")

    built = _build(file_data, tool_use)

    assert built.multipart_files == []
    assert isinstance(built.request.tool_use, LlmActionTurnRequest)
    conversation = built.request.tool_use.conversation
    assert conversation is not None
    tool_result = conversation[1]
    assert isinstance(tool_result, LlmTurnToolResultItem)
    (note,) = tool_result.content
    assert isinstance(note, LlmInputTextBlock)
    assert "tool_attachment:shot" in note.text
    assert str(tmp_path / "shot.png") in note.text
    assert [block.type for block in built.request.messages[0].content] == ["input_text"]
    # Every later turn sends the same words, so the prefix stays cacheable.
    assert _build(file_data, tool_use).request == built.request


def test_an_image_whose_workspace_file_changed_is_a_note_in_a_string_prompt(
    tmp_path: Path,
) -> None:
    file_data, tool_use = _legacy_workspace_turn(tmp_path, conversation=False)
    (tmp_path / "shot.png").unlink()

    built = _build(file_data, tool_use)

    assert built.multipart_files == []
    _prompt, note = built.request.messages[0].content
    assert isinstance(note, LlmInputTextBlock)
    assert "tool_attachment:shot" in note.text


def test_a_workspace_file_input_with_an_unsafe_path_still_fails(tmp_path: Path) -> None:
    file_data, tool_use = _legacy_workspace_turn(tmp_path, conversation=True)

    with pytest.raises(FileInputAccessError, match="unsafe path component"):
        _build({**file_data, "workspace_relative_path": "../shot.png"}, tool_use)


def _fail_opening_the_image(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    real_open = os.open

    def open_failing(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "shot.png":
            raise OSError(code, os.strerror(code))
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_failing)


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP])
def test_an_image_whose_file_is_gone_or_replaced_is_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    file_data, tool_use = _legacy_workspace_turn(tmp_path, conversation=True)
    _fail_opening_the_image(monkeypatch, code)

    built = _build(file_data, tool_use)

    assert built.multipart_files == []


@pytest.mark.parametrize("code", [errno.EACCES, errno.EMFILE, errno.EIO])
def test_a_failure_to_read_an_unchanged_image_fails_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A retryable failure must not drop the image for a note that it changed."""

    file_data, tool_use = _legacy_workspace_turn(tmp_path, conversation=True)
    _fail_opening_the_image(monkeypatch, code)

    with pytest.raises(FileInputAccessError) as failed:
        _build(file_data, tool_use)

    assert isinstance(failed.value.__cause__, OSError)
    assert failed.value.__cause__.errno == code

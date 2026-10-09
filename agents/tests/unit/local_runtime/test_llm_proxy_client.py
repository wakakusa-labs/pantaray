from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from pantaray_agents.local_runtime.llm_proxy.client import LocalLlmProxyClient
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.utils.llm_types import types
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.contracts.media import (
    LlmMaterializedMediaDescriptor,
    LlmMediaDescriptor,
    LlmMediaProjection,
)
from pantaray_llm.contracts.request import LlmProxyRequest
from pantaray_llm.contracts.tool_use import (
    LlmToolDefinition,
    LlmToolResult,
    LlmToolUseRequest,
    OpenAiContinuationMediaSlot,
    OpenAiToolContinuation,
)
from pantaray_llm.errors import LlmProxyExecutionError

from .migrated_db import prepare_test_database


class _StructuredResponse(BaseModel):
    answer: str


class _FakeHttpResponse:
    def __init__(self, *, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict[str, object]:
        return self._payload


class _RecordingAsyncClient:
    last_request: dict[str, object] | None = None

    def __init__(self, **_kwargs: object) -> None:
        self._response = _FakeHttpResponse(
            status_code=200,
            payload={
                "id": "resp_1",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"answer": "ok"}),
                            }
                        ],
                    }
                ],
                "thinking": "proxy thought",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 12,
                    "cached_prompt_tokens": 2,
                    "cache_write_prompt_tokens": 1,
                    "completion_tokens": 3,
                    "reasoning_tokens": 1,
                    "total_tokens": 15,
                },
                "meta": {
                    "local_job_id": "req-1",
                    "upstream_provider": "openai",
                    "profile_id": "dummy.default",
                    "outcome": "complete",
                    "upstream_request_id": "resp_1",
                    "resolved_model": "gpt-6-luna-2026-09-22",
                },
            },
        )

    async def __aenter__(self) -> _RecordingAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    async def post(
        self,
        url: str,
        *,
        data: dict[str, object],
        files: list[tuple[str, tuple[str, bytes, str]]],
        headers: dict[str, str],
    ) -> _FakeHttpResponse:
        type(self).last_request = {
            "url": url,
            "data": data,
            "files": files,
            "headers": headers,
        }
        return self._response


class _ToolRecordingAsyncClient(_RecordingAsyncClient):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._response._payload["output"] = []
        self._response._payload["tool_use"] = {
            "calls": [
                {
                    "call_id": "call-1",
                    "name": "completed",
                    "arguments": {"reason": "done"},
                }
            ],
            "continuation": {
                "provider": "openai",
                "history_items": [{"type": "message"}],
                "media_slots": [],
            },
        }


class _OverflowToolRecordingAsyncClient(_ToolRecordingAsyncClient):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        tool_use = self._response._payload["tool_use"]
        assert isinstance(tool_use, dict)
        calls = tool_use["calls"]
        assert isinstance(calls, list)
        calls.append(
            {
                "call_id": "call-2",
                "name": "completed",
                "arguments": {"reason": "extra"},
            }
        )


@pytest.fixture(scope="module")
def cloud_session_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("llm-proxy-session") / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    return db_path


@pytest.fixture(autouse=True)
def cloud_session(cloud_session_db: Path) -> None:
    """Every test here takes the cloud route, which a live session decides."""

    import_desktop_session(
        db_path=cloud_session_db,
        busy_timeout_ms=1_000,
        user_id="user-1",
        desktop_access_token="desktop-token",
        expires_at="2099-01-01T00:00:00Z",
        session_version="1",
    )
    mark_configured()


def _build_client() -> LocalLlmProxyClient:
    return LocalLlmProxyClient(proxy_url="https://llm.example.test/v1/responses")


def _continuation_tool_use(media: LlmMediaDescriptor) -> LlmToolUseRequest:
    projection = LlmMediaProjection(
        state="inline",
        source=media,
        materialized=LlmMaterializedMediaDescriptor(
            mime_type=media.mime_type,
            byte_size=media.byte_size,
            sha256=media.sha256,
            width_px=1,
            height_px=1,
        ),
    )
    continuation = OpenAiToolContinuation(
        provider="openai",
        history_items=[{"type": "message"}],
        media_slots=[
            OpenAiContinuationMediaSlot(
                item_index=0,
                content_index=0,
                data_field="image_url",
                projection=projection,
            )
        ],
    )
    return LlmToolUseRequest(
        tools=[
            LlmToolDefinition(
                name="completed",
                description="Finish the task.",
                parameters={"type": "object", "properties": {}},
            )
        ],
        continuation_mode="stateless",
        continuation=continuation,
        tool_result=LlmToolResult(
            call_id="call-1",
            name="completed",
            output={"ok": True},
        ),
    )


def _continuation_media() -> LlmMediaDescriptor:
    payload = b"png-bytes"
    return LlmMediaDescriptor(
        blob_ref="blob_continuation",
        mime_type="image/png",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


@pytest.mark.asyncio
async def test_generate_content_posts_multipart_responses_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    client = _build_client()
    payload = b"png-bytes"
    (tmp_path / "shot.png").write_bytes(payload)

    with TraceContextManager(user_id="user-1", local_job_id="req-1"):
        response = await client.aio.models.generate_content(
            contents=[
                "describe the screenshot",
                {
                    "file_data": {
                        "source_kind": "workspace_file",
                        "blob_ref": "attachment_blob_shot",
                        "application_ref": "tool_attachment:shot",
                        "mime_type": "image/png",
                        "workspace_root_path": str(tmp_path),
                        "workspace_relative_path": "shot.png",
                        "byte_size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            ],
            config=types.GenerateContentConfig(
                inference_profile="dummy.default",
                system_instruction="system prompt",
                response_mime_type="application/json",
                response_schema=_StructuredResponse,
            ),
        )

    assert response.text == '{"answer": "ok"}'
    assert isinstance(response.parsed, _StructuredResponse)
    assert response.parsed.answer == "ok"
    assert response.usage_metadata == {
        "prompt_tokens": 12,
        "cached_prompt_tokens": 2,
        "cache_write_prompt_tokens": 1,
        "completion_tokens": 3,
        "reasoning_tokens": 1,
        "total_tokens": 15,
    }
    assert response.thinking == "proxy thought"
    assert response.meta == {
        "local_job_id": "req-1",
        "upstream_provider": "openai",
        "profile_id": "dummy.default",
        "outcome": "complete",
        "upstream_request_id": "resp_1",
    }

    request = _RecordingAsyncClient.last_request
    assert request is not None
    assert request["url"] == "https://llm.example.test/v1/responses"
    assert request["headers"] == {"Authorization": "Bearer desktop-token"}

    request_json = json.loads(request["data"]["request"])  # type: ignore[index]
    assert request_json["metadata"]["user_id"] == "user-1"
    assert request_json["metadata"]["local_job_id"] == "req-1"
    assert request_json["inference_profile"] == "dummy.default"
    assert "runtime_overrides" not in request_json
    assert request_json["response_format"]["type"] == "json_schema"
    assert len(request_json["messages"]) == 2
    assert len(request["files"]) == 1


def _wire_request(request: dict[str, object]) -> str:
    """The serialized request part, checked against the cloud proxy's ingress.

    ``router.py`` parses this exact text with ``LlmProxyRequest`` and rejects
    anything else with 422, so a wire assertion that skips this check can pin a
    payload the cloud would refuse.
    """

    data = request["data"]
    assert isinstance(data, dict)
    serialized = data["request"]
    assert isinstance(serialized, str)
    LlmProxyRequest.model_validate(json.loads(serialized))
    return serialized


@pytest.mark.asyncio
async def test_wire_request_keeps_the_cloud_envelope_for_media_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The multipart envelope is the cloud proxy's wire contract, byte for byte."""

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    client = _build_client()
    screenshot_payload = b"\x89PNG\r\n\x1a\nscreenshot"
    screenshot_sha256 = hashlib.sha256(screenshot_payload).hexdigest()
    screenshot_storage_path = (
        "user-1/2026-09-08/3f1c8e2a-0b44-4c2e-9f13-7a5d8e6b2c91.png"
    )
    screenshot_path = tmp_path / "generated" / "images" / screenshot_storage_path
    screenshot_path.parent.mkdir(parents=True)
    screenshot_path.write_bytes(screenshot_payload)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path))

    with TraceContextManager(
        user_id="user-1", local_job_id="req-wire", action_id="action-42"
    ):
        await client.aio.models.generate_content(
            contents=[
                "describe the screenshot",
                {
                    "file_data": {
                        "source_kind": "local_image_blob",
                        "blob_ref": f"attachment_blob_{screenshot_sha256[:24]}",
                        "application_ref": f"user_attachment:{screenshot_sha256[:24]}",
                        "mime_type": "image/png",
                        "storage_path": screenshot_storage_path,
                        "byte_size": len(screenshot_payload),
                        "sha256": screenshot_sha256,
                    }
                },
            ],
            config=types.GenerateContentConfig(
                inference_profile="dummy.default",
                system_instruction="system prompt",
                response_mime_type="application/json",
                response_schema=_StructuredResponse,
            ),
        )

    request = _RecordingAsyncClient.last_request
    assert request is not None
    assert _wire_request(request) == json.dumps(
        {
            "inference_profile": "dummy.default",
            "messages": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": "system prompt"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe the screenshot"},
                        {
                            "type": "input_image",
                            "image": {
                                "blob_ref": f"attachment_blob_{screenshot_sha256[:24]}",
                                "mime_type": "image/png",
                                "byte_size": len(screenshot_payload),
                                "sha256": screenshot_sha256,
                                "application_ref": (
                                    f"user_attachment:{screenshot_sha256[:24]}"
                                ),
                            },
                        },
                    ],
                },
            ],
            "metadata": {
                "request_kind": "llm_inference",
                "user_id": "user-1",
                "local_job_id": "req-wire",
                "session_version": "1",
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": _StructuredResponse.model_json_schema(),
            },
            "prompt_cache_key": hashlib.sha256(b"user-1").hexdigest(),
        },
        ensure_ascii=False,
    )
    assert request["files"] == [
        (
            f"attachment_blob_{screenshot_sha256[:24]}",
            (
                f"attachment_blob_{screenshot_sha256[:24]}.bin",
                screenshot_payload,
                "image/png",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_wire_request_keeps_the_cloud_envelope_for_tool_use_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A null tool result stays on the wire: the cloud requires the field."""

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _ToolRecordingAsyncClient,
    )
    client = _build_client()
    media = _continuation_media()
    # A tool that returns nothing still has to send output: LlmToolResult.output
    # is a required JSONValue, so a null there must survive to the wire.
    tool_use = _continuation_tool_use(media).model_copy(
        update={
            "tool_result": LlmToolResult(
                call_id="call-1", name="completed", output=None
            )
        }
    )

    with TraceContextManager(user_id="user-1", local_job_id="req-wire-tool"):
        await client.aio.models.generate_content(
            contents=["continue"],
            config=types.GenerateContentConfig(
                inference_profile="dummy.default",
                tool_use=tool_use,
            ),
        )

    request = _ToolRecordingAsyncClient.last_request
    assert request is not None
    assert _wire_request(request) == json.dumps(
        {
            "inference_profile": "dummy.default",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "continue"}],
                }
            ],
            "metadata": {
                "request_kind": "llm_inference",
                "user_id": "user-1",
                "local_job_id": "req-wire-tool",
                "session_version": "1",
            },
            "tool_use": tool_use.model_dump(mode="json"),
            "prompt_cache_key": hashlib.sha256(b"user-1").hexdigest(),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_generate_content_checks_the_session_before_reading_file_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An owner without a usable session must not reach the file inputs."""

    def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("file inputs must not be read before the session check")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.request_builder.prepare_content_file",
        fail_if_called,
    )
    pdf_path = tmp_path / "attachment-blob.pdf"
    pdf_payload = b"%PDF-1.7\ncontent\n"
    pdf_path.write_bytes(pdf_payload)
    client = _build_client()

    # The signed-in account owns the session; nobody else's work may use it.
    with TraceContextManager(user_id="user-2", local_job_id="req-no-session"):
        with pytest.raises(RuntimeError, match="desktop session is not active"):
            await client.aio.models.generate_content(
                contents=[
                    "read this file",
                    {
                        "file_data": {
                            "blob_ref": "attachment_blob_pdf",
                            "application_ref": "tool_attachment:abc",
                            "mime_type": "application/pdf",
                            "blob_path": str(pdf_path),
                            "byte_size": len(pdf_payload),
                            "sha256": hashlib.sha256(pdf_payload).hexdigest(),
                        }
                    },
                ],
                config=types.GenerateContentConfig(inference_profile="dummy.default"),
            )


@pytest.mark.asyncio
async def test_generate_content_transports_native_tool_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _ToolRecordingAsyncClient,
    )
    client = _build_client()
    tool_use = LlmToolUseRequest(
        tools=[
            LlmToolDefinition(
                name="completed",
                description="Finish the task.",
                parameters={"type": "object", "properties": {}},
            )
        ],
        continuation_mode="stateless",
    )

    with TraceContextManager(user_id="user-1", local_job_id="req-tool"):
        response = await client.aio.models.generate_content(
            contents=["prompt"],
            config=types.GenerateContentConfig(
                inference_profile="dummy.default",
                tool_use=tool_use,
            ),
        )

    assert response.text == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "completed"
    assert response.tool_calls[0].arguments == {"reason": "done"}
    assert response.tool_continuation is not None
    request = _ToolRecordingAsyncClient.last_request
    assert request is not None
    request_json = json.loads(request["data"]["request"])  # type: ignore[index]
    assert request_json["tool_use"] == tool_use.model_dump(mode="json")
    assert "response_format" not in request_json


@pytest.mark.asyncio
async def test_generate_content_refuses_a_non_image_attachment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A document reaches the model as the text `read` returns, never as bytes.

    History a pre-#1395 `read` wrote can still name a PDF attachment, and
    sending those bytes as an image would misdescribe them to the provider.
    """

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    pdf_path = tmp_path / "attachment-blob.pdf"
    pdf_payload = b"%PDF-1.7\ncontent\n"
    pdf_path.write_bytes(pdf_payload)
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-pdf"):
        with pytest.raises(RuntimeError, match="not an image"):
            await client.aio.models.generate_content(
                contents=[
                    "read this file tool_attachment:abc",
                    {
                        "file_data": {
                            "blob_ref": "attachment_blob_pdf",
                            "application_ref": "tool_attachment:abc",
                            "mime_type": "application/pdf",
                            "blob_path": str(pdf_path),
                            "byte_size": len(pdf_payload),
                            "sha256": hashlib.sha256(pdf_payload).hexdigest(),
                        }
                    },
                ],
                config=types.GenerateContentConfig(inference_profile="dummy.default"),
            )


@pytest.mark.asyncio
async def test_generate_content_posts_user_image_as_input_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    storage_path = "user-1/2026-09-08/1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed.png"
    payload = b"\x89PNG\r\n\x1a\nuser-image"
    image_path = tmp_path / "generated" / "images" / storage_path
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(payload)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path))
    sha256 = hashlib.sha256(payload).hexdigest()
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-image"):
        await client.aio.models.generate_content(
            contents=[
                f"Attached images: user_attachment:{sha256[:24]}",
                {
                    "file_data": {
                        "source_kind": "local_image_blob",
                        "blob_ref": f"attachment_blob_{sha256[:24]}",
                        "application_ref": f"user_attachment:{sha256[:24]}",
                        "mime_type": "image/png",
                        "storage_path": storage_path,
                        "byte_size": len(payload),
                        "sha256": sha256,
                    }
                },
            ],
            config=types.GenerateContentConfig(inference_profile="dummy.default"),
        )

    request = _RecordingAsyncClient.last_request
    assert request is not None
    request_json = json.loads(request["data"]["request"])  # type: ignore[index]
    user_content = request_json["messages"][0]["content"]
    assert user_content[1]["type"] == "input_image"
    assert user_content[1]["image"]["application_ref"] == (
        f"user_attachment:{sha256[:24]}"
    )
    assert user_content[1]["image"]["sha256"] == sha256
    # The logical storage_path never leaves the local runtime.
    assert storage_path not in json.dumps(request_json)
    assert len(request["files"]) == 1
    assert request["files"][0][1][1] == payload


@pytest.mark.asyncio
async def test_generate_content_rejects_blob_size_mismatch_before_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    blob_path = tmp_path / "attachment-blob.pdf"
    blob_path.write_bytes(b"%PDF-1.7\nsmall\n")
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-pdf-size"):
        with pytest.raises(RuntimeError, match="byte_size mismatch"):
            await client.aio.models.generate_content(
                contents=[
                    "read this file tool_attachment:abc",
                    {
                        "file_data": {
                            "blob_ref": "attachment_blob_pdf",
                            "application_ref": "tool_attachment:abc",
                            "mime_type": "application/pdf",
                            "blob_path": str(blob_path),
                            "byte_size": 1,
                            "sha256": hashlib.sha256(b"x").hexdigest(),
                        }
                    },
                ],
                config=types.GenerateContentConfig(inference_profile="dummy.default"),
            )


@pytest.mark.asyncio
async def test_generate_content_rejects_declared_blob_over_limit_before_stat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.content_files.LLM_FILE_INPUT_MAX_BYTES",
        8,
    )
    client = _build_client()
    missing_blob_path = tmp_path / "missing-large-attachment.pdf"

    with TraceContextManager(user_id="user-1", local_job_id="req-pdf-too-large"):
        with pytest.raises(RuntimeError, match="too large"):
            await client.aio.models.generate_content(
                contents=[
                    "read this file tool_attachment:abc",
                    {
                        "file_data": {
                            "blob_ref": "attachment_blob_pdf",
                            "application_ref": "tool_attachment:abc",
                            "mime_type": "application/pdf",
                            "blob_path": str(missing_blob_path),
                            "byte_size": 9,
                            "sha256": hashlib.sha256(b"x" * 9).hexdigest(),
                        }
                    },
                ],
                config=types.GenerateContentConfig(inference_profile="dummy.default"),
            )


@pytest.mark.asyncio
async def test_generate_content_rejects_blob_that_grows_after_stat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.content_files.LLM_FILE_INPUT_MAX_BYTES",
        8,
    )
    blob_path = tmp_path / "grown-attachment.pdf"
    blob_path.write_bytes(b"x" * 9)
    original_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == blob_path:
            return SimpleNamespace(st_size=4)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-pdf-grown"):
        with pytest.raises(RuntimeError, match="too large"):
            await client.aio.models.generate_content(
                contents=[
                    "read this file tool_attachment:abc",
                    {
                        "file_data": {
                            "blob_ref": "attachment_blob_pdf",
                            "application_ref": "tool_attachment:abc",
                            "mime_type": "application/pdf",
                            "blob_path": str(blob_path),
                            "byte_size": 4,
                            "sha256": hashlib.sha256(b"x" * 4).hexdigest(),
                        }
                    },
                ],
                config=types.GenerateContentConfig(inference_profile="dummy.default"),
            )


@pytest.mark.asyncio
async def test_generate_content_stream_splits_text_and_attaches_usage_on_last_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-2"):
        chunks = [
            chunk
            async for chunk in client.aio.models.generate_content_stream(
                contents=["hello world from pantaray stream"],
                config=types.GenerateContentConfig(
                    inference_profile="dummy.default",
                    system_instruction="system",
                ),
            )
        ]

    assert len(chunks) >= 1
    assert chunks[-1].usage_metadata == {
        "prompt_tokens": 12,
        "cached_prompt_tokens": 2,
        "cache_write_prompt_tokens": 1,
        "completion_tokens": 3,
        "reasoning_tokens": 1,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_generate_content_rejects_unsupported_model_field() -> None:
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-4"):
        with pytest.raises(
            RuntimeError,
            match="Unsupported local LLM proxy call fields: model",
        ):
            await client.aio.models.generate_content(
                model="gpt-6-luna",
                contents=["hello"],
                config=types.GenerateContentConfig(
                    inference_profile="dummy.default",
                    system_instruction="system",
                ),
            )


@pytest.mark.asyncio
async def test_generate_content_rejects_raw_runtime_override_fields() -> None:
    client = _build_client()

    with TraceContextManager(user_id="user-1", local_job_id="req-5"):
        with pytest.raises(
            RuntimeError,
            match="Unsupported local LLM proxy config fields: temperature",
        ):
            await client.aio.models.generate_content(
                contents=["hello"],
                config={
                    "inference_profile": "dummy.default",
                    "system_instruction": "system",
                    "temperature": 0.2,
                },
            )


@pytest.mark.asyncio
async def test_generate_content_rejects_more_tool_calls_than_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _OverflowToolRecordingAsyncClient,
    )
    client = _build_client()
    tool_use = LlmToolUseRequest(
        tools=[
            LlmToolDefinition(
                name="completed",
                description="Finish the task.",
                parameters={"type": "object", "properties": {}},
            )
        ],
        continuation_mode="stateless",
    )

    with (
        TraceContextManager(user_id="user-1", local_job_id="req-tool"),
        pytest.raises(LlmProxyExecutionError),
    ):
        await client.aio.models.generate_content(
            contents=["prompt"],
            config=types.GenerateContentConfig(
                inference_profile="dummy.default",
                tool_use=tool_use,
            ),
        )


@pytest.mark.asyncio
async def test_generate_content_keys_the_prompt_cache_by_owner_not_by_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Action and a background job of one owner share one opaque cache key."""

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.llm_proxy.client.httpx2.AsyncClient",
        _RecordingAsyncClient,
    )
    client = _build_client()
    sent_keys: list[str] = []

    for trace in (
        {"local_job_id": "action-job", "action_id": "action-42"},
        {"local_job_id": "suggestion-job", "suggestion_id": "suggestion-7"},
    ):
        with TraceContextManager(user_id="user-1", **trace):
            await client.aio.models.generate_content(
                contents=["prompt"],
                config=types.GenerateContentConfig(
                    inference_profile="dummy.default",
                    system_instruction="system prompt",
                ),
            )
        request = _RecordingAsyncClient.last_request
        assert request is not None
        request_json = json.loads(request["data"]["request"])  # type: ignore[index]
        sent_keys.append(request_json["prompt_cache_key"])

    assert sent_keys == [hashlib.sha256(b"user-1").hexdigest()] * 2

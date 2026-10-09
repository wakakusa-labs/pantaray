"""Turn one ``generate_content`` call into the shared LLM request contract.

The cloud and direct routes differ in how a request is authorized and sent, not
in how a caller's ``contents`` and ``config`` become a request. That shared step
lives here so neither route duplicates content-block assembly or file reads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pantaray_agents.local_runtime.llm_proxy.content_files import (
    MultipartFile,
    PreparedContentFile,
    prepare_content_file,
)
from pantaray_agents.local_runtime.llm_proxy.generate_config import (
    serialize_for_json,
    serialize_generate_config,
)
from pantaray_agents.local_runtime.llm_proxy.response_parsing import (
    coerce_non_empty_string,
    is_mapping,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnRequest
from pantaray_llm.contracts.conversation import LlmTurnAssistantItem
from pantaray_llm.contracts.request import (
    LlmImageDescriptor,
    LlmInputBlock,
    LlmInputImageBlock,
    LlmInputTextBlock,
    LlmJsonSchemaResponseFormat,
    LlmMessage,
    LlmRequest,
    LlmRequestTrace,
)
from pantaray_llm.contracts.tool_use import LlmToolUseRequest


@dataclass(frozen=True, slots=True)
class BuiltLlmRequest:
    """A shared request plus the media payloads its descriptors point at."""

    request: LlmRequest
    multipart_files: list[MultipartFile]
    # The caller's own response type. It parses the response and is never sent.
    response_schema: object | None


@dataclass(frozen=True, slots=True)
class _GenerateSettings:
    """The supported ``config`` fields, read once out of a duck-typed value."""

    inference_profile: str
    system_instruction: str | None
    response_format: LlmJsonSchemaResponseFormat | None
    response_schema: object | None
    tool_use: LlmToolUseRequest | LlmActionTurnRequest | None


def build_llm_request(
    *,
    contents: object,
    config: object | None,
    user_id: str,
    local_job_id: str,
) -> BuiltLlmRequest:
    """Build the shared request for one ``generate_content`` call.

    ``user_id`` is a parameter rather than ambient state: a route resolves the
    owner, and with it the permission to run, before any file input is read.
    """

    if not isinstance(contents, list):
        raise RuntimeError("contents must be a list")
    settings = _read_generate_settings(config)
    tool_use = settings.tool_use
    conversation_media_refs = _conversation_media_refs(tool_use)
    content_blocks: list[LlmInputBlock] = []
    multipart_files: list[MultipartFile] = []
    for content in contents:
        if isinstance(content, str):
            content_blocks.append(LlmInputTextBlock(type="input_text", text=content))
            continue
        if not is_mapping(content):
            raise RuntimeError("LLM contents item is invalid")
        file_data = content.get("file_data")
        if not is_mapping(file_data):
            raise RuntimeError("Unsupported LLM contents block")
        prepared_file = prepare_content_file(
            file_data=file_data,
            user_id=user_id,
        )
        if prepared_file.application_ref not in conversation_media_refs:
            content_blocks.append(_file_block(prepared_file))
        multipart_files.append(
            (
                prepared_file.blob_ref,
                (
                    f"{prepared_file.blob_ref}.bin",
                    prepared_file.payload,
                    prepared_file.mime_type,
                ),
            )
        )
    messages: list[LlmMessage] = []
    if settings.system_instruction is not None:
        messages.append(
            LlmMessage(
                role="system",
                content=[
                    LlmInputTextBlock(
                        type="input_text", text=settings.system_instruction
                    )
                ],
            )
        )
    messages.append(LlmMessage(role="user", content=content_blocks))
    return BuiltLlmRequest(
        request=LlmRequest(
            purpose=settings.inference_profile,
            trace=LlmRequestTrace(local_job_id=local_job_id),
            messages=messages,
            response_format=settings.response_format,
            tool_use=tool_use,
            prompt_cache_key=_prompt_cache_key(user_id),
        ),
        multipart_files=multipart_files,
        response_schema=settings.response_schema,
    )


def _conversation_media_refs(
    tool_use: LlmToolUseRequest | LlmActionTurnRequest | None,
) -> frozenset[str]:
    """The application refs an Action conversation already places itself.

    A caller that sends a conversation hands the media in with the request, the
    way every other file input arrives, but the items carry the blocks: the
    payload still has to be uploaded, and repeating the block in the request's
    own message would send the same picture twice.
    """

    if not isinstance(tool_use, LlmActionTurnRequest) or tool_use.conversation is None:
        return frozenset()
    return frozenset(
        descriptor.application_ref
        for item in tool_use.conversation
        if not isinstance(item, LlmTurnAssistantItem)
        for block in item.content
        if not isinstance(block, LlmInputTextBlock)
        for descriptor in (
            block.image if isinstance(block, LlmInputImageBlock) else block.file,
        )
        if descriptor.application_ref is not None
    )


def _read_generate_settings(config: object | None) -> _GenerateSettings:
    response_schema = _read_generate_config_field(config, "response_schema")
    response_mime_type = _read_generate_config_field(config, "response_mime_type")
    raw_tool_use = _read_generate_config_field(config, "tool_use")
    tool_use = (
        raw_tool_use
        if isinstance(raw_tool_use, LlmToolUseRequest | LlmActionTurnRequest)
        else None
    )
    if raw_tool_use is not None and tool_use is None:
        raise RuntimeError("config.tool_use must be a typed native LLM request")
    config_payload = serialize_generate_config(config)
    config_payload.pop("response_mime_type", None)
    config_payload.pop("response_schema", None)
    config_payload.pop("tool_use", None)
    inference_profile = coerce_non_empty_string(
        config_payload.pop("inference_profile", None),
        field_name="inference_profile",
    )
    system_instruction = config_payload.pop("system_instruction", None)
    if config_payload:
        unexpected_keys = ", ".join(sorted(config_payload))
        raise RuntimeError(
            f"Unsupported local LLM proxy config fields: {unexpected_keys}"
        )
    return _GenerateSettings(
        inference_profile=inference_profile,
        system_instruction=(
            system_instruction
            if isinstance(system_instruction, str) and system_instruction.strip()
            else None
        ),
        response_format=(
            LlmJsonSchemaResponseFormat.model_validate(
                {
                    "type": "json_schema",
                    "json_schema": serialize_for_json(response_schema),
                }
            )
            if response_mime_type == "application/json" and response_schema is not None
            else None
        ),
        response_schema=response_schema,
        tool_use=tool_use,
    )


def _read_generate_config_field(
    config: object | None, field_name: str
) -> object | None:
    if config is None:
        return None
    if is_mapping(config):
        return config.get(field_name)
    return getattr(config, field_name, None)


def _file_block(prepared_file: PreparedContentFile) -> LlmInputBlock:
    # The model reads a document as the text `read` returns, so an attachment is
    # only ever an image. A non-image one can still reach here out of history a
    # pre-#1395 `read` wrote, and sending it as an image would misdescribe its
    # bytes to the provider, so refuse it instead.
    if not prepared_file.mime_type.startswith("image/"):
        raise RuntimeError(
            f"LLM file input is not an image: {prepared_file.blob_ref} "
            f"({prepared_file.mime_type})"
        )
    return LlmInputImageBlock(
        type="input_image",
        image=LlmImageDescriptor(
            blob_ref=prepared_file.blob_ref,
            mime_type=prepared_file.mime_type,
            byte_size=prepared_file.byte_size,
            sha256=prepared_file.sha256,
            application_ref=prepared_file.application_ref,
        ),
    )


def _prompt_cache_key(user_id: str) -> str:
    """One stable key per owner, the same on every route and for every job.

    On GPT-5.6 and later the key no longer steers cache routing; it only keeps
    each key's cached prefixes apart. Per owner, every run of a kind reuses the
    shared system prompt and tool prefix instead of rewriting it at 1.25x, and
    owners who share the cloud's one organization cannot probe each other's
    prompts through cache hits. The digest keeps the raw ID off the wire.
    https://developers.openai.com/api/docs/guides/prompt-caching#prompt-cache-keys
    """

    return hashlib.sha256(user_id.encode()).hexdigest()


__all__ = ["BuiltLlmRequest", "build_llm_request"]

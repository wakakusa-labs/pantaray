"""File input helpers for LLM content construction."""

from __future__ import annotations

from typing import Literal, TypedDict

from pantaray_agents.tools.contract import ToolImage

LLM_FILE_DATA_KEY = "file_data"
TOOL_ATTACHMENT_REF_PREFIX = "tool_attachment:"
USER_ATTACHMENT_REF_PREFIX = "user_attachment:"


class _CommonLlmFileInput(TypedDict):
    ref: str
    blob_ref: str
    mime_type: str
    byte_size: int
    sha256: str


class WorkspaceLlmFileInput(_CommonLlmFileInput):
    source_kind: Literal["workspace_file"]
    workspace_root_path: str
    workspace_relative_path: str


class LocalImageLlmFileInput(_CommonLlmFileInput):
    """A user-scoped local image artifact addressed only by its logical storage_path."""

    source_kind: Literal["local_image_blob"]
    storage_path: str


type LlmFileInput = WorkspaceLlmFileInput | LocalImageLlmFileInput


class _CommonBlobFileData(TypedDict):
    blob_ref: str
    application_ref: str
    mime_type: str
    byte_size: int
    sha256: str


class WorkspaceFileData(_CommonBlobFileData):
    source_kind: Literal["workspace_file"]
    workspace_root_path: str
    workspace_relative_path: str


class LocalImageFileData(_CommonBlobFileData):
    source_kind: Literal["local_image_blob"]
    storage_path: str


type BlobFileData = WorkspaceFileData | LocalImageFileData


class BlobFileContentBlock(TypedDict):
    file_data: BlobFileData


type TextOrFileContent = str | BlobFileContentBlock


def build_blob_file_block(file_input: LlmFileInput) -> BlobFileContentBlock:
    common: _CommonBlobFileData = {
        "blob_ref": file_input["blob_ref"],
        "application_ref": file_input["ref"],
        "mime_type": file_input["mime_type"],
        "byte_size": file_input["byte_size"],
        "sha256": file_input["sha256"],
    }
    if file_input["source_kind"] == "workspace_file":
        file_data: BlobFileData = {
            **common,
            "source_kind": "workspace_file",
            "workspace_root_path": file_input["workspace_root_path"],
            "workspace_relative_path": file_input["workspace_relative_path"],
        }
    else:
        file_data = {
            **common,
            "source_kind": "local_image_blob",
            "storage_path": file_input["storage_path"],
        }
    return {
        "file_data": file_data,
    }


def interleave_file_inputs(
    *,
    prompt: str,
    file_inputs: list[LlmFileInput] | None,
) -> list[TextOrFileContent]:
    if not file_inputs:
        return [prompt]

    contents: list[TextOrFileContent] = []
    cursor = 0
    for file_input in file_inputs:
        ref = file_input["ref"]
        position = prompt.find(ref, cursor)
        if position < 0:
            raise RuntimeError(f"LLM file attachment ref is missing from prompt: {ref}")
        end = position + len(ref)
        text_segment = prompt[cursor:end]
        if text_segment:
            contents.append(text_segment)
        contents.append(build_blob_file_block(file_input))
        cursor = end
    if cursor < len(prompt):
        contents.append(prompt[cursor:])
    return contents


def tool_image_file_input(image: ToolImage) -> WorkspaceLlmFileInput:
    """The file a request reads a tool's image from, under the ref it is shown by."""

    return {
        "ref": image.ref,
        "blob_ref": image.blob_ref,
        "mime_type": image.mime_type,
        "byte_size": image.byte_size,
        "sha256": image.sha256,
        "source_kind": "workspace_file",
        "workspace_root_path": image.workspace_root_path,
        "workspace_relative_path": image.workspace_relative_path,
    }


__all__ = [
    "LocalImageLlmFileInput",
    "LLM_FILE_DATA_KEY",
    "LlmFileInput",
    "WorkspaceLlmFileInput",
    "BlobFileContentBlock",
    "TOOL_ATTACHMENT_REF_PREFIX",
    "USER_ATTACHMENT_REF_PREFIX",
    "TextOrFileContent",
    "build_blob_file_block",
    "interleave_file_inputs",
    "tool_image_file_input",
]

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

TOOL_ATTACHMENT_REF_PREFIX = "tool_attachment:"
ATTACHMENT_BLOB_REF_PREFIX = "attachment_blob_"
ATTACHMENT_ID_HEX_LENGTH = 24


@dataclass(frozen=True, slots=True)
class WorkspaceFileAttachment:
    ref: str
    blob_ref: str
    canonical_root_path: Path
    root_relative_path: str
    byte_size: int
    sha256: str


def build_workspace_file_attachment(
    *,
    canonical_root_path: Path,
    root_relative_path: str,
    display_path: str,
    mime_type: str,
    payload: bytes,
) -> WorkspaceFileAttachment:
    sha256 = hashlib.sha256(payload).hexdigest()
    attachment_id = _build_attachment_id(
        display_path=display_path,
        mime_type=mime_type,
        sha256=sha256,
    )
    return WorkspaceFileAttachment(
        ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}",
        blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
        canonical_root_path=canonical_root_path,
        root_relative_path=root_relative_path,
        byte_size=len(payload),
        sha256=sha256,
    )


def _build_attachment_id(*, display_path: str, mime_type: str, sha256: str) -> str:
    ref_input = f"{display_path}\0{mime_type}\0{sha256}".encode()
    return hashlib.sha256(ref_input).hexdigest()[:ATTACHMENT_ID_HEX_LENGTH]


__all__ = [
    "ATTACHMENT_BLOB_REF_PREFIX",
    "TOOL_ATTACHMENT_REF_PREFIX",
    "WorkspaceFileAttachment",
    "build_workspace_file_attachment",
]

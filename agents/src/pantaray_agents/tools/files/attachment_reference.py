from __future__ import annotations

import hashlib
from dataclasses import dataclass

TOOL_ATTACHMENT_REF_PREFIX = "tool_attachment:"
ATTACHMENT_BLOB_REF_PREFIX = "attachment_blob_"
ATTACHMENT_ID_HEX_LENGTH = 24


@dataclass(frozen=True, slots=True)
class ReadImage:
    """An image ``read`` returns as itself: the bytes it read, and their names.

    The caller keeps these bytes where a later request reads them back. History
    never names the file itself: the next turn would send whatever the file
    holds by then, and a file rewritten or removed after the read would fail
    every turn after it.
    """

    ref: str
    blob_ref: str
    display_path: str
    mime_type: str
    payload: bytes
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.payload)


def build_read_image(
    *,
    display_path: str,
    mime_type: str,
    payload: bytes,
) -> ReadImage:
    sha256 = hashlib.sha256(payload).hexdigest()
    attachment_id = _build_attachment_id(
        display_path=display_path,
        mime_type=mime_type,
        sha256=sha256,
    )
    return ReadImage(
        ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}",
        blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
        display_path=display_path,
        mime_type=mime_type,
        payload=payload,
        sha256=sha256,
    )


def _build_attachment_id(*, display_path: str, mime_type: str, sha256: str) -> str:
    ref_input = f"{display_path}\0{mime_type}\0{sha256}".encode()
    return hashlib.sha256(ref_input).hexdigest()[:ATTACHMENT_ID_HEX_LENGTH]


__all__ = [
    "ATTACHMENT_BLOB_REF_PREFIX",
    "TOOL_ATTACHMENT_REF_PREFIX",
    "ReadImage",
    "build_read_image",
]

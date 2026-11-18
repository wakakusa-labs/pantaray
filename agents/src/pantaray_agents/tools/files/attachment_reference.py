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
    image_id = attachment_id(sha256)
    return ReadImage(
        ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{image_id}",
        blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{image_id}",
        display_path=display_path,
        mime_type=mime_type,
        payload=payload,
        sha256=sha256,
    )


def attachment_id(sha256: str) -> str:
    """An image's attachment id: its own bytes' digest, shortened.

    The id names the bytes alone, so the same image is one attachment wherever
    it came from. History keeps every ref as it was written and nothing derives
    one again, so a ref an earlier rule made still resolves.
    """

    return sha256[:ATTACHMENT_ID_HEX_LENGTH]


__all__ = [
    "ATTACHMENT_BLOB_REF_PREFIX",
    "TOOL_ATTACHMENT_REF_PREFIX",
    "ReadImage",
    "attachment_id",
    "build_read_image",
]

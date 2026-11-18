"""Name a stored image a tool returns, in both forms the image travels in.

A tool that hands the model an image (``read``, the drawn pages of a document,
``capture_screen``) first keeps the bytes in the user's image store, so a later
turn sends exactly what this one saw. The image then travels twice:

- the outcome attachment, which history keeps and the next request reads the
  bytes back through (it carries ``blob_ref`` and ``sha256``);
- the entry in the durable ``output.attachments[]``, which the conversation
  page shows and history deletion finds the image by. It names the image by its
  user-scoped ``storage_path`` and never carries the bytes or a digest.

Both are built here from the stored bytes alone, so they always name the same
image. Storing and verifying the bytes stays with each producer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files.attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    TOOL_ATTACHMENT_REF_PREFIX,
    attachment_id,
)

from .local_image_store import LocalImageBlob

_NO_OUTPUT_FIELDS: Final[Mapping[str, JSONValue]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ToolImageAttachment:
    """One stored image, as the outcome carries it and as the output names it.

    ``attachment`` has the shape ``coerce_tool_attachments`` reads as a
    ``LocalImageAttachment``. It is a plain mapping here because the broker,
    which renders pages for Suggestion as well, sits below the Action agent.
    """

    ref: str
    attachment: dict[str, JSONValue]
    output_entry: dict[str, JSONValue]


def assemble_tool_image(
    blob: LocalImageBlob,
    *,
    display_name: Callable[[str], str],
    output_fields: Mapping[str, JSONValue] = _NO_OUTPUT_FIELDS,
) -> ToolImageAttachment:
    """Build both forms of an image already stored and verified as ``blob``.

    ``display_name`` receives the attachment id, so a generated image can carry
    it in its name; ``output_fields`` adds tool-specific fields such as
    ``page_number`` to the output entry.
    """

    sha256 = hashlib.sha256(blob.payload).hexdigest()
    image_id = attachment_id(sha256)
    ref = f"{TOOL_ATTACHMENT_REF_PREFIX}{image_id}"
    name = display_name(image_id)
    byte_size = len(blob.payload)
    return ToolImageAttachment(
        ref=ref,
        attachment={
            "type": "file",
            "source_kind": "local_image_blob",
            "ref": ref,
            "blob_ref": f"{ATTACHMENT_BLOB_REF_PREFIX}{image_id}",
            "display_path": name,
            "mime_type": blob.mime_type,
            "byte_size": byte_size,
            "sha256": sha256,
            "storage_path": blob.storage_path,
        },
        output_entry={
            "type": "file",
            "source_kind": "local_image_blob",
            "mime_type": blob.mime_type,
            **output_fields,
            "path": name,
            "storage_path": blob.storage_path,
            "ref": ref,
            "byte_size": byte_size,
        },
    )


__all__ = ["ToolImageAttachment", "assemble_tool_image"]

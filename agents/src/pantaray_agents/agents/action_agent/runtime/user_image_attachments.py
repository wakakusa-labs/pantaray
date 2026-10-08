"""Bind USER-step images to the shared LLM file-input attachment contract."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    LocalImageAttachment,
)
from pantaray_agents.agents.core.llm_file_inputs import USER_ATTACHMENT_REF_PREFIX
from pantaray_agents.local_runtime.runtime.local_image_store import (
    read_local_image_blob,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.tools.files.attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
)


def build_user_image_attachments(
    *,
    user_id: str,
    images: Sequence[ImageInput],
) -> list[LocalImageAttachment]:
    """Read each USER image once and describe it for the LLM file-input path.

    The durable USER message carries only the logical ``storage_path``; the
    content identity the LLM path verifies (``sha256``/``byte_size``) is derived
    from the artifact itself on every projection, so a replaced or truncated
    file is rejected downstream instead of being silently sent.
    """

    attachments: list[LocalImageAttachment] = []
    for index, image in enumerate(images):
        blob = read_local_image_blob(
            user_id=user_id,
            storage_path=image.storage_path,
        )
        if blob is None:
            raise MigrationError(
                f"USER image artifact was not found: {image.storage_path}"
            )
        sha256 = hashlib.sha256(blob.payload).hexdigest()
        attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
        extension = blob.storage_path[blob.storage_path.rindex(".") :]
        attachments.append(
            {
                "type": "file",
                "ref": f"{USER_ATTACHMENT_REF_PREFIX}{attachment_id}",
                "blob_ref": f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
                "display_path": f"image-{index + 1}{extension}",
                "mime_type": blob.mime_type,
                "byte_size": len(blob.payload),
                "sha256": sha256,
                "source_kind": "local_image_blob",
                "storage_path": blob.storage_path,
            }
        )
    return attachments


__all__ = ["build_user_image_attachments"]

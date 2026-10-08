"""What chat items show the model beyond their own fields: the user's images.

A user's attached image goes to the model as the image itself, read from the
local image store with the digest its request is checked against. They are read
once per turn, off the loop, for the items the turn sends; an image no longer in
the store is said in words instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pantaray_agents.agents.core.llm_file_inputs import (
    USER_ATTACHMENT_REF_PREFIX,
    LocalImageLlmFileInput,
)
from pantaray_agents.local_runtime.chat.store import TurnChatItem
from pantaray_agents.local_runtime.runtime.local_image_store import (
    read_local_image_blob,
)
from pantaray_agents.schema.chat import UserMessageContent
from pantaray_agents.tools.files.attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
)
from pantaray_llm.contracts.input_block import LlmImageDescriptor, LlmInputImageBlock


@dataclass(frozen=True, slots=True)
class ItemMedia:
    # By storage_path; an image the store no longer has is absent.
    images: Mapping[str, tuple[LlmInputImageBlock, LocalImageLlmFileInput]]


NO_MEDIA = ItemMedia(images={})


def load_item_media(*, user_id: str, items: Sequence[TurnChatItem]) -> ItemMedia:
    """Read what ``items`` show; it reads files and the database."""

    images: dict[str, tuple[LlmInputImageBlock, LocalImageLlmFileInput]] = {}
    for entry in items:
        content = entry.item.content
        if not isinstance(content, UserMessageContent):
            continue
        for image in content.images:
            blob = read_local_image_blob(
                user_id=user_id, storage_path=image.storage_path
            )
            if blob is None:
                continue
            sha256 = hashlib.sha256(blob.payload).hexdigest()
            attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
            file_input: LocalImageLlmFileInput = {
                "ref": f"{USER_ATTACHMENT_REF_PREFIX}{attachment_id}",
                "blob_ref": f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
                "mime_type": blob.mime_type,
                "byte_size": len(blob.payload),
                "sha256": sha256,
                "source_kind": "local_image_blob",
                "storage_path": blob.storage_path,
            }
            block = LlmInputImageBlock(
                type="input_image",
                image=LlmImageDescriptor(
                    blob_ref=file_input["blob_ref"],
                    mime_type=file_input["mime_type"],
                    byte_size=file_input["byte_size"],
                    sha256=sha256,
                    application_ref=file_input["ref"],
                ),
            )
            images[image.storage_path] = (block, file_input)
    return ItemMedia(images=images)


__all__ = ["NO_MEDIA", "ItemMedia", "load_item_media"]

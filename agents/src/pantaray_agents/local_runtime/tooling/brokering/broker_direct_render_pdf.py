"""The Action's render tool: a document's pages, drawn, so the model can look.

``tools.files.render_pages`` draws them and decides which file may be opened.
What this module owns is where the Action keeps them: the converted PDF with
the Action, so the same file is converted once, and the images where the
conversation page and the next turn can both find them again. The bytes ride
the attachment path ``capture_screen`` puts a screenshot on, never the output,
which is durable history the memory tools read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pantaray_agents.local_runtime.runtime.local_image_store import (
    write_local_image_blob,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.documents import (
    RENDERED_PAGE_MEDIA_TYPE,
    RenderedPage,
)
from pantaray_agents.schema.action_conversation import RENDERER_PREPARING_OUTPUT_KIND
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files.attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
    TOOL_ATTACHMENT_REF_PREFIX,
)
from pantaray_agents.tools.files.read_target import (
    ReadTarget,
    action_reference_paths,
)
from pantaray_agents.tools.files.render_pages import (
    CONTINUE_WITH_READ,
    RendererPreparing,
    draw_document_pages,
)

from .action_path_policy import read_scope
from .broker_common import (
    BrokerContext,
    ensure_session_capabilities,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedRenderPdfPageRequest

# Under the Action's root, beside its workspace rather than in it, so deleting
# the conversation removes the converted PDFs and no tool of the model's can
# reach or replace one.
RENDERED_DOCUMENTS_DIRNAME: Final = "rendered_documents"


@dataclass(frozen=True, slots=True)
class _StoredPage:
    """One drawn page, once it has a name and a place to live."""

    number: int
    display_path: str
    ref: str
    blob_ref: str
    storage_path: str
    byte_size: int
    sha256: str


async def run_render_pdf_page_executor(
    *, context: BrokerContext, request: ValidatedRenderPdfPageRequest
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    drawn = await draw_document_pages(
        db_path=context.db_path,
        scope=read_scope(context),
        raw_path=request.path,
        pages=request.pages,
        # Under the Action's root, beside its workspace rather than in it.
        converted_dir=resolve_action_storage_paths(
            db_path=context.db_path,
            user_id=context.execution_session.user_id,
            # The render tool is offered to Action runs alone, which always
            # carry their Action.
            action_id=cast(str, context.execution_session.action_id),
        ).action_root
        / RENDERED_DOCUMENTS_DIRNAME,
    )
    if isinstance(drawn, RendererPreparing):
        return _preparing(drawn.target)
    target = drawn.target
    stored = [
        _store(user_id=context.execution_session.user_id, page=page)
        for page in drawn.pages
    ]
    output: dict[str, JSONValue] = {
        "kind": "pdf_pages",
        "path": target.display_path,
        # The refs are in the message as well as in the attachments: on the
        # string prompt path, the prompt text says where each image goes.
        "message": (
            f"Drew pages of {target.display_path}, which has "
            f"{drawn.page_count} pages. "
            + " ".join(f"Page {page.number}: {page.ref}" for page in stored)
        ),
        "page_count": drawn.page_count,
        # Durable history keeps this output, so an attachment names the image
        # by its user-scoped storage_path, never by path or by bytes.
        "attachments": [
            {
                "type": "file",
                "source_kind": "local_image_blob",
                "mime_type": RENDERED_PAGE_MEDIA_TYPE,
                "page_number": page.number,
                "path": page.display_path,
                "storage_path": page.storage_path,
                "ref": page.ref,
                "byte_size": page.byte_size,
            }
            for page in stored
        ],
    }
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output,
        search_text=None,
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
        attachments=tuple(
            {
                "type": "file",
                "source_kind": "local_image_blob",
                "ref": page.ref,
                "blob_ref": page.blob_ref,
                "display_path": page.display_path,
                "mime_type": RENDERED_PAGE_MEDIA_TYPE,
                "byte_size": page.byte_size,
                "sha256": page.sha256,
                "storage_path": page.storage_path,
            }
            for page in stored
        ),
    )


def _preparing(target: ReadTarget) -> UnprojectedBrokerToolOutcome:
    """A render that waits on the renderer's install: not a failure, no pages."""

    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "kind": RENDERER_PREPARING_OUTPUT_KIND,
            "path": target.display_path,
            "message": (
                f"Pages of {target.display_path} cannot be drawn yet: the "
                "viewer for this format is still being set up. "
                f"{CONTINUE_WITH_READ} Try drawing the pages again later if "
                "you still need them."
            ),
        },
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
    )


def _store(*, user_id: str, page: RenderedPage) -> _StoredPage:
    """Name one page and store it, as ``capture_screen`` does a screenshot."""

    sha256 = hashlib.sha256(page.payload).hexdigest()
    attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
    blob = write_local_image_blob(
        user_id=user_id, payload=page.payload, mime_type=RENDERED_PAGE_MEDIA_TYPE
    )
    return _StoredPage(
        number=page.number,
        display_path=(
            f"page-{page.number}-{attachment_id}{Path(blob.storage_path).suffix}"
        ),
        ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}",
        blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
        storage_path=blob.storage_path,
        byte_size=len(page.payload),
        sha256=sha256,
    )


__all__ = ["run_render_pdf_page_executor"]

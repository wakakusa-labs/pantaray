"""The Action's render tools: a document's pages, drawn, so the model can look.

``tools.files.render_pages`` draws them and decides which file may be opened.
What this module owns, for ``render_html_page`` as well, is where the Action
keeps them: the converted PDF with the Action, so the same file is converted
once, and the images where the conversation page and the next turn can both
find them again. The bytes ride
the attachment path ``capture_screen`` puts a screenshot on, never the output,
which is durable history the memory tools read.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from pantaray_agents.local_runtime.runtime.local_image_store import (
    write_local_image_blob,
)
from pantaray_agents.local_runtime.runtime.tool_image_attachments import (
    ToolImageAttachment,
    assemble_tool_image,
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


async def run_render_pdf_page_executor(
    *, context: BrokerContext, request: ValidatedRenderPdfPageRequest
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    drawn = await draw_document_pages(
        db_path=context.db_path,
        scope=read_scope(context),
        raw_path=request.path,
        pages=request.pages,
        converted_dir=rendered_documents_dir(context),
    )
    if isinstance(drawn, RendererPreparing):
        return _preparing(drawn.target)
    return rendered_pages_outcome(
        user_id=context.execution_session.user_id,
        target=drawn.target,
        pages=drawn.pages,
        message=(
            f"Drew pages of {drawn.target.display_path}, which has "
            f"{drawn.page_count} pages."
        ),
        output={
            "kind": "pdf_pages",
            "path": drawn.target.display_path,
            "page_count": drawn.page_count,
        },
    )


def rendered_documents_dir(context: BrokerContext) -> Path:
    """Under the Action's root, beside its workspace rather than in it."""

    return (
        resolve_action_storage_paths(
            db_path=context.db_path,
            user_id=context.execution_session.user_id,
            # The render tools are offered to Action runs alone, which always
            # carry their Action.
            action_id=cast(str, context.execution_session.action_id),
        ).action_root
        / RENDERED_DOCUMENTS_DIRNAME
    )


def rendered_pages_outcome(
    *,
    user_id: str,
    target: ReadTarget,
    pages: Sequence[RenderedPage],
    message: str,
    output: dict[str, JSONValue],
) -> UnprojectedBrokerToolOutcome:
    """Store a render's drawn pages and attach them to its ``output``."""

    stored = [_store(user_id=user_id, page=page) for page in pages]
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            **output,
            # The refs are in the message as well as in the attachments: on the
            # string prompt path, the prompt text says where each image goes.
            "message": " ".join(
                [
                    message,
                    *(
                        f"Page {page.number}: {image.ref}"
                        for page, image in zip(pages, stored, strict=True)
                    ),
                ]
            ),
            "attachments": [image.output_entry for image in stored],
        },
        search_text=None,
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
        attachments=tuple(image.attachment for image in stored),
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


def _store(*, user_id: str, page: RenderedPage) -> ToolImageAttachment:
    """Store one page and name it, as ``capture_screen`` does a screenshot."""

    blob = write_local_image_blob(
        user_id=user_id, payload=page.payload, mime_type=RENDERED_PAGE_MEDIA_TYPE
    )
    suffix = Path(blob.storage_path).suffix
    return assemble_tool_image(
        blob,
        display_name=lambda image_id: f"page-{page.number}-{image_id}{suffix}",
        output_fields={"page_number": page.number},
    )


__all__ = [
    "rendered_documents_dir",
    "rendered_pages_outcome",
    "run_render_pdf_page_executor",
]

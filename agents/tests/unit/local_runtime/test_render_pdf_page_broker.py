"""`render_pdf_page`: a PDF's pages, drawn and handed to the model as images.

Drawing belongs to `documents.page_render`, whose own tests run the real child
process. What is checked here is what the broker owns around it: which file it
opens, which interpreter it starts, where the images go, how they reach the
model, and how the renderer's declared failures are reported.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.agents.action_agent.tools import (
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
)
from pantaray_agents.local_runtime.runtime.local_image_store import (
    read_local_image_blob,
)
from pantaray_agents.local_runtime.tooling.brokering import broker_direct_render_pdf
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_direct_read_document import (
    READ_DOCUMENT_TOO_LARGE,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderPdfPageOutput,
)
from pantaray_agents.local_runtime.tooling.documents import (
    DocumentExtractionError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    PageOutOfRangeError,
    PageRenderTimeoutError,
    RenderedPage,
    RenderedPages,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    _CHILD_BROKER_TOOLS,
)

from .broker_test_support import BROKER_ACTOR_PROCESS_ID
from .read_tool_broker_support import ReadRuntimeContext, bootstrap_read_runtime_db
from .test_pdf_page_render import write_pdf

_USER_ID = "user-1"
_TOOL_ID = "render_pdf_page"
# Whatever the renderer draws is what the broker has to store and hash.
_WEBP = b"RIFF--WEBP--drawn-page"


async def render_pages(*, db_path: Path, context: ReadRuntimeContext, args: dict):
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id=_TOOL_ID,
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args=args,
    )


def stub_renderer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: tuple[RenderedPage, ...] = (),
    page_count: int = 3,
    error: Exception | None = None,
) -> dict[str, object]:
    asked: dict[str, object] = {}

    async def fake_render_pdf_pages(
        *,
        pdf_path: Path,
        pages: list[int],
        python_executable: Path,
        _drawn: tuple[RenderedPage, ...] = pages,
        _error: Exception | None = error,
    ) -> RenderedPages:
        asked.update(
            pdf_path=pdf_path, pages=list(pages), python_executable=python_executable
        )
        if _error is not None:
            raise _error
        return RenderedPages(page_count=page_count, pages=_drawn)

    monkeypatch.setattr(
        broker_direct_render_pdf, "render_pdf_pages", fake_render_pdf_pages
    )
    return asked


async def test_render_returns_each_page_as_an_image_stored_for_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    pdf_path = context.workspace_path / "report.pdf"
    write_pdf(pdf_path, marks=[(1, 0, 0), (0, 1, 0), (0, 0, 1)])
    asked = stub_renderer(
        monkeypatch,
        pages=(
            RenderedPage(number=3, width_px=1109, height_px=1568, payload=_WEBP),
            RenderedPage(number=1, width_px=1109, height_px=1568, payload=_WEBP + b"0"),
        ),
    )

    outcome = await render_pages(
        db_path=db_path,
        context=context,
        args={"path": "report.pdf", "pages": [3, 1]},
    )

    # The renderer is handed the resolved real path its sandbox admits, the
    # pages as asked, and the interpreter this process itself runs on.
    assert asked["pdf_path"] == pdf_path
    assert asked["pages"] == [3, 1]
    assert asked["python_executable"] == Path(sys.executable)

    assert outcome.status == "success"
    parsed = RenderPdfPageOutput.model_validate(outcome.output).root
    assert parsed.kind == "pdf_pages"
    assert parsed.path == str(pdf_path)
    assert parsed.page_count == 3
    # The pages asked for, in that order, each named in the message the string
    # prompt path reads.
    assert [page.page_number for page in parsed.attachments] == [3, 1]
    for page in parsed.attachments:
        assert page.ref in parsed.message

    for attachment, declared in zip(
        outcome.attachments, parsed.attachments, strict=True
    ):
        assert attachment["source_kind"] == "local_image_blob"
        assert attachment["mime_type"] == "image/webp"
        assert attachment["storage_path"] == declared.storage_path
        blob = read_local_image_blob(
            user_id=_USER_ID, storage_path=declared.storage_path
        )
        assert blob is not None and len(blob.payload) == declared.byte_size
        digest = hashlib.sha256(blob.payload).hexdigest()
        assert digest == attachment["sha256"]
        assert cast(str, declared.ref).endswith(digest[:24])


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PageOutOfRangeError(number=5, total_units=2), "RENDER_PAGE_OUT_OF_RANGE"),
        (EncryptedDocumentError("needs a password"), "RENDER_DOCUMENT_ENCRYPTED"),
        (PageRenderTimeoutError("did not finish"), "RENDER_TIMED_OUT"),
        (DocumentTooLargeError("larger than 20971520 bytes"), READ_DOCUMENT_TOO_LARGE),
        (DocumentExtractionError("killed by signal 9"), "RENDER_DOCUMENT_UNREADABLE"),
    ],
    ids=["past-the-end", "protected", "timed-out", "too-large", "unreadable"],
)
async def test_render_reports_each_way_the_renderer_can_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    write_pdf(context.workspace_path / "report.pdf", marks=[(0, 0, 0), (0, 0, 0)])
    stub_renderer(monkeypatch, error=error)

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(
            db_path=db_path,
            context=context,
            args={"path": "report.pdf", "pages": [1]},
        )

    assert caught.value.code == code
    # A page past the end is the one refusal the caller can act on directly, so
    # it has to say how long the document actually is.
    if code == "RENDER_PAGE_OUT_OF_RANGE":
        assert "2 pages" in str(caught.value)
        assert "1 and 2" in (caught.value.fix_hint or "")


@pytest.mark.parametrize(
    "subject", ["notebook", "legacy-word", "outside-the-readable-roots"]
)
async def test_render_refuses_before_it_starts_a_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    """Only a format it can draw, and only a file the read policy already allows."""

    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    if subject == "notebook":
        (context.workspace_path / "run.ipynb").write_text('{"cells": []}')
        requested, expected = "run.ipynb", {"RENDER_FORMAT_UNSUPPORTED"}
    elif subject == "legacy-word":
        (context.workspace_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0" * 8)
        requested, expected = "old.doc", {"RENDER_FORMAT_UNSUPPORTED"}
    else:
        outside = tmp_path / "outside.pdf"
        write_pdf(outside, marks=[(0, 0, 0)])
        requested = str(outside)
        expected = {"READ_PATH_DENIED", "READ_SCOPE_DENIED"}
    asked = stub_renderer(monkeypatch)

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(
            db_path=db_path, context=context, args={"path": requested, "pages": [1]}
        )

    assert caught.value.code in expected
    assert asked == {}


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param([1, 2, 3, 4, 5, 6, 7, 8, 9], id="more-than-one-call-may-draw"),
        pytest.param([2, 2], id="repeated-page"),
        pytest.param([0], id="page-zero"),
        pytest.param([], id="no-pages"),
    ],
)
async def test_render_rejects_page_lists_it_will_not_draw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pages: list[int]
) -> None:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    write_pdf(context.workspace_path / "one.pdf", marks=[(0, 0, 0)])

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(
            db_path=db_path, context=context, args={"path": "one.pdf", "pages": pages}
        )

    assert caught.value.code == "BROKER_TOOL_ARGS_INVALID"


def test_render_is_offered_to_the_supervisor_and_withheld_from_subagents() -> None:
    """A subagent's results carry no attachments, so this tool could only fail there."""

    assert _TOOL_ID in SUPERVISOR_SINGLE_REACT_TOOL_IDS
    assert _TOOL_ID not in {tool.tool_id for tool in _CHILD_BROKER_TOOLS}

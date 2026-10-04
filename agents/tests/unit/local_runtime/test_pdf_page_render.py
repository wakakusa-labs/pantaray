"""Drawing PDF pages in a process the helper can kill and can afford to lose."""

from __future__ import annotations

import json
import re
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import ContentStream, DictionaryObject, NameObject

from pantaray_agents.local_runtime.tooling.documents.document_model import (
    DocumentExtractionError,
    EncryptedDocumentError,
)
from pantaray_agents.local_runtime.tooling.documents.page_render import (
    MAX_PAGE_RENDER_SECONDS,
    RENDERED_PAGE_LONG_EDGE_PX,
    PageOutOfRangeError,
    PageRenderTimeoutError,
    RenderedPage,
    RenderedPages,
    render_pdf_pages,
)

# pypdf writes page objects but draws nothing, so a page that has to come back
# as visible ink carries a content stream written here: a filled rectangle in
# the given colour, placed by its lower-left corner in PDF points.
_MARK_WIDTH_PT = 120
_MARK_HEIGHT_PT = 80
_A4_PORTRAIT_PT = (595, 842)
_A4_LANDSCAPE_PT = (842, 595)
# A JPEG-like encoder smears an edge, so a pixel is called ink only when it is
# far enough from white that no amount of ringing explains it.
_INK_THRESHOLD = 200


def write_pdf(
    path: Path,
    *,
    marks: list[tuple[float, float, float]],
    size_pt: tuple[int, int] = _A4_PORTRAIT_PT,
    user_password: str | None = None,
) -> None:
    """One page per mark, each drawn as a filled rectangle of that colour."""

    writer = PdfWriter()
    for red, green, blue in marks:
        page = writer.add_blank_page(width=size_pt[0], height=size_pt[1])
        stream = ContentStream(None, None)
        stream.set_data(
            f"{red} {green} {blue} rg 40 40 {_MARK_WIDTH_PT} {_MARK_HEIGHT_PT} re f".encode()
        )
        page[NameObject("/Resources")] = DictionaryObject()
        page.replace_contents(stream)
    if user_password is not None:
        writer.encrypt(
            user_password=user_password,
            owner_password="owner-secret",
            algorithm="AES-256",
        )
    writer.write(path)


def ink_at_bottom_left(page: RenderedPage) -> tuple[int, int, int]:
    """The colour drawn inside the mark, read back out of the encoded image.

    PDF measures from the bottom left and an image from the top left, so the
    mark that was placed at the bottom of the page is read near the bottom of
    the raster.
    """

    image = Image.open(BytesIO(page.payload)).convert("RGB")
    assert (image.width, image.height) == (page.width_px, page.height_px)
    return image.getpixel((image.width // 12, image.height - image.height // 12))


async def render(
    path: Path,
    pages: list[int],
    timeout_seconds: float = MAX_PAGE_RENDER_SECONDS,
) -> RenderedPages:
    return await render_pdf_pages(
        pdf_path=path,
        pages=pages,
        python_executable=Path(sys.executable),
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize("size_pt", [_A4_PORTRAIT_PT, _A4_LANDSCAPE_PT])
async def test_a_page_is_drawn_to_the_long_edge_whichever_way_it_is_turned(
    tmp_path: Path, size_pt: tuple[int, int]
) -> None:
    path = tmp_path / "page.pdf"
    write_pdf(path, marks=[(0, 0, 1)], size_pt=size_pt)

    (page,) = (await render(path, [1])).pages

    assert max(page.width_px, page.height_px) == RENDERED_PAGE_LONG_EDGE_PX
    assert page.width_px / page.height_px == pytest.approx(
        size_pt[0] / size_pt[1], abs=0.01
    )
    red, green, blue = ink_at_bottom_left(page)
    assert (red, green) < (_INK_THRESHOLD, _INK_THRESHOLD) < (blue, 255)


async def test_pages_come_back_in_the_order_they_were_asked_for(tmp_path: Path) -> None:
    """The caller pairs an image with a page number by position, not by search."""

    path = tmp_path / "three.pdf"
    write_pdf(path, marks=[(1, 0, 0), (0, 1, 0), (0, 0, 1)])

    drawn = await render(path, [3, 1])

    # The whole document's length, which a laid-out Word file has nowhere else.
    assert drawn.page_count == 3
    assert [page.number for page in drawn.pages] == [3, 1]
    blue, red = (ink_at_bottom_left(page) for page in drawn.pages)
    assert blue[2] > _INK_THRESHOLD > blue[0]
    assert red[0] > _INK_THRESHOLD > red[2]


async def test_a_page_past_the_end_says_how_long_the_document_is(
    tmp_path: Path,
) -> None:
    path = tmp_path / "two.pdf"
    write_pdf(path, marks=[(0, 0, 0), (0, 0, 0)])

    with pytest.raises(PageOutOfRangeError) as caught:
        await render(path, [1, 5])

    assert caught.value.total_units == 2


async def test_a_pdf_that_needs_a_password_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "sealed.pdf"
    write_pdf(path, marks=[(0, 0, 0)], user_password="letmein")

    with pytest.raises(EncryptedDocumentError):
        await render(path, [1])


async def test_a_pdf_that_only_restricts_editing_is_drawn(tmp_path: Path) -> None:
    """An owner password leaves the user password empty, so the file still opens."""

    path = tmp_path / "restricted.pdf"
    write_pdf(path, marks=[(0, 0, 1)], user_password="")

    (page,) = (await render(path, [1])).pages

    assert ink_at_bottom_left(page)[2] > _INK_THRESHOLD


async def test_bytes_that_are_not_a_pdf_are_reported_as_unreadable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notes.pdf"
    path.write_bytes(b"plain notes\n" * 100)

    with pytest.raises(DocumentExtractionError):
        await render(path, [1])


async def test_a_renderer_that_runs_past_its_time_is_killed(tmp_path: Path) -> None:
    """A tool call has no deadline of its own, so this one has to end itself."""

    path = tmp_path / "page.pdf"
    write_pdf(path, marks=[(0, 0, 0)])

    with pytest.raises(PageRenderTimeoutError):
        await render(path, [1], timeout_seconds=0.001)


async def test_a_renderer_that_cannot_start_fails_with_a_type_rather_than_a_hang(
    tmp_path: Path,
) -> None:
    """A worker that dies before it replies -- a bad runtime, a denied exec."""

    path = tmp_path / "page.pdf"
    write_pdf(path, marks=[(0, 0, 0)])

    with pytest.raises(DocumentExtractionError, match="exited with status"):
        await render_pdf_pages(
            pdf_path=path, pages=[1], python_executable=Path("/usr/bin/false")
        )


async def test_a_renderer_that_declares_more_bytes_than_it_sends_is_not_believed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker is the process holding untrusted input; its header is checked."""

    header = {
        "status": "rendered",
        "total_units": 1,
        "pages": [{"number": 1, "width_px": 8, "height_px": 8, "byte_size": 4096}],
    }
    liar = tmp_path / "liar.py"
    liar.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({json.dumps(header).encode('utf-8')!r} + b'\\n')\n"
        "sys.stdout.buffer.write(b'short')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.documents.page_render.WORKER_SCRIPT",
        liar,
    )
    path = tmp_path / "page.pdf"
    write_pdf(path, marks=[(0, 0, 0)])

    with pytest.raises(DocumentExtractionError, match="5 image bytes of 4096"):
        await render(path, [1])


def test_nothing_the_helper_imports_pulls_in_pypdfium2() -> None:
    """PDFium initialises at import and stays resident, and is not thread-safe.

    Keeping it out of every module the helper loads is what makes the worker
    the only process that ever holds it, so this is checked rather than trusted
    to review: one stray import would put a C++ PDF parser back in the process
    that serves the local API and every agent.
    """

    source_root = Path(__file__).resolve().parents[3] / "src"
    worker = (
        source_root
        / "pantaray_agents/local_runtime/tooling/documents/pdf_render_worker.py"
    )
    assert worker.is_file()
    imports_pypdfium2 = re.compile(r"^\s*(?:import|from)\s+pypdfium2", re.MULTILINE)
    importers = sorted(
        str(module.relative_to(source_root))
        for module in source_root.rglob("*.py")
        if module != worker
        and imports_pypdfium2.search(module.read_text(encoding="utf-8"))
    )

    assert importers == []

"""Office pages drawn end to end through the broker, with the LibreOffice on this Mac.

Opt-in under the same condition as test_office_convert_libreoffice, whose
generated files it reuses: discovery, conversion under the verified profile,
the kept PDF and the page renderer all run for real. Only conversions that
succeed are run here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from tests.unit.local_runtime.read_tool_broker_support import (
    bootstrap_read_runtime_db,
)
from tests.unit.local_runtime.test_render_pdf_page_broker import render_pages

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderPdfPageOutput,
)

from . import test_office_convert_libreoffice as conversion
from .test_office_convert_libreoffice import write_pptx, write_xlsx

pytestmark = conversion.pytestmark


@pytest.mark.parametrize(
    ("name", "write", "pages", "page_count"),
    [
        # The hidden slide is page 2, so the last slide is still page 3.
        ("deck.pptx", write_pptx, [1, 3], 3),
        # Data, Hidden, Empty, Chart: sheet N is page N.
        ("book.xlsx", write_xlsx, [1, 4], 4),
    ],
)
async def test_an_office_file_is_drawn_through_the_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    write: Callable[[Path], None],
    pages: list[int],
    page_count: int,
) -> None:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    write(context.workspace_path / name)

    outcome = await render_pages(
        db_path=db_path, context=context, args={"path": name, "pages": pages}
    )

    parsed = RenderPdfPageOutput.model_validate(outcome.output).root
    assert parsed.kind == "pdf_pages"
    assert parsed.page_count == page_count
    assert [page.page_number for page in parsed.attachments] == pages
    assert all(page.byte_size > 0 for page in parsed.attachments)

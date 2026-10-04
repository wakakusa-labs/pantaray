"""A directory swapped for a link between the read's check and its open.

A command left running in the workspace can replace a directory there with a
link to anywhere, in the moment after the broker checked a path and before it
opens it. Each test makes that swap right after the check returns and expects
the read to fail rather than return what the link points at.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pantaray_agents.local_runtime.tooling.brokering import (
    broker_direct_read,
    broker_direct_render_pdf,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import BrokerPolicyError
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    app_owned_roots,
)

from .read_tool_broker_support import bootstrap_read_runtime_db, execute_read_tool
from .test_read_document_broker import write_sample_docx, write_sample_pptx
from .test_render_office_pages_broker import (
    ready_runtime,
    stub_converter,
    use_runtime,
)
from .test_render_pdf_page_broker import render_pages

_SENTINEL = "OUTSIDE SENTINEL"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _write_tree(directory: Path, *, text: str) -> None:
    inner = directory / "inner"
    inner.mkdir(parents=True)
    (inner / "notes.txt").write_text(f"{text}\n", encoding="utf-8")
    (inner / f"{text.replace(' ', '-')}.txt").write_text("x\n", encoding="utf-8")
    (inner / "pixel.png").write_bytes(_PNG + text.encode())
    write_sample_docx(inner / "report.docx")
    write_sample_pptx(inner / "deck.pptx")


def _swap_after_check(
    monkeypatch: pytest.MonkeyPatch, module: Any, *, swapped: Path, outside: Path
) -> list[bool]:
    """Replace ``swapped`` with a link to ``outside`` once the path is checked."""

    check: Callable[..., Any] = module.resolve_read_target
    swaps: list[bool] = []

    def check_then_swap(**kwargs: Any) -> Any:
        target = check(**kwargs)
        swapped.rename(swapped.with_name(f"{swapped.name}-moved"))
        swapped.symlink_to(outside, target_is_directory=True)
        swaps.append(True)
        return target

    monkeypatch.setattr(module, "resolve_read_target", check_then_swap)
    return swaps


@pytest.mark.parametrize(
    "path",
    [
        "sub/inner/notes.txt",
        "sub/inner/pixel.png",
        "sub/inner/report.docx",
        "sub/inner",
    ],
    ids=["text", "image", "document", "directory"],
)
async def test_workspace_read_does_not_follow_a_swapped_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    _write_tree(context.workspace_path / "sub", text="inside")
    outside = tmp_path / "outside"
    _write_tree(outside, text=_SENTINEL)
    swaps = _swap_after_check(
        monkeypatch,
        broker_direct_read,
        swapped=context.workspace_path / "sub",
        outside=outside,
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(db_path=db_path, context=context, args={"path": path})

    assert swaps == [True]
    assert _SENTINEL not in str(exc_info.value)


async def test_full_access_read_cannot_reach_app_storage_through_a_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = bootstrap_read_runtime_db(
        tmp_path, read_access_scope="full_access"
    )
    user_folder = tmp_path / "user-files"
    _write_tree(user_folder / "sub", text="inside")
    private = app_owned_roots(db_path)[0] / "private"
    _write_tree(private, text=_SENTINEL)
    swaps = _swap_after_check(
        monkeypatch,
        broker_direct_read,
        swapped=user_folder / "sub",
        outside=private,
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_read_tool(
            db_path=db_path,
            context=context,
            args={"path": str(user_folder / "sub" / "inner" / "notes.txt")},
        )

    assert swaps == [True]
    assert _SENTINEL not in str(exc_info.value)


async def test_office_render_does_not_copy_through_a_swapped_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    _write_tree(context.workspace_path / "sub", text="inside")
    outside = tmp_path / "outside"
    _write_tree(outside, text=_SENTINEL)
    use_runtime(monkeypatch, ready_runtime(tmp_path))
    conversions = stub_converter(monkeypatch)
    swaps = _swap_after_check(
        monkeypatch,
        broker_direct_render_pdf,
        swapped=context.workspace_path / "sub",
        outside=outside,
    )

    with pytest.raises(BrokerPolicyError):
        await render_pages(
            db_path=db_path,
            context=context,
            args={"path": "sub/inner/deck.pptx", "pages": [1]},
        )

    assert swaps == [True]
    assert conversions == []

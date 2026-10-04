from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    TextFileError,
    TextFileErrorCode,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    MemoryDraftToolSession,
    ReadableFileRoot,
    bounded_workspace_io,
    build_local_memory_file_tools,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor.bounded_workspace_io import (
    SearchTruncationReason,
    read_text_page,
    search_text,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MemoryContextSession,
)


@pytest.mark.asyncio
async def test_read_file_returns_bounded_pages(tmp_path: Path) -> None:
    tools = _build_tools(
        path="large.txt",
        text="".join(f"line {index}\n" for index in range(1, 1_001)),
    )
    read_tool = next(tool for tool in tools if tool.name == "read_file")

    first = await read_tool.execute(
        _tool_call(
            "read_file",
            {"root": "memory_draft", "path": "large.txt", "limit": 10},
        ),
        1,
    )
    second = await read_tool.execute(
        _tool_call(
            "read_file",
            {
                "root": "memory_draft",
                "path": "large.txt",
                "offset": first.output["next_offset"],
                "limit": 10,
            },
        ),
        2,
    )

    assert read_tool.request_schema["properties"]["offset"]["minimum"] == 1
    assert first.output["text"].splitlines() == [
        f"line {index}" for index in range(1, 11)
    ]
    assert first.output["next_offset"] == 11
    assert first.output["truncated"] is True
    assert first.output["truncation_reason"] == "page_limit"
    assert second.output["offset"] == 11
    assert second.output["text"].splitlines()[0] == "line 11"


@pytest.mark.asyncio
async def test_read_file_continues_long_line_from_exact_column(tmp_path: Path) -> None:
    tools = _build_tools(
        path="long.txt",
        text="x" * 2_100 + "\n",
    )
    read_tool = next(tool for tool in tools if tool.name == "read_file")

    first = await read_tool.execute(
        _tool_call(
            "read_file",
            {"root": "memory_draft", "path": "long.txt", "limit": 1},
        ),
        1,
    )
    second = await read_tool.execute(
        _tool_call(
            "read_file",
            {
                "root": "memory_draft",
                "path": "long.txt",
                "offset": first.output["next_offset"],
                "column": first.output["next_column"],
                "limit": 1,
            },
        ),
        2,
    )

    assert first.output["text"] == "x" * 2_000
    assert first.output["next_offset"] == 1
    assert first.output["next_column"] == 2_001
    assert second.output["text"] == "x" * 100 + "\n"
    assert second.output["next_offset"] is None


def test_search_stops_at_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text("no match\n", encoding="utf-8")
    monkeypatch.setattr(bounded_workspace_io, "SEARCH_MAX_ENTRIES", 2)

    result = search_text(
        sandbox_root=tmp_path,
        canonical_sandbox_root=None,
        query="missing",
        limit=20,
        match_paths=False,
    )

    assert result.truncation_reason == SearchTruncationReason.ENTRY_LIMIT


def test_search_does_not_follow_file_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("private-token", encoding="utf-8")
    (workspace / "secret-link.md").symlink_to(outside / "secret.md")

    result = search_text(
        sandbox_root=workspace,
        canonical_sandbox_root=None,
        query="private-token",
        limit=20,
        match_paths=False,
    )

    assert result.matches == ()


def _swap_tree(tmp_path: Path) -> tuple[Path, Callable[[str], None]]:
    """A workspace and a swap of one of its entries for a link outside it."""

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "sub").mkdir(parents=True)
    (outside / "sub").mkdir(parents=True)
    (workspace / "candidate.md").write_text("public", encoding="utf-8")
    (workspace / "sub" / "candidate.md").write_text("public", encoding="utf-8")
    (outside / "candidate.md").write_text("private-token", encoding="utf-8")
    (outside / "sub" / "candidate.md").write_text("private-token", encoding="utf-8")

    def swap(name: str) -> None:
        entry = workspace / name
        entry.rename(workspace / f"{name}-moved")
        entry.symlink_to(outside / name, target_is_directory=name == "sub")

    return workspace, swap


@pytest.mark.parametrize("swapped", ["candidate.md", "sub"])
def test_search_does_not_follow_an_entry_swapped_after_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str
) -> None:
    workspace, swap = _swap_tree(tmp_path)
    real_scandir = os.scandir
    swaps: list[str] = []

    def list_then_swap(target: Any) -> Any:
        with real_scandir(target) as iterator:
            listing = list(iterator)
        if not swaps:
            swap(swapped)
            swaps.append(swapped)
        return nullcontext(iter(listing))

    monkeypatch.setattr(bounded_workspace_io.os, "scandir", list_then_swap)

    result = search_text(
        sandbox_root=workspace,
        canonical_sandbox_root=None,
        query="private-token",
        limit=20,
        match_paths=False,
    )

    assert swaps == [swapped]
    assert result.matches == ()
    assert result.skipped_files == 1


@pytest.mark.parametrize(
    ("swapped", "path"),
    [("candidate.md", "candidate.md"), ("sub", "sub/candidate.md")],
)
def test_read_page_does_not_follow_an_entry_swapped_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swapped: str, path: str
) -> None:
    workspace, swap = _swap_tree(tmp_path)
    check = bounded_workspace_io.resolve_sandbox_path

    def check_then_swap(**kwargs: Any) -> Any:
        resolved = check(**kwargs)
        swap(swapped)
        return resolved

    monkeypatch.setattr(bounded_workspace_io, "resolve_sandbox_path", check_then_swap)

    with pytest.raises(TextFileError) as exc_info:
        read_text_page(
            sandbox_root=workspace,
            canonical_sandbox_root=None,
            path=path,
            offset=1,
            column=1,
            limit=20,
        )

    assert exc_info.value.code == TextFileErrorCode.IO_FAILED
    assert "private-token" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_file_converts_oserror_to_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "facts.md").write_text("content\n", encoding="utf-8")

    def fail_read(**_kwargs: object) -> None:
        raise OSError("temporary read failure")

    monkeypatch.setattr(bounded_workspace_io, "read_text_descriptor_lines", fail_read)
    policy = EditablePathPolicy(("draft.md",))
    session = _memory_session(path="draft.md", text="draft\n", policy=policy)
    tools = build_local_memory_file_tools(
        editable_policy=policy,
        memory_session=session,
        readable_roots=(
            ReadableFileRoot("workspace_1", "workspace", tmp_path, tmp_path.resolve()),
        ),
    )
    read_tool = next(tool for tool in tools if tool.name == "read_file")

    result = await read_tool.execute(
        _tool_call("read_file", {"root": "workspace_1", "path": "facts.md"}),
        1,
    )

    assert result.status == "error"
    assert result.output["error_code"] == "IO_FAILED"
    assert "temporary read failure" in result.output["message"]


def _build_tools(*, path: str, text: str) -> tuple[object, ...]:
    policy = EditablePathPolicy((path,))
    session = _memory_session(path=path, text=text, policy=policy)
    return build_local_memory_file_tools(
        editable_policy=policy,
        memory_session=session,
    )


def _memory_session(
    *, path: str, text: str, policy: EditablePathPolicy
) -> MemoryDraftToolSession:
    return MemoryDraftToolSession(
        editable_policy=policy,
        draft=create_memory_draft(
            user_id="user-1",
            owner_node_id="fact-1",
            base_revision_id=None,
            documents=(MemoryDocument(path, text),),
        ),
        memory_context=MemoryContextSession(
            user_id="user-1", run_id="run-1", epoch=None
        ),
    )


def _tool_call(tool_name: str, args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id=tool_name,
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_memory_draft_late_page_is_readable_without_splitting() -> None:
    line = "かな" * 100 + "\n"
    tools = _build_tools(path="facts.md", text=line * 1_500 + "LAST MARKER")
    read = next(tool for tool in tools if tool.name == "read_file")
    result = await read.execute(
        _tool_call(
            "read_file",
            {"root": "memory_draft", "path": "facts.md", "offset": 1_501, "limit": 2},
        ),
        1,
    )
    assert result.status == "success"
    assert result.output["text"] == "LAST MARKER"
    assert result.output["next_offset"] is None

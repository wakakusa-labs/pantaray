from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pantaray_agents.schema.read_access import ReadAccessScope
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
)
from pantaray_agents.tools.files import ripgrep
from pantaray_agents.tools.files.read_only_tools import (
    READ_IMAGE_NOT_SUPPORTED,
    build_read_only_file_tools,
)

from .test_read_document_broker import PIXEL_PNG, write_sample_docx


def _tools(
    tmp_path: Path, *, read_access_scope: ReadAccessScope = "workspace"
) -> tuple[ReactToolRegistry, Path, Path, Path]:
    """A registered folder that encloses the app's storage, as a home folder can."""

    folder = tmp_path / "home"
    storage = folder / "app-data"
    storage.mkdir(parents=True)
    (storage / "pantaray.db").write_text("app-storage-secret", encoding="utf-8")
    spill_root = storage / "suggestion_tool_results" / "run-1"
    definitions = build_read_only_file_tools(
        folders=(folder.resolve(),),
        read_access_scope=read_access_scope,
        app_storage_roots=(storage.resolve(),),
        spill_root=spill_root,
    )
    return ReactToolRegistry(definitions), folder.resolve(), storage, spill_root


async def _call(
    registry: ReactToolRegistry, name: str, args: dict[str, object]
) -> ReactToolResult:
    envelope = ToolCallEnvelope(tool_id=name, reason=None, args=args)  # type: ignore[arg-type]
    return await registry.execute(
        ReactToolCall(tool_name=name, tool_args=args, tool_call_envelope=envelope),  # type: ignore[arg-type]
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("read", {"path": "insights/todos.md"}),
        ("grep", {"base_path": ".", "pattern": "TODO"}),
    ],
)
async def test_a_relative_path_is_refused_and_points_to_memory_tools(
    tmp_path: Path, name: str, args: dict[str, object]
) -> None:
    registry, _folder, _storage, _spill = _tools(tmp_path)

    result = await _call(registry, name, args)

    assert result.status == "error"
    assert result.output["error_code"] == "PATH_NOT_ABSOLUTE"
    fix_hint = result.output["details"]["fix_hint"]
    assert "absolute" in fix_hint
    assert "memory_search" in fix_hint


@pytest.mark.asyncio
async def test_app_storage_inside_a_registered_folder_stays_hidden(
    tmp_path: Path,
) -> None:
    registry, folder, storage, _spill = _tools(tmp_path)
    (folder / "notes.md").write_text("plan\n", encoding="utf-8")

    read = await _call(registry, "read", {"path": str(storage / "pantaray.db")})
    listed = await _call(registry, "list", {"path": str(folder), "max_depth": 3})

    assert read.status == "error"
    assert read.output["error_code"] == "READ_PATH_DENIED"
    # Only the run's own spill folder is reachable inside the storage.
    assert sorted(entry["name"] for entry in listed.output["entries"]) == [
        "notes.md",
        "run-1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_access_scope", "expected"),
    [("workspace", "error"), ("full_access", "success")],
)
async def test_a_path_outside_the_folders_follows_the_read_access_setting(
    tmp_path: Path, read_access_scope: ReadAccessScope, expected: str
) -> None:
    registry, _folder, _storage, _spill = _tools(
        tmp_path, read_access_scope=read_access_scope
    )
    outside = tmp_path / "elsewhere.md"
    outside.write_text("outside\n", encoding="utf-8")

    result = await _call(registry, "read", {"path": str(outside)})

    assert result.status == expected
    if expected == "error":
        assert result.output["error_code"] == "READ_SCOPE_DENIED"
    else:
        assert result.output["content"] == "outside\n"


@pytest.mark.asyncio
async def test_a_large_result_is_spilled_to_a_file_the_tools_can_read(
    tmp_path: Path,
) -> None:
    registry, folder, _storage, spill_root = _tools(tmp_path)
    for index in range(400):
        (folder / f"document-with-a-long-name-{index:04}.md").write_text(
            "x", encoding="utf-8"
        )

    listed = await _call(registry, "list", {"path": str(folder), "limit": 500})

    spilled = listed.output
    assert listed.status == "success"
    assert spilled["storage"] == "action_file"
    spill_path = Path(spilled["path"])
    assert spill_path.is_relative_to(spill_root.resolve())
    assert str(spill_path) in spilled["retry_hint"]
    read = await _call(registry, "read", {"path": str(spill_path), "offset": 2})
    assert read.status == "success"
    assert "document-with-a-long-name-0000.md" in read.output["content"]


@pytest.mark.asyncio
async def test_a_document_in_a_registered_folder_is_read_as_text(
    tmp_path: Path,
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    write_sample_docx(folder / "review.docx")

    result = await _call(registry, "read", {"path": str(folder / "review.docx")})

    assert result.status == "success"
    assert result.output["kind"] == "document"
    assert "# Quarterly review" in result.output["content"]
    assert "| Region | Revenue |" in result.output["content"]


@pytest.mark.asyncio
async def test_an_image_is_refused_instead_of_reported_as_read(
    tmp_path: Path,
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    (folder / "chart.png").write_bytes(PIXEL_PNG)

    result = await _call(registry, "read", {"path": str(folder / "chart.png")})

    assert result.status == "error"
    assert result.output["error_code"] == READ_IMAGE_NOT_SUPPORTED
    assert str(folder / "chart.png") in result.output["message"]


@pytest.mark.asyncio
async def test_a_text_file_in_an_unknown_encoding_fails_only_its_own_call(
    tmp_path: Path,
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    (folder / "sales.csv").write_bytes(b"region,total\n\x81 east,120\n")

    result = await _call(registry, "read", {"path": str(folder / "sales.csv")})

    assert result.status == "error"
    assert result.output["error_code"] == "READ_TEXT_ENCODING_UNSUPPORTED"


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="grep runs ripgrep under sandbox-exec",
)
async def test_grep_does_not_follow_a_swapped_link_out_of_scope_for_an_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    line = "日本語 needle\n".encode("cp932")
    matched = folder / "notes.txt"
    matched.write_bytes(line)
    outside = tmp_path / "outside.txt"
    # Read whole, this file would decide CP932 and show the line as Japanese.
    outside.write_bytes(line)
    run_ripgrep_lines = ripgrep._run_ripgrep_lines

    def swap_after_search(**kwargs: object) -> ripgrep.RipgrepRunResult:
        result = run_ripgrep_lines(**kwargs)  # type: ignore[arg-type]
        matched.unlink()
        matched.symlink_to(outside)
        return result

    monkeypatch.setattr(ripgrep, "_run_ripgrep_lines", swap_after_search)

    result = await _call(
        registry, "grep", {"base_path": str(folder), "pattern": "needle"}
    )

    assert [match["line"] for match in result.output["matches"]] == [
        line.decode("utf-8", errors="replace").removesuffix("\n")
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="grep runs ripgrep under sandbox-exec",
)
async def test_grep_skips_app_storage_but_searches_a_spilled_result(
    tmp_path: Path,
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    for index in range(400):
        (folder / f"document-with-a-long-name-{index:04}.md").write_text("x")
    listed = await _call(registry, "list", {"path": str(folder), "limit": 500})
    spill_dir = str(Path(listed.output["path"]).parent)

    in_folder = await _call(
        registry, "grep", {"base_path": str(folder), "pattern": "app-storage-secret"}
    )
    in_spill = await _call(
        registry, "grep", {"base_path": spill_dir, "pattern": "name-0399"}
    )

    assert in_folder.output["matches"] == []
    assert len(in_spill.output["matches"]) == 2

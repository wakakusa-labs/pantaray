from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling import tool_result_storage
from pantaray_agents.local_runtime.tooling.tool_result_reading import (
    ToolResultLoadError,
    ToolResultReadLimitError,
    ToolResultTextOffsetError,
    load_action_file_result,
    read_tool_result_text_prefix,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    ToolResultStorageError,
    release_stored_tool_result,
    store_tool_result,
)
from pantaray_agents.tools.files.text_lines import read_text_descriptor_lines


def _display_text(output: object) -> str:
    return json.dumps(output, ensure_ascii=False, indent=2)


_LEFT_OUT = re.compile(r"\n\[… lines ([\d,]+)–([\d,]+) left out …\]\n")


def _head_left_out_and_tail(preview: str) -> tuple[str, int, int, str]:
    head, first, last, tail = _LEFT_OUT.split(preview)
    return head, int(first.replace(",", "")), int(last.replace(",", "")), tail


def _output_with_exact_display_size(character_count: int) -> dict[str, object]:
    """Many short values and no long one, so a large result moves whole as JSON."""
    output: dict[str, object] = {
        "items": ["x" * 100] * ((character_count - 1_000) // 108)
    }
    output["pad"] = ""
    output["pad"] = "x" * (character_count - len(_display_text(output)))
    assert len(_display_text(output)) == character_count
    return output


def test_text_prefix_rejects_invalid_cursor_and_storage_provenance() -> None:
    with pytest.raises(ToolResultTextOffsetError, match="splits a UTF-8"):
        read_tool_result_text_prefix(
            action_tool_results_path=None,
            output_owner_id="step-1",
            output="🙂",
            storage_kind="inline_json",
            start_byte=2,
            page_bytes=4,
            max_read_bytes=6,
        )
    with pytest.raises(ToolResultLoadError, match="metadata is missing"):
        read_tool_result_text_prefix(
            action_tool_results_path=None,
            output_owner_id="step-1",
            output=None,
            storage_kind="action_file",
            start_byte=0,
            page_bytes=4,
            max_read_bytes=6,
        )


def test_small_result_stays_inline_without_creating_storage_directory(
    tmp_path: Path,
) -> None:
    output = {"status": "success", "content": "日本語\nsecond line"}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="invocation-1",
        output=output,
        search_text="search\ntext",
        stdout_text="stdout\n",
        stderr_text="stderr\n",
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert result.search_text == "search\ntext"
    assert result.stdout_text == "stdout\n"
    assert result.stderr_text == "stderr\n"
    assert list(tmp_path.iterdir()) == []


def test_result_at_inline_character_limit_stays_inline(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT)

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="boundary-result",
        output=output,
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert list(tmp_path.iterdir()) == []


def test_result_above_inline_character_limit_is_stored(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="above-boundary",
        output=output,
    )

    assert result.output_json["character_count"] == (
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    assert result.storage_kind == "action_file"
    assert (tmp_path / str(result.output_json["path"])).is_file()
    release_stored_tool_result(result)


def test_large_result_without_long_texts_is_stored_whole_with_exact_metadata(
    tmp_path: Path,
) -> None:
    output = {"title": "日本語の結果", "lines": ["一行目二行目🌊" * 20] * 300}
    history_text = _display_text(output)
    assert len(history_text) > ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="large-result",
        output=output,
        search_text="duplicate search text",
        stdout_text="duplicate stdout",
        stderr_text="duplicate stderr",
    )

    relative_path = Path(str(result.output_json["path"]))
    stored_path = tmp_path / relative_path
    assert stored_path.read_text(encoding="utf-8") == history_text
    assert result.output_json["storage"] == "action_file"
    assert result.storage_kind == "action_file"
    assert result.output_json["path"] == relative_path.as_posix()
    assert result.output_json["media_type"] == "application/json"
    assert result.output_json["byte_size"] == len(history_text.encode("utf-8"))
    assert result.output_json["character_count"] == len(history_text)
    assert result.output_json["line_count"] == history_text.count("\n") + 1
    preview = result.output_json["preview"]
    assert preview.startswith('title="日本語の結果"\nlines[0]: "一行目')
    assert preview.endswith(" more of 301 lines")
    # The outline fills the room the reference leaves: one more line would not fit.
    shown = _display_text(result.output_json)
    assert len(shown) <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    assert len(shown) + len(json.dumps(output["lines"][0], ensure_ascii=False)) > (
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    assert (
        f"in {history_text.count(chr(10)) + 1:,} lines"
        in result.output_json["retry_hint"]
    )
    assert f"path={relative_path.as_posix()}" in result.output_json["retry_hint"]
    assert "sha256" not in result.output_json
    assert result.search_text is None
    assert result.stdout_text is None
    assert result.stderr_text is None
    release_stored_tool_result(result)

    # A result spilled before previews existed still loads.
    without_preview = {
        key: value
        for key, value in result.output_json.items()
        if key not in {"preview", "retry_hint"}
    }
    for metadata in (result.output_json, without_preview):
        loaded = load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="large-result",
            output=metadata,
            max_bytes=result.output_json["byte_size"],
        )
        assert loaded == output


def test_outline_of_a_result_stored_whole_names_every_short_value(
    tmp_path: Path,
) -> None:
    # Search results: no text long enough to move out alone, and many of them.
    results = [
        {
            "title": f"Result {index}",
            "url": f"https://example.com/{index}",
            "content": f"{index} " + "body " * 300,
            "score": 0.5,
        }
        for index in range(50)
    ]
    output = {"status": "success", "searches": [{"query": "q", "results": results}]}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="search-result",
        output=output,
    )

    preview = result.output_json["preview"]
    assert result.output_json["media_type"] == "application/json"
    assert preview.splitlines()[:3] == [
        'status="success"',
        'searches[0]: query="q"',
        'searches[0].results[0]: title="Result 0" url="https://example.com/0" '
        "score=0.5",
    ]
    assert all(
        f'title="Result {index}" url="https://example.com/{index}"' in preview
        for index in range(50)
    )
    assert "more of" not in preview
    assert "body" not in preview
    release_stored_tool_result(result)


def test_outline_says_how_many_lines_it_leaves_out(tmp_path: Path) -> None:
    output = {"matches": [f"folder/file-{index:04}.md" for index in range(2_000)]}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="glob-result",
        output=output,
    )

    preview = result.output_json["preview"]
    assert len(_display_text(result.output_json)) <= (
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    assert preview.startswith('matches[0]: "folder/file-0000.md"\n')
    shown = preview.count("\n")
    assert preview.endswith(f"… {2_000 - shown:,} more of 2,000 lines")
    assert f'matches[{shown - 1}]: "folder/file-{shown - 1:04}.md"' in preview
    release_stored_tool_result(result)


def _page(words: int) -> str:
    return "\n".join(f"line {number}: " + "word " * words for number in range(1, 4_000))


def test_long_texts_move_to_text_files_and_the_rest_stays_in_place(
    tmp_path: Path,
) -> None:
    pages = [_page(12), _page(6)]
    output = {
        "status": "success",
        "results": [
            {"url": "https://example.com/a", "raw_content": pages[0]},
            {"url": "https://example.com/b", "raw_content": pages[1]},
            {"url": "https://example.com/c", "raw_content": "short"},
        ],
        "truncated": False,
    }

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="web-extract",
        output=output,
    )
    release_stored_tool_result(result)

    projected = result.output_json
    assert result.storage_kind == "action_file"
    assert isinstance(projected, dict)
    assert len(_display_text(projected)) <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    assert projected["status"] == "success"
    assert projected["truncated"] is False
    results = projected["results"]
    assert [item["url"] for item in results] == [  # type: ignore[union-attr]
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert results[2]["raw_content"] == "short"  # type: ignore[index]
    for item, page in zip(results[:2], pages, strict=True):  # type: ignore[index]
        moved = item["raw_content"]  # type: ignore[index]
        stored_path = Path(moved["path"])
        assert stored_path.suffix == ".txt"
        assert stored_path.parent == tmp_path / "web-extract"
        assert stored_path.read_text(encoding="utf-8") == page
        assert moved["media_type"] == "text/plain"
        assert moved["byte_size"] == len(page.encode("utf-8"))
        assert moved["character_count"] == len(page)
        assert moved["line_count"] == page.count("\n") + 1
        head, first, last, tail = _head_left_out_and_tail(moved["preview"])
        assert page.startswith(head + "\n")
        assert page.endswith("\n" + tail)
        assert first == head.count("\n") + 2
        assert last == page.count("\n") - tail.count("\n")
        # The two previews share the room under the limit about equally.
        assert len(moved["preview"]) > 8_000
        assert f"in {page.count(chr(10)) + 1:,} lines" in moved["retry_hint"]
        assert f"read path={stored_path}" in moved["retry_hint"]

    assert (
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="web-extract",
            output=projected,
            max_bytes=1_000_000,
        )
        == output
    )
    with pytest.raises(ToolResultReadLimitError, match="read limit"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="web-extract",
            output=projected,
            max_bytes=len(pages[0].encode("utf-8")),
        )


def test_long_texts_share_the_room_alike(tmp_path: Path) -> None:
    # Pages read at once: a mid-sized one left in place would take the room the
    # previews of the others need.
    middle = "\n".join(f"line {number}: word" for number in range(1, 1_300))
    assert 15_000 < len(middle) < ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    pages = [_page(40), middle, _page(15)]
    output = {
        "status": "success",
        "results": [
            {"url": f"https://example.com/{index}", "raw_content": page}
            for index, page in enumerate(pages)
        ],
    }

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="pages",
        output=output,
    )
    release_stored_tool_result(result)

    assert len(_display_text(result.output_json)) <= (
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    previews = [
        item["raw_content"]["preview"]  # type: ignore[index]
        for item in result.output_json["results"]  # type: ignore[index,call-overload]
    ]
    assert all(len(preview) > 4_000 for preview in previews)


def test_read_continues_a_moved_text_where_its_preview_stops(tmp_path: Path) -> None:
    page = _page(30)
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="bash",
        output={"status": "success", "exit_code": 0, "stdout": page, "stderr": ""},
    )
    release_stored_tool_result(result)
    moved = result.output_json["stdout"]  # type: ignore[index,call-overload]
    offset = int(moved["retry_hint"].split("offset=")[1].split(",")[0])
    assert result.output_json["exit_code"] == 0  # type: ignore[index,call-overload]

    head, _first, _last, _tail = _head_left_out_and_tail(moved["preview"])
    read_back = [head, "\n"]
    column = 1
    descriptor = os.open(moved["path"], os.O_RDONLY)
    try:
        while offset is not None:
            os.lseek(descriptor, 0, os.SEEK_SET)
            page_read = read_text_descriptor_lines(
                descriptor=descriptor, offset=offset, column=column, limit=100_000
            )
            assert len(page_read.content.encode("utf-8")) <= 50 * 1024
            read_back.append(page_read.content)
            offset, column = page_read.next_offset, page_read.next_column or 1
    finally:
        os.close(descriptor)

    assert len(read_back) > 3
    assert "".join(read_back) == page


def test_moved_text_counts_lines_the_way_read_does(tmp_path: Path) -> None:
    # Progress output ends lines with a lone \r, and some tools with \r\n.
    endings = ("\r", "\r\n", "\n")
    stdout = "".join(
        f"progress {number:05d} of the download{endings[number % 3]}"
        for number in range(3_000)
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="progress",
        output={"status": "success", "exit_code": 0, "stdout": stdout, "stderr": ""},
    )
    release_stored_tool_result(result)
    moved = result.output_json["stdout"]  # type: ignore[index,call-overload]
    offset = int(moved["retry_hint"].split("offset=")[1].split(",")[0])
    head, first, last, tail = _head_left_out_and_tail(moved["preview"])
    preview_lines = re.split(r"\r\n|\r|\n", head)
    assert Path(moved["path"]).read_bytes() == stdout.encode("utf-8")

    descriptor = os.open(moved["path"], os.O_RDONLY)
    try:
        page = read_text_descriptor_lines(descriptor=descriptor, offset=offset, limit=1)
        os.lseek(descriptor, 0, os.SEEK_SET)
        tail_page = read_text_descriptor_lines(
            descriptor=descriptor, offset=last + 1, limit=1
        )
    finally:
        os.close(descriptor)

    assert offset == first == len(preview_lines) + 1
    assert preview_lines[-1] == f"progress {offset - 2:05d} of the download"
    assert page.content == f"progress {offset - 1:05d} of the download\n"
    # The tail starts on the line after the last one left out, as read counts them.
    assert tail.startswith(f"progress {last:05d} of the download")
    assert tail_page.content == f"progress {last:05d} of the download\n"
    assert moved["line_count"] == page.total_lines == 3_000
    assert f"in {3_000:,} lines" in moved["retry_hint"]


def test_preview_shows_how_command_output_ends(tmp_path: Path) -> None:
    stdout = "".join(f"PASSED test_{number:05d}\n" for number in range(5_000))
    stdout += "FAILED test_05000 - AssertionError: expected 3, got 4\n1 failed\n"
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="test-run",
        output={"status": "success", "exit_code": 1, "stdout": stdout, "stderr": ""},
    )
    release_stored_tool_result(result)

    shown = _display_text(result.output_json)
    assert len(shown) <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    moved = result.output_json["stdout"]  # type: ignore[index,call-overload]
    head, first, last, tail = _head_left_out_and_tail(moved["preview"])
    assert head.startswith("PASSED test_00000\n")
    assert tail.endswith(
        "FAILED test_05000 - AssertionError: expected 3, got 4\n1 failed\n"
    )
    assert f"lines {first:,} to {last:,} left out" in moved["retry_hint"]
    # Head and tail fill the room the rest of the result leaves.
    assert len(shown) > ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT - 100


def test_a_line_too_long_to_split_shows_only_its_head(tmp_path: Path) -> None:
    stdout = "x" * 50_000
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="one-line",
        output={"status": "success", "stdout": stdout},
    )
    release_stored_tool_result(result)

    moved = result.output_json["stdout"]  # type: ignore[index,call-overload]
    assert stdout.startswith(moved["preview"])
    assert len(moved["preview"]) > 19_000
    assert f"read path={moved['path']} offset=1," in moved["retry_hint"]


def test_text_moved_out_pages_whole_in_the_output_view(tmp_path: Path) -> None:
    output = {"status": "success", "stdout": "日本語\n" * 10_000}
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="output-view",
        output=output,
    )
    release_stored_tool_result(result)
    expected = _display_text(output).encode("utf-8")

    pages: list[str] = []
    start = 0
    while True:
        prefix = read_tool_result_text_prefix(
            action_tool_results_path=tmp_path,
            output_owner_id="output-view",
            output=result.output_json,
            storage_kind="action_file",
            start_byte=start,
            page_bytes=16_384,
            max_read_bytes=1_048_576,
        )
        pages.append(prefix.content)
        if prefix.next_byte_offset is None:
            break
        start = prefix.next_byte_offset

    assert prefix.total_bytes == len(expected)
    assert "".join(pages) == expected.decode("utf-8")


def test_result_holding_a_reference_shape_moves_whole_as_json(tmp_path: Path) -> None:
    # A reader could not tell this value from a reference storage put in.
    output = {
        "embedded": {"storage": "action_file", "media_type": "text/plain"},
        "body": "x" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
    }

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="reference-shaped",
        output=output,
    )
    release_stored_tool_result(result)

    assert result.output_json["media_type"] == "application/json"  # type: ignore[index,call-overload]
    assert (
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="reference-shaped",
            output=result.output_json,
            max_bytes=1_000_000,
        )
        == output
    )


def test_failure_on_a_later_text_file_removes_the_earlier_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {
        "stdout": "o" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
        "stderr": "e" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
    }
    original_rename = tool_result_storage.os.rename
    renamed = 0

    def fail_second_rename(
        source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int
    ) -> None:
        nonlocal renamed
        renamed += 1
        if renamed == 2:
            raise OSError("rename failed")
        original_rename(
            source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
        )

    monkeypatch.setattr(tool_result_storage.os, "rename", fail_second_rename)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="two-texts",
            output=output,
        )

    assert renamed == 2
    assert list((tmp_path / "two-texts").iterdir()) == []


def test_action_file_json_load_is_bounded(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="bounded-result",
        output=output,
    )
    release_stored_tool_result(result)

    with pytest.raises(ToolResultReadLimitError, match="read limit"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="bounded-result",
            output=result.output_json,
            max_bytes=1,
        )


def test_action_file_json_load_rejects_invalid_path_before_read_limit(
    tmp_path: Path,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="invalid-path",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    metadata = dict(result.output_json)
    metadata["path"] = str((tmp_path / "outside.json").absolute())

    with pytest.raises(ToolResultLoadError, match="outside its owner") as error:
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="invalid-path",
            output=metadata,
            max_bytes=1,
        )

    assert not isinstance(error.value, ToolResultReadLimitError)


def test_action_file_json_load_rejects_symlink_replacement(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="symlinked-result",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    stored_path = Path(str(result.output_json["path"]))
    outside = tmp_path.parent / f"{tmp_path.name}-outside-result.json"
    outside.write_text("{}", encoding="utf-8")
    stored_path.unlink()
    stored_path.symlink_to(outside)

    with pytest.raises(ToolResultLoadError, match="safely open"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="symlinked-result",
            output=result.output_json,
            max_bytes=int(result.output_json["byte_size"]),
        )


def test_action_file_json_load_rejects_byte_size_mismatch(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="size-mismatch",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    metadata = dict(result.output_json)
    metadata["byte_size"] = int(metadata["byte_size"]) + 1

    with pytest.raises(ToolResultLoadError, match="size or type"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="size-mismatch",
            output=metadata,
            max_bytes=int(metadata["byte_size"]),
        )


def test_action_file_json_load_rechecks_size_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="growing-result",
        output=output,
    )
    release_stored_tool_result(result)
    assert isinstance(result.output_json, dict)
    stored_path = Path(str(result.output_json["path"]))
    original_read = tool_result_storage.os.read
    grew = False

    def grow_after_read(file_descriptor: int, read_bytes: int) -> bytes:
        nonlocal grew
        chunk = original_read(file_descriptor, read_bytes)
        if chunk and not grew:
            with stored_path.open("ab") as handle:
                handle.write(b" ")
            grew = True
        return chunk

    monkeypatch.setattr(tool_result_storage.os, "read", grow_after_read)
    with pytest.raises(ToolResultLoadError, match="size changed"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="growing-result",
            output=result.output_json,
            max_bytes=int(result.output_json["byte_size"]),
        )


def test_action_file_json_load_rejects_infinite_exponent(tmp_path: Path) -> None:
    owner_path = tmp_path / "infinite-number"
    owner_path.mkdir()
    stored_path = owner_path / f"output-{'1' * 32}.json"
    text = '{"value": 1e9999}'
    stored_path.write_text(text, encoding="utf-8")
    metadata = {
        "storage": "action_file",
        "path": str(stored_path.absolute()),
        "media_type": "application/json",
        "byte_size": len(text.encode("utf-8")),
        "character_count": len(text),
        "line_count": 1,
    }

    with pytest.raises(ToolResultLoadError, match="valid UTF-8 JSON"):
        load_action_file_result(
            action_tool_results_path=tmp_path,
            invocation_id="infinite-number",
            output=metadata,
            max_bytes=1_000,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"\x00\x01raw\x00bytes",
        b"<script>alert('raw')</script>",
        b"small binary payload",
        b"x" * (ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1),
    ),
    ids=("empty", "nul", "active-content", "small", "large"),
)
def test_binary_result_is_always_stored_with_exact_metadata(
    tmp_path: Path,
    payload: bytes,
) -> None:
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="binary-result",
        output=payload,
        search_text="must not be duplicated",
        stdout_text="must not be duplicated",
        stderr_text="must not be duplicated",
    )

    assert isinstance(result.output_json, dict)
    assert result.storage_kind == "action_file"
    assert result.output_json == {
        "storage": "action_file",
        "path": result.output_json["path"],
        "media_type": "application/octet-stream",
        "byte_size": len(payload),
    }
    stored_path = Path(str(result.output_json["path"]))
    assert stored_path.suffix == ".bin"
    assert stored_path.read_bytes() == payload
    assert result.search_text is None
    assert result.stdout_text is None
    assert result.stderr_text is None
    release_stored_tool_result(result)


def test_small_storage_shaped_json_remains_host_classified_inline(
    tmp_path: Path,
) -> None:
    output = {"storage": "action_file"}

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="external-storage-value",
        output=output,
    )

    assert result.output_json is output
    assert result.storage_kind == "inline_json"
    assert list(tmp_path.iterdir()) == []


def test_json_null_stays_inline_and_preserves_audit_text(
    tmp_path: Path,
) -> None:
    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="no-output",
        output=None,
        search_text="search",
        stdout_text="stdout",
        stderr_text="stderr",
    )

    assert result.output_json is None
    assert result.storage_kind == "inline_json"
    assert result.search_text == "search"
    assert result.stdout_text == "stdout"
    assert result.stderr_text == "stderr"


@pytest.mark.parametrize(
    "invocation_id",
    ("", ".", "..", "../escape", "nested/id", r"nested\id", "nul\0id"),
)
def test_invalid_invocation_id_is_rejected_before_writing(
    tmp_path: Path,
    invocation_id: str,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    with pytest.raises(ValueError, match="invocation_id"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id=invocation_id,
            output=output,
        )

    assert list(tmp_path.iterdir()) == []


def test_runtime_invocation_id_is_encoded_as_one_path_segment(tmp_path: Path) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="bash:action-1:request-1:1",
        output=output,
    )

    result_path = str(result.output_json["path"])
    assert result_path.startswith(
        str(tmp_path / "bash%3Aaction-1%3Arequest-1%3A1" / "output-")
    )
    assert result_path.endswith(".json")
    release_stored_tool_result(result)


def test_storage_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_path.mkdir()
    (tmp_path / "symlink-escape").symlink_to(
        outside_path,
        target_is_directory=True,
    )
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )

    with pytest.raises(ToolResultStorageError, match="tool result directory"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="symlink-escape",
            output=output,
        )

    assert list(outside_path.iterdir()) == []


def test_atomic_rename_failure_preserves_previous_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_output = {
        "content": "previous" * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    }
    replacement_output = {
        "content": "replacement" * ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    }
    previous_result = store_tool_result(
        action_tool_results_path=tmp_path,
        invocation_id="atomic-result",
        output=previous_output,
    )
    stored_path = tmp_path / str(previous_result.output_json["content"]["path"])
    previous_text = stored_path.read_text(encoding="utf-8")

    def fail_rename(
        _source: str,
        _destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        _ = src_dir_fd, dst_dir_fd
        raise OSError("rename failed")

    monkeypatch.setattr(tool_result_storage.os, "rename", fail_rename)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="atomic-result",
            output=replacement_output,
        )

    assert stored_path.read_text(encoding="utf-8") == previous_text
    assert list(stored_path.parent.glob(".output-*.tmp")) == []
    release_stored_tool_result(previous_result)


def test_directory_fsync_failure_removes_renamed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    original_fsync = tool_result_storage.os.fsync
    call_count = 0

    def fail_result_directory_fsync(file_descriptor: int) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise OSError("directory fsync failed")
        original_fsync(file_descriptor)

    monkeypatch.setattr(tool_result_storage.os, "fsync", fail_result_directory_fsync)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="fsync-failure",
            output=output,
        )

    assert list(tmp_path.rglob("*.json")) == []


def test_parent_directory_fsync_failure_stops_before_writing_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _output_with_exact_display_size(
        ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT + 1
    )
    original_fsync = tool_result_storage.os.fsync
    parent_stat = tmp_path.stat()

    def fail_parent_directory_fsync(file_descriptor: int) -> None:
        descriptor_stat = tool_result_storage.os.fstat(file_descriptor)
        if (
            descriptor_stat.st_dev == parent_stat.st_dev
            and descriptor_stat.st_ino == parent_stat.st_ino
        ):
            raise OSError("parent directory fsync failed")
        original_fsync(file_descriptor)

    monkeypatch.setattr(tool_result_storage.os, "fsync", fail_parent_directory_fsync)

    with pytest.raises(ToolResultStorageError, match="failed to atomically store"):
        store_tool_result(
            action_tool_results_path=tmp_path,
            invocation_id="parent-fsync-failure",
            output=output,
        )

    assert list(tmp_path.rglob("output-*")) == []

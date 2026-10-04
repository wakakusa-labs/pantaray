from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker_direct_read_text
from pantaray_agents.local_runtime.tooling.brokering.broker_direct_read_text import (
    ReadLinesResult,
    read_text_descriptor_lines,
)


def read_text_lines(*, filepath: Path, **position: int) -> ReadLinesResult:
    descriptor = os.open(filepath, os.O_RDONLY)
    try:
        return read_text_descriptor_lines(descriptor=descriptor, **position)
    finally:
        os.close(descriptor)


class _CountingLineStream:
    def __init__(self, text: str) -> None:
        self._text = text
        self._index = 0

    def __enter__(self) -> _CountingLineStream:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def readline(self, size: int = -1) -> str:
        if self._index >= len(self._text):
            return ""
        newline_index = self._text.find("\n", self._index)
        line_end = len(self._text) if newline_index < 0 else newline_index + 1
        if size >= 0:
            line_end = min(line_end, self._index + size)
        chunk = self._text[self._index : line_end]
        self._index = line_end
        return chunk

    @property
    def position(self) -> int:
        return self._index


def _patch_path_stream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: _CountingLineStream,
) -> None:
    monkeypatch.setattr(broker_direct_read_text.os, "open", lambda *args: 42)
    monkeypatch.setattr(
        broker_direct_read_text.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG, st_size=0),
    )
    monkeypatch.setattr(
        broker_direct_read_text,
        "open",
        lambda *args, **kwargs: stream,
        raising=False,
    )
    monkeypatch.setattr(broker_direct_read_text.os, "close", lambda _fd: None)


def test_read_text_descriptor_lines_borrows_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "borrowed.txt"
    path.write_text("first\nsecond\n", encoding="utf-8")
    descriptor = broker_direct_read_text.os.open(
        path, broker_direct_read_text.os.O_RDONLY
    )
    try:
        result = read_text_descriptor_lines(
            descriptor=descriptor,
            offset=1,
            limit=1,
        )

        assert result.content == "first\n"
        assert (
            broker_direct_read_text.os.fstat(descriptor).st_size == path.stat().st_size
        )
    finally:
        broker_direct_read_text.os.close(descriptor)


def test_read_text_descriptor_lines_rejects_non_regular_without_closing(
    tmp_path: Path,
) -> None:
    descriptor = broker_direct_read_text.os.open(
        tmp_path,
        broker_direct_read_text.os.O_RDONLY,
    )
    try:
        with pytest.raises(OSError) as exc_info:
            read_text_descriptor_lines(
                descriptor=descriptor,
                offset=1,
                limit=1,
            )

        assert exc_info.value.errno == errno.EINVAL
        broker_direct_read_text.os.fstat(descriptor)
    finally:
        broker_direct_read_text.os.close(descriptor)


def test_read_text_descriptor_lines_keeps_descriptor_after_decode_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"\xff\n")
    descriptor = broker_direct_read_text.os.open(
        path, broker_direct_read_text.os.O_RDONLY
    )
    try:
        with pytest.raises(UnicodeDecodeError):
            read_text_descriptor_lines(
                descriptor=descriptor,
                offset=1,
                limit=1,
            )

        broker_direct_read_text.os.fstat(descriptor)
    finally:
        broker_direct_read_text.os.close(descriptor)


def test_read_text_lines_counts_total_lines_after_limit_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream("first\nsecond\nthird\n")
    _patch_path_stream(monkeypatch, stream=stream)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, limit=1)

    assert result.content == "first\n"
    assert result.end_line == 1
    assert result.total_lines == 3
    assert result.next_offset == 2
    assert result.truncated is True
    assert result.truncation_reason == "page_limit"
    assert result.next_column == 1
    assert result.retry_hint == (
        "Continue with offset=next_offset and column=next_column."
    )
    assert stream.position == len("first\nsecond\nthird\n")


def test_read_text_lines_counts_total_lines_after_byte_cap_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream("abc\nde\nthird\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_BYTES", 5)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, limit=2_000)

    assert result.content == "abc\nd"
    assert result.end_line == 2
    assert result.end_column == 1
    assert result.total_lines == 3
    assert result.next_offset == 2
    assert result.next_column == 2
    assert result.truncated is True
    assert result.truncation_reason == "page_limit"
    assert stream.position == len("abc\nde\nthird\n")


def test_read_text_lines_counts_total_after_long_line_exceeds_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream("fit\n" + ("a" * 10_000) + "\nnext\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_BYTES", 20)
    monkeypatch.setattr(broker_direct_read_text, "MAX_LINE_LENGTH", 8)
    monkeypatch.setattr(broker_direct_read_text, "_LINE_READ_AHEAD_CHARS", 9)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, limit=2_000)

    assert result.content == "fit\n" + ("a" * 8)
    assert result.end_line == 2
    assert result.end_column == 8
    assert result.total_lines == 3
    assert result.next_offset == 2
    assert result.next_column == 9
    assert result.truncated is True
    assert result.truncation_reason == "page_limit"
    assert stream.position == len("fit\n" + ("a" * 10_000) + "\nnext\n")


def test_read_text_lines_stops_after_long_line_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream(("a" * 10_000) + "\nnext\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_LINE_LENGTH", 8)
    monkeypatch.setattr(broker_direct_read_text, "_LINE_READ_AHEAD_CHARS", 9)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, limit=1)

    assert result.content == "a" * 8
    assert result.end_line == 1
    assert result.end_column == 8
    assert result.total_lines == 2
    assert result.next_offset == 1
    assert result.next_column == 9
    assert result.truncated is True
    assert result.truncation_reason == "page_limit"
    assert stream.position == len(("a" * 10_000) + "\nnext\n")


def test_read_text_lines_consumes_skipped_long_lines_to_reach_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream(("a" * 10_000) + "\nnext\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_LINE_LENGTH", 8)
    monkeypatch.setattr(broker_direct_read_text, "_LINE_READ_AHEAD_CHARS", 9)

    result = read_text_lines(filepath=Path("unused.txt"), offset=2, limit=1)

    assert result.content == "next\n"
    assert result.end_line == 2
    assert result.total_lines == 2
    assert result.next_offset is None
    assert result.truncated is False


def test_read_text_lines_continues_long_line_from_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream(("abcdefghij" * 2) + "\nnext\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_LINE_LENGTH", 8)
    monkeypatch.setattr(broker_direct_read_text, "_LINE_READ_AHEAD_CHARS", 9)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, column=9, limit=1)

    assert result.content == "ijabcdef"
    assert result.end_line == 1
    assert result.end_column == 16
    assert result.next_offset == 1
    assert result.next_column == 17
    assert result.total_lines == 2


def test_read_text_lines_pages_multibyte_line_without_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _CountingLineStream("あ" * 10 + "\n")
    _patch_path_stream(monkeypatch, stream=stream)
    monkeypatch.setattr(broker_direct_read_text, "MAX_BYTES", 10)

    result = read_text_lines(filepath=Path("unused.txt"), offset=1, limit=1)

    assert result.content == "あ" * 3
    assert result.next_offset == 1
    assert result.next_column == 4


def test_multimegabyte_file_tail_is_readable_without_splitting(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text(("かな" * 100 + "\n") * 5_000 + "LAST MARKER", encoding="utf-8")
    result = read_text_lines(filepath=path, offset=5_001, limit=2)
    assert result.content == "LAST MARKER"
    assert result.total_lines == 5_001
    assert result.next_offset is None


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_newline_at_chunk_boundary_preserves_lines_and_columns(
    tmp_path: Path,
    newline: str,
) -> None:
    path = tmp_path / "boundary.txt"
    line = "x" * broker_direct_read_text.MAX_LINE_LENGTH
    path.write_bytes((line + newline + "tail" + newline).encode("utf-8"))
    result = read_text_lines(filepath=path, offset=1, limit=2)
    assert result.content == line + "\ntail\n"
    assert result.total_lines == 2
    assert result.end_line == 2
    assert result.end_column == 4
    assert result.next_offset is None

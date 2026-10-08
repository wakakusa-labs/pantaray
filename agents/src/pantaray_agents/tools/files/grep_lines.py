"""Bounding one matching line that ripgrep printed to what grep returns."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .text_encoding import UnmarkedTextEncoding, byte_order_mark, whole_file_encoding

# ripgrep prints a line longer than this many bytes as a preview of its first
# this many graphemes, so no file size cap is needed to bound one match.
# Like Codex CLI and Pi, there is no file size cap: ripgrep itself still holds
# the line it is searching, about 150 MB for a 128 MiB line, until the timeout.
# Design limit: a first match further into a line than this is not centred in
# its excerpt; raise it when such matches are reported as hard to locate.
RIPGREP_MAX_COLUMNS = 64 * 1024
# What grep returns of one matching line, centred on the line's first match.
GREP_MAX_LINE_CHARS = 500
GREP_OMITTED_TEXT_MARKER = "…"
GREP_MAX_LISTED_BINARY_PATHS = 10
# Enough to tell a UTF-32 mark from a UTF-16 one.
_MARK_SAMPLE_BYTES = 4
_RIPGREP_PREVIEW_SUFFIX_PATTERN = re.compile(
    rb" \[\.\.\. (?:\d+ more match(?:es)?|omitted end of long line)\]\Z"
)


@dataclass(frozen=True, slots=True)
class RipgrepGrepMatch:
    relative_path: str
    line_number: int
    line: str
    line_truncated: bool


def grep_match(
    *,
    relative_path: str,
    line_number: int,
    text: str,
    match_start: int | None,
    cut: bool,
) -> RipgrepGrepMatch:
    """One matching line bounded to an excerpt around its first match.

    ``match_start`` is the first match's character index in ``text``, or None
    when ``text`` is a preview that ends before it; ``cut`` says that ``text``
    is such a preview of a longer line.
    """

    if not cut and len(text) <= GREP_MAX_LINE_CHARS:
        return RipgrepGrepMatch(relative_path, line_number, text, False)
    start = (
        0
        if match_start is None
        else max(
            0,
            min(
                match_start - GREP_MAX_LINE_CHARS // 2,
                len(text) - GREP_MAX_LINE_CHARS,
            ),
        )
    )
    end = start + GREP_MAX_LINE_CHARS
    head = GREP_OMITTED_TEXT_MARKER if start > 0 else ""
    tail = GREP_OMITTED_TEXT_MARKER if cut or end < len(text) else ""
    return RipgrepGrepMatch(
        relative_path, line_number, f"{head}{text[start:end]}{tail}", True
    )


def match_line_codec(path: Path) -> UnmarkedTextEncoding:
    """How to decode the lines ripgrep printed for one file, as read decides.

    ripgrep prints a file with a byte order mark transcoded to UTF-8 and any
    other file as its own bytes, which are decoded in the encoding read reports
    for that whole file, so grep and read show one line the same way. A file
    in neither UTF-8 nor CP932, or one gone since ripgrep read it, is shown as
    lossy UTF-8.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return "utf-8"
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return "utf-8"
        if byte_order_mark(os.pread(descriptor, _MARK_SAMPLE_BYTES, 0)) is not None:
            return "utf-8"
        return whole_file_encoding(descriptor) or "utf-8"
    finally:
        os.close(descriptor)


def grep_match_from_ripgrep(
    *,
    relative_path: str,
    line_number: int,
    column: int,
    content: bytes,
    codec: UnmarkedTextEncoding,
) -> RipgrepGrepMatch:
    # A line within RIPGREP_MAX_COLUMNS bytes is printed whole; a longer one as
    # a preview plus a suffix, which together are always longer than that.
    cut = len(content) > RIPGREP_MAX_COLUMNS
    if cut:
        content = _RIPGREP_PREVIEW_SUFFIX_PATTERN.sub(b"", content)
    # ripgrep's column is the 1-based byte offset of the line's first match.
    match_offset = column - 1
    # Design limit: ripgrep matches an unmarked file's bytes, so a non-ASCII
    # pattern cannot match CP932 text. Upgrade trigger: replay evidence of
    # Japanese patterns searched in CP932 projects; then add a second ripgrep
    # pass with -E shift_jis for non-ASCII patterns only, since a global -E
    # misreads UTF-8 files that have a byte order mark.
    return grep_match(
        relative_path=relative_path,
        line_number=line_number,
        # A preview can end inside a character, and a match can start on the
        # second byte of a CP932 character.
        text=content.decode(codec, errors="replace"),
        match_start=len(content[:match_offset].decode(codec, errors="replace"))
        if match_offset < len(content)
        else None,
        cut=cut,
    )


def binary_match_warning(paths: tuple[str, ...]) -> str:
    listed = paths[:GREP_MAX_LISTED_BINARY_PATHS]
    more = len(paths) - len(listed)
    return (
        f"{len(paths)} binary file(s) also match; their lines are not shown: "
        f"{', '.join(listed)}" + (f" and {more} more." if more else ".")
    )

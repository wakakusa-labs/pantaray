"""Bounding one matching line that ripgrep printed to what grep returns."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text_encoding import decode_unmarked_text

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


def grep_match_from_ripgrep(
    *,
    relative_path: str,
    line_number: int,
    column: int,
    content: bytes,
) -> RipgrepGrepMatch:
    # A line within RIPGREP_MAX_COLUMNS bytes is printed whole; a longer one as
    # a preview plus a suffix, which together are always longer than that.
    cut = len(content) > RIPGREP_MAX_COLUMNS
    if cut:
        content = _RIPGREP_PREVIEW_SUFFIX_PATTERN.sub(b"", content)
    # ripgrep's column is the 1-based byte offset of the line's first match.
    match_offset = column - 1
    # ripgrep prints a file with a byte order mark as UTF-8 and any other file
    # as its own bytes, so a match line is decoded like a read of an unmarked
    # file. Design limit: ripgrep matches those bytes, so a non-ASCII pattern
    # cannot match CP932 text. Upgrade trigger: replay evidence of Japanese
    # patterns searched in CP932 projects; then add a second ripgrep pass with
    # -E shift_jis for non-ASCII patterns only, since a global -E misreads
    # UTF-8 files that have a byte order mark.
    text, encoding = decode_unmarked_text(content)
    return grep_match(
        relative_path=relative_path,
        line_number=line_number,
        text=text,
        # A match can start on the second byte of a CP932 character.
        match_start=len(content[:match_offset].decode(encoding, errors="replace"))
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

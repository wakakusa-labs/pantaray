"""Which encoding a text file is read in, decided once for the file.

A byte order mark names the encoding. Without one, a file is UTF-8 when its
first ENCODING_DECISION_BYTES decode as UTF-8 and otherwise CP932 (Windows-31J,
the Japanese Windows Shift_JIS) when they decode as that, so every page of one
unchanged file is read and reported in the same encoding even when its first
pages are ASCII.
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.tools.contract import BrokerPolicyError

type TextEncoding = Literal["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp932"]
type UnmarkedTextEncoding = Literal["utf-8", "cp932"]

READ_TEXT_ENCODING_UNSUPPORTED = "READ_TEXT_ENCODING_UNSUPPORTED"
# Tried in this order on a file that has no mark.
UNMARKED_TEXT_ENCODINGS: tuple[UnmarkedTextEncoding, ...] = ("utf-8", "cp932")

# How much of an unmarked file decides its encoding. Legacy CP932 sources and
# data files are far smaller, so all of one is checked; a larger file, such as
# a log, costs a bounded read (about 40 ms of CP932 decoding) on every read
# page and every grep match file instead of a scan to its end. The prefix of
# an unchanged file is the same on every call, so its pages stay consistent.
# It covers MAX_TOTAL_LINE_COUNT_BYTES, so a file whose lines a page counts to
# its end is decided on all of its bytes.
ENCODING_DECISION_BYTES = 4 * 1024 * 1024
_UTF32_LE_BOM = b"\xff\xfe\x00\x00"


@dataclass(frozen=True, slots=True)
class ByteOrderMark:
    encoding: Literal["utf-8-sig", "utf-16-le", "utf-16-be"]
    # The codec that decodes the bytes after the mark.
    codec: Literal["utf-8", "utf-16-le", "utf-16-be"]
    length: int


def byte_order_mark(sample: bytes) -> ByteOrderMark | None:
    if sample.startswith(codecs.BOM_UTF8):
        return ByteOrderMark("utf-8-sig", "utf-8", len(codecs.BOM_UTF8))
    if sample.startswith(_UTF32_LE_BOM):
        # UTF-32 is left to the binary check, which refuses its NUL bytes.
        return None
    if sample.startswith(codecs.BOM_UTF16_LE):
        return ByteOrderMark("utf-16-le", "utf-16-le", len(codecs.BOM_UTF16_LE))
    if sample.startswith(codecs.BOM_UTF16_BE):
        return ByteOrderMark("utf-16-be", "utf-16-be", len(codecs.BOM_UTF16_BE))
    return None


def decided_encoding(descriptor: int) -> UnmarkedTextEncoding | None:
    """The first of UTF-8 and CP932 that decodes the unmarked file's prefix.

    A prefix of ASCII, or of bytes UTF-8 also accepts, decides UTF-8. Design
    limit: CP932 text that starts past ENCODING_DECISION_BYTES of UTF-8 text is
    refused on the page that holds it; raise the bound when such files are
    reported.
    """

    prefix = _read_prefix(descriptor, ENCODING_DECISION_BYTES + 1)
    at_end = len(prefix) <= ENCODING_DECISION_BYTES
    for encoding in UNMARKED_TEXT_ENCODINGS:
        decoder = codecs.getincrementaldecoder(encoding)("strict")
        try:
            # Short of the end, the prefix may stop inside a character.
            decoder.decode(prefix[:ENCODING_DECISION_BYTES], final=at_end)
        except UnicodeDecodeError:
            continue
        return encoding
    return None


def text_encoding_unsupported(
    *, display_path: str, encoding: TextEncoding | None
) -> BrokerPolicyError:
    if encoding is None:
        problem = (
            "it is neither UTF-8 nor CP932 (Shift_JIS) text; UTF-16 and UTF-32 "
            "without a byte order mark are not supported"
        )
    elif encoding in UNMARKED_TEXT_ENCODINGS:
        problem = (
            f"it is not valid {encoding} text, the encoding its first "
            f"{ENCODING_DECISION_BYTES // (1024 * 1024)} MB decided"
        )
    else:
        problem = f"it is not valid {encoding} text"
    return BrokerPolicyError(
        f"Cannot read {display_path}: {problem}",
        code=READ_TEXT_ENCODING_UNSUPPORTED,
        fix_hint=(
            "If you know the file's encoding, convert a copy to UTF-8 (for "
            "example with iconv) and read that copy."
        ),
    )


def _read_prefix(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size < limit and (chunk := os.pread(descriptor, limit - size, size)):
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


__all__ = [
    "ENCODING_DECISION_BYTES",
    "READ_TEXT_ENCODING_UNSUPPORTED",
    "ByteOrderMark",
    "TextEncoding",
    "UnmarkedTextEncoding",
    "byte_order_mark",
    "decided_encoding",
    "text_encoding_unsupported",
]

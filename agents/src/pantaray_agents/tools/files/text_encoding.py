"""Which encoding a text file is read in, decided once for the whole file.

A byte order mark names the encoding. Without one, a file is UTF-8 when all of
it decodes as UTF-8 and otherwise CP932 (Windows-31J, the Japanese Windows
Shift_JIS) when all of it decodes as that, so every page of one unchanged file
is read and reported in the same encoding even when its first pages are ASCII.
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
# Tried in this order on a file, or a grep match line, that has no mark.
UNMARKED_TEXT_ENCODINGS: tuple[UnmarkedTextEncoding, ...] = ("utf-8", "cp932")

_VALIDATION_CHUNK_BYTES = 1024 * 1024
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


def unmarked_text_encoding(
    descriptor: int, *, display_path: str
) -> UnmarkedTextEncoding:
    """The first of UTF-8 and CP932 that decodes the whole file.

    Design limit: the whole file is decoded on every read, about 0.5 s per
    100 MB of UTF-8 Japanese and 1 s per 100 MB of CP932; remember the decision
    per file size and mtime when read latency on such files is reported.
    """

    for encoding in UNMARKED_TEXT_ENCODINGS:
        if _decodes_whole_file(descriptor, encoding=encoding):
            return encoding
    raise text_encoding_unsupported(display_path=display_path, encoding=None)


def text_encoding_unsupported(
    *, display_path: str, encoding: TextEncoding | None
) -> BrokerPolicyError:
    if encoding is None:
        problem = (
            "it is neither UTF-8 nor CP932 (Shift_JIS) text; UTF-16 and UTF-32 "
            "without a byte order mark are not supported"
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


def decode_unmarked_text(content: bytes) -> tuple[str, UnmarkedTextEncoding]:
    """Text of bytes with no mark, replacing what neither encoding decodes."""

    for encoding in UNMARKED_TEXT_ENCODINGS:
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace"), "utf-8"


def _decodes_whole_file(descriptor: int, *, encoding: UnmarkedTextEncoding) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    position = 0
    try:
        while chunk := os.pread(descriptor, _VALIDATION_CHUNK_BYTES, position):
            decoder.decode(chunk)
            position += len(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


__all__ = [
    "READ_TEXT_ENCODING_UNSUPPORTED",
    "ByteOrderMark",
    "TextEncoding",
    "UnmarkedTextEncoding",
    "byte_order_mark",
    "decode_unmarked_text",
    "text_encoding_unsupported",
    "unmarked_text_encoding",
]

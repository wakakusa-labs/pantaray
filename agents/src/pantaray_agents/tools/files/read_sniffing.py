"""What read makes of a file from its first bytes.

read decides by these bytes, before the file's name, whether a file is an image,
a PDF, text or binary. The rules live here so that a writer of files meant for
read, such as a spilled tool result, can tell whether read will page one as text.
"""

from __future__ import annotations

from .text_encoding import byte_order_mark

# How much of a file's start read looks at to decide what the file is.
SAMPLE_BYTES = 4_096
_PDF_MAGIC = b"%PDF-"


def image_mime_type(sample: bytes) -> str | None:
    """The image type a file's first bytes announce, if any."""

    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith(b"GIF87a") or sample.startswith(b"GIF89a"):
        return "image/gif"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "image/webp"
    return None


def starts_like_pdf(sample: bytes) -> bool:
    return sample.startswith(_PDF_MAGIC)


def looks_binary(sample: bytes) -> bool:
    """Whether read refuses a file whose first SAMPLE_BYTES are ``sample`` as binary."""

    if not sample:
        return False
    non_printable = 0
    for byte in sample:
        if byte == 0:
            return True
        if byte < 9 or (byte > 13 and byte < 32):
            non_printable += 1
    return non_printable / len(sample) > 0.3


def reads_as_text(sample: bytes) -> bool:
    """Whether read pages a file that starts with ``sample`` as text, when its name
    says nothing of its type (as a .txt name does not)."""

    if image_mime_type(sample) is not None or starts_like_pdf(sample):
        return False
    # UTF-16 text carries NUL bytes, so its mark is looked for before them.
    return byte_order_mark(sample) is not None or not looks_binary(sample)


__all__ = [
    "SAMPLE_BYTES",
    "image_mime_type",
    "looks_binary",
    "reads_as_text",
    "starts_like_pdf",
]

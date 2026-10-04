"""The reference that stands in for a JSON tool result too large to keep inline.

The model reads this reference in place of the result, so it carries the head
of the result and says how to read the rest instead of only where it is.
"""

from __future__ import annotations

from collections.abc import Set

TOOL_RESULT_JSON_METADATA_KEYS = frozenset(
    {
        "storage",
        "path",
        "media_type",
        "byte_size",
        "character_count",
        "line_count",
    }
)
# A result spilled before previews existed has neither of these.
TOOL_RESULT_PREVIEW_KEYS = frozenset({"preview", "retry_hint"})
TOOL_RESULT_PREVIEW_CHARS = 1_000


def is_json_spill_shape(keys: Set[str]) -> bool:
    return keys in (
        TOOL_RESULT_JSON_METADATA_KEYS,
        TOOL_RESULT_JSON_METADATA_KEYS | TOOL_RESULT_PREVIEW_KEYS,
    )


def spill_preview(*, text: str, path: str) -> tuple[str, str]:
    """The head of a spilled result, and how to read all of it."""

    preview = text[:TOOL_RESULT_PREVIEW_CHARS]
    # The line the preview stops in, so reading from it repeats nothing earlier.
    next_line = preview.count("\n") + 1
    return preview, (
        f"This result is {len(text):,} characters, too large to show inline; "
        f"preview holds only its first {len(preview):,}. Read the rest with read "
        f"path={path} offset={next_line}, continuing with next_offset, or search "
        "it with grep."
    )

"""Ref tags a model writes back when it rewrites part of one memory file.

A model that merges or rewrites lines retypes a kept tag's note, leaves it out,
repeats the tag, or carries it to another file. Draft validation only says that
tags and links disagree, which names no ref to fix; these helpers keep what the
model meant to keep and name what it cannot do.
"""

from __future__ import annotations

from collections import Counter

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    REFERENCE_PATTERN,
    extract_markdown_references,
)


def restore_reference_tags(*, before: str, after: str) -> str:
    """Put back the stored tag of every ref the rewrite keeps.

    A ref's note belongs to its link, so a tag written without its note, or with
    the note retyped, still keeps that ref instead of failing validation. An ID
    the file does not have is left as written for validation to refuse.
    """

    stored = {
        occurrence.local_ref_id: before[occurrence.match_start : occurrence.match_end]
        for occurrence in extract_markdown_references(before)
    }
    return REFERENCE_PATTERN.sub(
        lambda match: stored.get(match.group("local_ref_id"), match.group(0)), after
    )


def misused_reference_ids(*, before: str, after: str) -> str | None:
    """Name the ref IDs a rewrite repeats or brings in, which validation refuses.

    An ID the file did not have is either invented or carried from another file;
    neither keeps a ref, since a ref belongs to the file it was linked in.
    """

    kept = [item.local_ref_id for item in extract_markdown_references(after)]
    known = {item.local_ref_id for item in extract_markdown_references(before)}
    repeated = sorted(ref_id for ref_id, count in Counter(kept).items() if count > 1)
    unknown = sorted(set(kept) - known)
    problems = [
        *([f"used more than once: {', '.join(repeated)}"] if repeated else []),
        *([f"not in this file: {', '.join(unknown)}"] if unknown else []),
    ]
    return "; ".join(f"ref IDs {problem}" for problem in problems) or None


__all__ = ["misused_reference_ids", "restore_reference_tags"]

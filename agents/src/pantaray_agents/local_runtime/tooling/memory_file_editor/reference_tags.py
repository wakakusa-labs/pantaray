"""Ref tags a model writes back when it rewrites part of one memory file.

A model that merges or rewrites lines retypes a kept tag's note, leaves it out,
repeats the tag, or carries it to another file. Draft validation only says that
tags and links disagree, which names no ref to fix; these helpers keep what the
model meant to keep and name what it cannot do.
"""

from __future__ import annotations

import re
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

    def restored(match: re.Match[str]) -> str:
        tag = match.group(0)
        # A tag whose closing quote went missing swallows the lines up to the
        # next closing quote; putting the stored tag back would drop them.
        return (
            tag if _spans_lines(tag) else stored.get(match.group("local_ref_id"), tag)
        )

    return REFERENCE_PATTERN.sub(restored, after)


def misused_reference_ids(*, before: str, after: str) -> str | None:
    """Name the ref IDs a rewrite breaks, repeats or brings in.

    An ID the file did not have is either invented or carried from another file;
    neither keeps a ref, since a ref belongs to the file it was linked in.
    """

    tags = extract_markdown_references(after)
    kept = [item.local_ref_id for item in tags]
    known = {item.local_ref_id for item in extract_markdown_references(before)}
    broken = sorted(
        {
            item.local_ref_id
            for item in tags
            if _spans_lines(after[item.match_start : item.match_end])
        }
    )
    repeated = sorted(ref_id for ref_id, count in Counter(kept).items() if count > 1)
    unknown = sorted(set(kept) - known)
    problems = [
        *([f"ref tags span lines: {', '.join(broken)}"] if broken else []),
        *([f"ref IDs used more than once: {', '.join(repeated)}"] if repeated else []),
        *([f"ref IDs not in this file: {', '.join(unknown)}"] if unknown else []),
    ]
    return "; ".join(problems) or None


def _spans_lines(tag: str) -> bool:
    return "\n" in tag or "\r" in tag


__all__ = ["misused_reference_ids", "restore_reference_tags"]

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

_TAG_START = "[[ref:"
_SPANS_LINES = "span lines"
_SWALLOWS_TAG = "swallow another tag"


def restore_reference_tags(*, before: str, after: str) -> str:
    """Put back the stored tag of every ref the rewrite keeps.

    A ref's note belongs to its link, so a tag written without its note, or with
    the note retyped, still keeps that ref instead of failing validation. An ID
    the file does not have, or a run-on tag, is left as written for
    `misused_reference_ids` to refuse.
    """

    stored = _stored_tags(before)

    def restored(match: re.Match[str]) -> str:
        tag = match.group(0)
        if _run_on_problem(tag, stored=stored) is not None:
            return tag
        return stored.get(match.group("local_ref_id"), tag)

    return REFERENCE_PATTERN.sub(restored, after)


def misused_reference_ids(*, before: str, after: str) -> str | None:
    """Name the ref IDs a rewrite breaks, repeats or brings in.

    An ID the file did not have is either invented or carried from another file;
    neither keeps a ref, since a ref belongs to the file it was linked in.
    """

    stored = _stored_tags(before)
    tags = extract_markdown_references(after)
    run_on: dict[str, set[str]] = {_SPANS_LINES: set(), _SWALLOWS_TAG: set()}
    for item in tags:
        problem = _run_on_problem(
            after[item.match_start : item.match_end], stored=stored
        )
        if problem is not None:
            run_on[problem].add(item.local_ref_id)
    kept = [item.local_ref_id for item in tags]
    repeated = sorted(ref_id for ref_id, count in Counter(kept).items() if count > 1)
    unknown = sorted(set(kept) - set(stored))
    problems = [
        *(
            f"ref tags {problem}: {', '.join(sorted(ref_ids))}"
            for problem, ref_ids in run_on.items()
            if ref_ids
        ),
        *([f"ref IDs used more than once: {', '.join(repeated)}"] if repeated else []),
        *([f"ref IDs not in this file: {', '.join(unknown)}"] if unknown else []),
    ]
    return "; ".join(problems) or None


def _stored_tags(text: str) -> dict[str, str]:
    return {
        occurrence.local_ref_id: text[occurrence.match_start : occurrence.match_end]
        for occurrence in extract_markdown_references(text)
    }


def _run_on_problem(tag: str, *, stored: dict[str, str]) -> str | None:
    """Tell a tag that ran on past its own end, which restoring would erase.

    The ref pattern lets a note hold any character, so a note whose closing
    quote went missing runs on to the next closing quote and takes the text and
    tags in between with it. A tag the file already holds verbatim is its own.
    """

    if tag in stored.values():
        return None
    if "\n" in tag or "\r" in tag:
        return _SPANS_LINES
    if _TAG_START in tag[len(_TAG_START) :]:
        return _SWALLOWS_TAG
    return None


__all__ = ["misused_reference_ids", "restore_reference_tags"]

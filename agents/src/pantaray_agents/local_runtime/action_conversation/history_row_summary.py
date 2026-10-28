"""What a history row shows under its title: a status line and file names.

The renderer derives the same things from a conversation it has open; these
mirror it, so a row and its open task agree. ``first_line`` follows
``frontend/src/history/historyLiveStage.ts`` and ``linked_document_paths``
follows ``deriveTaskFiles`` in ``frontend/src/components/task/taskFiles.ts``
with ``pantarayFilePaths`` in ``frontend/electron/src/actions/pantarayFileLinks.ts``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

from pantaray_agents.schema.conversation_history import (
    CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS,
)

# Leading Markdown block markers and inline emphasis would show as raw symbols on one plain line.
_LEADING_BLOCK_MARKER = re.compile(r"^(?:#{1,6}|>|[-*+]|\d+\.)\s+")
_INLINE_EMPHASIS = re.compile(r"\*\*|__|`")
# A link shows as its text: an answer often opens by linking the file it made.
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ELLIPSIS = "…"

_FILE_URL_START = "pantaray-file:///"
_URL_STOPS = frozenset('<>"“”。、「」『』')
_CLOSING_BRACKETS = {")": "(", "）": "（", "]": "[", "】": "【"}
_OPENING_BRACKETS = frozenset(_CLOSING_BRACKETS.values())
_TRAILING_PUNCTUATION = re.compile(r"[.,:;!?']+$")
# Code an Action edits stays a diff in its steps, so it never becomes a file chip.
_CODE_EXTENSIONS = frozenset(
    (
        "py ts tsx js jsx mjs cjs go rs java kt swift c h cc cpp hpp cs rb php sh bash zsh "
        "css scss sass less vue svelte sql lua pl r scala dart m mm yml yaml toml ini lock"
    ).split()
)


def first_line(text: str) -> str:
    """The first non-blank line, as plain text."""

    line = next((candidate for candidate in text.split("\n") if candidate.strip()), "")
    line = _LEADING_BLOCK_MARKER.sub("", line.strip(), count=1)
    line = _INLINE_EMPHASIS.sub("", _MARKDOWN_LINK.sub(r"\1", line))
    return _bare_file_urls_as_names(line).strip()


def _bare_file_urls_as_names(line: str) -> str:
    """A file linked by its bare URL shows as its name, as its chip does."""

    parts: list[str] = []
    cursor = 0
    start = line.find(_FILE_URL_START)
    while start != -1:
        url = _bare_file_url_at(line, start)
        path = _local_path(url)
        parts.append(line[cursor:start])
        parts.append(_name(path) if path is not None else url)
        cursor = start + len(url)
        start = line.find(_FILE_URL_START, cursor)
    parts.append(line[cursor:])
    return "".join(parts)


def _fit(line: str) -> str | None:
    if not line:
        return None
    if len(line) <= CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS:
        return line
    kept = line[: CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS - 1].rstrip()
    return kept + _ELLIPSIS


def answer_status_line(answer: str) -> str | None:
    """A conversation's line: the first line of its latest answer."""

    return _fit(first_line(answer))


def _bare_file_url_at(text: str, start: int) -> str:
    depth: dict[str, int] = {}
    end = start + len(_FILE_URL_START)
    while end < len(text):
        char = text[end]
        if char.isspace() or char in _URL_STOPS:
            break
        if char in _OPENING_BRACKETS:
            depth[char] = depth.get(char, 0) + 1
        opening = _CLOSING_BRACKETS.get(char)
        if opening is not None:
            if depth.get(opening, 0) == 0:
                break
            depth[opening] -= 1
        end += 1
    return _TRAILING_PUNCTUATION.sub("", text[start:end])


def _local_path(href: str) -> str | None:
    path = urlsplit(href).path
    try:
        local = unquote(path, errors="strict")
    except UnicodeDecodeError:
        return None
    return local or None


def _linked_paths(text: str) -> Iterable[str]:
    start = text.find(_FILE_URL_START)
    while start != -1:
        path = _local_path(_bare_file_url_at(text, start))
        if path is not None:
            yield path
        start = text.find(_FILE_URL_START, start + len(_FILE_URL_START))


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_document(path: str) -> bool:
    name = _name(path)
    dot = name.rfind(".")
    extension = name[dot + 1 :].lower() if dot > 0 else ""
    return extension != "" and extension not in _CODE_EXTENSIONS


def linked_document_names(answers: Iterable[str]) -> tuple[str, ...]:
    """The names of the documents the answers link, each path once, oldest first."""

    paths: dict[str, None] = {}
    for answer in answers:
        for path in _linked_paths(answer):
            if _is_document(path):
                paths.setdefault(path, None)
    return tuple(_name(path) for path in paths)


def artifact_names(paths: Iterable[str]) -> tuple[str, ...]:
    """The names of a suggestion's files, from their paths in its artifact folder."""

    return tuple(name for name in (_name(path).strip() for path in paths) if name)


__all__ = [
    "answer_status_line",
    "artifact_names",
    "first_line",
    "linked_document_names",
]

"""What the model reads in place of a tool result too large to keep inline.

The result keeps its own shape: only its longest text values move to files of
their own, written as the text itself so a line-based read pages through it, and
each moved value leaves a reference that holds its head and says how to read the
rest. Small fields such as a status or an exit code stay where they were.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Set
from functools import partial
from typing import Literal, TypeGuard

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output

ACTION_FILE_STORAGE: Literal["action_file"] = "action_file"
SPILLED_TEXT_MEDIA_TYPE: Literal["text/plain"] = "text/plain"
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
# The result's own status, kept beside the reference only when the result has one.
TOOL_RESULT_STATUS_KEY = "status"
SPILLED_TEXT_KEYS = TOOL_RESULT_JSON_METADATA_KEYS | TOOL_RESULT_PREVIEW_KEYS
TOOL_RESULT_PREVIEW_CHARS = 2_000


def is_json_spill_shape(keys: Set[str]) -> bool:
    return keys - {TOOL_RESULT_STATUS_KEY} in (
        TOOL_RESULT_JSON_METADATA_KEYS,
        TOOL_RESULT_JSON_METADATA_KEYS | TOOL_RESULT_PREVIEW_KEYS,
    )


def is_spilled_text(value: JSONValue) -> TypeGuard[dict[str, JSONValue]]:
    return (
        isinstance(value, dict)
        and value.get("storage") == ACTION_FILE_STORAGE
        and value.get("media_type") == SPILLED_TEXT_MEDIA_TYPE
    )


def spilled_texts(output: JSONValue) -> list[dict[str, JSONValue]]:
    """Every moved-text reference in a result, in document order."""

    return [value for value in _values(output) if is_spilled_text(value)]


def spill_long_texts(
    output: JSONValue, *, limit: int, new_path: Callable[[], str]
) -> tuple[JSONValue, list[tuple[str, str]]] | None:
    """Move the longest text values out until the result fits ``limit`` characters.

    Returns the result with references in place of the moved values, and each moved
    value's path and text. Returns None when the bulk is not in long text values,
    or when the result already holds something shaped like a reference, which a
    reader could not tell from the ones this puts in.
    """

    if any(
        isinstance(value, dict) and value.get("storage") == ACTION_FILE_STORAGE
        for value in _values(output)
    ):
        return None
    holder: list[JSONValue] = [copy.deepcopy(output)]
    candidates = sorted(
        (
            (text, put)
            for text, put in _texts(holder)
            if len(text) > TOOL_RESULT_PREVIEW_CHARS
        ),
        key=lambda candidate: len(candidate[0]),
        reverse=True,
    )
    # Design limit: each moved value keeps a preview of up to 2,000 characters, so
    # about eight fit the 20,000-character limit and a result with more moves whole
    # as JSON. Share the preview budget when such results show up.
    moved: list[tuple[str, str]] = []
    for text, put in candidates:
        file_path = new_path()
        put(_spilled_text(text=text, path=file_path))
        moved.append((file_path, text))
        if len(serialize_json_tool_output(holder[0])) <= limit:
            return holder[0], moved
    return None


def spill_preview(*, text: str, path: str) -> tuple[str, str]:
    """The head of a spilled text, cut at a line end, and how to read all of it."""

    preview = text[:TOOL_RESULT_PREVIEW_CHARS]
    if len(preview) < len(text) and (line_end := preview.rfind("\n")) > 0:
        preview = preview[:line_end]
    # The line after the preview, or the one it stops in when it ends mid-line.
    next_line = text.count("\n", 0, len(preview) + 1) + 1
    line_count = text.count("\n") + 1
    return preview, (
        f"This result is {len(text):,} characters in {line_count:,} "
        f"lines, too large to show inline; preview holds only its first "
        f"{len(preview):,}. Read the rest with read path={path} offset={next_line}, "
        "continuing with next_offset, or search it with grep."
    )


def _spilled_text(*, text: str, path: str) -> dict[str, JSONValue]:
    preview, retry_hint = spill_preview(text=text, path=path)
    return {
        "storage": ACTION_FILE_STORAGE,
        "path": path,
        "media_type": SPILLED_TEXT_MEDIA_TYPE,
        "byte_size": len(text.encode("utf-8")),
        "character_count": len(text),
        "line_count": text.count("\n") + 1,
        "preview": preview,
        "retry_hint": retry_hint,
    }


def _values(value: JSONValue) -> Iterator[JSONValue]:
    yield value
    if is_spilled_text(value):
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from _values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _values(child)


def _texts(
    container: dict[str, JSONValue] | list[JSONValue],
) -> Iterator[tuple[str, Callable[[JSONValue], None]]]:
    """Each text value under ``container``, with a setter for its place."""

    places: list[tuple[JSONValue, Callable[[JSONValue], None]]] = (
        [
            (value, partial(container.__setitem__, key))
            for key, value in container.items()
        ]
        if isinstance(container, dict)
        else [
            (value, partial(container.__setitem__, index))
            for index, value in enumerate(container)
        ]
    )
    for value, put in places:
        if isinstance(value, str):
            yield value, put
        elif isinstance(value, dict | list):
            yield from _texts(value)


def replace_spilled_texts(
    output: JSONValue, text_of: Callable[[dict[str, JSONValue]], str]
) -> JSONValue:
    """The result with each moved-text reference replaced by ``text_of`` it."""

    if is_spilled_text(output):
        return text_of(output)
    if isinstance(output, dict):
        return {
            key: replace_spilled_texts(value, text_of) for key, value in output.items()
        }
    if isinstance(output, list):
        return [replace_spilled_texts(value, text_of) for value in output]
    return output

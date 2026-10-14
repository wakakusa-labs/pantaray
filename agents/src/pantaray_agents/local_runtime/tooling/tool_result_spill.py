"""What the model reads in place of a tool result too large to keep inline.

The result keeps its own shape: only its longest text values move to files of
their own, written as the text itself so a line-based read pages through it, and
each moved value leaves a reference that holds its head and tail and says how to
read the rest. Small fields such as a status or an exit code stay where they were.
The previews share whatever room the inline limit leaves, so what is shown inline
is as much of the result as fits.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator, Set
from dataclasses import dataclass
from functools import partial
from typing import Literal, TypeGuard

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    largest_fitting_size,
    serialize_json_tool_output,
)
from pantaray_agents.tools.files.read_sniffing import SAMPLE_BYTES, reads_as_text
from pantaray_agents.tools.files.text_lines import LINE_BREAK, count_text_lines

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
# A text this short is not moved out alone: a result whose bulk is in many such
# values is stored whole, and its outline shows them better than many previews
# too small to say anything.
SPILL_MIN_TEXT_CHARS = 2_000
# A preview gives one part in this many to the end of its text. Command output
# ends with what matters most (the error, the failing tests, the exit summary),
# pages and listings start with it, so the head keeps the larger share.
_PREVIEW_TAIL_DIVISOR = 4
# What the outline of a result stored whole counts as a short value, such as a
# title, a URL, a path or a status; longer texts are only in the file.
OUTLINE_VALUE_CHARS = 200


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


# A preview and how to read the rest of what it shows part of, for a window of
# at most this many characters of preview.
type Previewer = Callable[[int], tuple[str, str]]


def spill_long_texts(
    output: JSONValue, *, limit: int, new_path: Callable[[], str]
) -> tuple[JSONValue, list[tuple[str, str]]] | None:
    """Move long text values out until the result fits ``limit`` characters.

    The long texts share the room under ``limit`` alike: each up to one length
    stays in place, and each longer one moves out, leaving a reference whose
    preview shows that much of it. Returns the result and each moved value's
    path and text. A text read would not page as text is not moved. Returns None
    when the result does not fit even with every long text moved, or when it
    already holds something shaped like a reference, which a reader could not
    tell from the ones this puts in.
    """

    if any(
        isinstance(value, dict) and value.get("storage") == ACTION_FILE_STORAGE
        for value in _values(output)
    ):
        return None
    holder: list[JSONValue] = [copy.deepcopy(output)]
    texts = [
        _LongText.of(text=text, path=new_path(), put=put)
        for text, put in _texts(holder)
        if len(text) > SPILL_MIN_TEXT_CHARS and _readable_as_text(text)
    ]

    def shown_up_to(length: int) -> JSONValue:
        for text in texts:
            text.put(text.shown(length))
        return holder[0]

    if not texts or len(serialize_json_tool_output(shown_up_to(0))) > limit:
        return None
    # Neither a text kept in place nor a preview can be longer than the limit.
    length = largest_fitting_size(
        shown_up_to,
        high=min(limit, max(len(text.text) for text in texts)),
        limit=limit,
    )
    return shown_up_to(length), [
        (text.path, text.text) for text in texts if len(text.text) > length
    ]


@dataclass(frozen=True, slots=True)
class _LongText:
    text: str
    path: str
    put: Callable[[JSONValue], None]
    # The reference without its preview, measured once however often it is shown.
    reference: dict[str, JSONValue]
    previewer: Previewer

    @classmethod
    def of(cls, *, text: str, path: str, put: Callable[[JSONValue], None]) -> _LongText:
        line_count = count_text_lines(text)
        return cls(
            text=text,
            path=path,
            put=put,
            reference={
                "storage": ACTION_FILE_STORAGE,
                "path": path,
                "media_type": SPILLED_TEXT_MEDIA_TYPE,
                "byte_size": len(text.encode("utf-8")),
                "character_count": len(text),
                "line_count": line_count,
            },
            previewer=spill_previewer(text=text, path=path, line_count=line_count),
        )

    def shown(self, length: int) -> JSONValue:
        """The text itself when it is at most ``length`` long, else its reference
        with a preview of up to ``length`` characters."""

        if len(self.text) <= length:
            return self.text
        preview, retry_hint = self.previewer(length)
        return {**self.reference, "preview": preview, "retry_hint": retry_hint}


def _readable_as_text(text: str) -> bool:
    """Whether read would page a file holding ``text`` as text: not one it takes
    for a PDF or an image by its first bytes, nor refuses as binary (as it does
    NUL-separated output such as find -print0)."""

    return reads_as_text(text[:SAMPLE_BYTES].encode("utf-8")[:SAMPLE_BYTES])


def spill_previewer(*, text: str, path: str, line_count: int) -> Previewer:
    """The head and tail of a spilled text, cut at line ends, and how to read the
    lines between.

    Lines end where read ends them (also at a lone carriage return, as progress
    output writes), so the line numbers given here are the ones read takes. A
    text with no line start near its end shows only its head.
    """

    size = f"This result is {len(text):,} characters in {line_count:,} lines"

    def preview(window: int) -> tuple[str, str]:
        tail_window = window // _PREVIEW_TAIL_DIVISOR
        head, next_line, head_end = _head(text, window - tail_window)
        # The tail starts at the first line start that leaves it within its window.
        tail_start = next(
            (
                line_end.end()
                for line_end in LINE_BREAK.finditer(
                    text, max(head_end, len(text) - tail_window - 1)
                )
                if len(text) - tail_window <= line_end.end() < len(text)
            ),
            None,
        )
        if tail_start is None:
            head, next_line, _end = _head(text, window)
            return head, (
                f"{size}, too large to show inline; preview holds only its first "
                f"{len(head):,}. Read the rest with read path={path} "
                f"offset={next_line}, continuing with next_offset, or search it "
                "with grep."
            )
        tail = text[tail_start:]
        last_left_out = line_count - count_text_lines(tail)
        marker = f"[… lines {next_line:,}–{last_left_out:,} left out …]"
        return f"{head}\n{marker}\n{tail}", (
            f"{size}, too large to show inline; preview holds its first "
            f"{len(head):,} and last {len(tail):,} characters, with lines "
            f"{next_line:,} to {last_left_out:,} left out where it says so. Read "
            f"them with read path={path} offset={next_line}, continuing with "
            "next_offset, or search it with grep."
        )

    return preview


def _head(text: str, window: int) -> tuple[str, int, int]:
    """Up to ``window`` characters from the start of ``text``, cut at a line end;
    the line read starts after it, and where in ``text`` what it leaves begins."""

    # One character past the window keeps a CRLF that straddles its end whole.
    breaks = [
        line_end
        for line_end in LINE_BREAK.finditer(text, 0, window + 1)
        if line_end.start() < window
    ]
    cut = next(
        (index for index in reversed(range(len(breaks))) if breaks[index].start()),
        None,
    )
    if len(text) > window and cut is not None:
        return text[: breaks[cut].start()], cut + 2, breaks[cut].end()
    # No line end to cut at: the preview stops inside the line read starts on.
    head = text[:window]
    next_line = 1 + sum(1 for line_end in breaks if line_end.end() <= window)
    return head, next_line, len(head)


def outline_previewer(*, output: JSONValue, text: str, path: str) -> Previewer:
    """An outline of a result stored whole as JSON, and how to read all of it.

    Such a result holds many values, none long enough to move out alone, so its
    head would show only the first few. The outline lists the short values of
    every object instead, one object per line under its place in the result,
    so what the result holds and where can be seen without opening the file.
    """

    line_count = count_text_lines(text)
    lines = list(_outline_lines(output, place=""))
    if len(lines) <= 1:
        # A result this large with at most one line of short values holds its
        # bulk in the texts the outline leaves out, so their head says more.
        return spill_previewer(text=text, path=path, line_count=line_count)
    retry_hint = (
        f"This result is {len(text):,} characters in {line_count:,} lines of "
        "JSON, too large to show inline; preview outlines it with its values of "
        f"up to {OUTLINE_VALUE_CHARS} characters, without the longer texts. Read "
        f"all of it with read path={path} offset=1, continuing with next_offset, "
        "or search it with grep."
    )

    def more(shown: int) -> list[str]:
        hidden = len(lines) - shown
        return [f"… {hidden:,} more of {len(lines):,} lines"] if hidden else []

    def preview(window: int) -> tuple[str, str]:
        shown, length = 0, -1
        # The lines shown so far, joined, with the line saying how many are left.
        while shown < len(lines) and (
            length
            + 1
            + len(lines[shown])
            + sum(len(line) + 1 for line in more(shown + 1))
            <= window
        ):
            length += 1 + len(lines[shown])
            shown += 1
        head = lines[:shown] or [lines[0][: window // 2] + "…"]
        return "\n".join([*head, *more(len(head))]), retry_hint

    return preview


def _outline_lines(value: JSONValue, *, place: str) -> Iterator[str]:
    prefix = f"{place}: " if place else ""
    if isinstance(value, dict):
        fields = " ".join(
            f"{key}={_outline_value(child)}"
            for key, child in value.items()
            if _is_short(child)
        )
        if fields:
            yield prefix + fields
        for key, child in value.items():
            if isinstance(child, dict | list):
                yield from _outline_lines(
                    child, place=f"{place}.{key}" if place else key
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, dict | list):
                yield from _outline_lines(child, place=f"{place}[{index}]")
            elif _is_short(child):
                yield f"{place}[{index}]: {_outline_value(child)}"
    elif _is_short(value):
        yield _outline_value(value)


def _is_short(value: JSONValue) -> bool:
    if isinstance(value, str):
        return len(value) <= OUTLINE_VALUE_CHARS and LINE_BREAK.search(value) is None
    return not isinstance(value, dict | list)


def _outline_value(value: JSONValue) -> str:
    return json.dumps(value, ensure_ascii=False)


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


# What storage puts in place of a long text value: its head and how to read the rest.
SPILLED_TEXT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*sorted(SPILLED_TEXT_KEYS)],
    "properties": {
        "storage": {"type": "string", "enum": ["action_file"]},
        "path": {"type": "string", "minLength": 1},
        "media_type": {"type": "string", "enum": [SPILLED_TEXT_MEDIA_TYPE]},
        "byte_size": {"type": "integer", "minimum": 0},
        "character_count": {"type": "integer", "minimum": 0},
        "line_count": {"type": "integer", "minimum": 1},
        "preview": {"type": "string"},
        "retry_hint": {"type": "string"},
    },
}


def accepting_spilled_texts(schema: JSONValue) -> JSONValue:
    """``schema`` with each text value also allowed to be a moved-text reference,
    which storage puts in place of a long text when the result is too large."""

    if not isinstance(schema, dict):
        return schema
    adjusted: dict[str, JSONValue] = {}
    for key, value in schema.items():
        if key in ("properties", "patternProperties", "$defs") and isinstance(
            value, dict
        ):
            adjusted[key] = {
                name: accepting_spilled_texts(child) for name, child in value.items()
            }
        elif key in ("items", "additionalProperties", "anyOf", "oneOf", "allOf"):
            adjusted[key] = (
                [accepting_spilled_texts(child) for child in value]
                if isinstance(value, list)
                else accepting_spilled_texts(value)
            )
        else:
            adjusted[key] = value
    schema_type = schema.get("type")
    if schema_type == "string" or (
        isinstance(schema_type, list) and "string" in schema_type
    ):
        return {"anyOf": [adjusted, dict(SPILLED_TEXT_SCHEMA)]}
    return adjusted

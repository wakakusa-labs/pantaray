"""Locale-neutral row material derived from one durable Tool step.

A conversation row says what the step acted on, so the projection publishes the one
salient argument (the subject) and, when the step has no such argument, the head of the
result it produced. Both are data only: the sentence around them belongs to the UI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    ToolOutputStorageKind,
    serialize_json_tool_output,
)

SUBJECT_MAX_CHARACTERS = 120
OUTPUT_PREVIEW_MAX_CHARACTERS = 80

_ELLIPSIS = "…"
_WHITESPACE_RUN = re.compile(r"\s+")
# Encoded bytes are not a preview: a data URL or a long unbroken base64 run tells a
# reader nothing and would fill the row with noise.
_ENCODED_RUN_MIN_CHARACTERS = 48
_ENCODED_PAYLOAD = re.compile(
    rf"data:[^;\s\"]*;base64,|[A-Za-z0-9+/]{{{_ENCODED_RUN_MIN_CHARACTERS},}}={{0,2}}"
)
# Neither is a serialized object or list: a row that ends in `{ "status": "draft_updated" }`
# shows the result's shape instead of saying anything. Only prose is worth a preview.
_STRUCTURE_OPENERS = ("{", "[")
# Hidden internal reasoning never reaches a row. `thinking` returns "detailed thinking and a
# concise answer (not shown to the user)" by its own definition, so its result is withheld
# here rather than left to depend on the shape its output happens to take.
_UNPREVIEWED_TOOLS = frozenset({"thinking"})

type _ArgsSubject = Callable[[dict[str, JSONValue]], str | None]


def _text(*fields: str) -> _ArgsSubject:
    """Read the first named argument that carries text."""

    def read(args: dict[str, JSONValue]) -> str | None:
        for field in fields:
            value = args.get(field)
            if isinstance(value, str) and value.strip():
                return value
        return None

    return read


def _joined(field: str) -> _ArgsSubject:
    """Read one list-of-strings argument as a single comma-separated subject."""

    def read(args: dict[str, JSONValue]) -> str | None:
        values = args.get(field)
        if not isinstance(values, list):
            return None
        items = [item for item in values if isinstance(item, str) and item.strip()]
        return ", ".join(items) or None

    return read


def _changed_paths(args: dict[str, JSONValue]) -> str | None:
    """Name the workspace files one patch touched."""

    changes = args.get("changes")
    if not isinstance(changes, list):
        return None
    paths = [
        change["path"]
        for change in changes
        if isinstance(change, dict)
        and isinstance(change.get("path"), str)
        and str(change["path"]).strip()
    ]
    return ", ".join(paths) or None


def _pattern_in_path(args: dict[str, JSONValue]) -> str | None:
    """Name what a text search looked for, and where when the caller said so."""

    pattern = _text("pattern")(args)
    if pattern is None:
        return None
    base_path = _text("base_path", "path")(args)
    return pattern if base_path is None else f"{pattern} ({base_path})"


# Only a tool whose call names what it acted on has a subject. A tool that always acts on
# the current plan or the answer being written has none, and its row falls back to the
# head of its result.
_TOOL_SUBJECTS: dict[str, _ArgsSubject] = {
    "read": _text("path"),
    "render_pdf_page": _text("path"),
    "list": _text("path"),
    "glob": _text("pattern"),
    "grep": _pattern_in_path,
    "bash": _text("command"),
    "run_python": _text("code"),
    "apply_patch": _changed_paths,
    "capture_screen": _text("app_name"),
    "web_search": _text("query"),
    "web_extract": _joined("urls"),
    "web_crawl": _text("url"),
    "memory_search": _text("query"),
    "memory_sql": _text("sql"),
    "get_memory_reference": _text("source_handle", "local_ref_id"),
    "link_memory": _text("target_handle"),
    "unlink_memory": _text("local_ref_id"),
    "remember": _text("note"),
    "history_fetch": _joined("refs"),
    "zanei_query": _text("event_id"),
    "spawn_subagent": _text("task"),
    "send_message_to_subagent": _text("content"),
    "wait_subagents": _joined("child_process_ids"),
    "cancel_subagent": _text("child_process_id"),
}


def project_tool_subject(tool_id: str, tool_args: str | None) -> str | None:
    """Name the one argument that says what this Tool step acted on."""

    read = _TOOL_SUBJECTS.get(tool_id)
    if read is None or tool_args is None:
        return None
    # agent_action_steps.tool_args carries a json_valid CHECK, so the column parses.
    payload = json.loads(tool_args)
    if not isinstance(payload, dict):
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    value = read(args)
    return None if value is None else _first_line(value, SUBJECT_MAX_CHARACTERS)


def project_tool_output_preview(
    tool_id: str, output: JSONValue, storage_kind: ToolOutputStorageKind
) -> str | None:
    """Preview the head of one prose Tool result the page can already read inline.

    A spilled action-file result stays unpreviewed: this projection reads the durable row
    only, and the reader can open the output panel for the whole body. A structured result
    stays unpreviewed too, because its serialization says nothing the row can use, and a
    tool whose result is hidden reasoning is never previewed at all.
    """

    if tool_id in _UNPREVIEWED_TOOLS or storage_kind != "inline_json" or output is None:
        return None
    text = output if isinstance(output, str) else serialize_json_tool_output(output)
    collapsed = _WHITESPACE_RUN.sub(" ", text).strip()
    if not collapsed or collapsed.startswith(_STRUCTURE_OPENERS):
        return None
    # Look for encoded bytes before cutting, and far enough past the cut that a run
    # beginning inside the preview is still whole enough to match: a run that starts late
    # keeps too few characters after truncation to reach the pattern's minimum length.
    lookahead = OUTPUT_PREVIEW_MAX_CHARACTERS + _ENCODED_RUN_MIN_CHARACTERS
    if _ENCODED_PAYLOAD.search(collapsed[:lookahead]):
        return None
    if len(collapsed) > OUTPUT_PREVIEW_MAX_CHARACTERS:
        return collapsed[:OUTPUT_PREVIEW_MAX_CHARACTERS] + _ELLIPSIS
    return collapsed


def _first_line(value: str, limit: int) -> str | None:
    """Keep the first line, and say with an ellipsis that the rest was cut."""

    head, _, rest = value.strip().partition("\n")
    collapsed = _WHITESPACE_RUN.sub(" ", head).strip()
    if not collapsed:
        return None
    if len(collapsed) > limit:
        return collapsed[:limit] + _ELLIPSIS
    return collapsed + _ELLIPSIS if rest.strip() else collapsed


__all__ = [
    "OUTPUT_PREVIEW_MAX_CHARACTERS",
    "SUBJECT_MAX_CHARACTERS",
    "project_tool_output_preview",
    "project_tool_subject",
]

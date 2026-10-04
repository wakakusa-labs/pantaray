"""Deterministic one-line tool outcomes for the Action History body.

The Supervisor never writes this line: the runtime derives it from the tool id
and the finalized output so that a step's history row is final the moment it is
appended. The note (``HistoryEntry.summary``) carries the model's intent; this
line carries what actually happened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from pantaray_agents.schema.action_conversation import RENDERER_PREPARING_OUTPUT_KIND
from pantaray_agents.schema.agent.base import JSONValue

OutcomeStatus = Literal[
    "ok",
    "failed",
    "awaiting_approval",
    "stopped",
    "interrupted",
    "not_executed",
]

_UNKNOWN_FAILURE_CODE = "UNKNOWN"
_ACTION_FILE_STORAGE = "action_file"


def build_outcome_line(
    tool_id: str,
    output: JSONValue,
    *,
    status: OutcomeStatus,
    error_code: str | None = None,
) -> str:
    """Render one locale-free ASCII line describing how a tool call ended."""

    match status:
        case "awaiting_approval":
            return f"{tool_id}: awaiting approval"
        case "stopped":
            return f"{tool_id}: stopped"
        case "not_executed":
            # The Stop was already durable before this call was issued, so it
            # had no effect at all and nothing has to be checked before retrying.
            return f"{tool_id}: not executed, stopped by user"
        case "interrupted":
            # Issued, then cut off. What it did to the world is not knowable from
            # here, and the line must not let the Supervisor assume otherwise.
            detail = _captured_detail(output)
            return f"{tool_id}: interrupted by user, outcome unknown{detail}"
        case "failed":
            code = _failure_code(output) or error_code or _UNKNOWN_FAILURE_CODE
            return f"{tool_id}: failed {code}"
        case "ok":
            return f"{tool_id}: ok{_success_detail(tool_id, output)}"


def _captured_detail(output: JSONValue) -> str:
    """Report how much output an interrupted call had already produced."""

    if not isinstance(output, Mapping):
        return ""
    captured = "".join(
        value
        for key in ("stdout", "stderr")
        for value in (output.get(key),)
        if isinstance(value, str)
    )
    return f", {_chars(captured)} captured" if captured else ""


def _success_detail(tool_id: str, output: JSONValue) -> str:
    """Return the ``, ...`` tail of a successful outcome line, or nothing."""

    if not isinstance(output, Mapping):
        return ""
    spill = _spill_detail(output)
    if spill is not None:
        return spill
    return "".join(f", {part}" for part in _tool_detail(tool_id, output))


def _tool_detail(tool_id: str, body: Mapping[str, JSONValue]) -> list[str]:
    match tool_id:
        case "read":
            return _read_detail(body)
        case "render_pdf_page":
            if body.get("kind") == RENDERER_PREPARING_OUTPUT_KIND:
                return ["no pages yet, viewer preparing"]
            return [_count(body, "attachments", "page", "pages")]
        case "list":
            return [_count(body, "entries", "entry", "entries")]
        case "glob" | "grep":
            return [_count(body, "matches", "match", "matches")]
        case "bash" | "run_python":
            return _command_detail(body)
        case "apply_patch":
            return _apply_patch_detail(body)
        case "web_search" | "memory_search":
            return [_count(body, "results", "result", "results")]
        case "web_extract":
            return _web_extract_detail(body)
        case "web_crawl":
            return [_count(body, "results", "page", "pages"), *_text(body, "base_url")]
        case "history_fetch":
            return [
                _count(body, "refs", "ref", "refs"),
                _chars(cast(str, body["content"])),
                *(["more available"] if body["next_cursor"] is not None else []),
            ]
        case "wait_subagents":
            return _wait_subagents_detail(body)
        case "zanei_timeline" | "zanei_query":
            return [_count(body, "events", "event", "events")]
        case _:
            return []


def _read_detail(body: Mapping[str, JSONValue]) -> list[str]:
    match body.get("kind"):
        case "file" | "document":
            content = body.get("content")
            return [_chars(content if isinstance(content, str) else "")]
        case "directory":
            return [_count(body, "entries", "entry", "entries")]
        case "attachment":
            return [_count(body, "attachments", "attachment", "attachments")]
        case _:
            return []


def _command_detail(body: Mapping[str, JSONValue]) -> list[str]:
    streams = "".join(
        value
        for key in ("stdout", "stderr")
        for value in (body.get(key),)
        if isinstance(value, str)
    )
    exit_code = body.get("exit_code")
    prefix = (
        [f"exit {exit_code}"]
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else []
    )
    return [*prefix, _chars(streams)]


def _apply_patch_detail(body: Mapping[str, JSONValue]) -> list[str]:
    details = [_count(body, "applied_paths", "file", "files")]
    diff = body.get("diff")
    if isinstance(diff, str) and diff:
        lines = diff.splitlines()
        added = sum(1 for line in lines if line[:1] == "+" and line[:3] != "+++")
        removed = sum(1 for line in lines if line[:1] == "-" and line[:3] != "---")
        details.append(f"+{added} -{removed}")
    return details


def _web_extract_detail(body: Mapping[str, JSONValue]) -> list[str]:
    results = [item for item in _items(body, "results") if isinstance(item, Mapping)]
    extracted = "".join(
        content
        for result in results
        for content in (result.get("raw_content"),)
        if isinstance(content, str)
    )
    urls = [url for result in results for url in _text(result, "url")]
    return [_chars(extracted), *urls[:1]]


def _wait_subagents_detail(body: Mapping[str, JSONValue]) -> list[str]:
    results = _items(body, "results")
    done = sum(
        1
        for result in results
        if isinstance(result, Mapping) and result.get("status") != "running"
    )
    return [f"{done:,} done", f"{len(results) - done:,} running"]


def _spill_detail(body: Mapping[str, JSONValue]) -> str | None:
    """Describe a result the runtime moved out of line into an action file."""

    if body.get("storage") != _ACTION_FILE_STORAGE:
        return None
    paths = _text(body, "path")
    if not paths:
        return None
    for key, unit in (("character_count", "chars"), ("byte_size", "bytes")):
        size = body.get(key)
        if isinstance(size, int) and not isinstance(size, bool):
            return f", spilled to {paths[0]}, {size:,} {unit}"
    return f", spilled to {paths[0]}"


def _failure_code(output: JSONValue) -> str | None:
    """Read the tool's own failure code, when the tool reported one.

    Only a declared code counts: a Python exception class name would name the
    transport, not the failure the Supervisor has to react to.
    """

    if not isinstance(output, Mapping):
        return None
    error = output.get("error")
    if not isinstance(error, Mapping):
        return None
    codes = [code for key in ("code", "error_code") for code in _text(error, key)]
    return codes[0] if codes else None


def _text(body: Mapping[str, JSONValue], key: str) -> list[str]:
    value = body.get(key)
    return [value] if isinstance(value, str) and value else []


def _items(body: Mapping[str, JSONValue], key: str) -> Sequence[JSONValue]:
    value = body.get(key)
    return value if isinstance(value, list) else ()


def _count(body: Mapping[str, JSONValue], key: str, one: str, many: str) -> str:
    total = len(_items(body, key))
    return f"{total:,} {one if total == 1 else many}"


def _chars(text: str) -> str:
    return f"{len(text):,} char{'' if len(text) == 1 else 's'}"


__all__ = ["OutcomeStatus", "build_outcome_line"]

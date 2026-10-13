"""Deterministic one-line tool outcomes for the Action History body.

The Supervisor never writes this line: the runtime derives it from the tool id
and the finalized output so that a step's history row is final the moment it is
appended. The note (``HistoryEntry.summary``) carries the model's intent; this
line carries what actually happened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from pantaray_agents.local_runtime.tooling.brokering.broker_structured_patch import (
    count_patch_diff_lines,
)
from pantaray_agents.local_runtime.tooling.tool_result_spill import is_spilled_text
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
    captured = sum(_length(output.get(key)) for key in ("stdout", "stderr"))
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
                _chars(len(cast(str, body["content"]))),
                *(["more available"] if body["next_cursor"] is not None else []),
            ]
        case "wait_subagents":
            return _wait_subagents_detail(body)
        case "send_message_to_subagent":
            # A stopped or finished subagent goes on as a new one.
            return [
                f"continued {finished} as new subagent {child}"
                for finished in _text(body, "continues_child_process_id")
                for child in _text(body, "child_process_id")
            ]
        case "zanei_timeline" | "zanei_query":
            return [_count(body, "events", "event", "events")]
        case _:
            return []


def _read_detail(body: Mapping[str, JSONValue]) -> list[str]:
    match body.get("kind"):
        case "file" | "document":
            return [_chars(_length(body.get("content")))]
        case "directory":
            return [_count(body, "entries", "entry", "entries")]
        case "attachment":
            return [_count(body, "attachments", "attachment", "attachments")]
        case _:
            return []


def _command_detail(body: Mapping[str, JSONValue]) -> list[str]:
    streams = sum(_length(body.get(key)) for key in ("stdout", "stderr"))
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
    counts = count_patch_diff_lines(diff) if isinstance(diff, str) and diff else None
    if counts is not None:
        details.append(f"+{counts[0]} -{counts[1]}")
    return details


def _web_extract_detail(body: Mapping[str, JSONValue]) -> list[str]:
    results = [item for item in _items(body, "results") if isinstance(item, Mapping)]
    extracted = sum(_length(result.get("raw_content")) for result in results)
    urls = [url for result in results for url in _text(result, "url")]
    return [_chars(extracted), *urls[:1]]


def _wait_subagents_detail(body: Mapping[str, JSONValue]) -> list[str]:
    results = _items(body, "results")
    # A child still going is "nonterminal"; every other status is an end.
    done = sum(
        1
        for result in results
        if isinstance(result, Mapping) and result.get("status") != "nonterminal"
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
    # Tools shared with Suggestion and the chat report the shared tool error
    # shape, whose code sits beside the status rather than under ``error``.
    body = error if isinstance(error, Mapping) else output
    codes = [code for key in ("code", "error_code") for code in _text(body, key)]
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


def _length(value: JSONValue | None) -> int:
    """A text's length, also when storage moved the text to a file of its own."""

    if isinstance(value, str):
        return len(value)
    if is_spilled_text(value):
        return cast(int, value["character_count"])
    return 0


def _chars(count: int) -> str:
    return f"{count:,} char{'' if count == 1 else 's'}"


__all__ = ["OutcomeStatus", "build_outcome_line"]

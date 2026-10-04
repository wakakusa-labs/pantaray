from __future__ import annotations

import json
import sqlite3
from typing import TypedDict, cast

from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_history import (
    HISTORY_FETCH_MAX_REFS,
    HISTORY_FETCH_SHORT_STEP_PATTERN,
    HISTORY_FETCH_SHORT_STEP_RE,
    history_fetch_step_schema,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.local_time import describe_utc_timestamp

from .conversation import (
    MemoryActionConversation,
    conversation_kind_schema,
    memory_action_conversation_schema,
    project_memory_action_conversation,
)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
SEARCH_EXCERPT_CHARS = 500


class StoredActionHistoryError(RuntimeError):
    """Persisted Action history violates its LLM-visible contract."""


class MemoryHistoryStep(TypedDict):
    """Memory evidence excludes the Action's execution prompt and reasoning."""

    short_step_id: str
    step_number: int
    local_step_number: int
    step_name: str
    step_type: str
    status: str
    conversation: MemoryActionConversation | None
    llm_response_text: str | None
    tool_args: dict[str, JSONValue] | None
    tool_output: dict[str, JSONValue] | None
    error: dict[str, JSONValue] | None
    goal_handle: str | None
    started_at: str | None
    completed_at: str | None


def parse_page(value: JSONValue) -> tuple[int, int]:
    args = _arguments(value)
    return _bounded_int(args, "offset", 0, minimum=0), _bounded_int(
        args,
        "limit",
        DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=MAX_PAGE_SIZE,
    )


def parse_search_request(value: JSONValue) -> tuple[str, int, int]:
    args = _arguments(value)
    query = _required_string(args, "query")
    offset, limit = parse_page(value)
    return query, offset, limit


def parse_from_step(value: JSONValue, default: int) -> int:
    return _bounded_int(_arguments(value), "from_step", default, minimum=1)


def parse_history_refs(value: JSONValue) -> tuple[str, ...]:
    args = _arguments(value)
    raw_refs = args.get("refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        raise ValueError("history_fetch refs must be a non-empty list")
    if len(raw_refs) > HISTORY_FETCH_MAX_REFS:
        raise ValueError(f"history_fetch refs must not exceed {HISTORY_FETCH_MAX_REFS}")
    refs: list[str] = []
    for raw_ref in raw_refs:
        if (
            not isinstance(raw_ref, str)
            or HISTORY_FETCH_SHORT_STEP_RE.fullmatch(raw_ref) is None
        ):
            raise ValueError(f"history_fetch ref is not canonical: {raw_ref!r}")
        refs.append(raw_ref)
    if len(set(refs)) != len(refs):
        raise ValueError("history_fetch refs must not contain duplicates")
    return tuple(refs)


def step_metadata(row: sqlite3.Row) -> dict[str, JSONValue]:
    return {
        "short_step_id": _required_ref(row["short_step_id"]),
        "step_number": _required_int(row["step_number"], field="step_number"),
        "local_step_number": _required_int(
            row["local_step_number"], field="local_step_number"
        ),
        "step_type": _required_text(row["step_type"], field="step_type"),
        "step_name": _required_text(row["step_name"], field="step_name"),
        "status": _required_text(row["status"], field="status"),
        "goal_handle": _optional_text(row["goal_handle"], field="goal_handle"),
        "retry_count": _required_int(row["retry_count"], field="retry_count"),
        "created_at": _local_time(row["created_at"], field="created_at"),
        "completed_at": _optional_local_time(row["completed_at"], field="completed_at"),
    }


def search_match(row: sqlite3.Row, *, query: str) -> dict[str, JSONValue]:
    conversation = memory_conversation_from_row(row)
    values = (
        row["user_request_text"],
        row["llm_response_text"],
        row["tool_args"],
        row["tool_output"],
        row["error"],
    )
    content = "\n".join(str(value) for value in values if value is not None)
    position = content.casefold().find(query.casefold())
    start = max(0, position - SEARCH_EXCERPT_CHARS // 2)
    return {
        "short_step_id": _required_ref(row["short_step_id"]),
        "step_number": _required_int(row["step_number"], field="step_number"),
        "step_type": _required_text(row["step_type"], field="step_type"),
        "step_name": _required_text(row["step_name"], field="step_name"),
        "status": _required_text(row["status"], field="status"),
        "excerpt": content[start : start + SEARCH_EXCERPT_CHARS],
        "conversation_origin": conversation["origin"]
        if conversation is not None
        else None,
    }


def history_fetch_step(row: sqlite3.Row, *, ref: str) -> MemoryHistoryStep:
    tool_output = _optional_json_object(row["tool_output"], field="tool_output")
    return {
        "short_step_id": ref,
        "step_number": _required_int(row["step_number"], field="step_number"),
        "local_step_number": _required_int(
            row["local_step_number"], field="local_step_number"
        ),
        "step_name": _required_text(row["step_name"], field="step_name"),
        "step_type": _required_text(row["step_type"], field="step_type"),
        "status": _required_text(row["status"], field="status"),
        "conversation": memory_conversation_from_row(row),
        "llm_response_text": (
            None
            if row["step_type"] == StepType.ASSISTANT_MESSAGE.value
            else _optional_text(row["llm_response_text"], field="llm_response_text")
        ),
        "tool_args": _optional_json_object(row["tool_args"], field="tool_args"),
        "tool_output": _memory_tool_output(tool_output)
        if tool_output is not None
        else None,
        "error": _optional_json_object(row["error"], field="error"),
        "goal_handle": _optional_text(row["goal_handle"], field="goal_handle"),
        "started_at": _optional_local_time(row["started_at"], field="started_at"),
        "completed_at": _optional_local_time(row["completed_at"], field="completed_at"),
    }


def memory_tool_output_json(value: str | None) -> str | None:
    """SQLite search sees the same image projection as history_fetch.

    The storage column guarantees valid JSON but permits legacy JSON values.
    Tool payloads are unstructured; only their declared attachment boundary is edited.
    """
    if value is None:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return value
    projected = _memory_tool_output(decoded)
    return value if projected is decoded else json.dumps(projected, ensure_ascii=False)


def _memory_tool_output(value: dict[str, JSONValue]) -> dict[str, JSONValue]:
    nested = value.get("output")
    body = (
        nested
        if value.get("schema_version") == 1 and isinstance(nested, dict)
        else value
    )
    attachments = body.get("attachments")
    if not isinstance(attachments, list):
        return value
    retained = [
        attachment
        for attachment in attachments
        if not (
            isinstance(attachment, dict)
            and attachment.get("source_kind") == "local_image_blob"
        )
    ]
    image_count = len(attachments) - len(retained)
    if not image_count:
        return value
    projected = {**body, "image_count": image_count, "image_content": "unavailable"}
    if retained:
        projected["attachments"] = retained
    else:
        del projected["attachments"]
    # A tool that attaches images names them again in its message, so that
    # message goes with them: it is a list of references to bytes Memory
    # cannot read. What the step acted on stays in the output's own fields.
    projected.pop("message", None)
    return {**value, "output": projected} if body is nested else projected


def memory_conversation_from_row(row: sqlite3.Row) -> MemoryActionConversation | None:
    try:
        return project_memory_action_conversation(
            step_type=_required_text(row["step_type"], field="step_type"),
            step_name=_required_text(row["step_name"], field="step_name"),
            user_message_id=_optional_text(
                row["user_message_id"], field="user_message_id"
            ),
            user_message_json=_optional_text(
                row["user_message_json"], field="user_message_json"
            ),
            user_request_text=_optional_text(
                row["user_request_text"], field="user_request_text"
            ),
            llm_response_text=_optional_text(
                row["llm_response_text"], field="llm_response_text"
            ),
            source_suggestion_id=_optional_text(
                row["source_suggestion_id"], field="source_suggestion_id"
            ),
        )
    except ValueError as exc:
        raise StoredActionHistoryError(str(exc)) from exc


def step_list_success_schema() -> dict[str, JSONValue]:
    return _collection_success_schema(
        collection_key="steps",
        item_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "short_step_id",
                "step_number",
                "local_step_number",
                "step_type",
                "step_name",
                "status",
                "goal_handle",
                "retry_count",
                "created_at",
                "completed_at",
            ],
            "properties": {
                "short_step_id": {
                    "type": "string",
                    "pattern": HISTORY_FETCH_SHORT_STEP_PATTERN,
                },
                "step_number": {"type": "integer", "minimum": 1},
                "local_step_number": {"type": "integer", "minimum": 1},
                "step_type": {
                    "type": "string",
                    "enum": [step_type.value for step_type in StepType],
                },
                "step_name": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "processing",
                        "success",
                        "error",
                        "timeout",
                    ],
                },
                "goal_handle": {"type": ["string", "null"]},
                "retry_count": {"type": "integer", "minimum": 0},
                "created_at": {"type": "string"},
                "completed_at": {"type": ["string", "null"]},
            },
        },
    )


def search_success_schema() -> dict[str, JSONValue]:
    return _collection_success_schema(
        collection_key="matches",
        item_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "short_step_id",
                "step_number",
                "step_type",
                "step_name",
                "status",
                "excerpt",
                "conversation_origin",
            ],
            "properties": {
                "short_step_id": {
                    "type": "string",
                    "pattern": HISTORY_FETCH_SHORT_STEP_PATTERN,
                },
                "step_number": {"type": "integer", "minimum": 1},
                "step_type": {
                    "type": "string",
                    "enum": [step_type.value for step_type in StepType],
                },
                "step_name": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string",
                    "enum": [
                        "queued",
                        "processing",
                        "success",
                        "error",
                        "timeout",
                    ],
                },
                "excerpt": {"type": "string"},
                "conversation_origin": {
                    "anyOf": [conversation_kind_schema(), {"type": "null"}],
                    "description": "Origin of this discovery excerpt. Fetch the step to distinguish the AI proposal, approval, and user supplement.",
                },
            },
        },
    )


def history_fetch_success_schema() -> dict[str, JSONValue]:
    step_schema = history_fetch_step_schema()
    properties = cast(dict[str, JSONValue], step_schema["properties"])
    del (
        properties["thinking"],
        properties["llm_prompt_text"],
        properties["user_request_text"],
    )
    properties["conversation"] = memory_action_conversation_schema()
    step_schema["required"] = list(properties)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "steps"],
        "properties": {
            "status": {"type": "string", "enum": ["success"]},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": HISTORY_FETCH_MAX_REFS,
                "items": step_schema,
            },
        },
    }


def _collection_success_schema(
    *, collection_key: str, item_schema: dict[str, JSONValue]
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", collection_key, "next_offset"],
        "properties": {
            "status": {"type": "string", "enum": ["success"]},
            collection_key: {"type": "array", "items": item_schema},
            "next_offset": {"type": ["integer", "null"], "minimum": 0},
        },
    }


def _arguments(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be an object")
    return value


def _required_string(args: dict[str, JSONValue], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be nonblank text")
    return value.strip()


def _bounded_int(
    args: dict[str, JSONValue],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{key} is invalid")
    if maximum is not None and value > maximum:
        raise ValueError(f"{key} exceeds {maximum}")
    return value


def _required_ref(value: object) -> str:
    ref = _required_text(value, field="short_step_id")
    if HISTORY_FETCH_SHORT_STEP_RE.fullmatch(ref) is None:
        raise StoredActionHistoryError(f"stored short_step_id is invalid: {ref!r}")
    return ref


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise StoredActionHistoryError(f"stored {field} must be non-empty text")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoredActionHistoryError(f"stored {field} must be text or null")
    return value


def _local_time(value: object, *, field: str) -> str:
    text = _required_text(value, field=field)
    try:
        return describe_utc_timestamp(text)
    except ValueError as exc:
        # The tools report ValueError as invalid model arguments; this is stored data.
        raise StoredActionHistoryError(f"stored {field} is not ISO 8601") from exc


def _optional_local_time(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _local_time(value, field=field)


def _required_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StoredActionHistoryError(f"stored {field} must be an integer")
    return value


def _optional_json_object(value: object, *, field: str) -> dict[str, JSONValue] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StoredActionHistoryError(f"stored {field} must be JSON text or null")
    try:
        parsed: JSONValue = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StoredActionHistoryError(f"stored {field} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise StoredActionHistoryError(f"stored {field} must contain a JSON object")
    return parsed


__all__ = [
    "StoredActionHistoryError",
    "history_fetch_step",
    "history_fetch_success_schema",
    "parse_from_step",
    "parse_history_refs",
    "parse_page",
    "parse_search_request",
    "search_match",
    "search_success_schema",
    "step_list_success_schema",
    "step_metadata",
]

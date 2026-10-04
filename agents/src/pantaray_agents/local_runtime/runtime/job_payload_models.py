from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypedDict, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.tasks.types import (
    ActionContinuationRef,
    ActionJobRuntimePayload,
    ActionSubagentJobPayload,
    ActivitySummaryJobPayload,
    InsightJobPayload,
    LanguageCode,
    MemoryUpdateActionTerminal,
    MemoryUpdateJobPayload,
    SuggestionJobRuntimePayload,
)

ACTION_SUBAGENT_TASK_MAX_CODE_POINTS = 32_000
ACTION_SUBAGENT_REF_LIST_MAX_ITEMS = 64
ACTION_SUBAGENT_PAYLOAD_MAX_BYTES = 262_144
MEMORY_UPDATE_SOURCE_MAX_ITEMS = 64


class RawObjectPayload(TypedDict):
    pass


def parse_action_job_payload_json(payload_json: str) -> ActionJobRuntimePayload:
    payload = _load_payload_object(payload_json)
    _reject_unexpected_fields(
        payload,
        expected_fields={
            "job_id",
            "process_id",
            "action_id",
            "user_id",
            "continuation_ref",
        },
        payload_name="Action job payload",
    )
    return {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "action_id": _require_string(payload, "action_id"),
        "user_id": _require_string(payload, "user_id"),
        "continuation_ref": _require_action_continuation_ref(payload),
    }


def parse_action_subagent_job_payload_json(
    payload_json: str,
) -> ActionSubagentJobPayload:
    payload = _load_payload_object(payload_json)
    _reject_unexpected_fields(
        payload,
        expected_fields={
            "job_id",
            "process_id",
            "user_id",
            "action_id",
            "parent_process_id",
            "inference_profile_id",
            "action_context",
            "task",
            "context_refs",
            "resource_claim_ids",
        },
        payload_name="Action subagent job payload",
    )
    parsed: ActionSubagentJobPayload = {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "user_id": _require_string(payload, "user_id"),
        "action_id": _require_string(payload, "action_id"),
        "parent_process_id": _require_string(payload, "parent_process_id"),
        "inference_profile_id": _require_string(payload, "inference_profile_id"),
        "action_context": _require_string(payload, "action_context"),
        "task": _require_bounded_task(payload),
        "context_refs": _require_opaque_refs(payload, "context_refs"),
        "resource_claim_ids": _require_opaque_refs(payload, "resource_claim_ids"),
    }
    canonical_json = _canonical_action_subagent_payload_json(parsed)
    byte_size = len(canonical_json.encode("utf-8"))
    if byte_size > ACTION_SUBAGENT_PAYLOAD_MAX_BYTES:
        raise MigrationError(
            "Action subagent job payload exceeds "
            f"{ACTION_SUBAGENT_PAYLOAD_MAX_BYTES} UTF-8 bytes"
        )
    return parsed


def serialize_action_subagent_job_payload(
    payload: ActionSubagentJobPayload,
) -> str:
    parsed = parse_action_subagent_job_payload_json(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return _canonical_action_subagent_payload_json(parsed)


def _canonical_action_subagent_payload_json(
    payload: ActionSubagentJobPayload,
) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_suggestion_job_payload_json(
    payload_json: str,
) -> SuggestionJobRuntimePayload:
    payload = _load_payload_object(payload_json)
    return {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "suggestion_id": _require_string(payload, "suggestion_id"),
        "user_id": _require_string(payload, "user_id"),
        "enqueued_at": _require_string(payload, "enqueued_at"),
        "insight_id": _require_string(payload, "insight_id"),
    }


def parse_activity_summary_job_payload_json(
    payload_json: str,
) -> ActivitySummaryJobPayload:
    payload = _load_payload_object(payload_json)
    return {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "summary_id": _require_string(payload, "summary_id"),
        "user_id": _require_string(payload, "user_id"),
        "enqueued_at": _require_string(payload, "enqueued_at"),
        "summary_type": _require_string(payload, "summary_type"),
        "period_start": _require_string(payload, "period_start"),
        "period_end": _require_string(payload, "period_end"),
    }


def parse_insight_job_payload_json(payload_json: str) -> InsightJobPayload:
    payload = _load_payload_object(payload_json)
    return {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "insight_id": _require_string(payload, "insight_id"),
        "user_id": _require_string(payload, "user_id"),
        "period_start": _require_string(payload, "period_start"),
        "period_end": _require_string(payload, "period_end"),
        "enqueued_at": _require_string(payload, "enqueued_at"),
    }


def parse_memory_update_job_payload_json(
    payload_json: str,
) -> MemoryUpdateJobPayload:
    payload = _load_payload_object(payload_json)
    _reject_unexpected_fields(
        payload,
        expected_fields={
            "job_id",
            "process_id",
            "user_id",
            "enqueued_at",
            "short_insight_ids",
            "summary_ids",
            "action_terminals",
        },
        payload_name="Memory update job payload",
    )
    parsed: MemoryUpdateJobPayload = {
        "job_id": _require_string(payload, "job_id"),
        "process_id": _require_string(payload, "process_id"),
        "user_id": _require_string(payload, "user_id"),
        "enqueued_at": _require_string(payload, "enqueued_at"),
        "short_insight_ids": _require_source_ids(payload, "short_insight_ids"),
        "summary_ids": _require_source_ids(payload, "summary_ids"),
        "action_terminals": _require_action_terminals(payload),
    }
    if not (
        parsed["short_insight_ids"]
        or parsed["summary_ids"]
        or parsed["action_terminals"]
    ):
        raise MigrationError("Memory update job payload has no source")
    return parsed


def _require_source_ids(payload: Mapping[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise MigrationError(f"Memory update {key} must be an array")
    if len(value) > MEMORY_UPDATE_SOURCE_MAX_ITEMS:
        raise MigrationError(
            f"Memory update {key} exceeds {MEMORY_UPDATE_SOURCE_MAX_ITEMS} items"
        )
    source_ids: list[str] = []
    for index, source_id in enumerate(value):
        if not isinstance(source_id, str) or not source_id.strip():
            raise MigrationError(
                f"Memory update {key}[{index}] must be a non-empty string"
            )
        source_ids.append(source_id)
    return source_ids


def _require_action_terminals(
    payload: Mapping[str, object],
) -> list[MemoryUpdateActionTerminal]:
    value = payload.get("action_terminals")
    if not isinstance(value, list):
        raise MigrationError("Memory update action_terminals must be an array")
    if len(value) > MEMORY_UPDATE_SOURCE_MAX_ITEMS:
        raise MigrationError(
            "Memory update action_terminals exceeds "
            f"{MEMORY_UPDATE_SOURCE_MAX_ITEMS} items"
        )
    terminals: list[MemoryUpdateActionTerminal] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise MigrationError(
                f"Memory update action_terminals[{index}] must be an object"
            )
        terminals.append(_require_action_terminal(item))
    return terminals


def _require_action_terminal(item: Mapping[str, object]) -> MemoryUpdateActionTerminal:
    _reject_unexpected_fields(
        item,
        expected_fields={
            "source_id",
            "action_id",
            "action_completed_at",
            "source_action_revision_id",
            "turn_start_step_number",
            "turn_end_step_number",
            "action_prompt_name",
            "action_prompt_version",
            "suggestion_id",
        },
        payload_name="Memory update action terminal",
    )
    terminal: MemoryUpdateActionTerminal = {
        "source_id": _require_string(item, "source_id"),
        "action_id": _require_string(item, "action_id"),
        "action_completed_at": _require_string(item, "action_completed_at"),
        "turn_start_step_number": _require_positive_integer(
            item, "turn_start_step_number"
        ),
        "turn_end_step_number": _require_positive_integer(item, "turn_end_step_number"),
        "action_prompt_name": _require_string(item, "action_prompt_name"),
        "action_prompt_version": _require_string(item, "action_prompt_version"),
    }
    if terminal["turn_end_step_number"] < terminal["turn_start_step_number"]:
        raise MigrationError("Memory update action terminal step range is invalid")
    revision_id = _optional_non_empty_string(item, "source_action_revision_id")
    if revision_id is not None:
        terminal["source_action_revision_id"] = revision_id
    suggestion_id = _optional_non_empty_string(item, "suggestion_id")
    if suggestion_id is not None:
        terminal["suggestion_id"] = suggestion_id
    return terminal


def _load_payload_object(payload_json: str) -> Mapping[str, object]:
    try:
        payload = json.loads(payload_json)
    except ValueError as exc:
        raise MigrationError("job payload must be valid JSON object") from exc
    if not isinstance(payload, dict):
        raise MigrationError("job payload must be JSON object")
    return cast(Mapping[str, object], payload)


def _require_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"job payload field must be non-empty string: {key}")
    return value


def _require_positive_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MigrationError(f"job payload field must be positive integer: {key}")
    return value


def _require_bounded_task(payload: Mapping[str, object]) -> str:
    task = _require_string(payload, "task")
    if len(task) > ACTION_SUBAGENT_TASK_MAX_CODE_POINTS:
        raise MigrationError(
            "Action subagent task exceeds "
            f"{ACTION_SUBAGENT_TASK_MAX_CODE_POINTS:,} Unicode code points"
        )
    return task


def _require_opaque_refs(payload: Mapping[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise MigrationError(f"Action subagent {key} must be an array")
    if len(value) > ACTION_SUBAGENT_REF_LIST_MAX_ITEMS:
        raise MigrationError(
            f"Action subagent {key} exceeds {ACTION_SUBAGENT_REF_LIST_MAX_ITEMS} items"
        )
    refs: list[str] = []
    for index, ref in enumerate(value):
        if not isinstance(ref, str) or not ref.strip():
            raise MigrationError(
                f"Action subagent {key}[{index}] must be a non-empty string"
            )
        refs.append(ref)
    return refs


def _require_action_continuation_ref(
    payload: Mapping[str, object],
) -> ActionContinuationRef:
    raw_ref = payload.get("continuation_ref")
    if not isinstance(raw_ref, Mapping):
        raise MigrationError("continuation_ref must be an object")
    kind = _require_string(raw_ref, "kind")
    if kind == "user_step":
        _reject_unexpected_fields(
            raw_ref,
            expected_fields={"kind", "user_step_id"},
            payload_name="Action user_step continuation_ref",
        )
        return {
            "kind": "user_step",
            "user_step_id": _require_string(raw_ref, "user_step_id"),
        }
    if kind == "tool_approval":
        _reject_unexpected_fields(
            raw_ref,
            expected_fields={"kind", "approval_session_id", "tool_request_id"},
            payload_name="Action tool_approval continuation_ref",
        )
        return {
            "kind": "tool_approval",
            "approval_session_id": _require_string(raw_ref, "approval_session_id"),
            "tool_request_id": _require_string(raw_ref, "tool_request_id"),
        }
    raise MigrationError(f"unsupported Action continuation_ref kind: {kind}")


def _reject_unexpected_fields(
    payload: Mapping[str, object],
    *,
    expected_fields: set[str],
    payload_name: str,
) -> None:
    unexpected_fields = sorted(set(payload) - expected_fields)
    if unexpected_fields:
        raise MigrationError(
            f"{payload_name} has unexpected fields: {', '.join(unexpected_fields)}"
        )


def _optional_language(payload: Mapping[str, object]) -> dict[str, LanguageCode]:
    value = payload.get("language")
    if value is None:
        return {}
    if value not in {"ja", "en"}:
        raise MigrationError(f"unsupported language: {value}")
    return {"language": cast(LanguageCode, value)}


def _optional_non_empty_string(payload: Mapping[str, object], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"job payload field must be non-empty string: {key}")
    return value

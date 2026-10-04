from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypedDict, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.tasks.types import (
    ActionContinuationRef,
    ActionJobPayload,
    ActionSubagentJobPayload,
    ActivitySummaryJobPayload,
    MemoryUpdateJobPayload,
)

from .job_payload_models import (
    parse_action_subagent_job_payload_json,
    parse_memory_update_job_payload_json,
)


def build_action_subagent_job_payload(
    params: ActionSubagentJobPayload,
) -> ActionSubagentJobPayload:
    payload: ActionSubagentJobPayload = {
        "job_id": params["job_id"],
        "process_id": params["process_id"],
        "user_id": params["user_id"],
        "action_id": params["action_id"],
        "parent_process_id": params["parent_process_id"],
        "inference_profile_id": params["inference_profile_id"],
        "action_context": params["action_context"],
        "task": params["task"],
        "context_refs": list(params["context_refs"]),
        "resource_claim_ids": list(params["resource_claim_ids"]),
    }
    return parse_action_subagent_job_payload_json(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


class BuildActionJobPayloadParams(TypedDict):
    job_id: str
    process_id: str
    action_id: str
    user_id: str
    continuation_ref: ActionContinuationRef


def build_action_job_payload(params: BuildActionJobPayloadParams) -> ActionJobPayload:
    return {
        "job_id": params["job_id"],
        "process_id": params["process_id"],
        "action_id": params["action_id"],
        "user_id": params["user_id"],
        "continuation_ref": _build_action_continuation_ref(params["continuation_ref"]),
    }


def _build_action_continuation_ref(
    continuation_ref: ActionContinuationRef,
) -> ActionContinuationRef:
    raw_ref = cast(Mapping[str, object], continuation_ref)
    kind = raw_ref.get("kind")
    if kind == "user_step":
        _require_exact_continuation_fields(raw_ref, {"kind", "user_step_id"})
        return {
            "kind": "user_step",
            "user_step_id": _require_continuation_string(raw_ref, "user_step_id"),
        }
    if kind == "tool_approval":
        _require_exact_continuation_fields(
            raw_ref,
            {"kind", "approval_session_id", "tool_request_id"},
        )
        return {
            "kind": "tool_approval",
            "approval_session_id": _require_continuation_string(
                raw_ref, "approval_session_id"
            ),
            "tool_request_id": _require_continuation_string(raw_ref, "tool_request_id"),
        }
    raise MigrationError("Action continuation_ref has unsupported or missing kind")


def _require_exact_continuation_fields(
    continuation_ref: Mapping[str, object],
    expected_fields: set[str],
) -> None:
    actual_fields = set(continuation_ref)
    unexpected_fields = sorted(actual_fields - expected_fields)
    if unexpected_fields:
        raise MigrationError(
            "Action continuation_ref has unexpected fields: "
            + ", ".join(unexpected_fields)
        )
    missing_fields = sorted(expected_fields - actual_fields)
    if missing_fields:
        raise MigrationError(
            "Action continuation_ref is missing fields: " + ", ".join(missing_fields)
        )


def _require_continuation_string(
    continuation_ref: Mapping[str, object],
    field: str,
) -> str:
    value = continuation_ref.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(
            f"Action continuation_ref field must be non-empty string: {field}"
        )
    return value


class BuildActivitySummaryJobPayloadParams(TypedDict):
    job_id: str
    process_id: str
    summary_id: str
    user_id: str
    enqueued_at: str
    summary_type: str
    period_start: str
    period_end: str


def build_activity_summary_job_payload(
    params: BuildActivitySummaryJobPayloadParams,
) -> ActivitySummaryJobPayload:
    return {
        "job_id": params["job_id"],
        "process_id": params["process_id"],
        "summary_id": params["summary_id"],
        "user_id": params["user_id"],
        "enqueued_at": params["enqueued_at"],
        "summary_type": params["summary_type"],
        "period_start": params["period_start"],
        "period_end": params["period_end"],
    }


def build_memory_update_job_payload(
    params: MemoryUpdateJobPayload,
) -> MemoryUpdateJobPayload:
    """Validate a coalesced Memory update payload against its stored contract."""

    return parse_memory_update_job_payload_json(
        json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    )

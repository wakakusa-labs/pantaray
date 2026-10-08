"""Enqueue side of the unified Memory agent.

One poll coalesces every due Memory Agent trigger of a user into a single
``memory_update`` job. The job's logical key is the user, so the existing
``(job_type, logical_key)`` active-job dedupe keeps at most one unified Memory
run per user without a new ledger.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from pantaray_agents.tasks.types import (
    MemoryUpdateActionTerminal,
    MemoryUpdateChatRange,
    MemoryUpdateJobPayload,
)

from .job_enqueue import LocalJobEnqueueRequest, enqueue_local_job
from .job_payload_builder import build_memory_update_job_payload
from .job_status import PROCESS_STATUS_ENQUEUED
from .job_types import LOCAL_MEMORY_UPDATE_JOB_TYPE, MEMORY_PROCESS_KIND
from .memory_agent_triggers import (
    ACTION_TERMINAL_MEMORY_TRIGGER_KIND,
    SHORT_INSIGHT_MEMORY_TRIGGER_KIND,
    SUMMARY_MEMORY_TRIGGER_KIND,
    MemoryAgentTrigger,
    MemoryAgentTriggerIntegrityError,
)
from .process_events import append_process_event_in_connection


@dataclass(frozen=True, slots=True)
class PendingMemoryTrigger:
    """One pending unified Memory trigger row with its captured turn binding."""

    trigger: MemoryAgentTrigger
    created_at: str
    action_terminal: MemoryUpdateActionTerminal | None


def build_local_memory_update_enqueue_request(
    payload: MemoryUpdateJobPayload,
) -> LocalJobEnqueueRequest:
    return {
        "job_id": payload["job_id"],
        "user_id": payload["user_id"],
        "job_type": LOCAL_MEMORY_UPDATE_JOB_TYPE,
        "process_id": payload["process_id"],
        "process_kind": MEMORY_PROCESS_KIND,
        "process_status": PROCESS_STATUS_ENQUEUED,
        "scheduled_at": payload["enqueued_at"],
        "logical_key": payload["user_id"],
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "process_started_at": payload["enqueued_at"],
        "process_updated_at": payload["enqueued_at"],
        "process_heartbeat_at": payload["enqueued_at"],
        "process_next_event_seq": 1,
    }


def build_coalesced_memory_update_payload(
    *,
    user_id: str,
    enqueued_at: str,
    pending: tuple[PendingMemoryTrigger, ...],
    chat: MemoryUpdateChatRange | None,
) -> MemoryUpdateJobPayload:
    short_insight_ids: list[str] = []
    summary_ids: list[str] = []
    action_terminals: list[MemoryUpdateActionTerminal] = []
    for entry in pending:
        kind = entry.trigger.trigger_kind
        if kind == SHORT_INSIGHT_MEMORY_TRIGGER_KIND:
            short_insight_ids.append(entry.trigger.source_id)
        elif kind == SUMMARY_MEMORY_TRIGGER_KIND:
            summary_ids.append(entry.trigger.source_id)
        elif kind == ACTION_TERMINAL_MEMORY_TRIGGER_KIND:
            if entry.action_terminal is None:
                raise MemoryAgentTriggerIntegrityError(
                    "Action terminal trigger has no stored turn binding"
                )
            action_terminals.append(entry.action_terminal)
        else:
            raise MemoryAgentTriggerIntegrityError(
                "unknown unified Memory Agent trigger kind"
            )
    payload: MemoryUpdateJobPayload = {
        "job_id": str(uuid.uuid4()),
        "process_id": str(uuid.uuid4()),
        "user_id": user_id,
        "enqueued_at": enqueued_at,
        "short_insight_ids": short_insight_ids,
        "summary_ids": summary_ids,
        "action_terminals": action_terminals,
    }
    if chat is not None:
        payload["chat"] = chat
    return build_memory_update_job_payload(payload)


def enqueue_memory_update_job_in_connection(
    *,
    connection: sqlite3.Connection,
    payload: MemoryUpdateJobPayload,
) -> str:
    result = enqueue_local_job(
        connection=connection,
        request=build_local_memory_update_enqueue_request(payload),
    )
    if not result["inserted_new"]:
        raise MemoryAgentTriggerIntegrityError(
            "an active Memory update job already owns this user"
        )
    append_process_event_in_connection(
        connection=connection,
        process_id=payload["process_id"],
        event_name="memory_update_requested",
        payload={
            "process_id": payload["process_id"],
            "user_id": payload["user_id"],
            "enqueued_at": payload["enqueued_at"],
            "short_insight_count": len(payload["short_insight_ids"]),
            "summary_count": len(payload["summary_ids"]),
            "action_terminal_count": len(payload["action_terminals"]),
        },
        created_at=payload["enqueued_at"],
    )
    return payload["job_id"]


__all__ = [
    "PendingMemoryTrigger",
    "build_coalesced_memory_update_payload",
    "build_local_memory_update_enqueue_request",
    "enqueue_memory_update_job_in_connection",
]

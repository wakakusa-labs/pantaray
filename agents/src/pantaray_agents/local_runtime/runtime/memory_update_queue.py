"""Enqueue side of the unified Memory agent.

The ``builtin.memory_update`` hook hands its inbox here: one run takes what
fits of every pending source, and the events of what it took are consumed in
the transaction that enqueues it. The job's logical key is the user, so the
``(job_type, logical_key)`` active-job dedupe keeps at most one unified Memory
run per user.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from pantaray_agents.hooks.definitions import EventValue
from pantaray_agents.hooks.engine import InboxEvent
from pantaray_agents.local_runtime.hooks.events import (
    ActionTurnEnded,
    ActivitySummaryRecorded,
    ChatMessageRecorded,
    InsightRecorded,
    SuggestionDismissed,
)
from pantaray_agents.local_runtime.hooks.inbox import consume_in_connection
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.tasks.types import (
    MemoryUpdateActionTerminal,
    MemoryUpdateJobPayload,
)

from .job_enqueue import LocalJobEnqueueRequest, enqueue_local_job
from .job_payload_builder import build_memory_update_job_payload
from .job_payload_models import MEMORY_UPDATE_SOURCE_MAX_ITEMS
from .job_status import PROCESS_STATUS_ENQUEUED
from .job_types import LOCAL_MEMORY_UPDATE_JOB_TYPE, MEMORY_PROCESS_KIND
from .memory_chat_range import chat_range
from .memory_dismissal_range import dismissals_for_one_run
from .process_events import append_process_event_in_connection

# Insights, daily summaries and Action turns share one run's item bound, oldest
# first.
_SOURCE_EVENTS = frozenset(
    {InsightRecorded.NAME, ActivitySummaryRecorded.NAME, ActionTurnEnded.NAME}
)


class MemoryUpdateEnqueueConflictError(MigrationError):
    """An active Memory update job already owns the user."""


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


def enqueue_memory_update_from_inbox(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    hook_id: str,
    events: Sequence[InboxEvent],
    superseded: Sequence[int],
    enqueued_at: str,
) -> None:
    """Enqueue one run with what fits of the events, and consume what it took
    and the superseded events.

    Dismissals no longer unanswered are consumed without being handed.
    """
    with (
        open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection,
        immediate_transaction(connection),
    ):
        consumed: list[InboxEvent] = []
        sources = sorted(
            (event for event in events if event.name in _SOURCE_EVENTS),
            key=lambda event: (event.occurred_at, event.delivery_id),
        )[:MEMORY_UPDATE_SOURCE_MAX_ITEMS]
        consumed.extend(sources)
        payload: MemoryUpdateJobPayload = {
            "job_id": str(uuid.uuid4()),
            "process_id": str(uuid.uuid4()),
            "user_id": user_id,
            "enqueued_at": enqueued_at,
            "short_insight_ids": _ids(sources, InsightRecorded.NAME, "insight_id"),
            "summary_ids": _ids(sources, ActivitySummaryRecorded.NAME, "summary_id"),
            "action_terminals": [
                _action_terminal(event.payload)
                for event in sources
                if event.name == ActionTurnEnded.NAME
            ],
        }
        chat = [event for event in events if event.name == ChatMessageRecorded.NAME]
        if chat:
            sequences = [_int(event.payload, "sequence") for event in chat]
            pinned = chat_range(
                connection,
                user_id=user_id,
                after=min(sequences) - 1,
                through=max(sequences),
            )
            payload["chat"] = pinned
            consumed.extend(
                event
                for event in chat
                if _int(event.payload, "sequence") <= pinned["through_sequence"]
            )
        dismissals = [
            event for event in events if event.name == SuggestionDismissed.NAME
        ]
        if dismissals:
            handed, rest = dismissals_for_one_run(
                connection,
                user_id=user_id,
                suggestion_ids=tuple(
                    dict.fromkeys(
                        _ids(dismissals, SuggestionDismissed.NAME, "suggestion_id")
                    )
                ),
            )
            if handed:
                payload["dismissed_suggestion_ids"] = list(handed)
            consumed.extend(
                event
                for event in dismissals
                if _str(event.payload, "suggestion_id") not in rest
            )
        if sources or chat or payload.get("dismissed_suggestion_ids"):
            _enqueue_in_connection(
                connection=connection, payload=build_memory_update_job_payload(payload)
            )
        consume_in_connection(
            connection,
            hook_id=hook_id,
            delivery_ids=[*(event.delivery_id for event in consumed), *superseded],
        )


def _enqueue_in_connection(
    *, connection: sqlite3.Connection, payload: MemoryUpdateJobPayload
) -> None:
    result = enqueue_local_job(
        connection=connection,
        request=build_local_memory_update_enqueue_request(payload),
    )
    if not result["inserted_new"]:
        raise MemoryUpdateEnqueueConflictError(
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


def _action_terminal(payload: Mapping[str, EventValue]) -> MemoryUpdateActionTerminal:
    terminal: MemoryUpdateActionTerminal = {
        "source_id": _str(payload, "source_id"),
        "action_id": _str(payload, "action_id"),
        "action_completed_at": _str(payload, "action_completed_at"),
        "turn_start_step_number": _int(payload, "turn_start_step_number"),
        "turn_end_step_number": _int(payload, "turn_end_step_number"),
        "action_prompt_name": _str(payload, "action_prompt_name"),
        "action_prompt_version": _str(payload, "action_prompt_version"),
    }
    if payload.get("source_action_revision_id") is not None:
        terminal["source_action_revision_id"] = _str(
            payload, "source_action_revision_id"
        )
    if payload.get("suggestion_id") is not None:
        terminal["suggestion_id"] = _str(payload, "suggestion_id")
    return terminal


def _ids(events: Sequence[InboxEvent], name: str, field: str) -> list[str]:
    return [_str(event.payload, field) for event in events if event.name == name]


# The inbox stores events as JSON written by `record_event`; a value of the
# wrong type means the row was not written by it.
def _str(payload: Mapping[str, EventValue], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise MigrationError(f"hook event field {field} is not a string")
    return value


def _int(payload: Mapping[str, EventValue], field: str) -> int:
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise MigrationError(f"hook event field {field} is not an integer")
    return value


__all__ = [
    "MemoryUpdateEnqueueConflictError",
    "build_local_memory_update_enqueue_request",
    "enqueue_memory_update_from_inbox",
]

"""Binding between a published memory revision and the unified Memory run.

One ``memory_update`` job owns every memory category it updates, so each
revision it publishes is bound to that job, its process, and the Action set the
job payload declares. The binding is what a publication and its crash recovery
both check: the durable job payload, not the caller, decides which Actions the
run may cite as evidence.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Final, cast

from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_memory_update_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
    MEMORY_PROCESS_KIND,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    append_memory_category_published_in_connection,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import (
    MemoryUpdateActionTerminal,
    MemoryUpdateJobPayload,
)

from .errors import MemoryCatalogIntegrityError
from .models import MemoryRevision, MemorySource

# One run may record several independent lessons; more than this is a sign the
# agent is batching unrelated observations rather than selecting.
MEMORY_RUN_MAX_NEW_EXPERIENCES: Final[int] = 4


@dataclass(frozen=True, slots=True)
class MemoryRunActionEvidence:
    """One Action the run may cite, with the revision it published, if any."""

    action_id: str
    source_action_revision_id: str | None


@dataclass(frozen=True, slots=True)
class MemoryRunBinding:
    user_id: str
    job_id: str
    process_id: str
    action_evidence: tuple[MemoryRunActionEvidence, ...]

    def __post_init__(self) -> None:
        for value in (self.user_id, self.job_id, self.process_id):
            if not value.strip():
                raise ValueError("memory run identity is incomplete")
        action_ids = tuple(item.action_id for item in self.action_evidence)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("memory run Action evidence is not unique per Action")

    def evidence_for(self, action_id: str) -> MemoryRunActionEvidence | None:
        for item in self.action_evidence:
            if item.action_id == action_id:
                return item
        return None


def merged_action_terminals(
    terminals: tuple[MemoryUpdateActionTerminal, ...],
) -> tuple[MemoryUpdateActionTerminal, ...]:
    """One terminal per Action whose step window spans all its turns in this run.

    Turns are independent step ranges, not cumulative states, so a run that
    coalesced several turns of one Action must record every one of them. They
    become one window because an Action carries exactly one evidence ref and
    one revision. The window keeps the newest turn's identity but the
    revision of the newest turn that published one: an error or cancel turn
    publishes nothing, so the Action's current revision is still the one an
    earlier successful turn published. Dispatch takes pending triggers oldest
    first and never re-queues one, so the turns of an Action inside one run are
    consecutive, and the span reaches no turn another run records.
    """

    turns_by_action: dict[str, list[MemoryUpdateActionTerminal]] = {}
    for terminal in terminals:
        turns_by_action.setdefault(terminal["action_id"], []).append(terminal)
    merged: list[MemoryUpdateActionTerminal] = []
    for action_id in sorted(turns_by_action):
        turns = sorted(
            turns_by_action[action_id], key=lambda turn: turn["turn_end_step_number"]
        )
        window = turns[-1].copy()
        window["turn_start_step_number"] = min(
            turn["turn_start_step_number"] for turn in turns
        )
        window.pop("source_action_revision_id", None)
        for turn in reversed(turns):
            if "source_action_revision_id" in turn:
                window["source_action_revision_id"] = turn["source_action_revision_id"]
                break
        merged.append(window)
    return tuple(merged)


def memory_run_binding_from_payload(
    payload: MemoryUpdateJobPayload,
) -> MemoryRunBinding:
    return MemoryRunBinding(
        user_id=payload["user_id"],
        job_id=payload["job_id"],
        process_id=payload["process_id"],
        action_evidence=tuple(
            MemoryRunActionEvidence(
                action_id=terminal["action_id"],
                source_action_revision_id=terminal.get("source_action_revision_id"),
            )
            for terminal in merged_action_terminals(tuple(payload["action_terminals"]))
        ),
    )


def derived_experience_ids(job_id: str) -> tuple[str, ...]:
    """Deterministic IDs so a rerun of the job writes the same entry paths."""

    return tuple(
        str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"pantaray://memory-runs/{job_id}/agent-experience/{index}",
            )
        )
        for index in range(1, MEMORY_RUN_MAX_NEW_EXPERIENCES + 1)
    )


def memory_run_binding_payload(binding: MemoryRunBinding) -> dict[str, JSONValue]:
    """The binding as it is stored in a durable artifact intent payload."""

    return {
        "user_id": binding.user_id,
        "job_id": binding.job_id,
        "process_id": binding.process_id,
        "action_evidence": [
            {
                "action_id": item.action_id,
                "source_action_revision_id": item.source_action_revision_id,
            }
            for item in binding.action_evidence
        ],
    }


def memory_run_binding_from_intent(value: object) -> MemoryRunBinding:
    if not isinstance(value, dict) or set(value) != {
        "user_id",
        "job_id",
        "process_id",
        "action_evidence",
    }:
        raise MemoryCatalogIntegrityError("memory run intent binding is invalid")
    evidence = value["action_evidence"]
    if not isinstance(evidence, list):
        raise MemoryCatalogIntegrityError("memory run Action evidence is invalid")
    try:
        return MemoryRunBinding(
            user_id=_required_string(value, "user_id"),
            job_id=_required_string(value, "job_id"),
            process_id=_required_string(value, "process_id"),
            action_evidence=tuple(
                _action_evidence_from_intent(item) for item in evidence
            ),
        )
    except ValueError as exc:
        raise MemoryCatalogIntegrityError(
            "memory run intent binding is invalid"
        ) from exc


def _action_evidence_from_intent(value: object) -> MemoryRunActionEvidence:
    if not isinstance(value, dict) or set(value) != {
        "action_id",
        "source_action_revision_id",
    }:
        raise MemoryCatalogIntegrityError("memory run Action evidence is invalid")
    revision_id = value["source_action_revision_id"]
    if revision_id is not None and (
        not isinstance(revision_id, str) or not revision_id.strip()
    ):
        raise MemoryCatalogIntegrityError("memory run Action revision is invalid")
    return MemoryRunActionEvidence(
        action_id=_required_string(value, "action_id"),
        source_action_revision_id=revision_id,
    )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MemoryCatalogIntegrityError(f"memory run {key} is invalid")
    return value


def record_memory_run_category(
    *,
    connection: sqlite3.Connection,
    binding: MemoryRunBinding,
    source: MemorySource,
    revision: MemoryRevision,
) -> None:
    """Mark one category activated inside its own activation transaction.

    A crash between two category activations must not republish the one that
    already landed, so the rerun of the same job reads these events first.
    """

    append_memory_category_published_in_connection(
        connection=connection,
        process_id=binding.process_id,
        source=source,
        revision_id=revision.revision_id,
        created_at=now_utc_iso(),
    )


def validate_memory_run_runtime(
    *, connection: sqlite3.Connection, binding: MemoryRunBinding
) -> None:
    """The run's job must own its process right now."""

    row = _require_bound_row(connection=connection, binding=binding)
    if not (
        str(row["job_status"]) == "running"
        and str(row["process_status"]) == "running"
        and str(row["current_job_id"]) == binding.job_id
    ):
        raise MemoryCatalogIntegrityError("memory run is not claimed by its job")


def validate_memory_run_intent_owner(
    *, connection: sqlite3.Connection, binding: MemoryRunBinding
) -> None:
    """The run either holds its process or waits, deferred, to reclaim it."""

    row = _require_bound_row(connection=connection, binding=binding)
    running = (
        str(row["job_status"]) == "running"
        and str(row["process_status"]) == "running"
        and str(row["current_job_id"]) == binding.job_id
    )
    deferred = (
        str(row["job_status"]) == "queued"
        and str(row["process_status"]) == "enqueued"
        and row["current_job_id"] is None
    )
    if not (running or deferred):
        raise MemoryCatalogIntegrityError(
            "memory run intent is not owned by a consistent job state"
        )


def _require_bound_row(
    *, connection: sqlite3.Connection, binding: MemoryRunBinding
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT jobs.user_id AS job_user_id, jobs.job_type, jobs.process_id,
               jobs.status AS job_status, jobs.logical_key,
               payloads.payload_json,
               processes.user_id AS process_user_id, processes.kind,
               processes.status AS process_status, processes.current_job_id
        FROM jobs
        JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
        JOIN processes ON processes.process_id = jobs.process_id
        WHERE jobs.job_id = ?
        """,
        (binding.job_id,),
    ).fetchone()
    if row is None or (
        str(row["job_user_id"]) != binding.user_id
        or str(row["job_type"]) != LOCAL_MEMORY_UPDATE_JOB_TYPE
        or str(row["process_id"]) != binding.process_id
        or str(row["logical_key"]) != binding.user_id
        or str(row["process_user_id"]) != binding.user_id
        or str(row["kind"]) != MEMORY_PROCESS_KIND
    ):
        raise MemoryCatalogIntegrityError(
            "memory run identity does not match its runtime"
        )
    try:
        payload = parse_memory_update_job_payload_json(str(row["payload_json"]))
    except MigrationError as exc:
        raise MemoryCatalogIntegrityError("memory run job payload is invalid") from exc
    if memory_run_binding_from_payload(payload) != binding:
        raise MemoryCatalogIntegrityError(
            "memory run binding does not match its job payload"
        )
    return cast(sqlite3.Row, row)


__all__ = [
    "MEMORY_RUN_MAX_NEW_EXPERIENCES",
    "MemoryRunActionEvidence",
    "MemoryRunBinding",
    "derived_experience_ids",
    "memory_run_binding_from_intent",
    "memory_run_binding_from_payload",
    "memory_run_binding_payload",
    "merged_action_terminals",
    "record_memory_run_category",
    "validate_memory_run_intent_owner",
    "validate_memory_run_runtime",
]

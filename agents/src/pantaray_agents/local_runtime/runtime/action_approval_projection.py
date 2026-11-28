"""Full pending-approval snapshots on the root Action event stream.

An approval blocker is anchored on the physical process that waits for the
decision: the root Action process for its own gated Tool call, each paused
subagent process for its own. Only the root stream is relayed to the client, so
every physical transaction that can change the waiting set appends one snapshot
of the whole set here, as a full state and never a delta, even when nothing
waits any more. The pause anchor binds a process to an approval session;
``approval_sessions`` owns the request itself and whether it is still pending.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue

from .job_types import ACTION_PROCESS_KIND, ACTION_SUBAGENT_PROCESS_KIND
from .process_events import append_process_event_in_connection

ACTION_APPROVAL_SNAPSHOT_EVENT = "action_approval_snapshot"
_PAUSE_ANCHOR_EVENT = "process_paused"
_APPROVAL_STATUS_PENDING = "pending"

_ANCHORED_BLOCKERS_SQL = """
SELECT process.process_id, session.action_id, session.approval_session_id,
       session.tool_request_id, session.tool_id, session.intent_class,
       session.command_summary_json, session.review_reason
FROM processes AS process
JOIN process_events AS paused
  ON paused.process_id = process.process_id
 AND paused.event_name = :pause_event
 AND paused.event_seq = (
       SELECT MAX(latest.event_seq) FROM process_events AS latest
       WHERE latest.process_id = process.process_id
         AND latest.event_name = :pause_event)
JOIN json_each(paused.payload_json, '$.approval_blockers') AS blocker
JOIN approval_sessions AS session
  ON session.user_id = process.user_id
 AND session.action_id = process.action_id
 AND session.approval_session_id = json_extract(blocker.value, '$.approval_session_id')
 AND session.tool_request_id = json_extract(blocker.value, '$.tool_request_id')
 AND session.status = :pending_status
WHERE process.user_id = :user_id
  AND process.action_id = :action_id
  AND process.status = 'paused'
  AND (
        (:include_root AND process.process_id = :root_process_id
         AND process.kind = :root_kind)
     OR (process.kind = :child_kind
         AND process.parent_process_id = :root_process_id)
      )
ORDER BY process.process_id <> :root_process_id, process.process_id, blocker.key
"""

_PENDING_SESSION_SQL = """
SELECT session.action_id, session.approval_session_id, session.tool_request_id,
       session.tool_id, session.intent_class, session.command_summary_json,
       session.review_reason
FROM approval_sessions AS session
WHERE session.user_id = :user_id AND session.action_id = :action_id
  AND session.approval_session_id = :approval_session_id
  AND session.tool_request_id = :tool_request_id
  AND session.status = :pending_status
"""


class PendingApprovalSnapshotError(RuntimeError):
    """A pending approval blocker cannot be projected onto the root stream."""


@dataclass(frozen=True, slots=True)
class PendingApprovalBlocker:
    """One approval request that is holding one physical Action process."""

    process_id: str
    action_id: str
    approval_session_id: str
    tool_request_id: str
    tool_id: str
    intent_class: str
    command_summary: dict[str, JSONValue]
    # Why the approval review left this request to the user, when it said.
    review_reason: str | None


def append_pending_approval_snapshot_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    root_process_id: str,
    created_at: str,
    root_pause_blockers: Sequence[Mapping[str, object]] | None = None,
) -> str:
    """Append the Action's whole pending-approval set to the root stream.

    ``root_pause_blockers`` carries the root pause anchor that the caller
    appends later in the same transaction. The snapshot must precede that anchor
    on the root stream, because the relay stops forwarding the process there, so
    the root's own blockers cannot be read back from its anchor yet and its
    previous anchor must be ignored.
    """

    blockers: list[PendingApprovalBlocker] = []
    for raw in root_pause_blockers or ():
        blockers.extend(
            _pending_session_blockers(
                connection,
                user_id=user_id,
                action_id=action_id,
                root_process_id=root_process_id,
                approval_session_id=_blocker_text(raw, "approval_session_id"),
                tool_request_id=_blocker_text(raw, "tool_request_id"),
            )
        )
    blockers.extend(
        _anchored_blockers(
            connection,
            user_id=user_id,
            action_id=action_id,
            root_process_id=root_process_id,
            include_root=root_pause_blockers is None,
        )
    )
    return append_process_event_in_connection(
        connection=connection,
        process_id=root_process_id,
        event_name=ACTION_APPROVAL_SNAPSHOT_EVENT,
        payload={
            "action_id": action_id,
            "user_id": user_id,
            "status": "processing",
            "reason": "approval_pending",
            "completed_at": created_at,
            "approval_blockers": [asdict(blocker) for blocker in blockers],
        },
        created_at=created_at,
    )


def load_pending_approval_blockers_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    root_process_id: str,
) -> list[PendingApprovalBlocker]:
    """The blockers the Action's snapshot shows: pending ones of paused processes."""

    return _anchored_blockers(
        connection,
        user_id=user_id,
        action_id=action_id,
        root_process_id=root_process_id,
        include_root=True,
    )


def _anchored_blockers(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    root_process_id: str,
    include_root: bool,
) -> list[PendingApprovalBlocker]:
    rows = connection.execute(
        _ANCHORED_BLOCKERS_SQL,
        {
            "action_id": action_id,
            "child_kind": ACTION_SUBAGENT_PROCESS_KIND,
            "include_root": include_root,
            "pause_event": _PAUSE_ANCHOR_EVENT,
            "pending_status": _APPROVAL_STATUS_PENDING,
            "root_kind": ACTION_PROCESS_KIND,
            "root_process_id": root_process_id,
            "user_id": user_id,
        },
    ).fetchall()
    return [_blocker(str(row[0]), row[1:]) for row in rows]


def _pending_session_blockers(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    root_process_id: str,
    approval_session_id: str,
    tool_request_id: str,
) -> list[PendingApprovalBlocker]:
    rows = connection.execute(
        _PENDING_SESSION_SQL,
        {
            "action_id": action_id,
            "approval_session_id": approval_session_id,
            "pending_status": _APPROVAL_STATUS_PENDING,
            "tool_request_id": tool_request_id,
            "user_id": user_id,
        },
    ).fetchall()
    return [_blocker(root_process_id, row) for row in rows]


def _blocker(process_id: str, session_row: Sequence[object]) -> PendingApprovalBlocker:
    command_summary = json.loads(str(session_row[5]))
    if not isinstance(command_summary, dict):
        raise PendingApprovalSnapshotError(
            "approval session command summary must be an object"
        )
    return PendingApprovalBlocker(
        process_id=process_id,
        action_id=str(session_row[0]),
        approval_session_id=str(session_row[1]),
        tool_request_id=str(session_row[2]),
        tool_id=str(session_row[3]),
        intent_class=str(session_row[4]),
        command_summary=cast(dict[str, JSONValue], command_summary),
        review_reason=None if session_row[6] is None else str(session_row[6]),
    )


def _blocker_text(raw: Mapping[str, object], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise PendingApprovalSnapshotError(
            f"approval blocker {field_name} must be a non-empty string"
        )
    return value

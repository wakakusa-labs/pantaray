"""Child-aware startup recovery for Action subagents.

A worker restart finds every child exactly where the crash left it, and the
generic in-flight repair would re-queue it without knowing it is a child. This
pass runs first and decides which of two restart outcomes each child gets.

A child the parent can no longer own -- a terminal, fenced or no longer
processing parent -- is settled as canceled here, together with a child that
already carries a durable cancel request. Their worker is gone, so nothing else
can settle them and the generic repair would re-queue decided-against work.

Every other child stays exactly as it is, and its root Action is reported back
so the interrupted-Action recovery does not expire the session, delete the temp
root or strip the execution context that child still depends on. The same holds
for an uncollected terminal result, this pass's own cancels included: only the
preserved root lets the resumed parent read the durable result back.

A child that crashed between claiming a gated Tool invocation and recording
that Tool in its private transcript is settled from the stored invocation
instead of re-executing it, so an approved side effect happens once.

A child whose payload no longer parses, or whose job the envelope owner has
already blocked, is quarantined instead: neither settlement nor collection can
speak for it, and one such row must never stop the pass for the runtimes that
follow it. Its root is preserved from the child's own row so the workspace it
still holds survives, and any approval it was waiting on is released.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.tools.contract import tool_error_output

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import immediate_transaction
from ..tooling.resources.resource_recovery import STARTUP_RECOVERY_ERROR_TYPE
from .action_approval_projection import (
    append_pending_approval_snapshot_in_connection,
)
from .action_subagent_messages import ACTION_SUBAGENT_TOOL_EVENT
from .action_subagent_pause import (
    ACTION_SUBAGENT_PAUSE_EVENT,
    cancel_paused_action_subagent_in_connection,
    decide_approval_session,
)
from .action_subagent_terminal import (
    build_action_subagent_canceled_result,
    finalize_action_subagent_terminal_in_connection,
)
from .job_claim import block_invalid_local_job
from .job_envelope import LocalJobEnvelopeIntegrityError
from .job_payload_models import (
    parse_action_subagent_job_payload_json,
    serialize_action_subagent_job_payload,
)
from .job_types import (
    ACTION_SUBAGENT_PROCESS_KIND,
    LOCAL_ACTION_JOB_TYPE,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE,
)
from .process_events import append_process_event_in_connection
from .utc_timestamps import now_utc_iso

type ActionRootIdentity = tuple[str, str]

_CHILD_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
_INTERRUPTED_TOOL_MESSAGE = "the approved child tool call was interrupted at startup"
_BLOCKED_JOB_STATUS = "blocked"
_APPROVAL_STATUS_INTERRUPTED = "interrupted"
_APPROVAL_STATUS_PENDING = "pending"
_ATTACHMENT_UNSUPPORTED_CODE = "SUBAGENT_ATTACHMENT_READ_UNSUPPORTED"
# The live child broker refuses attachment reads with exactly this projection,
# because attachment bytes travel only on the broker outcome and never reach the
# durable transcript this pass replays from.
_ATTACHMENT_UNSUPPORTED_MESSAGE = (
    "Image contents cannot be delivered to a subagent. "
    "Report the file path so the parent agent can read it, and "
    "continue with text paths such as grep, list, or text read."
)

# A parent that is still processing, unfenced and nonterminal can re-claim its
# job, and it can only read its children back while the root workspace the child
# also runs against survives; anything else has lost that authority for good.
_UNCOLLECTED_CHILDREN_SQL = """
SELECT job.job_id, job.process_id, child.user_id, child.action_id,
       child.parent_process_id,
       child.status AS child_status, job.status AS job_status,
       job.cancel_requested_at, payload.payload_json,
       COALESCE(action.status = 'processing' AND parent.kind = 'action'
         AND parent.status IN ('enqueued', 'running', 'paused')
         AND parent.completed_at IS NULL AND parent.terminal_event_id IS NULL
         AND parent_job.status IN ('queued', 'running', 'paused')
         AND parent_job.completed_at IS NULL
         AND parent_job.cancel_requested_at IS NULL
         AND EXISTS (SELECT 1 FROM workspace_manifests AS manifest
           JOIN execution_sessions AS session
             ON session.execution_session_id = manifest.execution_session_id
            AND session.user_id = manifest.user_id
            AND session.action_id = manifest.action_id
           WHERE manifest.user_id = child.user_id
             AND manifest.action_id = child.action_id
             AND manifest.status = 'ready'
             AND session.parent_execution_session_id IS NULL
             AND session.status = 'running'), 0) AS parent_resumable
FROM processes AS child
JOIN jobs AS job ON job.process_id = child.process_id
  AND job.user_id = child.user_id AND job.job_type = :child_job_type
JOIN job_payloads AS payload ON payload.job_id = job.job_id
LEFT JOIN processes AS parent ON parent.process_id = child.parent_process_id
  AND parent.user_id = child.user_id AND parent.action_id = child.action_id
LEFT JOIN jobs AS parent_job ON parent_job.process_id = parent.process_id
  AND parent_job.user_id = parent.user_id
  AND parent_job.job_type = :action_job_type
  AND parent_job.logical_key = parent.action_id
LEFT JOIN agent_actions AS action ON action.action_id = child.action_id
  AND action.user_id = child.user_id
WHERE child.kind = :child_kind AND child.result_collected_at IS NULL
ORDER BY child.process_id
"""

# ``tool_request_id`` is ``<child process id>:<process event seq at request>``,
# so the trailing sequence orders invocations against the child's own transcript.
_UNRECORDED_INVOCATION_SQL = """
SELECT invocation.tool_id, invocation.status, invocation.command_summary_json,
       output.output_json
FROM tool_invocations AS invocation
LEFT JOIN tool_outputs AS output
  ON output.invocation_id = invocation.invocation_id
WHERE invocation.user_id = :user_id AND invocation.action_id = :action_id
  AND invocation.tool_request_id LIKE :request_prefix
  AND CAST(substr(invocation.tool_request_id, :seq_offset) AS INTEGER) > (
        SELECT COALESCE(MAX(event_seq), 0) FROM process_events
        WHERE process_id = :process_id AND event_name = :tool_event)
ORDER BY invocation.started_at DESC, invocation.tool_request_id DESC
LIMIT 1
"""


# The write is bound to the job envelope the pass selected: the exact payload
# text pins every identity it carries, so a payload that no longer belongs to
# that row cannot redirect the write onto the child it names instead.
_RECOVERED_TOOL_OWNER_SQL = f"""
SELECT 1 FROM jobs AS job
JOIN processes AS child ON child.process_id = job.process_id
JOIN job_payloads AS payload ON payload.job_id = job.job_id
WHERE job.job_id = ? AND job.status <> '{_BLOCKED_JOB_STATUS}'
  AND payload.payload_json = ? AND child.process_id = ?
  AND child.result_collected_at IS NULL AND child.terminal_event_id IS NULL
  AND child.status IN ('enqueued', 'running', 'paused')
"""


# The pending sessions one paused child still holds, read from its own latest
# anchor the way the projection owner reads them, and keyed on the process alone
# so a job already blocked elsewhere does not hide them.
_QUARANTINED_BLOCKER_SQL = """
SELECT session.approval_session_id, session.tool_request_id
FROM processes AS child
JOIN process_events AS paused ON paused.process_id = child.process_id
  AND paused.event_name = :pause_event
  AND paused.event_seq = (
        SELECT MAX(latest.event_seq) FROM process_events AS latest
        WHERE latest.process_id = child.process_id
          AND latest.event_name = :pause_event)
JOIN json_each(paused.payload_json, '$.approval_blockers') AS blocker
JOIN approval_sessions AS session
  ON session.user_id = child.user_id AND session.action_id = child.action_id
 AND session.approval_session_id =
       json_extract(blocker.value, '$.approval_session_id')
 AND session.tool_request_id = json_extract(blocker.value, '$.tool_request_id')
 AND session.status = :pending_status
WHERE child.process_id = :process_id AND child.user_id = :user_id
  AND child.action_id = :action_id AND child.status = 'paused'
  AND child.terminal_event_id IS NULL
ORDER BY blocker.key
"""


@dataclass(frozen=True, slots=True)
class ActionSubagentStartupRecovery:
    """What one startup pass decided about the Action subagent children."""

    preserved_roots: frozenset[ActionRootIdentity]
    settled_child_count: int
    collected_invocation_count: int
    blocked_child_count: int
    # Unreadable payloads on jobs no longer blockable: re-read on every start.
    unreadable_child_count: int


@dataclass(frozen=True, slots=True)
class _UncollectedChild:
    job_id: str
    process_id: str
    user_id: str
    action_id: str
    parent_process_id: str
    # ``None`` once the durable payload no longer parses, which leaves this row
    # with nothing to reason about beyond its job identity.
    payload: ActionSubagentJobPayload | None
    child_status: str
    job_status: str
    cancel_requested_at: str | None
    parent_resumable: bool

    @property
    def terminal(self) -> bool:
        return self.child_status in _CHILD_TERMINAL_STATUSES

    @property
    def root(self) -> ActionRootIdentity:
        return (self.user_id, self.action_id)

    @property
    def quarantined(self) -> bool:
        """A child no worker can ever pick up again, by payload or by block."""

        return self.payload is None or self.job_status == _BLOCKED_JOB_STATUS


def recover_action_subagent_children_for_startup(
    *, db_path: Path, busy_timeout_ms: int
) -> ActionSubagentStartupRecovery:
    """Settle unowned children and report the roots recovery must not destroy."""

    preserved: set[ActionRootIdentity] = set()
    settled_child_count = 0
    collected_invocation_count = 0
    blocked_child_count = 0
    unreadable_child_count = 0
    for child in _load_uncollected_children(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ):
        # Root preservation reads the child's own row, so a quarantined child
        # keeps its parent's workspace alive even with an unreadable payload:
        # deleting it would strand this child's process and claims for good.
        if child.parent_resumable:
            preserved.add(child.root)
        # A quarantined child owes its parent nothing it can still deliver, and
        # the terminal owner refuses to settle it, so it never reaches either
        # path: its own row already carries the failure an operator has to see.
        if child.quarantined:
            if _quarantine_child(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, child=child
            ):
                blocked_child_count += 1
            else:
                unreadable_child_count += 1
            continue
        payload = child.payload
        assert payload is not None
        if child.terminal:
            continue
        if not child.parent_resumable or child.cancel_requested_at is not None:
            _settle_canceled_child(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, child=child
            )
            settled_child_count += 1
            continue
        if _collect_claimed_invocation(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            payload=payload,
            job_id=child.job_id,
        ):
            collected_invocation_count += 1
    return ActionSubagentStartupRecovery(
        preserved_roots=frozenset(preserved),
        settled_child_count=settled_child_count,
        collected_invocation_count=collected_invocation_count,
        blocked_child_count=blocked_child_count,
        unreadable_child_count=unreadable_child_count,
    )


def _load_uncollected_children(
    *, db_path: Path, busy_timeout_ms: int
) -> tuple[_UncollectedChild, ...]:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            _UNCOLLECTED_CHILDREN_SQL,
            {
                "action_job_type": LOCAL_ACTION_JOB_TYPE,
                "child_job_type": LOCAL_ACTION_SUBAGENT_JOB_TYPE,
                "child_kind": ACTION_SUBAGENT_PROCESS_KIND,
            },
        ).fetchall()
    return tuple(_child_snapshot(row) for row in rows)


def _child_snapshot(row: sqlite3.Row) -> _UncollectedChild:
    """Bind one child row, keeping a payload that cannot speak for it out."""

    identity = (
        str(row["job_id"]),
        str(row["process_id"]),
        str(row["user_id"]),
        str(row["action_id"]),
        str(row["parent_process_id"]),
    )
    try:
        payload = parse_action_subagent_job_payload_json(str(row["payload_json"]))
    except MigrationError:
        # One bad row must not stop the pass; quarantine it by identity alone.
        payload = None
    if payload is not None and identity != (
        payload["job_id"],
        payload["process_id"],
        payload["user_id"],
        payload["action_id"],
        payload["parent_process_id"],
    ):
        # It parses, but it names another child, so every write it authorises
        # would land on that child instead of the row being scanned.
        payload = None
    return _UncollectedChild(
        job_id=identity[0],
        process_id=identity[1],
        user_id=identity[2],
        action_id=identity[3],
        parent_process_id=identity[4],
        payload=payload,
        child_status=str(row["child_status"]),
        job_status=str(row["job_status"]),
        cancel_requested_at=(
            None
            if row["cancel_requested_at"] is None
            else str(row["cancel_requested_at"])
        ),
        parent_resumable=bool(row["parent_resumable"]),
    )


def _quarantine_child(
    *, db_path: Path, busy_timeout_ms: int, child: _UncollectedChild
) -> bool:
    """Report whether this row now rests on a blocked job.

    An already blocked row still has its approval released, because a block that
    happened before this pass left the same decision stranded. A terminal job is
    outside the envelope owner's blockable statuses, so an unreadable payload on
    one cannot be quarantined at all and is not reported as though it were.
    """

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            _release_quarantined_approval(connection=connection, child=child)
            if child.job_status != _BLOCKED_JOB_STATUS:
                block_invalid_local_job(connection=connection, job_id=child.job_id)
            blocked = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (child.job_id,)
            ).fetchone()
    return blocked is not None and str(blocked[0]) == _BLOCKED_JOB_STATUS


def _release_quarantined_approval(
    *, connection: sqlite3.Connection, child: _UncollectedChild
) -> None:
    """Free the user decisions a quarantined child still holds.

    No worker reads the anchor back, so a blocked job alone strands them
    pending. Selecting only pending sessions makes the release idempotent: a
    child already released on an earlier start finds nothing left to settle.
    """

    rows = connection.execute(
        _QUARANTINED_BLOCKER_SQL,
        {
            "action_id": child.action_id,
            "pause_event": ACTION_SUBAGENT_PAUSE_EVENT,
            "pending_status": _APPROVAL_STATUS_PENDING,
            "process_id": child.process_id,
            "user_id": child.user_id,
        },
    ).fetchall()
    if not rows:
        return
    released_at = now_utc_iso()
    for approval_session_id, tool_request_id in rows:
        status = decide_approval_session(
            connection,
            user_id=child.user_id,
            action_id=child.action_id,
            approval_session_id=str(approval_session_id),
            tool_request_id=str(tool_request_id),
            next_status=_APPROVAL_STATUS_INTERRUPTED,
            decided_at=released_at,
        )
        if status != "updated":
            raise LocalJobEnvelopeIntegrityError(
                "Quarantined subagent approval could not be interrupted"
            )
    append_pending_approval_snapshot_in_connection(
        connection,
        user_id=child.user_id,
        action_id=child.action_id,
        root_process_id=child.parent_process_id,
        created_at=released_at,
    )


def _settle_canceled_child(
    *, db_path: Path, busy_timeout_ms: int, child: _UncollectedChild
) -> None:
    """Terminalize one child whose worker died and cannot reach a safe boundary."""

    payload = child.payload
    assert payload is not None
    settled_at = now_utc_iso()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            released_blocker = child.job_status == "paused"
            job_status = child.job_status
            if released_blocker:
                cancel_paused_action_subagent_in_connection(
                    connection=connection,
                    payload=payload,
                    canceled_at=settled_at,
                )
                job_status = "queued"
            if child.cancel_requested_at is None:
                requested = connection.execute(
                    "UPDATE jobs SET cancel_requested_at=? "
                    "WHERE job_id=? AND status=? AND cancel_requested_at IS NULL",
                    (settled_at, payload["job_id"], job_status),
                )
                if int(requested.rowcount) != 1:
                    raise LocalJobEnvelopeIntegrityError(
                        "Action subagent startup cancel request changed"
                    )
            finalize_action_subagent_terminal_in_connection(
                connection=connection,
                payload=payload,
                result=build_action_subagent_canceled_result(),
                completed_at=settled_at,
            )
            if released_blocker:
                append_pending_approval_snapshot_in_connection(
                    connection,
                    user_id=payload["user_id"],
                    action_id=payload["action_id"],
                    root_process_id=payload["parent_process_id"],
                    created_at=settled_at,
                )


def _collect_claimed_invocation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    job_id: str,
) -> bool:
    """Write back a claimed Tool result the child died before recording.

    A child commits its invocation row before its own transcript entry, and a
    settings-approved call reaches that window with no pause anchor to key on.
    The child's request identity carries the process event sequence it claimed
    at, so an invocation newer than the last recorded Tool event is exactly the
    one the crash swallowed. Replaying it stops the resumed child from running
    an approved side effect twice.
    """

    process_id = payload["process_id"]
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            _UNRECORDED_INVOCATION_SQL,
            {
                "action_id": payload["action_id"],
                "process_id": process_id,
                "request_prefix": f"{process_id}:%",
                "seq_offset": len(process_id) + 2,
                "tool_event": ACTION_SUBAGENT_TOOL_EVENT,
                "user_id": payload["user_id"],
            },
        ).fetchone()
    if row is None:
        return False
    completed = str(row[1]) == "completed" and isinstance(row[3], str)
    output = _decoded_json(
        row[3],
        default=build_runtime_tool_error_output(
            error_type=STARTUP_RECOVERY_ERROR_TYPE,
            message=_INTERRUPTED_TOOL_MESSAGE,
        ),
    )
    error_message = None if completed else _INTERRUPTED_TOOL_MESSAGE
    if completed and _carries_attachment(output):
        completed = False
        error_message = _ATTACHMENT_UNSUPPORTED_MESSAGE
        output = cast(
            JSONValue,
            tool_error_output(
                error_code=_ATTACHMENT_UNSUPPORTED_CODE,
                message=_ATTACHMENT_UNSUPPORTED_MESSAGE,
            ),
        )
    _record_recovered_tool(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        payload=payload,
        job_id=job_id,
        entry={
            "tool_name": str(row[0]),
            "status": "completed" if completed else "error",
            "arguments": _decoded_json(row[2], default={}),
            "output": output,
            "error_message": error_message,
        },
    )
    return True


def _carries_attachment(output: JSONValue) -> bool:
    """The stored read produced bytes the durable transcript cannot carry."""

    return isinstance(output, dict) and (
        output.get("kind") == "attachment" or bool(output.get("attachments"))
    )


def _record_recovered_tool(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    job_id: str,
    entry: dict[str, JSONValue],
) -> None:
    """Append one recovered Tool entry to a child no worker is holding.

    The live transcript writer requires a running claim because a worker owns
    that write. Recovery owns a different authority: no worker exists yet, and
    the generic in-flight repair may already have returned this child to the
    queue, so the claim it would check is exactly what a restart removes.
    """

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            owned = connection.execute(
                _RECOVERED_TOOL_OWNER_SQL,
                (
                    job_id,
                    serialize_action_subagent_job_payload(payload),
                    payload["process_id"],
                ),
            ).fetchone()
            if owned is None:
                raise LocalJobEnvelopeIntegrityError(
                    "Action subagent recovered Tool authority is not active"
                )
            append_process_event_in_connection(
                connection=connection,
                process_id=payload["process_id"],
                event_name=ACTION_SUBAGENT_TOOL_EVENT,
                payload=dict(entry),
                created_at=now_utc_iso(),
            )


def _decoded_json(raw: object, *, default: JSONValue) -> JSONValue:
    if not isinstance(raw, str):
        return default
    try:
        return cast(JSONValue, json.loads(raw))
    except json.JSONDecodeError as exc:
        raise LocalJobEnvelopeIntegrityError(
            "Action subagent claimed invocation JSON is invalid"
        ) from exc


__all__ = [
    "ActionRootIdentity",
    "ActionSubagentStartupRecovery",
    "recover_action_subagent_children_for_startup",
]

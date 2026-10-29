"""Which delivered suggestions and Actions a Memory update looks back on.

An item is looked back on at checkpoints after its delivery, by how the user's
and the environment's state changed since -- never by the buttons pressed on it.
A run takes the checkpoints that fell since the last completed Memory update and
up to its own enqueue, and only the latest one per item: a checkpoint skipped
while nothing ran is not looked back on afterward.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pantaray_agents.local_runtime.runtime.job_types import MEMORY_PROCESS_KIND
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    MEMORY_UPDATE_COMPLETED_EVENT,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    parse_utc_iso,
)

# Days after delivery at which an item's outcome is looked back on.
OUTCOME_CHECKPOINT_DAYS = (1, 7, 30)
# Days. How far back a run looks when no Memory update has completed before it.
OUTCOME_WINDOW_DAYS = 45
# Design limit: one run looks back on at most this many, earliest checkpoint
# first, so the look-back stays a small part of the update. An item beyond the
# cap is not revisited at that checkpoint; raise the cap when runs regularly
# have more due than this.
MAX_OUTCOMES_PER_RUN = 5
# A hypothesis section a suggestion's summary opens; older ones have none.
_HYPOTHESIS_HEADING = "### Hypothesis"

type DueKind = Literal["suggestion", "action"]


@dataclass(frozen=True, slots=True)
class DueOutcome:
    """One suggestion or Action at the checkpoint it is due for."""

    kind: DueKind
    # The suggestion_id or the action_id.
    item_id: str
    checkpoint_days: int
    delivered_at: str
    # What it was meant to change: a suggestion's hypothesis, or what the user
    # asked the Action for.
    expectation: str
    # The suggestion an Action came from, read beside it; None otherwise.
    source_suggestion_id: str | None


def latest_checkpoint_between(
    *, delivered_at: datetime, after: datetime, through: datetime
) -> int | None:
    """The latest checkpoint in (after, through], if any."""

    passed = [
        days
        for days in OUTCOME_CHECKPOINT_DAYS
        if after < delivered_at + timedelta(days=days) <= through
    ]
    return passed[-1] if passed else None


def hypothesis_of(summary: str) -> str:
    """The summary's hypothesis section, or the whole summary without one."""

    start = summary.find(_HYPOTHESIS_HEADING)
    if start < 0:
        return summary.strip()
    body = summary[start + len(_HYPOTHESIS_HEADING) :]
    ends = [index for index in (body.find("\n## "), body.find("\n### ")) if index >= 0]
    return body[: min(ends)].strip() if ends else body.strip()


# The start of the last Memory update that completed, before this run was
# enqueued: a memory process starts when it is enqueued, so consecutive runs'
# ranges meet. A run that failed showed nothing and moves no boundary, and a
# retry of this job keeps the same range.
_BOUNDARY_SQL = """
SELECT MAX(processes.started_at) FROM processes
JOIN process_events AS completed
  ON completed.process_id = processes.process_id AND completed.event_name = ?
WHERE processes.user_id = ? AND processes.kind = ? AND processes.started_at < ?
"""

# A suggestion is delivered when its process completes: the terminal event the
# relay forwards to the user, after the row was committed.
_SUGGESTIONS_SQL = """
SELECT suggestions.suggestion_id, suggestions.suggestion_summary,
       MAX(processes.completed_at) AS delivered_at
FROM agent_suggestions AS suggestions
JOIN processes
  ON processes.user_id = suggestions.user_id
 AND processes.suggestion_id = suggestions.suggestion_id
 AND processes.kind = 'suggestion' AND processes.completed_at IS NOT NULL
WHERE suggestions.user_id = ? AND suggestions.status = 'success'
  AND suggestions.has_suggestion = 1
GROUP BY suggestions.suggestion_id
HAVING delivered_at >= ?
"""

# An Action is delivered when its first turn completes: the turn that answered
# the user's original request. It came from a suggestion when it took that offer
# up, or when its conversation opens with the suggestion the user replied to.
_ACTIONS_SQL = """
SELECT actions.action_id, request.user_request_text,
       COALESCE(actions.suggestion_id, reply.source_suggestion_id)
           AS source_suggestion_id,
       MIN(turns.completed_at) AS delivered_at
FROM agent_actions AS actions
JOIN processes AS turns
  ON turns.user_id = actions.user_id AND turns.action_id = actions.action_id
 AND turns.kind = 'action' AND turns.status = 'completed'
 AND turns.completed_at IS NOT NULL
JOIN agent_action_steps AS request
  ON request.user_id = actions.user_id AND request.action_id = actions.action_id
 AND request.step_type = 'user_request'
 AND request.user_message_id = actions.initial_user_message_id
LEFT JOIN agent_action_steps AS reply
  ON reply.user_id = actions.user_id AND reply.action_id = actions.action_id
 AND reply.source_suggestion_id IS NOT NULL
WHERE actions.user_id = ?
GROUP BY actions.action_id
HAVING delivered_at >= ?
"""


def load_due_outcomes(
    connection: sqlite3.Connection, *, user_id: str, enqueued_at: str
) -> tuple[DueOutcome, ...]:
    """Up to MAX_OUTCOMES_PER_RUN due items, the earliest checkpoint first."""

    through = parse_utc_iso(enqueued_at)
    (boundary,) = connection.execute(
        _BOUNDARY_SQL,
        (MEMORY_UPDATE_COMPLETED_EVENT, user_id, MEMORY_PROCESS_KIND, enqueued_at),
    ).fetchone()
    after = (
        through - timedelta(days=OUTCOME_WINDOW_DAYS)
        if boundary is None
        else parse_utc_iso(str(boundary))
    )
    # A day of slack: the exact range is checked on parsed times below.
    earliest = format_utc_iso(after - timedelta(days=OUTCOME_CHECKPOINT_DAYS[-1] + 1))
    rows: list[tuple[DueKind, str, str, str, str | None]] = [
        (
            "suggestion",
            str(row["suggestion_id"]),
            str(row["delivered_at"]),
            hypothesis_of(str(row["suggestion_summary"] or "")),
            None,
        )
        for row in connection.execute(_SUGGESTIONS_SQL, (user_id, earliest))
    ] + [
        (
            "action",
            str(row["action_id"]),
            str(row["delivered_at"]),
            str(row["user_request_text"] or "").strip(),
            row["source_suggestion_id"],
        )
        for row in connection.execute(_ACTIONS_SQL, (user_id, earliest))
    ]
    due: list[tuple[datetime, DueOutcome]] = []
    for kind, item_id, delivered_at, expectation, source_suggestion_id in rows:
        delivered = parse_utc_iso(delivered_at)
        checkpoint = latest_checkpoint_between(
            delivered_at=delivered, after=after, through=through
        )
        if checkpoint is not None:
            due.append(
                (
                    delivered + timedelta(days=checkpoint),
                    DueOutcome(
                        kind=kind,
                        item_id=item_id,
                        checkpoint_days=checkpoint,
                        delivered_at=delivered_at,
                        expectation=expectation,
                        source_suggestion_id=source_suggestion_id,
                    ),
                )
            )
    due.sort(key=lambda entry: (entry[0], entry[1].kind, entry[1].item_id))
    return tuple(item for _, item in due[:MAX_OUTCOMES_PER_RUN])


__all__ = [
    "MAX_OUTCOMES_PER_RUN",
    "OUTCOME_CHECKPOINT_DAYS",
    "OUTCOME_WINDOW_DAYS",
    "DueKind",
    "DueOutcome",
    "hypothesis_of",
    "latest_checkpoint_between",
    "load_due_outcomes",
]

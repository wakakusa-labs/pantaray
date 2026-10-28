"""Which delivered suggestions are due for an outcome verdict, and their verdicts.

A suggestion is judged at checkpoints after its delivery, by how the user's and
the environment's state changed since -- never by the buttons pressed on it.
Only the latest checkpoint passed is judged: one skipped while nothing ran is
not judged afterward. Verdicts are append-only (migration 0133).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypedDict

from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    parse_utc_iso,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

# Days after delivery at which a suggestion's outcome is judged.
OUTCOME_CHECKPOINT_DAYS = (1, 7, 30)
# Days. Past the last checkpoint with room for runs that came late.
OUTCOME_WINDOW_DAYS = 45
# One Memory update judges at most this many, so judging stays a small part of it.
MAX_OUTCOMES_PER_RUN = 3
# A hypothesis section a suggestion's summary opens; older ones have none.
_HYPOTHESIS_HEADING = "### Hypothesis"

type OutcomeVerdictValue = Literal["improved", "no_effect", "worse", "unknown"]


class OutcomeEvidence(TypedDict):
    source: str
    ref: str
    observed_at: str
    note: str


@dataclass(frozen=True, slots=True)
class EarlierVerdict:
    checkpoint_days: int
    verdict: OutcomeVerdictValue
    evidence: tuple[OutcomeEvidence, ...]
    reason: str
    judged_at: str


@dataclass(frozen=True, slots=True)
class DueOutcome:
    """One suggestion at the checkpoint it is due for, with what judging reads.

    The reaction fields are what the user did with the suggestion: context for
    reading the records, not evidence of what it changed.
    """

    suggestion_id: str
    checkpoint_days: int
    delivered_at: str
    message: str
    interaction_contract: str
    hypothesis: str
    user_reaction: str | None
    accepted_at: str | None
    rejected_at: str | None
    dismiss_reason: str | None
    action_status: str | None
    earlier_verdicts: tuple[EarlierVerdict, ...]


@dataclass(frozen=True, slots=True)
class JudgedOutcome:
    suggestion_id: str
    checkpoint_days: int
    verdict: OutcomeVerdictValue
    evidence: tuple[OutcomeEvidence, ...]
    reason: str


def latest_due_checkpoint(
    *, delivered_at: datetime, now: datetime, judged: frozenset[int]
) -> int | None:
    """The latest checkpoint passed, unless it is already judged."""

    passed = [
        days
        for days in OUTCOME_CHECKPOINT_DAYS
        if delivered_at + timedelta(days=days) <= now
    ]
    if not passed or passed[-1] in judged:
        return None
    return passed[-1]


def hypothesis_of(summary: str) -> str:
    """The summary's hypothesis section, or the whole summary without one."""

    start = summary.find(_HYPOTHESIS_HEADING)
    if start < 0:
        return summary.strip()
    body = summary[start + len(_HYPOTHESIS_HEADING) :]
    ends = [index for index in (body.find("\n## "), body.find("\n### ")) if index >= 0]
    return body[: min(ends)].strip() if ends else body.strip()


# A suggestion is delivered when its process completes: the terminal event the
# relay forwards to the user, after the row was committed.
_DELIVERED_SQL = """
SELECT suggestions.suggestion_id, suggestions.answer,
       suggestions.interaction_contract, suggestions.suggestion_summary,
       suggestions.user_reaction, suggestions.accepted_at,
       suggestions.rejected_at, suggestions.dismiss_reason,
       suggestions.action_status, MAX(processes.completed_at) AS delivered_at
FROM agent_suggestions AS suggestions
JOIN processes
  ON processes.user_id = suggestions.user_id
 AND processes.suggestion_id = suggestions.suggestion_id
 AND processes.kind = 'suggestion' AND processes.completed_at IS NOT NULL
WHERE suggestions.user_id = ? AND suggestions.status = 'success'
  AND suggestions.has_suggestion = 1
GROUP BY suggestions.suggestion_id
HAVING delivered_at >= ?
ORDER BY delivered_at, suggestions.suggestion_id
"""


def load_due_outcomes(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> tuple[DueOutcome, ...]:
    """Up to MAX_OUTCOMES_PER_RUN due suggestions, the earliest delivered first."""

    cutoff = format_utc_iso(now - timedelta(days=OUTCOME_WINDOW_DAYS))
    earlier = _verdicts_by_suggestion(connection, user_id=user_id, since=cutoff)
    due: list[DueOutcome] = []
    for row in connection.execute(_DELIVERED_SQL, (user_id, cutoff)):
        suggestion_id = str(row["suggestion_id"])
        verdicts = earlier.get(suggestion_id, ())
        checkpoint = latest_due_checkpoint(
            delivered_at=parse_utc_iso(str(row["delivered_at"])),
            now=now,
            judged=frozenset(item.checkpoint_days for item in verdicts),
        )
        if checkpoint is None:
            continue
        due.append(
            DueOutcome(
                suggestion_id=suggestion_id,
                checkpoint_days=checkpoint,
                delivered_at=str(row["delivered_at"]),
                message=str(row["answer"]),
                interaction_contract=str(row["interaction_contract"]),
                hypothesis=hypothesis_of(str(row["suggestion_summary"] or "")),
                user_reaction=row["user_reaction"],
                accepted_at=row["accepted_at"],
                rejected_at=row["rejected_at"],
                dismiss_reason=row["dismiss_reason"],
                action_status=row["action_status"],
                earlier_verdicts=verdicts,
            )
        )
        if len(due) == MAX_OUTCOMES_PER_RUN:
            break
    return tuple(due)


def _verdicts_by_suggestion(
    connection: sqlite3.Connection, *, user_id: str, since: str
) -> dict[str, tuple[EarlierVerdict, ...]]:
    rows = connection.execute(
        """
        SELECT verdicts.suggestion_id, verdicts.checkpoint_days, verdicts.verdict,
               verdicts.evidence_json, verdicts.reason, verdicts.judged_at
        FROM suggestion_outcome_verdicts AS verdicts
        WHERE verdicts.user_id = ? AND verdicts.judged_at >= ?
        ORDER BY verdicts.suggestion_id, verdicts.checkpoint_days
        """,
        (user_id, since),
    ).fetchall()
    grouped: dict[str, list[EarlierVerdict]] = {}
    for row in rows:
        grouped.setdefault(str(row["suggestion_id"]), []).append(
            EarlierVerdict(
                checkpoint_days=int(row["checkpoint_days"]),
                verdict=row["verdict"],
                evidence=tuple(json.loads(str(row["evidence_json"]))),
                reason=str(row["reason"]),
                judged_at=str(row["judged_at"]),
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def record_outcome_verdicts(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    job_id: str,
    prompt_name: str,
    prompt_version: str,
    judged_at: str,
    outcomes: tuple[JudgedOutcome, ...],
) -> None:
    """Append the verdicts in one transaction.

    A verdict already stored for the same checkpoint fails the whole write: the
    user's Memory update lock lets only one run judge at a time, and every run
    leaves judged checkpoints out, so a duplicate means the due set was stale
    and none of this run's verdicts is trusted.
    """

    with immediate_transaction(connection):
        connection.executemany(
            """
            INSERT INTO suggestion_outcome_verdicts(
                verdict_id, user_id, suggestion_id, checkpoint_days, verdict,
                evidence_json, reason, judged_at, job_id, prompt_name,
                prompt_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid.uuid4()),
                    user_id,
                    outcome.suggestion_id,
                    outcome.checkpoint_days,
                    outcome.verdict,
                    json.dumps(list(outcome.evidence), ensure_ascii=False),
                    outcome.reason,
                    judged_at,
                    job_id,
                    prompt_name,
                    prompt_version,
                )
                for outcome in outcomes
            ],
        )


__all__ = [
    "MAX_OUTCOMES_PER_RUN",
    "OUTCOME_CHECKPOINT_DAYS",
    "OUTCOME_WINDOW_DAYS",
    "DueOutcome",
    "EarlierVerdict",
    "JudgedOutcome",
    "OutcomeEvidence",
    "OutcomeVerdictValue",
    "latest_due_checkpoint",
    "load_due_outcomes",
    "record_outcome_verdicts",
]

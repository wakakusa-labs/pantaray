"""What a run that opens after a Stop is told about the run the user stopped.

The run's opening USER step carries it into the model input, next to the user's
words. A Resume step already says the run was stopped
(``ACTION_RESUME_REQUEST_TEXT``), so it gets only the subagents; any other
opening message gets the stop as well.
"""

from __future__ import annotations

import sqlite3

from .job_payload_models import parse_action_subagent_job_payload_json
from .job_types import LOCAL_ACTION_SUBAGENT_JOB_TYPE

ACTION_STOPPED_RUN_NOTE = "The user stopped the previous run before it finished."
ACTION_STOPPED_SUBAGENTS_NOTE = (
    "These subagents were stopped with the run and won't continue on their own; "
    "send one a message to continue it:"
)
# A task is a full brief the run already holds; its first line names it.
_TASK_LABEL_MAX_CHARS = 200


def read_stopped_run_note(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    process_id: str,
    resumed: bool,
) -> str | None:
    """The note for the run ``process_id`` opens, or None after any other end.

    The previous run is the one that ended last: runs of one Action never overlap.
    """

    previous = connection.execute(
        """
        SELECT process_id,status FROM processes
        WHERE user_id=? AND action_id=? AND kind='action' AND process_id<>?
          AND completed_at IS NOT NULL
        ORDER BY completed_at DESC, process_id DESC LIMIT 1
        """,
        (user_id, action_id, process_id),
    ).fetchone()
    if previous is None or previous[1] != "canceled":
        return None
    children = connection.execute(
        """
        SELECT child.process_id,payload.payload_json
        FROM processes AS child
        JOIN jobs AS job ON job.process_id=child.process_id
          AND job.job_type=? AND job.logical_key=job.job_id
        JOIN job_payloads AS payload ON payload.job_id=job.job_id
        WHERE child.user_id=? AND child.action_id=? AND child.parent_process_id=?
          AND child.kind='action_subagent' AND child.status='canceled'
        ORDER BY child.started_at, child.process_id
        """,
        (LOCAL_ACTION_SUBAGENT_JOB_TYPE, user_id, action_id, str(previous[0])),
    ).fetchall()
    sections = [] if resumed else [ACTION_STOPPED_RUN_NOTE]
    if children:
        lines = [f"- {child_id}: {_task_label(str(raw))}" for child_id, raw in children]
        sections.append("\n".join([ACTION_STOPPED_SUBAGENTS_NOTE, *lines]))
    return "\n\n".join(sections) or None


def _task_label(payload_json: str) -> str:
    task = parse_action_subagent_job_payload_json(payload_json)["task"]
    first_line = task.strip().splitlines()[0]
    if len(first_line) <= _TASK_LABEL_MAX_CHARS:
        return first_line
    return first_line[: _TASK_LABEL_MAX_CHARS - 1] + "…"


__all__ = [
    "ACTION_STOPPED_RUN_NOTE",
    "ACTION_STOPPED_SUBAGENTS_NOTE",
    "read_stopped_run_note",
]

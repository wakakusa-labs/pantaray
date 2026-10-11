"""Which subagents a parent run may name in its subagent tools.

A child belongs to its Action, not only to the run that spawned it: a Stop or a
resume starts a new parent run whose conversation still names the earlier run's
children. The parent's terminal settles every child first, so a child of an
earlier run is always finished, and the new run may read its final result (or
ask to cancel or message it, which reports that result). A child that is still
going belongs to the active parent alone. Callers keep the user and Action
conditions, so a child of another Action or user is never owned.
"""

from __future__ import annotations


def owned_child_sql(parent_process_id: str) -> str:
    """SQL condition on ``child`` for the parent identity ``parent_process_id``."""

    return (
        f"(child.parent_process_id={parent_process_id}"
        " OR child.status IN ('completed','failed','canceled'))"
    )


__all__ = ["owned_child_sql"]

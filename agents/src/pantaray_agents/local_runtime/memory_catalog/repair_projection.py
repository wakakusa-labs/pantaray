from __future__ import annotations

import sqlite3

from .errors import MemoryCatalogIntegrityError
from .models import MemoryRevision, MemorySource


def write_inline_repair_projection(
    *,
    connection: sqlite3.Connection,
    source: MemorySource,
    source_record_id: str,
    revision: MemoryRevision,
) -> None:
    if source == "source_records":
        # Search projections never rewrite the immutable captured records.
        return
    if source in {"action_file_read", "agent_experience", "memory_note"}:
        # Catalog-native records have no second domain projection to mutate.
        return
    body = revision.inline_body
    if body is None:
        raise MemoryCatalogIntegrityError("inline repair body is absent")
    targets = {
        "action": ("agent_actions", "action_id", "final_output"),
        "short_term_insight": (
            "agent_insights",
            "insight_id",
            "short_term_insight_data",
        ),
        "suggestion": ("agent_suggestions", "suggestion_id", "answer"),
        "activity_log": ("activity_logs", "log_id", "description"),
        "activity_summary": ("activity_summaries", "summary_id", "summary"),
    }
    target = targets.get(source)
    if target is None:
        raise MemoryCatalogIntegrityError("unsupported inline repair source")
    table, id_column, body_column = target
    cursor = connection.execute(
        f'UPDATE "{table}" SET "{body_column}" = ?, updated_at = ? '
        f'WHERE user_id = ? AND "{id_column}" = ?',
        (body, revision.created_at, revision.user_id, source_record_id),
    )
    if cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError("inline repair domain projection is absent")


__all__ = ["write_inline_repair_projection"]

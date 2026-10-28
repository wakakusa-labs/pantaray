"""The run's evidence sections, rendered as the Memory update prompt shows them."""

from __future__ import annotations

import sqlite3

from pantaray_agents.local_runtime.runtime.memory_dismissal_range import (
    DismissedSuggestion,
)
from pantaray_agents.local_runtime.runtime.suggestion_outcomes import RecentVerdict
from pantaray_agents.local_runtime.storage.chat_messages import ChatMessage
from pantaray_agents.utils.local_time import describe_utc_timestamp, local_period


def render_chat_messages(messages: tuple[ChatMessage, ...]) -> str:
    entries: list[str] = []
    for message in messages:
        content = message.content
        speaker = "user" if content.kind == "user_message" else "Pantaray"
        notes = [describe_utc_timestamp(message.created_at)]
        if content.quote_item_id is not None:
            notes.append(f"quoting {content.quote_item_id}")
        if content.kind == "user_message" and (content.images or content.files):
            notes.append(
                f"with {len(content.images)} image(s) and {len(content.files)} file(s)"
            )
        entries.append(
            f"### chat item {message.item_id}, {speaker} ({', '.join(notes)})\n"
            f"{content.text}"
        )
    return "\n\n".join(entries)


def render_dismissed_suggestions(
    suggestions: tuple[DismissedSuggestion, ...],
) -> str:
    return "\n\n".join(
        f"### suggestion {suggestion.suggestion_id} "
        f"(dismissed {describe_utc_timestamp(suggestion.rejected_at)})\n"
        f"{suggestion.answer}\n\nDismissed"
        + (
            ""
            if suggestion.dismiss_reason is None
            else f", the user's reason: {suggestion.dismiss_reason}"
        )
        for suggestion in suggestions
    )


def render_short_insights(
    *, connection: sqlite3.Connection, user_id: str, insight_ids: tuple[str, ...]
) -> str:
    if not insight_ids:
        return ""
    placeholders = ",".join("?" for _ in insight_ids)
    rows = connection.execute(
        f"""
        SELECT insights.insight_id, insights.short_term_insight_data,
               insights.created_at, activity.description,
               activity.period_start, activity.period_end
        FROM agent_insights AS insights
        LEFT JOIN activity_logs AS activity
          ON activity.user_id = insights.user_id
         AND activity.log_id = insights.insight_id AND activity.status = 'success'
        WHERE insights.user_id = ? AND insights.status = 'success'
          AND insights.insight_id IN ({placeholders})
        ORDER BY insights.created_at ASC, insights.insight_id ASC
        """,
        (user_id, *insight_ids),
    ).fetchall()
    return "\n\n".join(
        f"### short-term Insight {row['insight_id']} "
        f"({describe_utc_timestamp(row['created_at'])})\n"
        f"{str(row['short_term_insight_data'] or '').strip()}\n\n"
        + (
            "#### Objective Activity "
            f"({local_period(row['period_start'], row['period_end'])})\n"
            f"{row['description']}"
            if row["description"] is not None
            else "Objective Activity: unavailable for this Insight."
        )
        for row in rows
    )


def render_activity_summaries(
    *, connection: sqlite3.Connection, user_id: str, summary_ids: tuple[str, ...]
) -> str:
    if not summary_ids:
        return ""
    placeholders = ",".join("?" for _ in summary_ids)
    rows = connection.execute(
        f"""
        SELECT summary_id, summary, period_start, period_end
        FROM activity_summaries
        WHERE user_id = ? AND status = 'success' AND summary_type = '24h'
          AND summary_id IN ({placeholders})
        ORDER BY period_end ASC, summary_id ASC
        """,
        (user_id, *summary_ids),
    ).fetchall()
    return "\n\n".join(
        f"### 24h Activity Summary {row['summary_id']} "
        f"({local_period(row['period_start'], row['period_end'])})\n"
        f"{str(row['summary'] or '').strip()}"
        for row in rows
    )


def render_outcome_verdicts(verdicts: tuple[RecentVerdict, ...]) -> str:
    entries: list[str] = []
    for item in verdicts:
        verdict = item.verdict
        refs = ", ".join(
            f"{evidence['source']} {evidence['ref']}" for evidence in verdict.evidence
        )
        entries.append(
            f"### suggestion {item.suggestion_id}: {verdict.verdict} "
            f"{verdict.checkpoint_days} days after delivery "
            f"(judged {describe_utc_timestamp(verdict.judged_at)})\n"
            f"{item.message}\n\n"
            f"Reason: {verdict.reason}\n"
            f"Evidence: {refs or 'none'}"
        )
    return "\n\n".join(entries)


__all__ = [
    "render_activity_summaries",
    "render_chat_messages",
    "render_dismissed_suggestions",
    "render_outcome_verdicts",
    "render_short_insights",
]

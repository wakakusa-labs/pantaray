"""SuggestionAgent のコンテキスト整形ヘルパー。"""

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from pantaray_agents.schema.agent.suggestion import (
    SuggestionHistoryEntry,
)
from pantaray_agents.schema.repositories.repository import JSONValue
from pantaray_agents.utils.local_time import describe_utc_timestamp, local_period
from pantaray_agents.utils.timestamps import parse_iso8601_utc

from .context_density import is_activity_summary_row_present
from .context_types import ActivityDescriptionRow, ActivitySummaryRow

NO_ACTIVITY_DESCRIPTIONS_TEXT = "(no activity descriptions available)"
NO_ACTIVITY_SUMMARY_1H_TEXT = "(no 1h summary available)"
NO_RECENT_ANSWERS_TEXT = "(no recent answers)"
RECENT_SUGGESTION_REPLY_MAX_CHARS = 2_000
# Stored Action outputs were 947 chars or fewer in 34 of 36 runs, and the head
# carries the conclusion; twenty entries then add at most 20,000 chars.
RECENT_SUGGESTION_ACTION_RESULT_MAX_CHARS = 1_000
# The latest instructions carry the current scope; older ones are superseded
# or already reflected in the result.
RECENT_SUGGESTION_FOLLOWUPS_MAX = 5
RECENT_SUGGESTION_FOLLOWUP_MAX_CHARS = 400
# Older entries are dropped past this total, so entries filled to the per-field
# limits stay within what five such entries (about 5,900 chars each) took before.
RECENT_SUGGESTIONS_MAX_CHARS = 30_000
# Heads the list in every prompt that shows it (lenses and selector), so the
# rule for reading an unanswered offer's age lives only here.
RECENT_SUGGESTIONS_READING_NOTE = (
    "The longer an offer went without a reaction, the more likely it was not"
    " useful. This is a weak signal: the time counts from when it was offered,"
    " not from when the user saw it."
)
# Coarse steps change the prompt text only at a boundary, so repeated runs keep
# the prompt cache. Capped at 24 hours: 97% of reactions came within a day.
_UNANSWERED_OFFER_AGE_STEPS = (
    (timedelta(hours=24), "none for 24+ hours"),
    (timedelta(hours=12), "none for 12+ hours"),
    (timedelta(hours=6), "none for 6+ hours"),
)
_UNANSWERED_OFFER_RECENT = "none yet (offered under 6 hours ago)"
_MESSAGE_ONLY_REACTION = "none asked for (message only)"
# The history's period; the prompt lists the latest RECENT_SUGGESTIONS_SHOWN.
SUGGESTION_HISTORY_DAYS = 28
RECENT_SUGGESTIONS_SHOWN = 20

logger = logging.getLogger(__name__)


def format_activity_descriptions(rows: list[ActivityDescriptionRow] | None) -> str:
    """直近のActivity Descriptionログをプロンプト用に整形（新しい順）。"""
    if not rows:
        return NO_ACTIVITY_DESCRIPTIONS_TEXT

    lines: list[str] = []
    for row in rows:
        desc = str(row.get("description") or "").strip()
        period = local_period(row["period_start"], row["period_end"])
        lines.append(f"- [{period}] {desc}")
    return "\n".join(lines)


def format_activity_summary(rows: list[ActivitySummaryRow] | None) -> str:
    """直近1時間サマリを1件だけ整形する。

    present 判定は Context Availability と同じ。材料のない「活動なし」の定型文は、
    スロットと同じく「無い」ことだけを伝える。
    """
    if not rows or not is_activity_summary_row_present(rows[0]):
        return NO_ACTIVITY_SUMMARY_1H_TEXT

    row = rows[0]
    summary = str(row.get("summary") or "").strip()
    period = local_period(row["period_start"], row["period_end"])
    return f"[{period}] {summary}"


def _bounded(text: str, max_chars: int, marker: str) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[{marker} truncated]"


def format_recent_suggestions(
    rows: list[SuggestionHistoryEntry] | None, now: datetime
) -> str:
    """直近の提案と記録された反応・返信・Action の状態と結果を整形する。"""
    if not rows:
        return NO_RECENT_ANSWERS_TEXT

    blocks: list[str] = [RECENT_SUGGESTIONS_READING_NOTE]
    total = len(RECENT_SUGGESTIONS_READING_NOTE) + 1
    for row in rows:
        block = format_recent_suggestion(row, now)
        total += len(block) + 1
        if len(blocks) > 1 and total > RECENT_SUGGESTIONS_MAX_CHARS:
            break
        blocks.append(block)
    return "\n".join(blocks)


def _reaction_text(row: SuggestionHistoryEntry, now: datetime) -> str:
    if row.interaction_contract == "message_only":
        return _MESSAGE_ONLY_REACTION
    if row.user_reaction is not None:
        return row.user_reaction
    age = now - parse_iso8601_utc(row.created_at)
    for threshold, text in _UNANSWERED_OFFER_AGE_STEPS:
        if age >= threshold:
            return text
    return _UNANSWERED_OFFER_RECENT


def format_recent_suggestion(row: SuggestionHistoryEntry, now: datetime) -> str:
    lines: list[str] = []
    created_at = describe_utc_timestamp(row.created_at)
    lines.append(f"- [{created_at}] answer: {row.answer}")
    if row.lens is not None:
        # The lens prompt calls a lens the kind of suggestion for the run.
        lines.append(f"  Kind of suggestion: {row.lens}")
    lines.append(f"  User reaction: {_reaction_text(row, now)}")
    if row.dismiss_reason is not None:
        reason = _bounded(
            row.dismiss_reason, RECENT_SUGGESTION_REPLY_MAX_CHARS, "reason"
        )
        lines.append(f"  User's reason for dismissing: {reason}")
    if row.user_reply is not None:
        reply = _bounded(row.user_reply, RECENT_SUGGESTION_REPLY_MAX_CHARS, "reply")
        lines.append(f"  User reply: {reply}")
    followups = row.action_followups[-RECENT_SUGGESTION_FOLLOWUPS_MAX:]
    if followups:
        lines.append("  User instructions during the Action (oldest first):")
        lines.extend(
            "    - "
            + _bounded(
                " ".join(text.split()),
                RECENT_SUGGESTION_FOLLOWUP_MAX_CHARS,
                "instruction",
            )
            for text in followups
        )
    if row.action_status is not None:
        lines.append(f"  Action status: {row.action_status}")
    if row.action_result is not None:
        result = _bounded(
            row.action_result, RECENT_SUGGESTION_ACTION_RESULT_MAX_CHARS, "result"
        )
        lines.append(f"  Latest Action result: {result}")
    return "\n".join(lines)


def normalize_recent_suggestion_entry(
    row: Mapping[str, JSONValue] | None,
) -> SuggestionHistoryEntry | None:
    """Repository 取得行を SuggestionHistoryEntry へ正規化する。"""
    if row is None or not isinstance(row, Mapping):
        logger.debug(
            "Skip recent suggestion row because it is not a mapping: %s",
            type(row).__name__,
        )
        return None

    answer = str(row.get("answer") or "")
    created_at = str(row.get("created_at") or "")
    raw_thinking = row.get("thinking")
    thinking = None if raw_thinking is None else str(raw_thinking)
    return SuggestionHistoryEntry.model_validate(
        {
            "answer": answer,
            "interaction_contract": row.get("interaction_contract"),
            "created_at": created_at,
            "thinking": thinking,
            "user_reaction": row.get("user_reaction"),
            "dismiss_reason": row.get("dismiss_reason"),
            "user_reply": row.get("user_reply"),
            "action_status": row.get("action_status"),
            "action_result": row.get("action_result"),
            "action_followups": row.get("action_followups") or [],
            "lens": row.get("lens"),
        }
    )

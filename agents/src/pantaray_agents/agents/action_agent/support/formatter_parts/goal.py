"""Goal and insight formatter mixin."""

from __future__ import annotations


class GoalFormattingMixin:
    def build_insight_text(self, profile_brief: str | None) -> str:
        if not profile_brief:
            return "(No insight information)"
        return f"[Long-term (brief)]\n{profile_brief}"

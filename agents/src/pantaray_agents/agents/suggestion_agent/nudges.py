"""Known misjudgments of a Suggestion run, judged from its submission.

A condition holds on what a run submitted and the Suggestions the user was
already shown; its text, in ``suggestion_nudges.yaml``, goes back once in place
of accepting the call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pantaray_agents.schema.agent.suggestion import SuggestionHistoryEntry
from pantaray_agents.utils.prompt_loader import PromptConfig

from .context_formatters import format_recent_suggestion

SUGGESTION_NUDGES_PROMPT_NAME = "suggestion/suggestion_nudges"
REPEATED_SUGGESTION = "repeated_suggestion"
# The bundled model's cosine between the openings of two suggestions. In the
# 108 shown from 10-04 to 10-12, pairs at or above it offered the same thing,
# or another artifact for the same topic, again; below it, another topic. A
# follow-up to an accepted suggestion is not a repeat: 4 of 9 were accepted.
REPEAT_SIMILARITY = 0.60
REPEATS_SHOWN = 3
# A suggestion leads with what it offers, and the model reads 512 characters.
REPEAT_OPENING_CHARS = 200

type Embed = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def _opening(text: str) -> str:
    return text[:REPEAT_OPENING_CHARS]


@dataclass(frozen=True, slots=True)
class PastSuggestions:
    """The Suggestions shown in the history's period, newest first.

    ``embed`` is None when the bundled embedding model is not installed; the
    repeat check is then off, as semantic memory search is.
    """

    rows: tuple[SuggestionHistoryEntry, ...]
    now: datetime
    embed: Embed | None = None
    vectors: tuple[Sequence[float], ...] = ()

    def repeated(self, key_point: str) -> list[SuggestionHistoryEntry]:
        """The unaccepted shown suggestions close to ``key_point``'s opening."""
        if self.embed is None or not self.rows:
            return []
        (vector,) = self.embed([_opening(key_point)])
        # The model returns unit vectors, so the dot product is the cosine.
        scored = sorted(
            (
                (sum(a * b for a, b in zip(vector, other, strict=True)), index)
                for index, other in enumerate(self.vectors)
                if self.rows[index].user_reaction != "accepted"
            ),
            reverse=True,
        )
        return [
            self.rows[index]
            for score, index in scored[:REPEATS_SHOWN]
            if score >= REPEAT_SIMILARITY
        ]

    def describe(self, rows: Sequence[SuggestionHistoryEntry]) -> str:
        return "\n".join(format_recent_suggestion(row, self.now) for row in rows)


async def load_past_suggestions(
    rows: Sequence[SuggestionHistoryEntry], now: datetime, embed: Embed | None
) -> PastSuggestions:
    if embed is None or not rows:
        return PastSuggestions(tuple(rows), now)
    # The model runs on the CPU; the event loop keeps serving meanwhile.
    vectors = await asyncio.to_thread(embed, [_opening(row.answer) for row in rows])
    return PastSuggestions(tuple(rows), now, embed, tuple(vectors))


def nudge_text(config: PromptConfig, condition: str, **values: str) -> str:
    return config.require_role_rule(condition).format(**values).strip()


def render_nudge(config: PromptConfig, tool: str, *notices: str) -> str:
    return config.prompt.format(notice="\n".join(notices), tool=tool).strip()


__all__ = [
    "Embed",
    "PastSuggestions",
    "REPEATED_SUGGESTION",
    "SUGGESTION_NUDGES_PROMPT_NAME",
    "load_past_suggestions",
    "nudge_text",
    "render_nudge",
]

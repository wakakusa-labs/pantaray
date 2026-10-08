"""What one send lays out from the history, and the context budget over it.

The loop holds the budget (``budget.py``) in its own unit, the history entry:
``omit_before`` is an index into the history as the hooks rebuild it. A tool
result before it is sent with its output omitted and its media dropped, as the
Action omits a result body; its call, every user and assistant item and every
notice stay, so each call keeps its answer and nothing is summarized. The
latest run of results is never omitted, and neither are the head, the system
instruction and the tools: when what is left still reaches the budget, the run
cannot go on (``ContextCapacityExceeded``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from pantaray_agents.conversation.budget import ContextBudget, TurnUsage
from pantaray_agents.conversation.prefix import ConversationLayout, replayable_turn
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnToolResultItem,
)

# The same mark the Action sends for a body past its omission boundary.
OMITTED_OUTPUT_MARK = "…"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """One history item; ``turn_id`` keys an assistant's turn in the store."""

    item: LlmTurnItem
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class WindowState:
    """Where a run's window stands, for the caller to store and resume with.

    ``omit_before`` only ever moves forward: an output shown again would
    rewrite a prefix already sent, and lose its prompt cache with it.
    """

    budget: ContextBudget
    omit_before: int

    def fit(
        self,
        history: Sequence[ConversationEntry],
        lay: Callable[[int], LaidOutWindow],
    ) -> tuple[WindowState, LaidOutWindow]:
        """The window to send, laid out by ``lay`` at a boundary, and where the
        state stands after it: rebuilt behind a later boundary when it must be.
        """

        fitted = self.budget.fit(
            lay,
            omit_before=self.omit_before,
            resolve_boundary=lambda byte_budget: resolve_omission(
                history, omit_before=self.omit_before, byte_budget=byte_budget
            ),
        )
        if fitted.rebuilt_at is None:
            return self, fitted.window
        rebuilt = WindowState(
            budget=replace(self.budget, reset_pending=False),
            omit_before=fitted.rebuilt_at,
        )
        return rebuilt, fitted.window

    def record(
        self, usage: TurnUsage, *, rendered_bytes: int, rebuilt: bool
    ) -> WindowState:
        """Calibrate on what one send reported, and reserve a rebuild at 85%.

        ``rebuilt`` says the send moved the boundary, which rewrote the prefix
        on purpose: the cache read that loses is no miss.
        """

        baseline = self.budget.baseline
        recorded = self.budget.record(
            usage,
            rendered_bytes=rendered_bytes,
            did_rebuild=rebuilt,
            # The input the last measured send reported, which this one's cache
            # should read again.
            previous_prompt_tokens=0 if baseline is None else baseline["prompt_tokens"],
        )
        if recorded.cache_misses > 0:
            logger.info(
                "Conversation prompt cache missed outside a rebuild: "
                "cache_misses=%d prompt_tokens=%d cache_read_tokens=%d",
                recorded.cache_misses,
                usage.prompt_tokens,
                usage.cache_read_tokens,
            )
        return replace(
            self,
            budget=replace(
                self.budget,
                reset_pending=recorded.reset_pending,
                baseline=baseline if recorded.baseline is None else recorded.baseline,
            ),
        )


@dataclass(frozen=True, slots=True)
class LaidOutWindow:
    """One send's items behind the head, and the bytes the budget reads."""

    layout: ConversationLayout
    history_bytes: int
    rendered_bytes: int


def lay_out(
    history: Sequence[ConversationEntry],
    *,
    omit_before: int,
    fingerprint: str,
    turns: Mapping[str, ActionProviderTurnRecord],
    notices: Sequence[str],
    head_bytes: int,
) -> LaidOutWindow:
    """Lay the history out behind the head, each turn back where it still fits.

    ``head_bytes`` measures the head, the system instruction and the tools. A
    turn's opaque payload stays out of the measure, as media does: both ride
    on the request outside the serialized items and are left to the baseline
    the provider measured.
    """

    layout = ConversationLayout(fingerprint=fingerprint)
    history_bytes = 0
    for index, entry in enumerate(history):
        item = _shown(entry.item, omit=index < omit_before)
        history_bytes += _item_bytes(item)
        if isinstance(item, LlmTurnAssistantItem) and entry.turn_id is not None:
            turn = replayable_turn(
                turns.get(entry.turn_id), item.calls, prefix=layout.fingerprint
            )
            if turn is not None:
                layout.add(
                    item.model_copy(update={"provider_turn": turn}),
                    turn_of=entry.turn_id,
                )
                continue
        layout.add(item)
    for notice in notices:
        layout.add_text(f"# System Notice\n{notice}")
    notice_bytes = sum(_item_bytes(item) for item in layout.items[len(history) :])
    return LaidOutWindow(
        layout=layout,
        history_bytes=history_bytes,
        rendered_bytes=head_bytes + history_bytes + notice_bytes,
    )


def resolve_omission(
    history: Sequence[ConversationEntry], *, omit_before: int, byte_budget: int
) -> int:
    """The boundary past the oldest runs of results that brings the history
    within ``byte_budget``, never into the latest run and never back.

    What is left may still exceed the budget; ``ContextBudget.fit`` checks it.
    """

    remaining = sum(
        _item_bytes(_shown(entry.item, omit=index < omit_before))
        for index, entry in enumerate(history)
    )
    boundary = omit_before
    for run in _result_runs(history)[:-1]:
        if remaining <= byte_budget:
            break
        for index in run:
            if index >= boundary:
                item = history[index].item
                remaining -= _item_bytes(item) - _item_bytes(_shown(item, omit=True))
        boundary = max(boundary, run[-1] + 1)
    return boundary


def _result_runs(history: Sequence[ConversationEntry]) -> list[list[int]]:
    """The indices of each run of consecutive tool results: one turn's answers."""

    runs: list[list[int]] = []
    current: list[int] = []
    for index, entry in enumerate(history):
        if isinstance(entry.item, LlmTurnToolResultItem):
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _shown(item: LlmTurnItem, *, omit: bool) -> LlmTurnItem:
    if omit and isinstance(item, LlmTurnToolResultItem):
        return item.model_copy(update={"output": OMITTED_OUTPUT_MARK, "content": []})
    return item


def _item_bytes(item: LlmTurnItem) -> int:
    return len(item.model_dump_json(exclude={"provider_turn"}).encode("utf-8"))


__all__ = [
    "OMITTED_OUTPUT_MARK",
    "ConversationEntry",
    "LaidOutWindow",
    "WindowState",
    "lay_out",
    "resolve_omission",
]

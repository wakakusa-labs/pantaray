"""The Supervisor's context budget, kept in the Action's run state.

The budget itself is the conversation layer's (``conversation/budget.py``);
this module reads its measurements from the run context, writes back what a
turn changed, and resolves the omission boundary in step numbers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.state.types import (
    ActionAgentContext,
    ActionAgentState,
)
from pantaray_agents.agents.core.llm_file_inputs import LlmFileInput
from pantaray_agents.agents.core.mixins.llm_usage import LlmUsage
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.conversation.budget import (
    ContextBudget,
    ContextCapacityExceeded,
    turn_usage,
)
from pantaray_llm.contracts.conversation import LlmConversation

if TYPE_CHECKING:
    from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
        PromptRenderingService,
    )

logger = logging.getLogger(__name__)

CONTEXT_RESET_RESULT_LINE = (
    "Context reset: the prompt window was rebuilt at the model context limit "
    "without a summary; omitted outputs stay retrievable with history_fetch."
)


def _window_tokens() -> int:
    return load_local_runtime_tunables().action_agent.context_window_tokens


@dataclass(frozen=True, slots=True)
class PreparedWindow:
    """What one THINK sends, and the measurement the capacity policy reads."""

    # The request's own user message: the unchanging head when a conversation
    # carries the history, the whole rendering when it does not.
    prompt: str
    # The full deterministic rendering, which the step record keeps whichever
    # shape was sent.
    recorded_prompt: str
    conversation: LlmConversation | None
    # The conversation's prefix, which a provider turn this window produces is
    # recorded with; None when the turn is sent as one string.
    fingerprint: str | None
    # The context message this turn appended, which the step record keeps so a
    # later turn replays the bytes that were sent rather than rebuilding them.
    turn_context: str | None
    # The head sections that message brings up to date, by field, recorded with
    # it so a later turn can tell what the conversation already shows.
    world_state: dict[str, str] | None
    # The media the sent shape references, uploaded with the request.
    file_inputs: tuple[LlmFileInput, ...]
    # The history's share of the rendering, in the unit the omission boundary
    # is resolved in.
    history_bytes: int
    rendered_bytes: int
    did_rebuild: bool = False


def prepare_window(
    state: ActionAgentState,
    *,
    rendering: PromptRenderingService,
    # The caller owns what a turn looks like at a given omission boundary; the
    # budget owns whether it fits and how far the boundary has to move.
    assemble: Callable[[int], PreparedWindow],
) -> PreparedWindow:
    context = state["context"]
    boundary = stored_body_omission(context)
    try:
        fitted = read_budget(context).fit(
            assemble,
            omit_before=boundary,
            resolve_boundary=lambda byte_budget: (
                rendering.resolve_history_omission_boundary(
                    state,
                    byte_budget=byte_budget,
                    omit_before_step_number=boundary,
                )
            ),
        )
    except ContextCapacityExceeded as exc:
        context["context_body_omitted_before_step"] = exc.omit_before
        raise
    if fitted.rebuilt_at is None:
        return fitted.window
    context["context_body_omitted_before_step"] = fitted.rebuilt_at
    context["context_reset_pending"] = False
    return replace(fitted.window, did_rebuild=fitted.rebuilt_at > boundary)


def stored_body_omission(context: ActionAgentContext) -> int:
    """結果本文の省略境界（この step_number 未満の結果本文を表示しない）。"""

    return int(context.get("context_body_omitted_before_step", 0) or 0)


def record_think_usage(
    context: ActionAgentContext,
    *,
    usage_before: LlmUsage,
    usage_after: LlmUsage,
    rendered_bytes: int,
    did_rebuild: bool,
) -> None:
    """THINK 1 回分の usage を計上し、次の境界判定とキャッシュ計測を更新する。"""

    usage = turn_usage(usage_before, usage_after)
    recorded = read_budget(context).record(
        usage,
        rendered_bytes=rendered_bytes,
        did_rebuild=did_rebuild,
        previous_prompt_tokens=int(
            context.get("context_cache_prev_prompt_tokens", 0) or 0
        ),
    )
    if recorded.baseline is None:
        context["context_reset_pending"] = recorded.reset_pending
        return
    context["context_input_baseline"] = recorded.baseline
    context["context_reset_pending"] = recorded.reset_pending
    # The next turn's cache reads against the input this turn measured.
    context["context_cache_prev_prompt_tokens"] = recorded.baseline["prompt_tokens"]
    if recorded.cache_misses > 0:
        logger.info(
            "Supervisor THINK prompt cache missed outside a reset boundary: "
            "cache_misses=%d prompt_tokens=%d cache_read_tokens=%d "
            "reasoning_tokens=%d",
            recorded.cache_misses,
            usage.prompt_tokens,
            usage.cache_read_tokens,
            usage.reasoning_tokens,
        )


def read_budget(context: ActionAgentContext) -> ContextBudget:
    """The budget as this run last measured it."""

    return ContextBudget(
        window_tokens=_window_tokens(),
        baseline=context.get("context_input_baseline"),
        reset_pending=bool(context.get("context_reset_pending", False)),
    )


__all__ = [
    "CONTEXT_RESET_RESULT_LINE",
    "PreparedWindow",
    "prepare_window",
    "read_budget",
    "record_think_usage",
    "stored_body_omission",
]

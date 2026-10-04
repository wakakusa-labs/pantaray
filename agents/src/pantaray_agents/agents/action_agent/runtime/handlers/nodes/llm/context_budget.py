"""Supervisor input accounting and output-only pruning before provider requests.

Provider usage calibrates the complete text input (prompt, system and tool schemas).
Between measurements, UTF-8 bytes / 4 estimates changes; media/provider overhead
remains in the measured baseline. This is an estimate, not a provider tokenizer.
"""

from __future__ import annotations

import logging
import math
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
from pantaray_llm.contracts.conversation import LlmConversation

if TYPE_CHECKING:
    from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
        PromptRenderingService,
    )

logger = logging.getLogger(__name__)

# Provider usage がない区間の既存の UTF-8 バイト推定係数。
_BYTES_PER_TOKEN = 4
# 85% でリセットを予約し、次の THINK 境界で組み直す。
_RESET_ARM_RATIO = 0.85
# 95% を超えたらその THINK の前に必ず組み直す。
_RESET_FORCE_RATIO = 0.95
# 固定部分も含めた入力全体の目標割合。保護対象は目標のために削らない。
_INPUT_TARGET_RATIO = 0.5
# LlmUsage.fields に載る cache 読み出しトークンのキー。
# local_runtime/llm_proxy/response_parsing.py の usage 抽出と同じ名前。
_CACHE_READ_TOKENS_FIELD = "cached_prompt_tokens"
# Same source: how much of the output the model spent on reasoning, which is the
# other half of what a turn's shape costs.
_REASONING_TOKENS_FIELD = "reasoning_tokens"

CONTEXT_RESET_RESULT_LINE = (
    "Context reset: the prompt window was rebuilt at the model context limit "
    "without a summary; omitted outputs stay retrievable with history_fetch."
)


def _window_tokens() -> int:
    return load_local_runtime_tunables().action_agent.context_window_tokens


class ContextCapacityExceeded(RuntimeError):
    """Protected input still exceeds the safe capacity after all eligible pruning."""


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


def input_bytes(*texts: str) -> int:
    return sum(len(text.encode("utf-8")) for text in texts)


def estimate_input_tokens(context: ActionAgentContext, *, rendered_bytes: int) -> int:
    byte_estimate = math.ceil(rendered_bytes / _BYTES_PER_TOKEN)
    baseline = context.get("context_input_baseline")
    if baseline is None:
        return byte_estimate
    calibrated = baseline["prompt_tokens"] + math.ceil(
        (rendered_bytes - baseline["rendered_bytes"]) / _BYTES_PER_TOKEN
    )
    return max(byte_estimate, calibrated)


def prepare_window(
    state: ActionAgentState,
    *,
    rendering: PromptRenderingService,
    # The caller owns what a turn looks like at a given omission boundary; this
    # module owns whether it fits and how far the boundary has to move.
    assemble: Callable[[int], PreparedWindow],
) -> PreparedWindow:
    context = state["context"]
    boundary = stored_body_omission(context)
    window = assemble(boundary)
    if not must_rebuild_window(context, rendered_bytes=window.rendered_bytes):
        return window

    estimated = estimate_input_tokens(context, rendered_bytes=window.rendered_bytes)
    history_budget = window.history_bytes + _BYTES_PER_TOKEN * (
        int(_window_tokens() * _INPUT_TARGET_RATIO) - estimated
    )
    new_boundary = rendering.resolve_history_omission_boundary(
        state,
        byte_budget=history_budget,
        omit_before_step_number=boundary,
    )
    store_body_omission(context, boundary=new_boundary)
    window = assemble(new_boundary)
    estimated = estimate_input_tokens(context, rendered_bytes=window.rendered_bytes)
    if estimated >= int(_window_tokens() * _RESET_ARM_RATIO):
        raise ContextCapacityExceeded(
            f"Protected action input is approximately {estimated} tokens after pruning; "
            f"it must be below 85% of the {_window_tokens()}-token input window."
        )
    context["context_reset_pending"] = False
    return replace(window, did_rebuild=new_boundary > boundary)


def stored_body_omission(context: ActionAgentContext) -> int:
    """結果本文の省略境界（この step_number 未満の結果本文を表示しない）。"""

    return int(context.get("context_body_omitted_before_step", 0) or 0)


def store_body_omission(context: ActionAgentContext, *, boundary: int) -> int:
    """本体の省略境界を進める。

    境界は決して戻さない。戻すと一度省略した行が本体に復活し、追記専用規約と
    プロンプトキャッシュの両方を壊す。
    """

    updated = max(stored_body_omission(context), boundary)
    context["context_body_omitted_before_step"] = updated
    return updated


def must_rebuild_window(
    context: ActionAgentContext,
    *,
    rendered_bytes: int,
) -> bool:
    """この THINK 境界で窓を組み直すかどうかを返す。"""

    estimated_tokens = estimate_input_tokens(context, rendered_bytes=rendered_bytes)
    return estimated_tokens > int(_window_tokens() * _RESET_FORCE_RATIO) or bool(
        context.get("context_reset_pending", False)
    )


def record_think_usage(
    context: ActionAgentContext,
    *,
    usage_before: LlmUsage,
    usage_after: LlmUsage,
    rendered_bytes: int,
    did_rebuild: bool,
) -> None:
    """THINK 1 回分の usage を計上し、次の境界判定とキャッシュ計測を更新する。"""

    prompt_tokens = _delta_tokens(usage_before.prompt_tokens, usage_after.prompt_tokens)
    if prompt_tokens <= 0:
        # Keep the last measured pair, but still arm from complete-input growth.
        context["context_reset_pending"] = estimate_input_tokens(
            context,
            rendered_bytes=rendered_bytes,
        ) >= int(_window_tokens() * _RESET_ARM_RATIO)
        return
    window_tokens = _window_tokens()
    context["context_input_baseline"] = {
        "prompt_tokens": prompt_tokens,
        "rendered_bytes": rendered_bytes,
    }
    context["context_reset_pending"] = prompt_tokens >= int(
        window_tokens * _RESET_ARM_RATIO
    )

    previous_prompt_tokens = int(
        context.get("context_cache_prev_prompt_tokens", 0) or 0
    )
    context["context_cache_prev_prompt_tokens"] = prompt_tokens
    if did_rebuild or previous_prompt_tokens <= 0:
        # リセット境界ではベースラインを張り直すだけで、ミスは数えない。
        return
    cache_read_tokens = _delta_tokens(
        usage_before.fields.get(_CACHE_READ_TOKENS_FIELD),
        usage_after.fields.get(_CACHE_READ_TOKENS_FIELD),
    )
    cache_misses = min(previous_prompt_tokens, prompt_tokens) - cache_read_tokens
    if cache_misses > 0:
        logger.info(
            "Supervisor THINK prompt cache missed outside a reset boundary: "
            "cache_misses=%d prompt_tokens=%d cache_read_tokens=%d "
            "reasoning_tokens=%d",
            cache_misses,
            prompt_tokens,
            cache_read_tokens,
            _delta_tokens(
                usage_before.fields.get(_REASONING_TOKENS_FIELD),
                usage_after.fields.get(_REASONING_TOKENS_FIELD),
            ),
        )


def _delta_tokens(before: int | None, after: int | None) -> int:
    return (after or 0) - (before or 0)


__all__ = [
    "CONTEXT_RESET_RESULT_LINE",
    "ContextCapacityExceeded",
    "PreparedWindow",
    "prepare_window",
    "input_bytes",
    "estimate_input_tokens",
    "must_rebuild_window",
    "record_think_usage",
    "store_body_omission",
    "stored_body_omission",
]

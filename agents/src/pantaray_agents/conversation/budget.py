"""How much of a model's input window a conversation may fill, and what it used.

Provider usage calibrates the complete text input (prompt, system instruction
and tool schemas). Between measurements, UTF-8 bytes / 4 estimates the change;
media and provider overhead stay in the measured baseline. This is an estimate,
not a provider tokenizer.

At 85% of the window a rebuild is reserved for the next turn, and past 95% the
turn is rebuilt before it is sent. A rebuild omits older outputs up to a
boundary the caller resolves in its own unit; the boundary only moves forward,
since an output shown again would rewrite a prefix already sent and lose both
the append-only history and its prompt cache.

The caller keeps the measurements between turns: these functions take them as
values and return what changed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypedDict

# The UTF-8 bytes one token is estimated at between provider measurements.
_BYTES_PER_TOKEN = 4
# At 85% a rebuild is reserved for the next turn's boundary.
_RESET_ARM_RATIO = 0.85
# Past 95% the turn is rebuilt before it is sent.
_RESET_FORCE_RATIO = 0.95
# The share of the window a rebuild aims the complete input at. Protected parts
# are never cut to reach it.
_INPUT_TARGET_RATIO = 0.5
# The usage fields the provider reports cache reads and reasoning under, as
# local_runtime/llm_proxy/response_parsing.py extracts them.
_CACHE_READ_TOKENS_FIELD = "cached_prompt_tokens"
_REASONING_TOKENS_FIELD = "reasoning_tokens"


class InputBaseline(TypedDict):
    """Provider input tokens, paired with the complete input bytes they measured."""

    prompt_tokens: int
    rendered_bytes: int


class ContextCapacityExceeded(RuntimeError):
    """Protected input still exceeds the safe capacity after all eligible pruning."""

    def __init__(self, message: str, *, omit_before: int) -> None:
        super().__init__(message)
        # The boundary the rebuild reached, which stays moved.
        self.omit_before = omit_before


class MeasuredWindow(Protocol):
    """What one turn sends, measured in the units this budget reads."""

    @property
    def history_bytes(self) -> int: ...

    @property
    def rendered_bytes(self) -> int: ...


class UsageTotals(Protocol):
    """A run's usage totals, which one turn's usage is the difference of."""

    @property
    def prompt_tokens(self) -> int | None: ...

    @property
    def fields(self) -> Mapping[str, int]: ...


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """What one provider turn reported."""

    prompt_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int


@dataclass(frozen=True, slots=True)
class FittedWindow[W: MeasuredWindow]:
    window: W
    # The omission boundary the window was rebuilt at; None when it fit as it was.
    rebuilt_at: int | None


@dataclass(frozen=True, slots=True)
class RecordedUsage:
    """What the next turn's budget reads after one turn's usage."""

    reset_pending: bool
    # The new measurement; None when the turn reported no input and the
    # previous one still stands.
    baseline: InputBaseline | None
    # Input tokens the prompt cache should have read and did not. A rebuilt
    # turn changes its prefix on purpose and counts none.
    cache_misses: int


def input_bytes(*texts: str) -> int:
    return sum(len(text.encode("utf-8")) for text in texts)


def turn_usage(before: UsageTotals, after: UsageTotals) -> TurnUsage:
    """One turn's usage, from the run's totals before and after it."""

    return TurnUsage(
        prompt_tokens=_delta(before.prompt_tokens, after.prompt_tokens),
        cache_read_tokens=_delta(
            before.fields.get(_CACHE_READ_TOKENS_FIELD),
            after.fields.get(_CACHE_READ_TOKENS_FIELD),
        ),
        reasoning_tokens=_delta(
            before.fields.get(_REASONING_TOKENS_FIELD),
            after.fields.get(_REASONING_TOKENS_FIELD),
        ),
    )


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """One model's input window, and what this conversation last measured of it."""

    window_tokens: int
    baseline: InputBaseline | None
    reset_pending: bool

    def estimate(self, *, rendered_bytes: int) -> int:
        byte_estimate = math.ceil(rendered_bytes / _BYTES_PER_TOKEN)
        if self.baseline is None:
            return byte_estimate
        calibrated = self.baseline["prompt_tokens"] + math.ceil(
            (rendered_bytes - self.baseline["rendered_bytes"]) / _BYTES_PER_TOKEN
        )
        return max(byte_estimate, calibrated)

    def must_rebuild(self, *, rendered_bytes: int) -> bool:
        """Whether the turn about to be sent is rebuilt first."""

        return self.reset_pending or self.estimate(rendered_bytes=rendered_bytes) > int(
            self.window_tokens * _RESET_FORCE_RATIO
        )

    def fit[W: MeasuredWindow](
        self,
        assemble: Callable[[int], W],
        *,
        omit_before: int,
        resolve_boundary: Callable[[int], int],
    ) -> FittedWindow[W]:
        """The window to send, rebuilt behind a later boundary when it must be.

        ``assemble`` lays the turn out with outputs omitted before a boundary;
        ``resolve_boundary`` finds the boundary that brings the history within
        a byte budget, in the caller's unit. Raises ``ContextCapacityExceeded``
        when what cannot be omitted still reaches the reservation threshold.
        """

        window = assemble(omit_before)
        if not self.must_rebuild(rendered_bytes=window.rendered_bytes):
            return FittedWindow(window=window, rebuilt_at=None)
        estimated = self.estimate(rendered_bytes=window.rendered_bytes)
        history_budget = window.history_bytes + _BYTES_PER_TOKEN * (
            int(self.window_tokens * _INPUT_TARGET_RATIO) - estimated
        )
        boundary = max(omit_before, resolve_boundary(history_budget))
        window = assemble(boundary)
        estimated = self.estimate(rendered_bytes=window.rendered_bytes)
        if estimated >= int(self.window_tokens * _RESET_ARM_RATIO):
            raise ContextCapacityExceeded(
                f"Protected action input is approximately {estimated} tokens after "
                f"pruning; it must be below 85% of the {self.window_tokens}-token "
                "input window.",
                omit_before=boundary,
            )
        return FittedWindow(window=window, rebuilt_at=boundary)

    def record(
        self,
        usage: TurnUsage,
        *,
        rendered_bytes: int,
        did_rebuild: bool,
        previous_prompt_tokens: int,
    ) -> RecordedUsage:
        """Calibrate on what one turn reported, and reserve a rebuild at 85%.

        ``previous_prompt_tokens`` is the input the last measured turn reported,
        which a prompt cache should read again on this one.
        """

        arm_tokens = int(self.window_tokens * _RESET_ARM_RATIO)
        if usage.prompt_tokens <= 0:
            # Keep the last measured pair, but still arm from complete-input growth.
            return RecordedUsage(
                reset_pending=self.estimate(rendered_bytes=rendered_bytes)
                >= arm_tokens,
                baseline=None,
                cache_misses=0,
            )
        cache_misses = (
            0
            if did_rebuild or previous_prompt_tokens <= 0
            else min(previous_prompt_tokens, usage.prompt_tokens)
            - usage.cache_read_tokens
        )
        return RecordedUsage(
            reset_pending=usage.prompt_tokens >= arm_tokens,
            baseline={
                "prompt_tokens": usage.prompt_tokens,
                "rendered_bytes": rendered_bytes,
            },
            cache_misses=cache_misses,
        )


def _delta(before: int | None, after: int | None) -> int:
    return (after or 0) - (before or 0)


__all__ = [
    "ContextBudget",
    "ContextCapacityExceeded",
    "FittedWindow",
    "InputBaseline",
    "MeasuredWindow",
    "RecordedUsage",
    "TurnUsage",
    "UsageTotals",
    "input_bytes",
    "turn_usage",
]

"""Which of one model turn's tool calls run now, and how.

The plan is pure: it reads each called tool's declared ``ToolConcurrency`` and
returns the calls in the order the model gave them, never reordered. Running
the batch and numbering its steps stay with the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pantaray_agents.tools.contract import ToolConcurrency

type BatchMode = Literal["parallel", "sequential"]

type ExclusionReason = Literal[
    "run_ending_tool",
    "solo_turn_tool",
    "after_solo_turn_tool",
    "max_parallel_exceeded",
    "tool_step_budget_exhausted",
]


EXCLUSION_NOTICES: dict[ExclusionReason, str] = {
    "run_ending_tool": "must be the only call of its turn; send exactly one, alone",
    "solo_turn_tool": "must be the only call of its turn",
    "after_solo_turn_tool": "was queued behind a call that must run alone",
    "max_parallel_exceeded": "exceeded the parallel tool call limit of this turn",
    "tool_step_budget_exhausted": "exceeded the remaining tool step budget",
}
"""What the model is told about a call this turn did not run."""

PROVIDER_DROPPED_NOTICE = "was dropped by the model provider above the requested limit"

# A call naming no declared tool runs in order; the tool registry answers it.
_UNDECLARED = ToolConcurrency("sequential")


class ToolCallLike(Protocol):
    """The one attribute the plan reads from a call."""

    @property
    def tool_id(self) -> str: ...


@dataclass(frozen=True)
class ExcludedToolCall[CallT: ToolCallLike]:
    """A call this turn does not run, and why."""

    call: CallT
    reason: ExclusionReason


@dataclass(frozen=True)
class ToolBatchPlan[CallT: ToolCallLike]:
    """One turn's tool batch."""

    calls: tuple[CallT, ...]
    """The calls to run now, in the order the model gave them."""

    mode: BatchMode
    """How ``calls`` run."""

    deferred: tuple[ExcludedToolCall[CallT], ...]
    """Calls the model should request again in a later turn."""

    dropped: tuple[ExcludedToolCall[CallT], ...]
    """Calls cut for the parallel limit or the tool step budget."""


def plan_tool_batch[CallT: ToolCallLike](
    calls: Sequence[CallT],
    *,
    concurrency: Mapping[str, ToolConcurrency],
    max_parallel: int,
    remaining_tool_steps: int,
) -> ToolBatchPlan[CallT]:
    """Split one turn's calls into what runs now, deterministically.

    ``concurrency`` maps each offered tool's id to what it declares.

    0. A ``run_ending`` call that is not the turn's single call is held back
       (``run_ending_tool``) wherever it stands and however often it repeats:
       run first, it would end the run before its siblings came back, and of
       two copies the second, a correction, would be lost. The rest is planned
       below, so the plan may run nothing.
    1. A ``solo_turn`` call splits the list. At the head it runs alone and the
       rest waits (``after_solo_turn_tool``); further in, the calls before it
       run and it (``solo_turn_tool``) and everything after it wait.
    2. What remains is cut to ``min(max_parallel, remaining_tool_steps)`` from
       the head; the rest is ``dropped``.
    3. The batch is ``parallel`` only when it holds two or more calls, every
       one ``parallel``, and no ``shared_state`` named twice. Otherwise it is
       ``sequential``.
    """

    def declared(call: CallT) -> ToolConcurrency:
        return concurrency.get(call.tool_id, _UNDECLARED)

    ending = [call for call in calls if declared(call).placement == "run_ending"]
    held_back: tuple[ExcludedToolCall[CallT], ...] = ()
    if ending and len(calls) > 1:
        held_back = _defer_all(ending, "run_ending_tool")
        calls = [call for call in calls if declared(call).placement != "run_ending"]
    runnable, deferred = _split_at_solo_turn_call(
        calls, [declared(call).placement == "solo_turn" for call in calls]
    )
    limit = max(min(max_parallel, remaining_tool_steps), 0)
    dropped = tuple(
        ExcludedToolCall(
            call=call,
            reason=(
                "tool_step_budget_exhausted"
                if index >= remaining_tool_steps
                else "max_parallel_exceeded"
            ),
        )
        for index, call in enumerate(runnable[limit:], start=limit)
    )
    accepted = runnable[:limit]
    return ToolBatchPlan(
        calls=accepted,
        mode=_batch_mode([declared(call) for call in accepted]),
        deferred=(*deferred, *held_back),
        dropped=dropped,
    )


def _split_at_solo_turn_call[CallT: ToolCallLike](
    calls: Sequence[CallT], solo: Sequence[bool]
) -> tuple[tuple[CallT, ...], tuple[ExcludedToolCall[CallT], ...]]:
    for index, call in enumerate(calls):
        if solo[index]:
            if index == 0:
                return (calls[0],), _defer_all(calls[1:], "after_solo_turn_tool")
            return tuple(calls[:index]), (
                ExcludedToolCall(call=call, reason="solo_turn_tool"),
                *_defer_all(calls[index + 1 :], "after_solo_turn_tool"),
            )
    return tuple(calls), ()


def _defer_all[CallT: ToolCallLike](
    calls: Sequence[CallT], reason: ExclusionReason
) -> tuple[ExcludedToolCall[CallT], ...]:
    return tuple(ExcludedToolCall(call=call, reason=reason) for call in calls)


def _batch_mode(declared: Sequence[ToolConcurrency]) -> BatchMode:
    # A single call takes the same path as an unbatched one.
    if len(declared) < 2:
        return "sequential"
    if any(item.placement != "parallel" for item in declared):
        return "sequential"
    shared = [item.shared_state for item in declared if item.shared_state is not None]
    return "sequential" if len(shared) != len(set(shared)) else "parallel"

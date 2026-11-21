"""One engine tick: fire every due hook whose gates are open."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Literal, Protocol

from .definitions import Every, HookDefinition
from .schedule import latest_occurrence, next_occurrence_after

logger = logging.getLogger(__name__)

# done: the occurrence is spent and the hook waits for its next one.
# retry: nothing is spent; the hook stays due and fires again next tick.
FireOutcome = Literal["done", "retry"]


@dataclass(frozen=True, slots=True)
class Fire:
    hook: HookDefinition
    occurrence: datetime
    now: datetime


class HookStateStore(Protocol):
    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        """Each hook that has fired: its next fire time, None once spent.

        A hook absent here has never fired, so its latest occurrence is due.
        """
        ...

    def write_next_fire_time(
        self, hook_id: str, next_fire_at: datetime | None
    ) -> None: ...


def run_hook_tick(
    hooks: Sequence[HookDefinition],
    *,
    now: datetime,
    local_zone: Callable[[], tzinfo],
    states: HookStateStore,
    gate_is_open: Callable[[str], bool],
    enqueue: Callable[[Fire], FireOutcome],
) -> None:
    """Fire each due hook at most once.

    The state is written after ``enqueue`` returns, so a crash in between fires
    the same occurrence again: ``enqueue`` MUST be idempotent per occurrence.
    """
    now = now.astimezone(UTC)
    next_fire_times = states.read_next_fire_times()
    for hook in hooks:
        try:
            # Resolved only when used: an unknown OS zone must not stop UTC hooks.
            zone = (
                local_zone()
                if isinstance(hook.trigger, Every) and hook.trigger.tz == "local"
                else UTC
            )
            first_fire = hook.id not in next_fire_times
            fire_at = (
                latest_occurrence(hook.trigger, now, zone)
                if first_fire
                else next_fire_times[hook.id]
            )
            if fire_at is None or fire_at > now:
                continue
            if not all(gate_is_open(gate) for gate in hook.requires):
                continue
            # An unaligned trigger has no grid: its occurrence is the due time,
            # pinned before a first fire so a repeat after a failed state write
            # stands for the same occurrence.
            if first_fire:
                states.write_next_fire_time(hook.id, fire_at)
            occurrence = (
                fire_at
                if isinstance(hook.trigger, Every) and not hook.trigger.align
                else latest_occurrence(hook.trigger, now, zone)
            )
            if enqueue(Fire(hook=hook, occurrence=occurrence, now=now)) == "retry":
                continue
            states.write_next_fire_time(
                hook.id, next_occurrence_after(hook.trigger, now, zone)
            )
        except Exception:
            # One hook's failure must not hold back the others; it stays due.
            logger.exception("hook fire failed: hook=%s", hook.id)

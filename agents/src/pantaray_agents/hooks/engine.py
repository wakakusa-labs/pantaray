"""One engine tick: fire every due hook whose gates are open."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Literal, Protocol

from .definitions import (
    EventValue,
    Every,
    FieldEquals,
    HookDefinition,
    NamedPredicate,
    On,
    RunJob,
)
from .schedule import latest_occurrence, next_occurrence_after

logger = logging.getLogger(__name__)

# done: the occurrence is spent and the hook waits for its next one.
# retry: nothing is spent; the hook stays due and fires again next tick.
FireOutcome = Literal["done", "retry"]


@dataclass(frozen=True, slots=True)
class InboxEvent:
    """One event waiting in a hook's inbox."""

    delivery_id: int
    name: str
    payload: Mapping[str, EventValue]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Fire:
    hook: HookDefinition
    occurrence: datetime
    now: datetime
    # An on hook's pending events, oldest first; empty for a time trigger.
    events: tuple[InboxEvent, ...] = ()


class HookStateStore(Protocol):
    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        """Each hook that has fired: its next fire time, None once spent.

        A hook absent here has never fired, so its latest occurrence is due.
        """
        ...

    def write_next_fire_time(
        self, hook_id: str, next_fire_at: datetime | None
    ) -> None: ...


class HookInbox(Protocol):
    def read(self, hook_id: str) -> Sequence[InboxEvent]:
        """The hook's pending events, oldest first."""
        ...

    def drop(self, hook_id: str, delivery_ids: Collection[int]) -> None:
        """Consume events that will never be handed to a run."""
        ...


def run_hook_tick(
    hooks: Sequence[HookDefinition],
    *,
    now: datetime,
    local_zone: Callable[[], tzinfo],
    states: HookStateStore,
    inbox: HookInbox,
    gate_is_open: Callable[[str], bool],
    predicate_holds: Callable[[str, InboxEvent], bool],
    run_is_active: Callable[[RunJob], bool],
    enqueue: Callable[[Fire], FireOutcome],
) -> None:
    """Fire each due hook at most once.

    A time trigger's state is written after ``enqueue`` returns, so a crash in
    between fires the same occurrence again: ``enqueue`` MUST be idempotent per
    occurrence. For an on trigger, ``enqueue`` MUST consume the events it hands
    to the run in the transaction that enqueues it; the events it leaves (to fit
    one run) stay pending for the next run.
    """
    now = now.astimezone(UTC)
    next_fire_times = states.read_next_fire_times()
    for hook in hooks:
        try:
            if isinstance(hook.trigger, On):
                _fire_on(
                    hook,
                    hook.trigger,
                    now=now,
                    inbox=inbox,
                    gate_is_open=gate_is_open,
                    predicate_holds=predicate_holds,
                    run_is_active=run_is_active,
                    enqueue=enqueue,
                )
                continue
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


def _fire_on(
    hook: HookDefinition,
    trigger: On,
    *,
    now: datetime,
    inbox: HookInbox,
    gate_is_open: Callable[[str], bool],
    predicate_holds: Callable[[str, InboxEvent], bool],
    run_is_active: Callable[[RunJob], bool],
    enqueue: Callable[[Fire], FireOutcome],
) -> None:
    delivery = trigger.delivery
    pending = inbox.read(hook.id)
    if not pending:
        return
    after = None if delivery.min_interval is None else delivery.min_interval.after
    anchors = [event for event in pending if event.name == after]
    held: list[InboxEvent] = []
    dropped_events = anchors[:-1]
    for event in pending:
        if event.name == after:
            continue
        match trigger.conditions.get(event.name):
            case FieldEquals(field=field, value=value):
                holds = event.payload.get(field) == value
            case NamedPredicate(name=name):
                holds = predicate_holds(name, event)
            case None:
                holds = True
        (held if holds else dropped_events).append(event)
    if delivery.collapse == "latest":
        dropped_events, held = dropped_events + held[:-1], held[-1:]
    if dropped_events:
        inbox.drop(hook.id, [event.delivery_id for event in dropped_events])
    if not held or not _is_due(trigger, held, now=now):
        return
    if delivery.min_interval is not None and anchors:
        if now < anchors[-1].occurred_at + delivery.min_interval.duration:
            return
    if not all(gate_is_open(gate) for gate in hook.requires):
        return
    if run_is_active(hook.run):
        return
    enqueue(Fire(hook=hook, occurrence=now, now=now, events=tuple(held)))


def _is_due(trigger: On, held: Sequence[InboxEvent], *, now: datetime) -> bool:
    delivery = trigger.delivery
    if any(event.name in delivery.immediate for event in held):
        return True
    oldest = min(event.occurred_at for event in held)
    return delivery.max_wait is not None and now - oldest >= delivery.max_wait

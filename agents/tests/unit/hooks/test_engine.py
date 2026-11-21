from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.hooks.definitions import (
    At,
    CalendarUnit,
    Collapse,
    Condition,
    Delivery,
    EventValue,
    Every,
    FieldEquals,
    HookDefinition,
    MinInterval,
    NamedPredicate,
    On,
    RunJob,
    load_hook_definitions,
)
from pantaray_agents.hooks.engine import (
    Fire,
    FireOutcome,
    InboxEvent,
    run_hook_tick,
)


class _Store:
    def __init__(self) -> None:
        self.next_fire_times: dict[str, datetime | None] = {}

    def read_next_fire_times(self) -> Mapping[str, datetime | None]:
        return dict(self.next_fire_times)

    def write_next_fire_time(self, hook_id: str, next_fire_at: datetime | None) -> None:
        self.next_fire_times[hook_id] = next_fire_at


def _hook(hook_id: str = "h", *, requires: tuple[str, ...] = ()) -> HookDefinition:
    return HookDefinition(
        id=hook_id,
        trigger=Every(
            every=timedelta(minutes=15), align=True, offset=timedelta(0), tz="utc"
        ),
        requires=requires,
        run=RunJob(job="j", args={}),
    )


def _utc(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 7, hour, minute, second, tzinfo=UTC)


def _no_local_zone() -> tzinfo:
    raise AssertionError("a UTC hook must not need the local zone")


class _Inbox:
    def __init__(self) -> None:
        self.events: dict[str, list[InboxEvent]] = {}
        self._next_id = 1

    def add(self, hook_id: str, name: str, at: datetime, **payload: EventValue) -> int:
        delivery_id = self._next_id
        self._next_id += 1
        self.events.setdefault(hook_id, []).append(
            InboxEvent(
                delivery_id=delivery_id, name=name, payload=payload, occurred_at=at
            )
        )
        return delivery_id

    def read(self, hook_id: str) -> Sequence[InboxEvent]:
        return tuple(self.events.get(hook_id, ()))

    def drop(self, hook_id: str, delivery_ids: Collection[int]) -> None:
        self.events[hook_id] = [
            event
            for event in self.events.get(hook_id, [])
            if event.delivery_id not in delivery_ids
        ]


class _Harness:
    def __init__(self, *hooks: HookDefinition, zone: tzinfo | None = None) -> None:
        self.zone = zone
        self.hooks = hooks
        self.store = _Store()
        self.inbox = _Inbox()
        self.fires: list[tuple[str, datetime]] = []
        self.handed: list[tuple[int, ...]] = []
        self.open_gates: set[str] = set()
        self.true_predicates: set[str] = set()
        self.active_runs: set[str] = set()
        self.outcome: FireOutcome | Literal["raise"] = "done"
        # How many of the handed events the run takes, oldest first.
        self.takes: int | None = None

    def _enqueue(self, fire: Fire) -> FireOutcome:
        self.fires.append((fire.hook.id, fire.occurrence))
        if isinstance(fire.hook.trigger, On):
            taken = fire.events[: self.takes]
            self.handed.append(tuple(event.delivery_id for event in taken))
            if self.outcome == "raise":
                raise sqlite3.OperationalError("disk I/O error")
            if self.outcome == "done":
                self.inbox.drop(
                    fire.hook.id,
                    [event.delivery_id for event in taken] + list(fire.superseded),
                )
        return "retry" if self.outcome == "raise" else self.outcome

    def tick(self, now: datetime) -> None:
        zone = self.zone
        run_hook_tick(
            self.hooks,
            now=now,
            local_zone=_no_local_zone if zone is None else lambda: zone,
            states=self.store,
            inbox=self.inbox,
            gate_is_open=lambda name: name in self.open_gates,
            predicate_holds=lambda name, _event: name in self.true_predicates,
            run_is_active=lambda hook: (
                isinstance(hook.run, RunJob) and hook.run.job in self.active_runs
            ),
            enqueue=self._enqueue,
        )


def test_each_occurrence_fires_once() -> None:
    # Catches a tick that refires inside an occurrence, or never fires again.
    harness = _Harness(_hook())

    for now in (_utc(10, 0, 10), _utc(10, 5), _utc(10, 14, 59), _utc(10, 15, 1)):
        harness.tick(now)

    assert harness.fires == [("h", _utc(10, 0)), ("h", _utc(10, 15))]


def test_a_sleep_over_several_occurrences_fires_the_latest_once() -> None:
    # Catches a catch-up that replays every missed window after a long sleep.
    harness = _Harness(_hook())
    harness.tick(_utc(10, 1))

    harness.tick(_utc(12, 40))
    harness.tick(_utc(12, 41))

    assert harness.fires == [("h", _utc(10, 0)), ("h", _utc(12, 30))]
    assert harness.store.next_fire_times == {"h": _utc(12, 45)}


def test_a_closed_gate_holds_the_hook_until_it_opens() -> None:
    # Catches a closed gate spending the occurrence (the hook would then skip
    # a whole window after the gate opens) or firing the stale window.
    harness = _Harness(_hook(requires=("ready",)))

    harness.tick(_utc(10, 1))
    harness.tick(_utc(10, 20))
    harness.open_gates.add("ready")
    harness.tick(_utc(10, 31))
    harness.tick(_utc(10, 32))

    assert harness.fires == [("h", _utc(10, 30))]


def test_retry_keeps_the_hook_due() -> None:
    # Catches "retry" treated as spent, which holds a waiting summary an hour.
    harness = _Harness(_hook())
    harness.outcome = "retry"
    harness.tick(_utc(10, 1))
    harness.outcome = "done"
    harness.tick(_utc(10, 2))
    harness.tick(_utc(10, 3))

    assert harness.fires == [("h", _utc(10, 0)), ("h", _utc(10, 0))]


def test_a_failing_hook_stays_due_and_does_not_stop_the_others() -> None:
    harness = _Harness(_hook("broken"), _hook("fine"))
    failed = False

    def enqueue(fire: Fire) -> FireOutcome:
        nonlocal failed
        if fire.hook.id == "broken" and not failed:
            failed = True
            raise RuntimeError("boom")
        harness.fires.append((fire.hook.id, fire.occurrence))
        return "done"

    harness._enqueue = enqueue  # type: ignore[method-assign]
    harness.tick(_utc(10, 1))
    harness.tick(_utc(10, 2))

    assert harness.fires == [("fine", _utc(10, 0)), ("broken", _utc(10, 0))]


def test_a_one_shot_hook_fires_once_when_its_time_comes() -> None:
    hook = HookDefinition(
        id="once", trigger=At(at=_utc(11, 0)), requires=(), run=RunJob("j", {})
    )
    harness = _Harness(hook)

    for now in (_utc(10, 0), _utc(11, 0), _utc(12, 0)):
        harness.tick(now)

    assert harness.fires == [("once", _utc(11, 0))]
    assert harness.store.next_fire_times == {"once": None}


def _local(every: timedelta | CalendarUnit, offset: timedelta) -> HookDefinition:
    trigger = Every(every=every, align=True, offset=offset, tz="local")
    return HookDefinition(id="h", trigger=trigger, requires=(), run=RunJob("j", {}))


@pytest.mark.parametrize(
    ("hook", "start", "expected"),
    [
        # Clocks set back: 02:00-02:45 local fire on their first pass only.
        (
            _local(timedelta(minutes=15), timedelta(0)),
            datetime(2026, 10, 25, 0, 10, tzinfo=UTC),
            [
                datetime(2026, 10, 25, h, m, tzinfo=UTC)
                for h in (0, 2)
                for m in (0, 15, 30, 45)
            ],
        ),
        # The same, starting without state during the repeated hour.
        (
            _local(timedelta(minutes=15), timedelta(0)),
            datetime(2026, 10, 25, 1, 1, tzinfo=UTC),
            [datetime(2026, 10, 25, 0, 45, tzinfo=UTC)]
            + [datetime(2026, 10, 25, 2, m, tzinfo=UTC) for m in (0, 15, 30, 45)],
        ),
        # Clocks set forward: the missing 02:30 fires once, at the jump.
        (
            _local("hour", timedelta(minutes=30)),
            datetime(2026, 3, 29, 0, 10, tzinfo=UTC),
            [
                datetime(2026, 3, 28, 23, 30, tzinfo=UTC),
                datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
                datetime(2026, 3, 29, 1, 0, tzinfo=UTC),
                datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
                datetime(2026, 3, 29, 2, 30, tzinfo=UTC),
            ],
        ),
    ],
)
def test_ticks_across_a_dst_change_fire_each_occurrence_once_on_time(
    hook: HookDefinition, start: datetime, expected: list[datetime]
) -> None:
    # Catches a stateless or caught-up hook refiring one occurrence every tick
    # after clocks are set back, and one fired early before clocks jump.
    harness = _Harness(hook, zone=ZoneInfo("Europe/Berlin"))
    now = start  # the first tick has no state, so it catches up
    fired_at: list[datetime] = []
    while now < start.replace(hour=2, minute=50):
        before = len(harness.fires)
        harness.tick(now)
        fired_at += [now] * (len(harness.fires) - before)
        now += timedelta(minutes=1)

    assert [occurrence for _hook, occurrence in harness.fires] == expected
    assert all(
        occurrence <= tick
        for (_hook, occurrence), tick in zip(harness.fires, fired_at, strict=True)
    )


def test_an_unaligned_hook_refires_the_same_occurrence_after_a_lost_state_write() -> (
    None
):
    # Catches an occurrence taken from the tick time: after the state write
    # fails, the repeat would carry a new occurrence and run the job twice.
    hook = HookDefinition(
        id="h",
        trigger=Every(
            every=timedelta(hours=2), align=False, offset=timedelta(0), tz="utc"
        ),
        requires=(),
        run=RunJob("j", {}),
    )
    harness = _Harness(hook)
    write = harness.store.write_next_fire_time
    writes = 0

    def write_failing_after_the_side_effect(
        hook_id: str, next_fire_at: datetime | None
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk full")
        write(hook_id, next_fire_at)

    harness.store.write_next_fire_time = write_failing_after_the_side_effect  # type: ignore[method-assign]
    harness.tick(_utc(10, 0, 7))
    harness.tick(_utc(10, 1, 3))
    harness.tick(_utc(10, 2, 0))

    assert harness.fires == [("h", _utc(10, 0, 7)), ("h", _utc(10, 0, 7))]
    assert len(set(harness.fires)) == 1  # an idempotent enqueue runs it once
    assert harness.store.next_fire_times == {"h": _utc(12, 1, 3)}


def _on_hook(
    *,
    events: tuple[str, ...] = ("insight", "turn"),
    immediate: frozenset[str] = frozenset({"insight"}),
    max_wait: timedelta | None = timedelta(minutes=15),
    collapse: Collapse = "all",
    conditions: Mapping[str, Condition] | None = None,
    min_interval: MinInterval | None = None,
    requires: tuple[str, ...] = (),
) -> HookDefinition:
    return HookDefinition(
        id="on",
        trigger=On(
            events=events,
            conditions={} if conditions is None else conditions,
            delivery=Delivery(
                immediate=immediate,
                max_wait=max_wait,
                collapse=collapse,
                min_interval=min_interval,
            ),
        ),
        requires=requires,
        run=RunJob(job="memory", args={}),
    )


def test_an_immediate_event_fires_at_once_and_a_ride_along_waits() -> None:
    # Catches a ride-along event starting a run of its own, one that never
    # starts without an immediate event, and a wait counted from the newest
    # event instead of the oldest.
    harness = _Harness(_on_hook())
    turn = harness.inbox.add("on", "turn", _utc(10, 0))
    later_turn = harness.inbox.add("on", "turn", _utc(10, 10))

    harness.tick(_utc(10, 14, 59))
    assert harness.handed == []
    harness.tick(_utc(10, 15))
    assert harness.handed == [(turn, later_turn)]

    insight = harness.inbox.add("on", "insight", _utc(10, 20))
    harness.tick(_utc(10, 20))
    assert harness.handed[-1] == (insight,)


def test_events_during_an_active_run_go_to_the_next_single_run() -> None:
    # Catches a second run started beside the active one, and events that
    # arrive meanwhile split over several runs or lost.
    harness = _Harness(_on_hook())
    harness.active_runs.add("memory")
    first = harness.inbox.add("on", "insight", _utc(10, 0))
    second = harness.inbox.add("on", "turn", _utc(10, 1))
    third = harness.inbox.add("on", "insight", _utc(10, 2))

    harness.tick(_utc(10, 3))
    assert harness.handed == []

    harness.active_runs.clear()
    harness.tick(_utc(10, 4))
    harness.tick(_utc(10, 5))
    assert harness.handed == [(first, second, third)]
    assert harness.inbox.read("on") == ()


def test_what_a_run_cannot_take_stays_for_the_next_run() -> None:
    # Catches events beyond one run's capacity consumed with the rest, and a
    # retried enqueue handing the same events twice or dropping them.
    harness = _Harness(_on_hook())
    ids = [harness.inbox.add("on", "insight", _utc(10, minute)) for minute in range(3)]
    harness.outcome = "retry"
    harness.tick(_utc(10, 5))
    harness.outcome = "done"
    harness.takes = 2
    harness.tick(_utc(10, 6))
    harness.tick(_utc(10, 7))

    assert harness.handed == [tuple(ids), tuple(ids[:2]), (ids[2],)]
    assert harness.inbox.read("on") == ()


def test_an_event_whose_condition_fails_is_consumed_without_a_run() -> None:
    # Catches a run started for an undone dismissal or a summary of another
    # kind, and such an event handed along with the others.
    harness = _Harness(
        _on_hook(
            conditions={
                "insight": FieldEquals(field="kind", value="24h"),
                "turn": NamedPredicate(name="still_dismissed"),
            }
        )
    )
    harness.inbox.add("on", "insight", _utc(10, 0), kind="1h")
    harness.inbox.add("on", "turn", _utc(9, 0))
    harness.tick(_utc(10, 1))
    assert harness.fires == []
    assert harness.inbox.read("on") == ()

    harness.true_predicates.add("still_dismissed")
    kept_turn = harness.inbox.add("on", "turn", _utc(10, 2))
    daily = harness.inbox.add("on", "insight", _utc(10, 3), kind="24h")
    harness.tick(_utc(10, 4))
    assert harness.handed == [(kept_turn, daily)]


def test_collapse_latest_keeps_an_older_immediate_event_due() -> None:
    # Catches collapsing before the due check: the ride-along that follows an
    # immediate event would take its place and make the run wait.
    harness = _Harness(_on_hook(collapse="latest"))
    harness.inbox.add("on", "insight", _utc(10, 0))
    turn = harness.inbox.add("on", "turn", _utc(10, 1))
    harness.tick(_utc(10, 2))

    assert harness.handed == [(turn,)]
    assert harness.inbox.read("on") == ()


@pytest.mark.parametrize("failure", ["retry", "raise"])
def test_a_failed_collapsed_fire_leaves_the_inbox_due(
    failure: FireOutcome | Literal["raise"],
) -> None:
    # Catches the older immediate event consumed apart from the enqueue: after a
    # retry or a failure the remaining ride-along would never be due again.
    harness = _Harness(_on_hook(collapse="latest"))
    harness.inbox.add("on", "insight", _utc(10, 0))
    turn = harness.inbox.add("on", "turn", _utc(10, 1))
    harness.outcome = failure
    harness.tick(_utc(10, 2))
    harness.outcome = "done"
    harness.tick(_utc(10, 3))

    assert harness.handed == [(turn,), (turn,)]
    assert harness.inbox.read("on") == ()


def test_collapse_latest_waits_from_the_oldest_ride_along() -> None:
    # Catches each newer ride-along restarting max_wait, so one arriving every
    # 10 minutes would never be delivered.
    harness = _Harness(_on_hook(collapse="latest"))
    for minute in (0, 10):
        harness.inbox.add("on", "turn", _utc(10, minute))
        harness.tick(_utc(10, minute))
    assert harness.handed == []
    newest = harness.inbox.add("on", "turn", _utc(10, 14))
    harness.tick(_utc(10, 15))

    assert harness.handed == [(newest,)]
    assert harness.inbox.read("on") == ()


def test_an_event_the_hook_no_longer_listens_to_is_dropped() -> None:
    # Catches events written under an earlier definition (a renamed event or
    # min_interval anchor) handed to a run that cannot read them.
    harness = _Harness(
        _on_hook(
            min_interval=MinInterval(after="started", duration=timedelta(minutes=15))
        )
    )
    harness.inbox.add("on", "renamed", _utc(10, 0))
    harness.inbox.add("on", "old_started", _utc(10, 0))
    insight = harness.inbox.add("on", "insight", _utc(10, 1))
    harness.tick(_utc(10, 2))

    assert harness.handed == [(insight,)]
    assert harness.inbox.read("on") == ()


def test_collapse_latest_hands_only_the_newest_event() -> None:
    harness = _Harness(_on_hook(collapse="latest"))
    harness.inbox.add("on", "insight", _utc(10, 0))
    newest = harness.inbox.add("on", "insight", _utc(10, 1))
    harness.active_runs.add("memory")
    harness.tick(_utc(10, 2))
    harness.active_runs.clear()
    harness.tick(_utc(10, 3))

    assert harness.handed == [(newest,)]
    assert harness.inbox.read("on") == ()


def test_min_interval_defers_the_run_instead_of_dropping_it() -> None:
    # Catches a run started too soon after the anchor event, and one dropped
    # when it is held back rather than started once the interval has passed.
    harness = _Harness(
        _on_hook(
            min_interval=MinInterval(after="started", duration=timedelta(minutes=15))
        )
    )
    harness.inbox.add("on", "started", _utc(9, 0))
    anchor = harness.inbox.add("on", "started", _utc(10, 0))
    insight = harness.inbox.add("on", "insight", _utc(10, 5))

    harness.tick(_utc(10, 14, 59))
    assert harness.handed == []
    harness.tick(_utc(10, 15))

    assert harness.handed == [(insight,)]
    assert [event.delivery_id for event in harness.inbox.read("on")] == [anchor]


def test_a_closed_gate_holds_the_inbox() -> None:
    harness = _Harness(_on_hook(requires=("owner",)))
    insight = harness.inbox.add("on", "insight", _utc(10, 0))
    harness.tick(_utc(10, 1))
    harness.open_gates.add("owner")
    harness.tick(_utc(10, 2))

    assert harness.handed == [(insight,)]


def test_a_hook_past_its_until_never_fires() -> None:
    # Catches until ignored for a time trigger, or for events waiting in the
    # inbox of an on trigger.
    until = _utc(10, 0)
    timed = HookDefinition(
        id="timed",
        trigger=Every(every="hour", align=True, offset=timedelta(0), tz="utc"),
        requires=(),
        run=RunJob(job="j", args={}),
        until=until,
    )
    heard = HookDefinition(
        id="heard",
        trigger=On(
            events=("a.done",),
            conditions={},
            delivery=Delivery(
                immediate=frozenset({"a.done"}),
                max_wait=None,
                collapse="latest",
                min_interval=None,
            ),
        ),
        requires=(),
        run=RunJob(job="k", args={}),
        until=until,
    )
    harness = _Harness(timed, heard)
    harness.inbox.add("heard", "a.done", _utc(9, 59))

    harness.tick(_utc(10, 0))

    assert harness.fires == []


def test_a_toml_hook_with_until_fires_up_to_it_and_never_after() -> None:
    # Catches a TOML definition with until refused at load (only the Markdown
    # loader took it), and a catch-up after a sleep that fires an occurrence
    # at or past the deadline.
    (hook,) = load_hook_definitions(
        """
        [[hooks]]
        id = "h"
        trigger = { every = "15m", align = true, tz = "utc" }
        until = 2026-09-07T10:30:00Z
        run = { job = "j" }
        """,
        gate_names=frozenset(),
        event_names=frozenset(),
        predicate_names=frozenset(),
        validate_run=lambda _run: None,
    )
    assert hook.until == _utc(10, 30)
    harness = _Harness(hook)

    harness.tick(_utc(10, 1))
    harness.tick(_utc(10, 29))  # After a sleep: the latest missed one, once.
    harness.tick(_utc(10, 30))
    harness.tick(_utc(11, 10))

    assert harness.fires == [("h", _utc(10, 0)), ("h", _utc(10, 15))]

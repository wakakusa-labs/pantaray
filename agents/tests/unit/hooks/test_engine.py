from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.hooks.definitions import (
    At,
    CalendarUnit,
    Every,
    HookDefinition,
    RunJob,
)
from pantaray_agents.hooks.engine import Fire, FireOutcome, run_hook_tick


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


class _Harness:
    def __init__(self, *hooks: HookDefinition, zone: tzinfo | None = None) -> None:
        self.zone = zone
        self.hooks = hooks
        self.store = _Store()
        self.fires: list[tuple[str, datetime]] = []
        self.open_gates: set[str] = set()
        self.outcome: FireOutcome = "done"

    def _enqueue(self, fire: Fire) -> FireOutcome:
        self.fires.append((fire.hook.id, fire.occurrence))
        return self.outcome

    def tick(self, now: datetime) -> None:
        zone = self.zone
        run_hook_tick(
            self.hooks,
            now=now,
            local_zone=_no_local_zone if zone is None else lambda: zone,
            states=self.store,
            gate_is_open=lambda name: name in self.open_gates,
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

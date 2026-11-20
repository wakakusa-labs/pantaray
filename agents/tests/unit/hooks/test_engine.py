from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo

from pantaray_agents.hooks.definitions import At, Every, HookDefinition, RunJob
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
    def __init__(self, *hooks: HookDefinition) -> None:
        self.hooks = hooks
        self.store = _Store()
        self.fires: list[tuple[str, datetime]] = []
        self.open_gates: set[str] = set()
        self.outcome: FireOutcome = "done"

    def _enqueue(self, fire: Fire) -> FireOutcome:
        self.fires.append((fire.hook.id, fire.occurrence))
        return self.outcome

    def tick(self, now: datetime) -> None:
        run_hook_tick(
            self.hooks,
            now=now,
            local_zone=_no_local_zone,
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

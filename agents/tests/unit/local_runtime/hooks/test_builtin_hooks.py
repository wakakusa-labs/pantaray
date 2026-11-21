from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.hooks.definitions import Every, On
from pantaray_agents.hooks.schedule import next_occurrence_after
from pantaray_agents.local_runtime.activity_summary_schedule import (
    ACTIVITY_SUMMARY_SCHEDULE_SPECS,
    latest_eligible_window,
    next_period_end,
)
from pantaray_agents.local_runtime.hooks import gates, jobs
from pantaray_agents.local_runtime.hooks.builtin import builtin_hooks
from pantaray_agents.local_runtime.hooks.runner import (
    SqliteHookStateStore,
    run_builtin_hooks_once,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    SHORT_INSIGHT_WINDOW_SECONDS,
)
from pantaray_agents.local_runtime.runtime.session_store import mark_configured
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from ..migrated_db import prepare_test_database


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_summary_hooks_fire_when_the_scheduler_they_replace_did() -> None:
    # Catches an offset that drifts from the summary's grace (the hook would
    # fire before its window is eligible and skip the period), a wrong unit, and
    # month or quarter boundaries that differ from the summary's own calendar.
    hooks = {hook.id: hook for hook in builtin_hooks()}
    instant = _utc(2026, 1, 1)
    while instant < _utc(2028, 1, 1):
        for spec in ACTIVITY_SUMMARY_SCHEDULE_SPECS:
            # What the scheduler wrote after taking the latest window at `instant`.
            window = latest_eligible_window(spec=spec, now=instant)
            replaced_next_run_after = next_period_end(
                window.period_end, spec.summary_type
            ) + timedelta(seconds=spec.grace_seconds)
            hook = hooks[f"builtin.summary_{spec.summary_type}"]
            assert next_occurrence_after(hook.trigger, instant, UTC) == (
                replaced_next_run_after
            ), (spec.summary_type, instant)
        instant += timedelta(hours=7, minutes=13)


def test_the_insight_hook_fires_on_the_insight_window() -> None:
    # Catches the hook drifting from the window the run identity aligns to: a
    # shorter period would refire into one window, a longer one skip windows.
    (insight,) = (hook for hook in builtin_hooks() if hook.id == "builtin.insight")
    assert isinstance(insight.trigger, Every)
    assert insight.trigger.every == timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS)
    assert insight.trigger.align


def test_the_suggestion_hook_waits_the_interval_its_job_holds_retries_to() -> None:
    # Catches the hook's interval edited alone: a first start and a retry would
    # then be spaced differently, and one of them more often than every window.
    (suggestion,) = (
        hook for hook in builtin_hooks() if hook.id == "builtin.suggestion"
    )
    assert isinstance(suggestion.trigger, On)
    min_interval = suggestion.trigger.delivery.min_interval
    assert min_interval is not None
    assert min_interval.duration == timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS)


class _Recorder:
    def __init__(self) -> None:
        self.recording = True
        self.insights: list[datetime] = []
        self.summaries: list[tuple[str, str]] = []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    recorder = _Recorder()

    async def recording_is_ready(**_kwargs: object) -> bool:
        return recorder.recording

    def enqueue_insight(*, moment: datetime, **_kwargs: object) -> None:
        recorder.insights.append(moment)

    def enqueue_summary(**kwargs: object) -> dict[str, str]:
        recorder.summaries.append(
            (str(kwargs["summary_type"]), str(kwargs["period_start"]))
        )
        return {"job_id": "j", "process_id": "p", "summary_id": "s"}

    monkeypatch.setattr(gates, "_recording_is_ready", recording_is_ready)
    monkeypatch.setattr(jobs, "enqueue_short_insight_job", enqueue_insight)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        enqueue_summary,
    )
    register_logged_out_owner("local-owner")
    yield recorder
    reset_logged_out_owner()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, 1_000, load_default_migrations())
    return path


def _tick(db_path: Path, now: datetime) -> None:
    run_builtin_hooks_once(
        db_path=db_path, busy_timeout_ms=1_000, user_id="local-owner", now=now
    )


def test_insight_fires_once_per_window_and_waits_for_recording(
    db_path: Path, recorder: _Recorder
) -> None:
    # Catches an Insight enqueued for an owner not yet settled, one enqueued
    # while recording is off, the window lost while it was off (the old
    # interval timer spent it), and a sleep replaying every missed window.
    _tick(db_path, _utc(2026, 9, 7, 10, 0, 30))
    assert recorder.insights == []

    mark_configured()
    for now in (_utc(2026, 9, 7, 10, 0, 30), _utc(2026, 9, 7, 10, 14, 59)):
        _tick(db_path, now)
    recorder.recording = False
    _tick(db_path, _utc(2026, 9, 7, 10, 16))
    recorder.recording = True
    for now in (_utc(2026, 9, 7, 12, 40), _utc(2026, 9, 7, 12, 41)):
        _tick(db_path, now)

    assert recorder.insights == [_utc(2026, 9, 7, 10, 0), _utc(2026, 9, 7, 12, 30)]
    states = SqliteHookStateStore(
        db_path=db_path, busy_timeout_ms=1_000, user_id="local-owner"
    )
    assert states.read_next_fire_times()["builtin.insight"] == _utc(2026, 9, 7, 12, 45)


def test_a_summary_backlog_after_a_sleep_drains_without_double_fires(
    db_path: Path, recorder: _Recorder
) -> None:
    # Catches the catch-up rule swallowing the hours missed during a sleep
    # (one fire would summarize only the oldest of them), and a window enqueued
    # twice once the backlog is drained.
    mark_configured()
    _tick(db_path, _utc(2026, 4, 15, 10, 35))
    for second in range(5):
        _tick(db_path, _utc(2026, 4, 15, 13, 20, second))

    assert [start for kind, start in recorder.summaries if kind == "1h"] == [
        "2026-04-15T09:00:00Z",
        "2026-04-15T10:00:00Z",
        "2026-04-15T11:00:00Z",
        "2026-04-15T12:00:00Z",
    ]
    assert [kind for kind, _start in recorder.summaries].count("24h") == 1
    states = SqliteHookStateStore(
        db_path=db_path, busy_timeout_ms=1_000, user_id="local-owner"
    )
    assert states.read_next_fire_times()["builtin.summary_1h"] == _utc(
        2026, 4, 15, 14, 5
    )

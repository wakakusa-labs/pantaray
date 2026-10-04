from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
)

from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    SCREEN_CAPTURE_REQUESTED_EVENT,
    ScreenCaptureBroker,
    ScreenCaptureRefused,
    announce_screen_capture_request,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError

TIMESTAMP = "2026-09-08T00:00:00Z"
CHILD_PROCESS_ID = "process:action-1:child"
APP_NAME = "Google Chrome"


def _announce(db_path: Path, *, process_id: str) -> None:
    announce_screen_capture_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        process_id=process_id,
        tool_request_id="action-1:step-1:1",
        capture_request_id="capture-1",
        app_name=APP_NAME,
    )


def _announced(db_path: Path) -> list[tuple[str, str]]:
    """Each announcement's stream, and the app Electron is asked to capture."""

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT process_id, payload_json FROM process_events WHERE event_name = ?",
            (SCREEN_CAPTURE_REQUESTED_EVENT,),
        ).fetchall()
    return [(str(row[0]), json.loads(row[1])["app_name"]) for row in rows]


def _insert_child_process(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, started_at,
                updated_at, heartbeat_at, next_event_seq, parent_process_id
            ) VALUES (?, 'user-1', 'action_subagent', 'running', 'action-1',
                      ?, ?, ?, 1, ?)
            """,
            (
                CHILD_PROCESS_ID,
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
                BROKER_ACTOR_PROCESS_ID,
            ),
        )


def test_a_subagent_request_is_announced_on_the_root_process_stream(
    tmp_path: Path,
) -> None:
    """Only the root Action stream reaches the desktop client."""

    db_path, _ = _bootstrap_runtime_db(tmp_path)
    _insert_child_process(db_path)

    _announce(db_path, process_id=CHILD_PROCESS_ID)

    assert _announced(db_path) == [(BROKER_ACTOR_PROCESS_ID, APP_NAME)]


def test_a_root_request_is_announced_on_its_own_stream(tmp_path: Path) -> None:
    db_path, _ = _bootstrap_runtime_db(tmp_path)

    _announce(db_path, process_id=BROKER_ACTOR_PROCESS_ID)

    assert _announced(db_path) == [(BROKER_ACTOR_PROCESS_ID, APP_NAME)]


def test_a_request_from_another_users_process_is_refused(tmp_path: Path) -> None:
    db_path, _ = _bootstrap_runtime_db(tmp_path)

    with pytest.raises(MigrationError):
        announce_screen_capture_request(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-2",
            action_id="action-1",
            process_id=BROKER_ACTOR_PROCESS_ID,
            tool_request_id="action-1:step-1:1",
            capture_request_id="capture-1",
            app_name=APP_NAME,
        )
    assert _announced(db_path) == []


@pytest.mark.asyncio
async def test_a_request_is_answered_once_and_then_forgotten() -> None:
    broker = ScreenCaptureBroker()
    capture_request_id = broker.open(user_id="user-1")
    refusal = ScreenCaptureRefused(code="CAPTURE_REFUSED_URL_UNAVAILABLE", details={})

    assert (
        broker.answer(
            capture_request_id=capture_request_id,
            user_id="user-1",
            outcome=refusal,
        )
        == "accepted"
    )
    assert (
        await broker.wait(capture_request_id=capture_request_id, timeout_seconds=1)
        is refusal
    )
    # The waiter took the request with it, so a late answer has nothing to resolve.
    assert (
        broker.answer(
            capture_request_id=capture_request_id,
            user_id="user-1",
            outcome=refusal,
        )
        == "unknown_request"
    )


@pytest.mark.asyncio
async def test_an_unanswered_request_times_out_and_is_dropped() -> None:
    broker = ScreenCaptureBroker()
    capture_request_id = broker.open(user_id="user-1")

    assert (
        await broker.wait(capture_request_id=capture_request_id, timeout_seconds=0.01)
        is None
    )
    with pytest.raises(KeyError):
        await broker.wait(capture_request_id=capture_request_id, timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_a_stopped_waiter_closes_its_request_before_electron_answers() -> None:
    """停止で待ち手が消えたら、遅れて届いた Electron の答えは受け取らない。"""

    import asyncio

    broker = ScreenCaptureBroker()
    capture_request_id = broker.open(user_id="user-1")
    waiter = asyncio.ensure_future(
        broker.wait(capture_request_id=capture_request_id, timeout_seconds=60)
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (
        broker.answer(
            capture_request_id=capture_request_id,
            user_id="user-1",
            outcome=ScreenCaptureRefused(
                code="CAPTURE_REFUSED_URL_UNAVAILABLE", details={}
            ),
        )
        == "unknown_request"
    )

from __future__ import annotations

import pytest
from tests.unit.local_runtime.broker_test_support import desktop_session

from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    ScreenCaptureOutcome,
    ScreenCaptureRefused,
    announce_screen_capture_request,
)
from pantaray_agents.schema.events import OutboundEvent

SUBAGENT_PROCESS_ID = "process:action-1:child"
APP_NAME = "Google Chrome"


async def _announce(*, user_id: str = "user-1") -> None:
    await announce_screen_capture_request(
        user_id=user_id,
        action_id="action-1",
        process_id=SUBAGENT_PROCESS_ID,
        tool_request_id="action-1:step-1:1",
        capture_request_id="capture-1",
        app_name=APP_NAME,
    )


async def test_a_request_reaches_the_owners_desktop_whichever_process_asked() -> None:
    """No session follows a chat-launched Action or a subagent, and both still ask."""

    with desktop_session() as desktop:
        await _announce()

    assert desktop.events == [OutboundEvent.SCREEN_CAPTURE_REQUESTED]
    assert desktop.requests == [
        {
            "kind": "action",
            "process_id": SUBAGENT_PROCESS_ID,
            "action_id": "action-1",
            "tool_request_id": "action-1:step-1:1",
            "capture_request_id": "capture-1",
            "app_name": APP_NAME,
        }
    ]


async def test_only_the_newest_session_that_can_send_is_asked() -> None:
    """Each delivery is one capture, so two sessions must not both be asked."""

    with desktop_session() as older, desktop_session() as newer:
        await _announce()
        assert (len(older.requests), len(newer.requests)) == (0, 1)

        # A session whose send failed can never send again.
        newer.open = False
        await _announce()
        assert (len(older.requests), len(newer.requests)) == (1, 1)


async def test_another_owners_session_is_not_asked() -> None:
    with desktop_session(owner_id="user-2") as other:
        await _announce(user_id="user-1")

    assert other.requests == []


@pytest.mark.asyncio
async def test_a_request_is_answered_once_and_then_forgotten() -> None:
    broker = OneShotRequests[ScreenCaptureOutcome]()
    capture_request_id = broker.open(user_id="user-1")
    refusal = ScreenCaptureRefused(code="CAPTURE_REFUSED_URL_UNAVAILABLE", details={})

    assert (
        broker.answer(
            request_id=capture_request_id,
            user_id="user-1",
            outcome=refusal,
        )
        == "accepted"
    )
    assert (
        await broker.wait(request_id=capture_request_id, timeout_seconds=1) is refusal
    )
    # The waiter took the request with it, so a late answer has nothing to resolve.
    assert (
        broker.answer(
            request_id=capture_request_id,
            user_id="user-1",
            outcome=refusal,
        )
        == "unknown_request"
    )


@pytest.mark.asyncio
async def test_an_unanswered_request_times_out_and_is_dropped() -> None:
    broker = OneShotRequests[ScreenCaptureOutcome]()
    capture_request_id = broker.open(user_id="user-1")

    assert (
        await broker.wait(request_id=capture_request_id, timeout_seconds=0.01) is None
    )
    with pytest.raises(KeyError):
        await broker.wait(request_id=capture_request_id, timeout_seconds=0.01)


@pytest.mark.asyncio
async def test_a_stopped_waiter_closes_its_request_before_electron_answers() -> None:
    """停止で待ち手が消えたら、遅れて届いた Electron の答えは受け取らない。"""

    import asyncio

    broker = OneShotRequests[ScreenCaptureOutcome]()
    capture_request_id = broker.open(user_id="user-1")
    waiter = asyncio.ensure_future(
        broker.wait(request_id=capture_request_id, timeout_seconds=60)
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (
        broker.answer(
            request_id=capture_request_id,
            user_id="user-1",
            outcome=ScreenCaptureRefused(
                code="CAPTURE_REFUSED_URL_UNAVAILABLE", details={}
            ),
        )
        == "unknown_request"
    )

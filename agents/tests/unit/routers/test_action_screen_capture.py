from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    ScreenCaptureBroker,
    ScreenCaptureCaptured,
    ScreenCaptureRefused,
)
from pantaray_agents.routers import action_screen_capture as router_module

STORAGE_PATH = "user-1/2026-09-08/6a6f1b2c-6e34-4d21-9a11-0f2c3d4e5f60.png"
SHA256 = "a" * 64


def _captured_body(capture_request_id: str) -> dict[str, object]:
    return {
        "capture_request_id": capture_request_id,
        "result": {
            "status": "captured",
            "storage_path": STORAGE_PATH,
            "mime_type": "image/png",
            "byte_size": 21,
            "sha256": SHA256,
            "width_px": 1440,
            "height_px": 900,
            "app_name": "Finder",
            "captured_at": "2026-09-08T00:00:01Z",
        },
    }


def _bind(monkeypatch: pytest.MonkeyPatch) -> ScreenCaptureBroker:
    broker = ScreenCaptureBroker()
    monkeypatch.setattr(router_module, "screen_capture_broker", lambda: broker)
    return broker


def _waiter(broker: ScreenCaptureBroker, capture_request_id: str):
    """Stand in for the tool that is blocked on this request."""

    return asyncio.ensure_future(
        broker.wait(capture_request_id=capture_request_id, timeout_seconds=5)
    )


async def _post(body: dict[str, object], *, user_id: str = "user-1"):
    return await router_module.answer_action_screen_capture(
        router_module.ActionScreenCaptureRequest.model_validate(body),
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_captured_answer_resolves_the_waiting_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    capture_request_id = broker.open(user_id="user-1")
    waiting = _waiter(broker, capture_request_id)

    response = await _post(_captured_body(capture_request_id))

    assert response.accepted is True
    outcome = await waiting
    assert isinstance(outcome, ScreenCaptureCaptured)
    assert outcome.storage_path == STORAGE_PATH


@pytest.mark.asyncio
async def test_refusal_answer_carries_its_code_and_axis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    capture_request_id = broker.open(user_id="user-1")
    waiting = _waiter(broker, capture_request_id)

    await _post(
        {
            "capture_request_id": capture_request_id,
            "result": {
                "status": "refused",
                "code": "CAPTURE_REFUSED_BY_PRIVACY_FILTER",
                "axis": "website",
                "host": "mail.example.com",
            },
        }
    )

    outcome = await waiting
    assert isinstance(outcome, ScreenCaptureRefused)
    assert outcome.code == "CAPTURE_REFUSED_BY_PRIVACY_FILTER"
    assert outcome.details == {"axis": "website", "host": "mail.example.com"}


@pytest.mark.asyncio
async def test_target_not_found_answer_carries_the_apps_that_can_be_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    capture_request_id = broker.open(user_id="user-1")
    waiting = _waiter(broker, capture_request_id)

    await _post(
        {
            "capture_request_id": capture_request_id,
            "result": {
                "status": "refused",
                "code": "CAPTURE_TARGET_NOT_FOUND",
                "available_apps": ["Notes", "Google Chrome"],
            },
        }
    )

    outcome = await waiting
    assert isinstance(outcome, ScreenCaptureRefused)
    assert outcome.code == "CAPTURE_TARGET_NOT_FOUND"
    assert outcome.details == {"available_apps": ["Notes", "Google Chrome"]}


@pytest.mark.asyncio
async def test_unknown_request_id_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await _post(_captured_body("no-such-request"))

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_another_users_token_cannot_answer_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    capture_request_id = broker.open(user_id="user-1")
    waiting = _waiter(broker, capture_request_id)

    with pytest.raises(HTTPException) as raised:
        await _post(_captured_body(capture_request_id), user_id="user-2")

    assert raised.value.status_code == 403
    assert not waiting.done()
    waiting.cancel()


@pytest.mark.asyncio
async def test_replayed_answer_conflicts_instead_of_replacing_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    capture_request_id = broker.open(user_id="user-1")
    waiting = _waiter(broker, capture_request_id)
    await _post(_captured_body(capture_request_id))

    replay = _captured_body(capture_request_id)
    assert isinstance(replay["result"], dict)
    replay["result"]["storage_path"] = STORAGE_PATH.replace("6a6f", "0000")

    with pytest.raises(HTTPException) as raised:
        await _post(replay)

    assert raised.value.status_code == 409
    outcome = await waiting
    assert isinstance(outcome, ScreenCaptureCaptured)
    assert outcome.storage_path == STORAGE_PATH


def test_unknown_refusal_code_is_rejected_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        router_module.ActionScreenCaptureRequest.model_validate(
            {
                "capture_request_id": "request-1",
                "result": {"status": "refused", "code": "SOMETHING_ELSE"},
            }
        )

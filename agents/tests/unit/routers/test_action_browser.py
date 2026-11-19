from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import BrowserAnswer
from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.routers import action_browser as router_module

RESULT = {
    "status": "ok",
    "page_data": {
        "url": "http://localhost:5173/",
        "title": "App",
        "snapshot": None,
        "console": None,
        "popups": [],
        "blocked": [],
    },
    "notes": [],
    "truncated": None,
    "network": [{"host": "localhost", "port": 5173}],
    "artifact": {"kind": "pdf", "byte_size": 2048, "sha256": "a" * 64},
    "opened_host": None,
}


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> OneShotRequests[BrowserAnswer]:
    broker = OneShotRequests[BrowserAnswer]()
    monkeypatch.setattr(router_module, "browser_operation_broker", lambda: broker)
    return broker


def _post(request_id: str, *, user_id: str = "user-1") -> int:
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: user_id
    response = TestClient(app).post(
        "/local/action-browser",
        json={"operation_request_id": request_id, "result": RESULT},
    )
    return response.status_code


@pytest.mark.asyncio
async def test_an_answer_reaches_the_waiting_operation_once(
    broker: OneShotRequests[BrowserAnswer],
) -> None:
    # Catches: the strict contract refusing a real JSON body, or a replayed POST
    # swapping a result the relay already verified.
    request_id = broker.open(user_id="user-1")
    waiting = asyncio.ensure_future(
        broker.wait(request_id=request_id, timeout_seconds=5)
    )

    # Electron deletes its temporary file on a 404.
    assert _post("no-such-request") == 404
    assert _post(request_id, user_id="user-2") == 403
    assert _post(request_id) == 200
    assert _post(request_id) == 409
    assert await waiting == BrowserOperationSucceeded.model_validate(RESULT)

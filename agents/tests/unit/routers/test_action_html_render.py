from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.html_render_relay import (
    BlockedRequest,
    HtmlRenderAnswer,
    HtmlRenderClaimed,
    HtmlRenderFailed,
)
from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.routers import action_html_render as router_module

SHA256 = "a" * 64
BLOCKED = {"reason": "outside_read_scope", "url": "file:///Users/me/secret.png"}
RENDERED = {
    "status": "rendered",
    "byte_size": 2048,
    "sha256": SHA256,
    "blocked_requests": [BLOCKED],
}


def _bind(monkeypatch: pytest.MonkeyPatch) -> OneShotRequests[HtmlRenderAnswer]:
    broker = OneShotRequests[HtmlRenderAnswer]()
    monkeypatch.setattr(router_module, "html_render_broker", lambda: broker)
    return broker


async def _post(
    render_request_id: str, result: dict[str, object], *, user_id: str = "user-1"
):
    return await router_module.answer_action_html_render(
        router_module.ActionHtmlRenderRequest.model_validate(
            {"render_request_id": render_request_id, "result": result}
        ),
        user_id=user_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            RENDERED,
            HtmlRenderClaimed(
                byte_size=2048,
                sha256=SHA256,
                blocked_requests=(
                    BlockedRequest(reason="outside_read_scope", url=BLOCKED["url"]),
                ),
            ),
        ),
        (
            {
                "status": "failed",
                "code": "HTML_PDF_TOO_LARGE",
                "message": "The PDF is over 100 MB.",
                "blocked_requests": [],
            },
            HtmlRenderFailed(
                code="HTML_PDF_TOO_LARGE",
                message="The PDF is over 100 MB.",
                blocked_requests=(),
            ),
        ),
    ],
    ids=["rendered", "failed"],
)
async def test_an_answer_resolves_the_waiting_request(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
    expected: HtmlRenderAnswer,
) -> None:
    broker = _bind(monkeypatch)
    render_request_id = broker.open(user_id="user-1")
    waiting = asyncio.ensure_future(
        broker.wait(request_id=render_request_id, timeout_seconds=5)
    )

    response = await _post(render_request_id, result)

    assert response.accepted is True
    assert await waiting == expected


@pytest.mark.asyncio
async def test_an_answer_nobody_waits_for_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Electron deletes its temporary PDF on this 404."""

    _bind(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        await _post("no-such-request", RENDERED)

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_another_users_token_cannot_answer_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _bind(monkeypatch)
    render_request_id = broker.open(user_id="user-1")

    with pytest.raises(HTTPException) as raised:
        await _post(render_request_id, RENDERED, user_id="user-2")

    assert raised.value.status_code == 403
    # The rightful answer still lands.
    assert (await _post(render_request_id, RENDERED)).accepted is True


@pytest.mark.parametrize(
    "result",
    [
        {"status": "failed", "code": "OTHER", "message": "", "blocked_requests": []},
        {"status": "rendered", "byte_size": 1, "sha256": SHA256},
        {**RENDERED, "blocked_requests": [BLOCKED] * 11},
        {**RENDERED, "path": "/tmp/elsewhere.pdf"},
    ],
    ids=["unknown_code", "no_blocked_requests", "too_many_blocked", "extra_field"],
)
def test_an_answer_outside_the_contract_is_rejected_at_the_boundary(
    result: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        router_module.ActionHtmlRenderRequest.model_validate(
            {"render_request_id": "request-1", "result": result}
        )

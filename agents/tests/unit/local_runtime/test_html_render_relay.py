from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    RecordingDesktopSession,
    desktop_session,
)

from pantaray_agents.local_runtime.runtime import html_render_relay as relay_module
from pantaray_agents.local_runtime.runtime.html_render_relay import (
    BlockedRequest,
    HtmlRenderAnswer,
    HtmlRenderClaimed,
    HtmlRendered,
    HtmlRenderFailed,
    HtmlRenderOutcome,
)
from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.schema.events import OutboundEvent

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()
BLOCKED = (BlockedRequest(reason="network_off", url="https://example.com/a.css"),)
CLAIMED = HtmlRenderClaimed(
    byte_size=len(PDF_BYTES), sha256=PDF_SHA256, blocked_requests=BLOCKED
)

type Runtime = tuple[RecordingDesktopSession, Path, OneShotRequests[HtmlRenderAnswer]]


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    broker = OneShotRequests[HtmlRenderAnswer]()
    monkeypatch.setattr(relay_module, "html_render_broker", lambda: broker)
    with desktop_session() as desktop:
        yield desktop, artifact_root, broker


async def _start_render(runtime: Runtime, data: bytes | None):
    """Start a render, then write the PDF where Electron would once it is asked."""

    desktop, artifact_root, _ = runtime
    render = asyncio.ensure_future(
        relay_module.request_html_render(
            user_id="user-1",
            action_id="action-1",
            process_id=BROKER_ACTOR_PROCESS_ID,
            tool_request_id="action-1:step-1:1",
            html_path=Path("/work/deck.html"),
            file_roots=[Path("/work")],
            private_roots=[Path("/app-data")],
            private_root_exceptions=[Path("/app-data/attachments")],
            allow_network=False,
        )
    )
    while not desktop.requests:
        await asyncio.sleep(0.005)
    render_request_id = str(desktop.requests[0]["render_request_id"])
    temp_pdf = artifact_root / ".tmp" / "html-renders" / f"{render_request_id}.pdf"
    if data is not None:
        temp_pdf.parent.mkdir(parents=True, exist_ok=True)
        temp_pdf.write_bytes(data)
    return render, render_request_id, temp_pdf


async def _render(
    runtime: Runtime, *, data: bytes | None, answer: HtmlRenderAnswer | None
) -> HtmlRenderOutcome:
    render, render_request_id, temp_pdf = await _start_render(runtime, data)
    if answer is not None:
        assert (
            runtime[2].answer(
                request_id=render_request_id, user_id="user-1", outcome=answer
            )
            == "accepted"
        )
    outcome = await render
    assert not temp_pdf.exists()
    return outcome


@pytest.mark.asyncio
async def test_a_rendered_pdf_is_verified_returned_and_removed(
    runtime: Runtime,
) -> None:
    outcome = await _render(runtime, data=PDF_BYTES, answer=CLAIMED)

    assert outcome == HtmlRendered(pdf=PDF_BYTES, blocked_requests=BLOCKED)
    assert runtime[0].events == [OutboundEvent.HTML_RENDER_REQUESTED]
    [payload] = runtime[0].requests
    assert payload == {
        "kind": "action",
        "action_id": "action-1",
        "process_id": BROKER_ACTOR_PROCESS_ID,
        "tool_request_id": "action-1:step-1:1",
        "render_request_id": payload["render_request_id"],
        "html_path": "/work/deck.html",
        "file_roots": ["/work"],
        "private_roots": ["/app-data"],
        "private_root_exceptions": ["/app-data/attachments"],
        "allow_network": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "claimed"),
    [
        (PDF_BYTES, HtmlRenderClaimed(len(PDF_BYTES), "0" * 64, BLOCKED)),
        (PDF_BYTES, HtmlRenderClaimed(len(PDF_BYTES) + 1, PDF_SHA256, BLOCKED)),
        (None, CLAIMED),
    ],
    ids=["sha256_differs", "byte_size_differs", "no_file"],
)
async def test_a_pdf_that_does_not_match_the_claim_fails_integrity(
    runtime: Runtime, data: bytes | None, claimed: HtmlRenderClaimed
) -> None:
    outcome = await _render(runtime, data=data, answer=claimed)

    assert isinstance(outcome, HtmlRenderFailed)
    assert outcome.code == "HTML_RENDER_INTEGRITY_FAILED"
    assert outcome.blocked_requests == BLOCKED


@pytest.mark.asyncio
async def test_an_electron_failure_is_returned_as_answered(runtime: Runtime) -> None:
    failure = HtmlRenderFailed(
        code="HTML_RENDERER_CRASHED",
        message="The page's renderer process exited.",
        blocked_requests=BLOCKED,
    )

    # A partial file Electron left behind is removed all the same.
    assert await _render(runtime, data=b"%PDF-", answer=failure) == failure


@pytest.mark.asyncio
async def test_an_unanswered_render_times_out_and_drops_a_late_file(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_module, "HTML_RENDER_TIMEOUT_SECONDS", 0.05)

    outcome = await _render(runtime, data=PDF_BYTES, answer=None)

    assert isinstance(outcome, HtmlRenderFailed)
    assert outcome.code == "HTML_RENDER_RELAY_TIMED_OUT"
    # The request is closed, so Electron's late answer is told nobody waits.
    render_request_id = str(runtime[0].requests[0]["render_request_id"])
    late = runtime[2].answer(
        request_id=render_request_id, user_id="user-1", outcome=CLAIMED
    )
    assert late == "unknown_request"


@pytest.mark.asyncio
async def test_a_stopped_render_still_removes_the_file(runtime: Runtime) -> None:
    render, _, temp_pdf = await _start_render(runtime, PDF_BYTES)

    render.cancel()
    with pytest.raises(asyncio.CancelledError):
        await render

    assert not temp_pdf.exists()

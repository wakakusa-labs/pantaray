"""`render_html_page`: an HTML file printed by the desktop app, then drawn.

Electron is played by a task that waits for the announcement, writes a real
PDF where Electron would and answers through the relay's broker; the pages are
then drawn by the real renderer. What is checked is what the broker owns: which
file it hands over, what the page may load, how the pages come back, and how
the relay's failures reach the model.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.tools import (
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
)
from pantaray_agents.local_runtime.runtime import html_render_relay as relay_module
from pantaray_agents.local_runtime.runtime.html_render_relay import (
    HTML_RENDER_TEMP_DIRECTORY,
    BlockedRequest,
    HtmlRenderClaimed,
    HtmlRenderFailed,
    html_render_broker,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderHtmlPageOutput,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    _CHILD_BROKER_TOOLS,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    RecordingDesktopSession,
    _bootstrap_runtime_db,
    desktop_session,
)
from .test_pdf_page_render import write_pdf

_USER_ID = "user-1"
_TOOL_ID = "render_html_page"
_BLOCKED = (BlockedRequest(reason="network_off", url="https://cdn.example/a.css"),)

type Runtime = tuple[Path, Path, object, RecordingDesktopSession]


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[Runtime]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    read_access_scope = getattr(request, "param", "workspace")
    # save_html_as_pdf prints through the same relay and shares this runtime.
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope=read_access_scope,
        allowed_tool_ids=(_TOOL_ID, "save_html_as_pdf"),
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    with desktop_session(_USER_ID) as desktop:
        yield db_path, artifact_root, context, desktop


def _workspace(runtime: Runtime) -> Path:
    return Path(runtime[2].workspace_path)  # type: ignore[attr-defined]


async def _render(runtime: Runtime, args: dict) -> object:
    db_path, _, context, _ = runtime
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id=_TOOL_ID,
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        tool_request_id="action-1:step-1:1",
        args=args,
    )


def _announced(runtime: Runtime) -> list[dict[str, object]]:
    """What the desktop app has been asked to render."""

    return runtime[3].requests


async def _electron(
    runtime: Runtime, *, page_count: int = 0, failure: HtmlRenderFailed | None = None
) -> dict[str, object]:
    """Answer the one announced render as Electron main would."""

    artifact_root = runtime[1]
    while not _announced(runtime):
        await asyncio.sleep(0.005)
    [payload] = _announced(runtime)
    render_request_id = str(payload["render_request_id"])
    if failure is None:
        pdf_path = (
            artifact_root / HTML_RENDER_TEMP_DIRECTORY / f"{render_request_id}.pdf"
        )
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(pdf_path, marks=[(0, 0, 0)] * page_count)
        pdf = pdf_path.read_bytes()
        answer: HtmlRenderClaimed | HtmlRenderFailed = HtmlRenderClaimed(
            byte_size=len(pdf),
            sha256=hashlib.sha256(pdf).hexdigest(),
            blocked_requests=_BLOCKED,
        )
    else:
        answer = failure
    status = html_render_broker().answer(
        request_id=render_request_id, user_id=_USER_ID, outcome=answer
    )
    assert status == "accepted"
    return payload


@pytest.mark.parametrize(
    ("pages", "drawn", "past_the_end"),
    [
        pytest.param([3, 1, 5], [3, 1], "5", id="asked-with-one-past-the-end"),
        pytest.param(None, [1, 2, 3], None, id="first-pages-by-default"),
    ],
)
async def test_render_prints_in_electron_and_draws_the_pages_it_has(
    runtime: Runtime, pages: list[int] | None, drawn: list[int], past_the_end: str
) -> None:
    db_path, artifact_root, _, _ = runtime
    html = _workspace(runtime) / "deck.html"
    html.write_text("<h1>Deck</h1>")
    args: dict[str, object] = {"path": "deck.html"}
    if pages is not None:
        args["pages"] = pages

    outcome, payload = await asyncio.gather(
        _render(runtime, args), _electron(runtime, page_count=3)
    )

    # Electron is handed the authorized file and what read itself may open:
    # the workspace that holds the page, Pantaray's storage around it refused
    # except the Action's own roots, and the network by the user's setting.
    assert payload["html_path"] == str(html)
    assert payload["tool_request_id"] == "action-1:step-1:1"
    file_roots = [Path(root) for root in payload["file_roots"]]  # type: ignore[union-attr]
    assert any(html.is_relative_to(root) for root in file_roots)
    assert Path(payload["private_roots"][0]) == db_path.parent  # type: ignore[index]
    assert str(_workspace(runtime)) in payload["private_root_exceptions"]  # type: ignore[operator]
    assert payload["allow_network"] is True

    assert outcome.status == "success"  # type: ignore[attr-defined]
    parsed = RenderHtmlPageOutput.model_validate(outcome.output)  # type: ignore[attr-defined]
    assert parsed.page_count == 3
    assert [page.page_number for page in parsed.attachments] == drawn
    assert len(outcome.attachments) == len(drawn)  # type: ignore[attr-defined]
    assert [(b.reason, b.url) for b in parsed.blocked_requests] == [
        ("network_off", "https://cdn.example/a.css")
    ]
    for page in parsed.attachments:
        assert page.ref in parsed.message
    if past_the_end is None:
        assert "past the end" not in parsed.message
    else:
        assert f"past the end, not drawn: {past_the_end}." in parsed.message
    # Neither Electron's PDF nor the copy that was drawn is left behind.
    assert not list((artifact_root / HTML_RENDER_TEMP_DIRECTORY).iterdir())
    assert not list(db_path.parent.rglob("*.pdf"))


@pytest.mark.parametrize("runtime", ["full_access"], indirect=True)
async def test_full_access_opens_every_folder_and_the_network_setting_is_followed(
    runtime: Runtime,
) -> None:
    db_path = runtime[0]
    update_command_network_enabled(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        command_network_enabled=False,
        now="2026-03-23T00:00:01Z",
    )
    (_workspace(runtime) / "deck.htm").write_text("<p>quote</p>")

    _, payload = await asyncio.gather(
        _render(runtime, {"path": "deck.htm", "pages": [1]}),
        _electron(runtime, page_count=1),
    )

    assert payload["file_roots"] == ["/"]
    assert payload["allow_network"] is False


async def test_electron_failure_reaches_the_model_with_its_code_and_requests(
    runtime: Runtime,
) -> None:
    (_workspace(runtime) / "deck.html").write_text("<link href='/x.css'>")
    failure = HtmlRenderFailed(
        code="HTML_LOAD_FAILED",
        message="The page did not load.",
        blocked_requests=(
            BlockedRequest(reason="outside_read_scope", url="file:///etc/x.css"),
        ),
    )

    results = await asyncio.gather(
        _render(runtime, {"path": "deck.html"}),
        _electron(runtime, failure=failure),
        return_exceptions=True,
    )

    error = results[0]
    assert isinstance(error, BrokerPolicyError)
    assert error.code == "HTML_LOAD_FAILED"
    assert "The page did not load." in str(error)
    assert "file:///etc/x.css" in str(error)
    assert error.fix_hint


async def test_an_unanswered_render_times_out_as_a_tool_error(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_module, "HTML_RENDER_TIMEOUT_SECONDS", 0.05)
    (_workspace(runtime) / "deck.html").write_text("<p>slow</p>")

    with pytest.raises(BrokerPolicyError) as caught:
        await _render(runtime, {"path": "deck.html"})

    assert caught.value.code == "HTML_RENDER_RELAY_TIMED_OUT"


@pytest.mark.parametrize("runtime", ["full_access"], indirect=True)
@pytest.mark.parametrize("subject", ["private-storage", "not-html", "linked-text"])
async def test_render_refuses_before_electron_is_asked(
    runtime: Runtime, tmp_path: Path, subject: str
) -> None:
    db_path = runtime[0]
    if subject == "private-storage":
        target = db_path.parent / "secret.html"
        target.write_text("<p>app data</p>")
        requested, expected = str(target), "READ_PATH_DENIED"
    elif subject == "not-html":
        (_workspace(runtime) / "notes.txt").write_text("plain")
        requested, expected = "notes.txt", "RENDER_FORMAT_UNSUPPORTED"
    else:
        (tmp_path / "notes.txt").write_text("plain")
        (_workspace(runtime) / "deck.html").symlink_to(tmp_path / "notes.txt")
        requested, expected = "deck.html", "RENDER_FORMAT_UNSUPPORTED"

    with pytest.raises(BrokerPolicyError) as caught:
        await _render(runtime, {"path": requested})

    assert caught.value.code == expected
    assert _announced(runtime) == []


def test_render_is_offered_to_the_root_action_alone() -> None:
    """Electron answers only the root Action's stream, and a subagent sees no image."""

    assert _TOOL_ID in SUPERVISOR_SINGLE_REACT_TOOL_IDS
    assert _TOOL_ID not in {tool.tool_id for tool in _CHILD_BROKER_TOOLS}

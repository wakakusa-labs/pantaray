"""The make stage prints HTML in a browser session of its own, offline, and closes it."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime import browser_relay as relay_module
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationFailed,
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BROWSER_TEMP_DIRECTORY,
    BrowserAnswer,
    browser_operation_broker,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.html_pages import (
    BLOCKED_REQUESTS_NOTE,
)
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
)

from .broker_test_support import RecordingDesktopSession, desktop_session
from .test_pdf_page_render import write_pdf
from .test_suggestion_commands import suggestion_db as suggestion_db  # the fixture
from .test_suggestion_make_tools import _tools
from .test_suggestion_research_tools import BUSY_TIMEOUT_MS
from .test_workspace_settings_repository import TIMESTAMP

PROCESS_ID = "process-sug-1"
OPERATION = OutboundEvent.BROWSER_OPERATION_REQUESTED
CLOSE = OutboundEvent.BROWSER_SESSION_CLOSE_REQUESTED
BLOCKED = {"reason": "network_off", "url": "https://fonts.example.com/a.css"}


def _call(name: str, args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(name, None, args),  # type: ignore[arg-type]
        call_id="c1",
    )


def _ok(
    *, blocked: list[dict[str, str]], artifact: dict[str, object] | None
) -> BrowserOperationSucceeded:
    return BrowserOperationSucceeded.model_validate(
        {
            "status": "ok",
            "page_data": {
                "url": "file:///deck.html",
                "title": "Deck",
                "snapshot": None,
                "console": None,
                "popups": [],
                "blocked": blocked,
            },
            "notes": [],
            "truncated": None,
            "network": [],
            "artifact": artifact,
            "opened_host": None,
        }
    )


def _failed(code: str) -> BrowserOperationFailed:
    return BrowserOperationFailed.model_validate(
        {"status": "failed", "code": code, "message": "x", "network": []}
    )


# The print answer whose PDF does not match its claim.
_MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class Electron:
    """Electron main answering browser operations as the responder would."""

    desktop: RecordingDesktopSession
    artifact_root: Path

    def sent(self) -> list[tuple[str, dict[str, object]]]:
        return list(zip(self.desktop.events, self.desktop.requests, strict=True))

    async def serve(
        self,
        *,
        blocked: Mapping[str, list[dict[str, str]]] | None = None,
        answers: Mapping[str, BrowserAnswer | str | None] | None = None,
        sweep_db: Path | None = None,
    ) -> None:
        """Answer every operation until cancelled. ``answers`` names what an
        action gets instead of success; None leaves it unanswered."""

        answered = 0
        while True:
            operations = [
                request for event, request in self.sent() if event == OPERATION
            ]
            for request in operations[answered:]:
                answered += 1
                action = str(request["operation"]["action"])  # type: ignore[index]
                if sweep_db is not None:
                    # The sweep runs while the call holds its session.
                    await relay_module.sweep_browser_sessions(
                        db_path=sweep_db, busy_timeout_ms=BUSY_TIMEOUT_MS
                    )
                answer = (answers or {}).get(action, "ok")
                if answer is None:
                    continue
                if isinstance(answer, str):
                    answer = self._success(
                        request, (blocked or {}).get(action, []), answer == _MISMATCH
                    )
                assert (
                    browser_operation_broker().answer(
                        request_id=str(request["operation_request_id"]),
                        user_id="user-1",
                        outcome=answer,
                    )
                    == "accepted"
                )
            await asyncio.sleep(0.005)

    def _success(
        self, request: dict[str, object], blocked: list[dict[str, str]], mismatch: bool
    ) -> BrowserOperationSucceeded:
        if request["operation"] != {"action": "print"}:
            return _ok(blocked=blocked, artifact=None)
        pdf_path = (
            self.artifact_root
            / BROWSER_TEMP_DIRECTORY
            / f"{request['operation_request_id']}.pdf"
        )
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(pdf_path, marks=[(0, 0, 0)] * 2)
        pdf = pdf_path.read_bytes()
        digest = "0" * 64 if mismatch else hashlib.sha256(pdf).hexdigest()
        claim = {"kind": "pdf", "byte_size": len(pdf), "sha256": digest}
        return _ok(blocked=blocked, artifact=claim)


@pytest.fixture
def make_db(
    suggestion_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The Suggestion's run, live as while its job runs, and a desktop app."""

    artifact_root = (tmp_path / "relay").resolve()
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("LOCAL_DB_PATH", str(suggestion_db))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    with sqlite3.connect(suggestion_db) as connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, suggestion_id, started_at,
                updated_at, heartbeat_at, next_event_seq
            ) VALUES (?, 'user-1', 'suggestion', 'running', 'sug-1', ?, ?, ?, 1)
            """,
            (PROCESS_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
    return suggestion_db


async def _registry(db_path: Path) -> tuple[ReactToolRegistry, Path, str]:
    make = await _tools(db_path).build_make_tools(user_id="user-1", run_id="sug-1")
    (make.folder / "deck.html").write_text("<h1>Deck</h1>")
    return (
        ReactToolRegistry(make.definitions),
        make.folder,
        str(make.folder / "deck.html"),
    )


async def _run(
    registry: ReactToolRegistry,
    call: ReactToolCall,
    **serve: object,
) -> tuple[ReactToolResult, list[tuple[str, dict[str, object]]]]:
    with desktop_session() as desktop:
        electron = Electron(desktop, Path(os.environ["LOCAL_ARTIFACT_ROOT"]))
        serving = asyncio.ensure_future(electron.serve(**serve))  # type: ignore[arg-type]
        try:
            result = await registry.execute(call, 1)
        finally:
            serving.cancel()
        return result, electron.sent()


def _assert_one_session(
    sent: list[tuple[str, dict[str, object]]], *actions: str, closed: bool = True
) -> None:
    """The call ran ``actions`` in one session, then closed exactly that one."""

    assert [event for event, _ in sent] == [OPERATION] * len(actions) + (
        [CLOSE] if closed else []
    )
    session_ids = {request["session_id"] for _, request in sent}
    assert len(session_ids) == 1
    operations = sent[: len(actions)]
    assert [request["operation"]["action"] for _, request in operations] == list(  # type: ignore[index]
        actions
    )


@pytest.mark.asyncio
async def test_html_is_printed_offline_in_a_session_of_its_own(make_db: Path) -> None:
    # Commands may use the network; the page never does.
    update_command_network_enabled(
        db_path=make_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        command_network_enabled=True,
        now=TIMESTAMP,
    )
    registry, folder, page = await _registry(make_db)

    saved, save_sent = await _run(
        registry,
        _call("save_html_as_pdf", {"path": page, "pdf_path": "deck.pdf"}),
        sweep_db=make_db,
    )
    rendered, render_sent = await _run(
        registry,
        _call("render_html_page", {"path": page, "pages": None}),
        blocked={"navigate": [BLOCKED]},
    )

    # The outputs are the ones the print path has always given.
    assert saved.output == {
        "status": "saved",
        "pdf_path": "deck.pdf",
        "blocked_requests": [],
        "message": f"Saved {page} as deck.pdf.",
    }
    assert (folder / "deck.pdf").read_bytes().startswith(b"%PDF-")
    refs = " ".join(
        f"Page {number}: {image.ref}" for number, image in enumerate(rendered.images, 1)
    )
    assert rendered.output == {
        "kind": "html_pages",
        "path": page,
        "page_count": 2,
        "blocked_requests": [BLOCKED],
        "message": f"Printed {page} to 2 pages. {BLOCKED_REQUESTS_NOTE} {refs}",
    }
    assert len(rendered.images) == 2
    for sent in (save_sent, render_sent):
        # A sweep while the call prints leaves its session open: the run is live.
        _assert_one_session(sent, "navigate", "print")
        navigate = sent[0][1]
        assert navigate["operation"] == {
            "action": "navigate",
            "target": {"kind": "file", "path": str(Path(page).resolve())},
        }
        policy = navigate["policy"]
        assert (policy["allow_network"], policy["write_hosts"]) == (False, [])  # type: ignore[index]
    assert save_sent[0][1]["session_id"] != render_sent[0][1]["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "actions", "code", "closed"),
    [
        (
            {"navigate": _failed("BROWSER_NAVIGATION_FAILED")},
            ("navigate",),
            "BROWSER_NAVIGATION_FAILED",
            True,
        ),
        (
            {"navigate": _failed("BROWSER_TIMED_OUT")},
            ("navigate",),
            "BROWSER_TIMED_OUT",
            True,
        ),
        (
            {"print": _failed("BROWSER_ACTION_FAILED")},
            ("navigate", "print"),
            "BROWSER_ACTION_FAILED",
            True,
        ),
        (
            {"print": _failed("BROWSER_RENDERER_CRASHED")},
            ("navigate", "print"),
            "BROWSER_RENDERER_CRASHED",
            # Electron discarded the crashed session; there is nothing to close.
            False,
        ),
        (
            {"print": _MISMATCH},
            ("navigate", "print"),
            "BROWSER_INTEGRITY_FAILED",
            True,
        ),
        ({"navigate": None}, ("navigate",), "BROWSER_RELAY_TIMED_OUT", True),
    ],
)
async def test_a_failed_print_keeps_its_error_and_closes_the_session(
    make_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, BrowserAnswer | str | None],
    actions: tuple[str, ...],
    code: str,
    closed: bool,
) -> None:
    monkeypatch.setattr(relay_module, "BROWSER_RELAY_TIMEOUT_SECONDS", 0.05)
    registry, folder, page = await _registry(make_db)

    result, sent = await _run(
        registry,
        _call("save_html_as_pdf", {"path": page, "pdf_path": "deck.pdf"}),
        answers=answers,
    )

    assert result.status == "error"
    assert result.output["error_code"] == code  # type: ignore[index]
    assert str(result.output["message"]).startswith(f"Could not print {page}: ")  # type: ignore[index]
    assert result.output["details"]["fix_hint"]  # type: ignore[index]
    _assert_one_session(sent, *actions, closed=closed)
    assert not (folder / "deck.pdf").exists()


@pytest.mark.asyncio
async def test_a_stopped_call_closes_its_session(make_db: Path) -> None:
    registry, _, page = await _registry(make_db)

    with desktop_session() as desktop:
        call = asyncio.ensure_future(
            registry.execute(
                _call("render_html_page", {"path": page, "pages": None}), 1
            )
        )
        while not desktop.requests:
            await asyncio.sleep(0.005)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    _assert_one_session(
        list(zip(desktop.events, desktop.requests, strict=True)), "navigate"
    )


@pytest.mark.asyncio
async def test_a_pdf_is_never_saved_outside_the_folder(
    make_db: Path, tmp_path: Path
) -> None:
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    registry, folder, page = await _registry(make_db)
    (folder / "link").symlink_to(outside, target_is_directory=True)

    with desktop_session() as desktop:
        refused = [
            await registry.execute(
                _call("save_html_as_pdf", {"path": page, "pdf_path": pdf_path}), 1
            )
            for pdf_path in (str(outside / "a.pdf"), "../outside/a.pdf")
        ]
    # Paths outside are refused before printing.
    assert desktop.requests == []
    # A link inside the folder is refused when the PDF is written.
    linked, sent = await _run(
        registry, _call("save_html_as_pdf", {"path": page, "pdf_path": "link/a.pdf"})
    )

    assert [result.status for result in (*refused, linked)] == ["error"] * 3
    _assert_one_session(sent, "navigate", "print")
    assert list(outside.iterdir()) == []

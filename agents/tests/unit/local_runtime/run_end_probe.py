"""What a run's end reached: whose browser sessions closed and processes stopped.

The browser relay runs for real behind a fake desktop app, which answers every
operation at once and records every close. The process stop is a recorder that
also asks for a browser operation for each owner while it stops, which a run
whose end has begun must already refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime import browser_relay as relay_module
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BrowserOperationDone,
    BrowserOperationOutcome,
    BrowserRelayFailed,
)
from pantaray_agents.local_runtime.runtime.deliverable_sessions import (
    DesktopRequestMessage,
)
from pantaray_agents.local_runtime.tooling.resources import run_end as run_end_module
from pantaray_agents.schema.websocket.server_messages import (
    BrowserNavigateOperation,
    BrowserOperationRequestedMessage,
    BrowserPagePolicy,
    BrowserSessionCloseRequestedMessage,
    BrowserWebTarget,
)

_NAVIGATE = BrowserNavigateOperation(
    action="navigate",
    target=BrowserWebTarget(kind="web", url="http://localhost:5173/"),
)
_POLICY = BrowserPagePolicy(
    allow_network=False,
    file_roots=[],
    private_roots=[],
    private_root_exceptions=[],
    secret_paths=[],
    write_hosts=[],
)
_NAVIGATED = BrowserOperationSucceeded.model_validate(
    {
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
        "network": [],
        "artifact": None,
    }
)


@dataclass(slots=True)
class RunEndProbe:
    # Owner -> the session its last operation ran in.
    sessions: dict[str, str] = field(default_factory=dict)
    # Owners whose session close the desktop app was sent, in order.
    closed: list[str] = field(default_factory=list)
    # Each process stop: its owners, and whether each was refused a browser
    # operation while its processes stopped.
    stopped: list[tuple[tuple[str, ...], bool]] = field(default_factory=list)

    async def open_browser(self, db_path: Path, owner: str) -> None:
        assert isinstance(await self._request(db_path, owner), BrowserOperationDone)

    async def refused(self, db_path: Path, owner: str) -> bool:
        outcome = await self._request(db_path, owner)
        return (
            isinstance(outcome, BrowserRelayFailed)
            and outcome.code == "BROWSER_SESSION_GONE"
        )

    async def _request(self, db_path: Path, owner: str) -> BrowserOperationOutcome:
        return await relay_module.request_browser_operation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            owner_process_id=owner,
            tool_request_id=f"{owner}:browser",
            operation=_NAVIGATE,
            policy=_POLICY,
        )

    async def _deliver(self, user_id: str, message: DesktopRequestMessage) -> bool:
        if isinstance(message, BrowserSessionCloseRequestedMessage):
            self.closed.extend(
                owner
                for owner, session_id in self.sessions.items()
                if session_id == message.session_id
            )
            return True
        assert isinstance(message, BrowserOperationRequestedMessage)
        self.sessions[message.process_id] = message.session_id
        relay_module.browser_operation_broker().answer(
            request_id=message.operation_request_id,
            user_id=user_id,
            outcome=_NAVIGATED,
        )
        return True

    async def _stop(
        self, *, db_path: Path, busy_timeout_ms: int, owner_process_ids: tuple[str, ...]
    ) -> None:
        refused = [await self.refused(db_path, owner) for owner in owner_process_ids]
        self.stopped.append((owner_process_ids, all(refused)))


def install_run_end_probe(monkeypatch: pytest.MonkeyPatch) -> RunEndProbe:
    probe = RunEndProbe()
    monkeypatch.setattr(relay_module, "_OWNERS", {})
    monkeypatch.setattr(relay_module, "deliver_desktop_request", probe._deliver)
    monkeypatch.setattr(run_end_module, "stop_run_processes", probe._stop)
    return probe

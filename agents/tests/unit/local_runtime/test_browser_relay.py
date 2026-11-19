from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    RecordingDesktopSession,
    _seed_broker_actor_process,
    desktop_session,
)
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.local_runtime.runtime import browser_relay as relay_module
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationFailed,
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BrowserAnswer,
    BrowserOperationDone,
    BrowserOperationOutcome,
    BrowserRelayFailed,
)
from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    BrowserNavigateOperation,
    BrowserOperation,
    BrowserPagePolicy,
    BrowserPrintOperation,
    BrowserScreenshotOperation,
    BrowserWebTarget,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
OWNER = BROKER_ACTOR_PROCESS_ID
NETWORK = [{"host": "localhost", "port": 5173}]
SCREENSHOT = BrowserScreenshotOperation(action="screenshot")
NAVIGATE = BrowserNavigateOperation(
    action="navigate",
    target=BrowserWebTarget(kind="web", url="http://localhost:5173/"),
)
POLICY = BrowserPagePolicy(
    allow_network=True,
    file_roots=["/work"],
    private_roots=["/app-data"],
    private_root_exceptions=[],
    secret_paths=[],
    write_hosts=["api.example.com"],
)


def _succeeded(artifact: dict[str, object] | None) -> BrowserOperationSucceeded:
    return BrowserOperationSucceeded.model_validate(
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
            "network": NETWORK,
            "artifact": artifact,
        }
    )


PNG_CLAIM = {
    "kind": "png",
    "byte_size": len(PNG_BYTES),
    "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
    "width_px": 1280,
    "height_px": 800,
}
SCREENSHOT_ANSWER = _succeeded(PNG_CLAIM)
NAVIGATE_ANSWER = _succeeded(None)


def _failed(code: str) -> BrowserOperationFailed:
    return BrowserOperationFailed.model_validate(
        {"status": "failed", "code": code, "message": "x", "network": NETWORK}
    )


@dataclass(frozen=True, slots=True)
class Runtime:
    desktop: RecordingDesktopSession
    artifact_root: Path
    broker: OneShotRequests[BrowserAnswer]
    db_path: Path

    def closes(self) -> list[object]:
        sent = zip(self.desktop.events, self.desktop.requests, strict=True)
        close = OutboundEvent.BROWSER_SESSION_CLOSE_REQUESTED
        return [request["session_id"] for event, request in sent if event == close]

    async def sweep(self) -> None:
        await relay_module.sweep_browser_sessions(
            db_path=self.db_path, busy_timeout_ms=1_000
        )

    def seed_run(self, owner: str, status: str = "running") -> None:
        insert_agent_action(db_path=self.db_path, action_id=owner)
        _seed_broker_actor_process(
            self.db_path, process_id=owner, action_id=owner, status=status
        )

    def finish_run(self, owner: str) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE processes SET status = 'completed' WHERE process_id = ?",
                (owner,),
            )

    async def request(
        self, owner: str, operation: BrowserOperation = NAVIGATE
    ) -> BrowserOperationOutcome:
        return await relay_module.request_browser_operation(
            db_path=self.db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            owner_process_id=owner,
            operation=operation,
            policy=POLICY,
        )


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    broker = OneShotRequests[BrowserAnswer]()
    monkeypatch.setattr(relay_module, "browser_operation_broker", lambda: broker)
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)  # OWNER, running
    with desktop_session() as desktop:
        yield Runtime(desktop, artifact_root, broker, db_path)


async def _start(
    runtime: Runtime,
    *,
    operation: BrowserOperation = SCREENSHOT,
    owner: str = OWNER,
    data: bytes | None = None,
    kind: str = "png",
) -> tuple[asyncio.Future[BrowserOperationOutcome], dict[str, object], Path]:
    """Start an operation, then write its artifact where Electron would."""

    sent = len(runtime.desktop.requests)
    task = asyncio.ensure_future(runtime.request(owner, operation))
    while len(runtime.desktop.requests) == sent:
        await asyncio.sleep(0.005)
    payload = runtime.desktop.requests[-1]
    name = f"{payload['operation_request_id']}.{kind}"
    artifact = runtime.artifact_root / ".tmp" / "browser" / name
    if data is not None:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(data)
    return task, payload, artifact


async def _run(
    runtime: Runtime,
    answer: BrowserAnswer,
    *,
    operation: BrowserOperation = SCREENSHOT,
    owner: str = OWNER,
    data: bytes | None = None,
    kind: str = "png",
) -> tuple[BrowserOperationOutcome, dict[str, object]]:
    task, payload, artifact = await _start(
        runtime, operation=operation, owner=owner, data=data, kind=kind
    )
    accepted = runtime.broker.answer(
        request_id=str(payload["operation_request_id"]),
        user_id="user-1",
        outcome=answer,
    )
    assert accepted == "accepted"
    outcome = await task
    assert not artifact.exists()
    return outcome, payload


async def _navigate(runtime: Runtime, owner: str = OWNER) -> object:
    """Navigate the owner's page and return the session id it ran in."""

    _, payload = await _run(runtime, NAVIGATE_ANSWER, operation=NAVIGATE, owner=owner)
    return payload["session_id"]


@pytest.mark.asyncio
async def test_a_screenshot_is_verified_returned_and_removed(
    runtime: Runtime,
) -> None:
    outcome, payload = await _run(runtime, SCREENSHOT_ANSWER, data=PNG_BYTES)

    assert outcome == BrowserOperationDone(result=SCREENSHOT_ANSWER, artifact=PNG_BYTES)
    assert runtime.desktop.events == [OutboundEvent.BROWSER_OPERATION_REQUESTED]
    assert payload == {
        "operation_request_id": payload["operation_request_id"],
        "session_id": payload["session_id"],
        "operation": {"action": "screenshot", "ref": None, "full_page": None},
        "policy": POLICY.model_dump(mode="json"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "answer", "data", "kind"),
    [
        (SCREENSHOT, _succeeded({**PNG_CLAIM, "sha256": "0" * 64}), PNG_BYTES, "png"),
        (
            SCREENSHOT,
            _succeeded({**PNG_CLAIM, "byte_size": len(PNG_BYTES) + 1}),
            PNG_BYTES,
            "png",
        ),
        (SCREENSHOT, SCREENSHOT_ANSWER, None, "png"),
        # A print must come with its PDF, and a page read with no file at all.
        (BrowserPrintOperation(action="print"), NAVIGATE_ANSWER, None, "pdf"),
        (NAVIGATE, SCREENSHOT_ANSWER, PNG_BYTES, "png"),
    ],
    ids=[
        "sha256_differs",
        "byte_size_differs",
        "no_file",
        "print_without_pdf",
        "unasked_png",
    ],
)
async def test_output_that_does_not_match_the_answer_fails_integrity(
    runtime: Runtime,
    operation: BrowserOperation,
    answer: BrowserOperationSucceeded,
    data: bytes | None,
    kind: str,
) -> None:
    outcome, _ = await _run(runtime, answer, operation=operation, data=data, kind=kind)

    assert isinstance(outcome, BrowserRelayFailed)
    assert outcome.code == "BROWSER_INTEGRITY_FAILED"
    # The page reached these hosts whether or not its output holds up.
    assert [host.model_dump() for host in outcome.network] == NETWORK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "same_session"),
    [
        ("BROWSER_TIMED_OUT", True),
        ("BROWSER_RENDERER_CRASHED", False),
        ("BROWSER_SESSION_GONE", False),
    ],
)
async def test_an_electron_failure_is_returned_and_a_dropped_session_is_replaced(
    runtime: Runtime, code: str, same_session: bool
) -> None:
    # Catches: reusing an id Electron already dropped, which it would refuse for
    # the rest of the run.
    outcome, first = await _run(runtime, _failed(code), data=b"\x89PN")

    assert isinstance(outcome, BrowserRelayFailed)
    assert (outcome.code, outcome.network) == (code, tuple(_failed(code).network))
    assert (await _navigate(runtime) == first["session_id"]) is same_session


@pytest.mark.asyncio
async def test_an_unanswered_operation_times_out_and_a_late_answer_is_unknown(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(relay_module, "BROWSER_RELAY_TIMEOUT_SECONDS", 0.05)
    task, payload, artifact = await _start(runtime, data=PNG_BYTES)

    outcome = await task

    assert isinstance(outcome, BrowserRelayFailed)
    assert outcome.code == "BROWSER_RELAY_TIMED_OUT"
    assert not artifact.exists()
    # The router turns this into a 404, on which Electron deletes its file.
    late = runtime.broker.answer(
        request_id=str(payload["operation_request_id"]),
        user_id="user-1",
        outcome=SCREENSHOT_ANSWER,
    )
    assert late == "unknown_request"


@pytest.mark.asyncio
async def test_a_stopped_operation_still_removes_the_file(runtime: Runtime) -> None:
    task, _, artifact = await _start(runtime, data=PNG_BYTES)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not artifact.exists()


@pytest.mark.asyncio
async def test_after_its_end_begins_a_run_gets_no_operation(runtime: Runtime) -> None:
    opened = await _navigate(runtime)

    await relay_module.end_browser_sessions(owner_process_ids=(OWNER, "p-unused"))
    await relay_module.end_browser_sessions(owner_process_ids=(OWNER,))
    # Its status is still running: the fence in memory refuses it.
    refused = await runtime.request("p-unused")

    # One close, for the one session that existed; a run with none (one whose
    # navigate races its Stop) is refused all the same.
    assert runtime.closes() == [opened]
    assert isinstance(refused, BrowserRelayFailed)
    assert refused.code == "BROWSER_SESSION_GONE"
    assert len(runtime.desktop.requests) == 2


@pytest.mark.asyncio
async def test_closing_is_idempotent_and_the_run_may_open_a_new_session(
    runtime: Runtime,
) -> None:
    first = await _navigate(runtime)

    await relay_module.close_browser_session(owner_process_id=OWNER)
    await relay_module.close_browser_session(owner_process_id=OWNER)

    assert runtime.closes() == [first]
    assert await _navigate(runtime) != first


@pytest.mark.asyncio
async def test_the_sweep_closes_runs_that_ended_and_keeps_live_ones(
    runtime: Runtime,
) -> None:
    runtime.seed_run("p-paused", "paused")
    runtime.seed_run("p-done")
    sessions = {
        owner: await _navigate(runtime, owner)
        for owner in (OWNER, "p-paused", "p-done")
    }
    # Catches: a session minted per operation, or one shared between runs.
    assert len(set(sessions.values())) == 3
    runtime.finish_run("p-done")
    runtime.desktop.open = False

    await runtime.sweep()
    runtime.desktop.open = True
    # An ended run stays listed until its close is delivered.
    await runtime.sweep()
    await runtime.sweep()

    assert runtime.closes() == [sessions["p-done"]]
    assert set(relay_module._OWNERS) == {OWNER, "p-paused"}
    # Catches: forgetting the ended owner reopening its browser. A late request
    # finds no fence in memory, and the run's status refuses it; so does a run
    # with no process row at all.
    for owner in ("p-done", "p-no-row"):
        late = await runtime.request(owner)
        assert isinstance(late, BrowserRelayFailed)
        assert late.code == "BROWSER_SESSION_GONE"
    # A live run, waiting for an approval or not, keeps its page.
    assert await _navigate(runtime) == sessions[OWNER]


@pytest.mark.asyncio
async def test_a_close_send_that_never_completes_does_not_hold_the_sweep(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: one stuck socket stalling the shared worker's periodic tasks and
    # every other owner's close.
    monkeypatch.setattr(relay_module, "BROWSER_CLOSE_SEND_TIMEOUT_SECONDS", 0.05)
    runtime.seed_run("p-2")
    opened = {owner: await _navigate(runtime, owner) for owner in (OWNER, "p-2")}
    runtime.finish_run("p-2")

    async def never_sends(event: object, message: object) -> bool:
        await asyncio.Event().wait()
        return True

    runtime.desktop.send_desktop_request = never_sends  # type: ignore[method-assign]
    # A live run's own close, and an ended run's, both stuck.
    await relay_module.close_browser_session(owner_process_id=OWNER)
    started = time.monotonic()
    await runtime.sweep()
    # One deadline per owner, then on to the next.
    assert time.monotonic() - started < 1.0
    assert runtime.closes() == []

    del runtime.desktop.send_desktop_request
    await runtime.sweep()
    await runtime.sweep()
    # Each retried once delivered; only the live run stays listed.
    assert sorted(runtime.closes()) == sorted(opened.values())
    assert set(relay_module._OWNERS) == {OWNER}

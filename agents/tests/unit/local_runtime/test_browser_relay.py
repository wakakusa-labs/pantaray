from __future__ import annotations

import asyncio
import hashlib
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
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
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
    "sha256": PNG_SHA256,
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


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    broker = OneShotRequests[BrowserAnswer]()
    monkeypatch.setattr(relay_module, "browser_operation_broker", lambda: broker)
    monkeypatch.setattr(relay_module, "_OWNERS", {})
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
    task = asyncio.ensure_future(
        relay_module.request_browser_operation(
            user_id="user-1",
            action_id="action-1",
            owner_process_id=owner,
            tool_request_id="action-1:step-1:1",
            operation=operation,
            policy=POLICY,
        )
    )
    while len(runtime.desktop.requests) == sent:
        await asyncio.sleep(0.005)
    payload = runtime.desktop.requests[-1]
    artifact = (
        runtime.artifact_root
        / ".tmp"
        / "browser"
        / f"{payload['operation_request_id']}.{kind}"
    )
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
        "action_id": "action-1",
        "process_id": OWNER,
        "tool_request_id": "action-1:step-1:1",
        "operation": {"action": "screenshot", "ref": None, "full_page": None},
        "policy": POLICY.model_dump(mode="json"),
    }


@pytest.mark.asyncio
async def test_a_run_keeps_one_session_and_another_run_gets_its_own(
    runtime: Runtime,
) -> None:
    # Catches: a session minted per operation (the page would be lost between
    # calls), or one shared between runs.
    first = await _navigate(runtime)

    assert await _navigate(runtime) == first
    assert await _navigate(runtime, "p-2") != first


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

    assert outcome == BrowserRelayFailed(
        code=code,  # type: ignore[arg-type]
        message="x",
        network=tuple(_failed(code).network),
    )
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
    refused = await relay_module.request_browser_operation(
        user_id="user-1",
        action_id="action-1",
        owner_process_id="p-unused",
        tool_request_id="t",
        operation=NAVIGATE,
        policy=POLICY,
    )

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
async def test_the_sweep_retries_a_close_no_desktop_session_received(
    runtime: Runtime,
) -> None:
    opened = await _navigate(runtime)
    runtime.desktop.open = False
    await relay_module.close_browser_session(owner_process_id=OWNER)
    assert runtime.closes() == []

    runtime.desktop.open = True
    await runtime.sweep()
    await runtime.sweep()

    assert runtime.closes() == [opened]


@pytest.mark.asyncio
async def test_the_sweep_closes_runs_that_ended_and_keeps_live_ones(
    runtime: Runtime,
) -> None:
    for owner, status in (("p-done", "completed"), ("p-paused", "paused")):
        insert_agent_action(db_path=runtime.db_path, action_id=owner)
        _seed_broker_actor_process(
            runtime.db_path, process_id=owner, action_id=owner, status=status
        )
    sessions = {
        owner: await _navigate(runtime, owner)
        for owner in (OWNER, "p-paused", "p-done", "p-no-row")
    }
    runtime.desktop.open = False

    await runtime.sweep()
    runtime.desktop.open = True
    # An ended run stays listed until its close is delivered.
    await runtime.sweep()
    await runtime.sweep()

    assert set(runtime.closes()) == {sessions["p-done"], sessions["p-no-row"]}
    assert set(relay_module._OWNERS) == {OWNER, "p-paused"}
    # A live run, waiting for an approval or not, keeps its page.
    assert await _navigate(runtime) == sessions[OWNER]

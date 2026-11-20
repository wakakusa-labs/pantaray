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
from pantaray_agents.schema.user_browser import UserBrowserId
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
    # The relay sends the run's approved hosts instead.
    write_hosts=["caller.example"],
)


def _succeeded(
    artifact: dict[str, object] | None, opened_host: str | None = None
) -> BrowserOperationSucceeded:
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
            "opened_host": opened_host,
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
NAVIGATE_ANSWER = _succeeded(None, opened_host="localhost")


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
        self,
        owner: str,
        operation: BrowserOperation = NAVIGATE,
        user_browser: UserBrowserId | None = None,
    ) -> BrowserOperationOutcome:
        """On the app's page, or with ``user_browser`` on the user's page."""

        if user_browser is None:
            return await relay_module.request_browser_operation(
                db_path=self.db_path,
                busy_timeout_ms=1_000,
                user_id="user-1",
                owner_process_id=owner,
                browser="pantaray",
                user_browser=None,
                operation=operation,
                policy=POLICY,
            )
        return await relay_module.request_browser_operation(
            db_path=self.db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            owner_process_id=owner,
            browser="user",
            user_browser=user_browser,
            operation=operation,
            policy=POLICY,
            action_id="action-1",
        )

    def answer(self, payload: dict[str, object], answer: BrowserAnswer) -> None:
        accepted = self.broker.answer(
            request_id=str(payload["operation_request_id"]),
            user_id="user-1",
            outcome=answer,
        )
        assert accepted == "accepted"


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
    user_browser: UserBrowserId | None = None,
) -> tuple[asyncio.Future[BrowserOperationOutcome], dict[str, object], Path]:
    """Start an operation, then write its artifact where Electron would."""

    sent = len(runtime.desktop.events)
    task = asyncio.ensure_future(runtime.request(owner, operation, user_browser))
    while (
        OutboundEvent.BROWSER_OPERATION_REQUESTED not in runtime.desktop.events[sent:]
    ):
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
    user_browser: UserBrowserId | None = None,
) -> tuple[BrowserOperationOutcome, dict[str, object]]:
    task, payload, artifact = await _start(
        runtime,
        operation=operation,
        owner=owner,
        data=data,
        kind=kind,
        user_browser=user_browser,
    )
    runtime.answer(payload, answer)
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
        "policy": {**POLICY.model_dump(mode="json"), "write_hosts": []},
        "user_browser": None,
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

    await relay_module.close_browser_session(owner_process_id=OWNER, browser="pantaray")
    await relay_module.close_browser_session(owner_process_id=OWNER, browser="pantaray")

    assert runtime.closes() == [first]
    assert await _navigate(runtime) != first


# Catches: one run's approved write host reaching another run's page, a host
# lost when the run closes its page, and the hosts outliving the run's end.
@pytest.mark.asyncio
async def test_write_hosts_belong_to_one_run_until_it_ends(runtime: Runtime) -> None:
    runtime.seed_run("p-other")
    relay_module.allow_browser_writes(
        owner_process_id=OWNER, browser="pantaray", host="api.example.com"
    )

    _, mine = await _run(runtime, NAVIGATE_ANSWER, operation=NAVIGATE)
    _, theirs = await _run(
        runtime, NAVIGATE_ANSWER, operation=NAVIGATE, owner="p-other"
    )
    await relay_module.close_browser_session(owner_process_id=OWNER, browser="pantaray")
    _, reopened = await _run(runtime, NAVIGATE_ANSWER, operation=NAVIGATE)
    await relay_module.end_browser_sessions(owner_process_ids=(OWNER,))

    assert mine["policy"]["write_hosts"] == ["api.example.com"]  # type: ignore[index]
    assert theirs["policy"]["write_hosts"] == []  # type: ignore[index]
    assert reopened["policy"]["write_hosts"] == ["api.example.com"]  # type: ignore[index]
    assert relay_module.browser_write_hosts(OWNER, "pantaray") == ()


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
    await relay_module.close_browser_session(owner_process_id=OWNER, browser="pantaray")
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


def _hosts(payload: dict[str, object]) -> tuple[object, object]:
    """The write hosts and the user's browser an operation was sent with."""

    return payload["policy"]["write_hosts"], payload["user_browser"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_user_page_waits_for_its_first_answer_without_a_deadline(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: the relay timing out while the user is still deciding whether to
    # allow the connection, or a connected page left without a deadline.
    monkeypatch.setattr(relay_module, "BROWSER_RELAY_TIMEOUT_SECONDS", 0.05)
    task, payload, _ = await _start(runtime, operation=NAVIGATE, user_browser="chrome")
    await asyncio.sleep(0.2)
    assert not task.done()
    runtime.answer(payload, NAVIGATE_ANSWER)
    assert isinstance(await task, BrowserOperationDone)

    unanswered = await asyncio.wait_for(runtime.request(OWNER, NAVIGATE, "chrome"), 1)

    assert isinstance(unanswered, BrowserRelayFailed)
    assert unanswered.code == "BROWSER_RELAY_TIMED_OUT"


@pytest.mark.asyncio
async def test_an_undelivered_first_user_operation_fails_at_once(
    runtime: Runtime,
) -> None:
    # Catches: waiting forever for an answer no desktop app was asked for.
    runtime.desktop.open = False

    outcome = await asyncio.wait_for(runtime.request(OWNER, NAVIGATE, "chrome"), 1)

    assert isinstance(outcome, BrowserRelayFailed)
    assert outcome.code == "BROWSER_RELAY_TIMED_OUT"


# Catches: a host approved on one page reaching the other, so an approval on the
# app's page would let the user's logged-in browser write (or the reverse), and
# the user's page opening hosts the run was not approved for.
@pytest.mark.asyncio
async def test_each_page_has_its_own_session_and_hosts(runtime: Runtime) -> None:
    relay_module.allow_browser_writes(
        owner_process_id=OWNER, browser="pantaray", host="app.example"
    )
    relay_module.allow_browser_writes(
        owner_process_id=OWNER, browser="user", host="mail.example"
    )
    relay_module.allow_user_browser_reads(owner_process_id=OWNER, host="mail.example")

    _, app = await _run(runtime, NAVIGATE_ANSWER, operation=NAVIGATE)
    _, user = await _run(
        runtime, NAVIGATE_ANSWER, operation=NAVIGATE, user_browser="chrome"
    )

    assert app["session_id"] != user["session_id"]
    assert _hosts(app) == (["app.example"], None)
    assert _hosts(user) == (
        ["mail.example"],
        {"browser": "chrome", "action_id": "action-1", "read_hosts": ["mail.example"]},
    )
    await relay_module.close_browser_session(owner_process_id=OWNER, browser="user")
    assert runtime.closes() == [user["session_id"]]
    assert relay_module.browser_page_host(OWNER, "pantaray") == "localhost"
    assert relay_module.browser_page_host(OWNER, "user") is None


@pytest.mark.asyncio
async def test_the_user_page_stays_bound_to_its_first_browser(
    runtime: Runtime,
) -> None:
    # Catches: hosts approved for one browser sent to another one.
    relay_module.allow_user_browser_reads(owner_process_id=OWNER, host="mail.example")
    assert relay_module.bound_user_browser(OWNER) is None
    await _run(runtime, NAVIGATE_ANSWER, operation=NAVIGATE, user_browser="chrome")
    sent = len(runtime.desktop.requests)

    with pytest.raises(ValueError):
        await runtime.request(OWNER, NAVIGATE, "edge")

    assert len(runtime.desktop.requests) == sent
    assert relay_module.bound_user_browser(OWNER) == "chrome"


@pytest.mark.asyncio
async def test_a_lost_connection_asks_again_in_a_new_session(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: reusing an id Electron dropped, or putting the deadline on a new
    # connection the user has to allow again.
    monkeypatch.setattr(relay_module, "BROWSER_RELAY_TIMEOUT_SECONDS", 0.05)
    sessions = []
    for answer in (
        _failed("BROWSER_USER_DENIED"),
        NAVIGATE_ANSWER,
        # The user quit the browser after allowing it.
        _failed("BROWSER_USER_NOT_RUNNING"),
    ):
        _, payload = await _run(
            runtime, answer, operation=NAVIGATE, user_browser="chrome"
        )
        sessions.append(payload["session_id"])
    # The session ended; the page is still the same browser's.
    assert relay_module.bound_user_browser(OWNER) == "chrome"

    task, last, _ = await _start(runtime, operation=NAVIGATE, user_browser="chrome")
    await asyncio.sleep(0.2)

    assert not task.done()
    assert sessions[0] != sessions[1] == sessions[2] != last["session_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_the_run_end_closes_both_pages_and_drops_their_hosts(
    runtime: Runtime,
) -> None:
    relay_module.allow_browser_writes(
        owner_process_id=OWNER, browser="user", host="mail.example"
    )
    relay_module.allow_user_browser_reads(owner_process_id=OWNER, host="mail.example")
    app = await _navigate(runtime)
    _, user = await _run(
        runtime, NAVIGATE_ANSWER, operation=NAVIGATE, user_browser="chrome"
    )

    await relay_module.end_browser_sessions(owner_process_ids=(OWNER,))

    assert set(runtime.closes()) == {app, user["session_id"]}
    assert relay_module.browser_write_hosts(OWNER, "user") == ()
    assert relay_module.user_browser_read_hosts(OWNER) == ()

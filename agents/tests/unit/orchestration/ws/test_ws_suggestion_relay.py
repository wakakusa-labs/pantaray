"""WS relay of runtime-started Suggestion processes."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.periodic_schedule import (
    PeriodicTaskContext,
    _run_suggestion_release,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import (
    deliverable_sessions,
    suggestion_job_timing,
    suggestion_relay,
)
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.suggestion_relay import LiveSuggestionProcess
from pantaray_agents.repositories.suggestion_runtime_results import (
    AppendProcessEventResult,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult

LIVE_PROCESS = LiveSuggestionProcess(process_id="p1", suggestion_id="s1")


class _ProcessingRepository:
    """Keeps the Suggestion row non-terminal so the relay stays attached."""

    async def get_suggestion(self, *, user_id: str, suggestion_id: str):  # noqa: ANN201
        return RepositoryResult(
            data={
                "user_id": user_id,
                "suggestion_id": suggestion_id,
                "status": "processing",
            }
        )

    async def append_process_event_and_project_history(  # noqa: ANN201
        self,
        *,
        event_id: str,
        suggestion_id: str,
        user_id: str,
        event_name: str,
        payload: dict,
        action_id: str | None,
    ):
        _ = (event_id, suggestion_id, user_id, event_name, payload, action_id)
        return RepositoryResult(
            data=AppendProcessEventResult(sequence=1, inserted=True)
        )


class _SucceededRepository(_ProcessingRepository):
    """The Suggestion row reached its terminal state after the reconnect."""

    async def get_suggestion(self, *, user_id: str, suggestion_id: str):  # noqa: ANN201
        return RepositoryResult(
            data={
                "user_id": user_id,
                "suggestion_id": suggestion_id,
                "status": "success",
                "has_suggestion": True,
                "answer": "resumed answer",
                "delivery_state": "released",
            }
        )


class _HeldRepository(_ProcessingRepository):
    """A stored Suggestion the release task has not decided on yet."""

    def __init__(self) -> None:
        self.delivery_state = "held"

    async def get_suggestion(self, *, user_id: str, suggestion_id: str):  # noqa: ANN201
        return RepositoryResult(
            data={
                "user_id": user_id,
                "suggestion_id": suggestion_id,
                "status": "success",
                "has_suggestion": True,
                "answer": "held answer",
                "interaction_contract": "message_only",
                "delivery_state": self.delivery_state,
            }
        )


def _build_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_state: WebSocketState,
    repository: _ProcessingRepository | None = None,
) -> tuple[WSOrchestrationHandler, MagicMock]:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    monkeypatch.setattr(
        suggestion_job_timing, "SUGGESTION_JOB_POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(suggestion_relay, "SUGGESTION_RELAY_TICK_SECONDS", 0.01)
    monkeypatch.setattr(
        suggestion_relay,
        "read_local_runtime_db_config",
        lambda: (Path("/nonexistent/runtime.sqlite3"), 1000),
    )
    monkeypatch.setattr(
        suggestion_relay,
        "read_relayable_suggestion_processes",
        lambda **_kwargs: [LIVE_PROCESS],
    )

    websocket = MagicMock()
    websocket.client_state = client_state
    websocket.send_json = AsyncMock()
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id="user-1",
    )
    repository = repository or _ProcessingRepository()
    handler._get_suggestion_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    handler._get_action_state_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    return handler, websocket


def _sent_events(websocket: MagicMock) -> list[str]:
    return [
        str(call.args[0].get("event"))
        for call in websocket.send_json.call_args_list
        if call.args and isinstance(call.args[0], dict)
    ]


@pytest.mark.asyncio
async def test_relay_attaches_one_runtime_process_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.CONNECTED
    )
    try:
        await handler._relay_live_suggestion_processes()
        await handler._relay_live_suggestion_processes()
    finally:
        await handler.close()

    assert _sent_events(websocket).count("process_started") == 1


@pytest.mark.asyncio
async def test_relay_leaves_the_process_unattached_when_the_socket_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.DISCONNECTED
    )
    try:
        await handler._relay_live_suggestion_processes()
    finally:
        await handler.close()

    assert _sent_events(websocket) == []
    assert handler._relayed_suggestion_processes == set()
    session = handler.session_store.get("sess-1")
    assert session is not None
    assert LIVE_PROCESS.process_id not in session.processes


@pytest.mark.asyncio
async def test_a_resumed_process_is_not_relayed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect + resume_session must not double-deliver a live process."""
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.CONNECTED
    )
    store = handler.session_store
    store.create_session("sess-0", user_id="user-1")
    store.ensure_process("sess-0", LIVE_PROCESS.process_id)
    store.set_process_metadata(
        "sess-0",
        LIVE_PROCESS.process_id,
        suggestion_id=LIVE_PROCESS.suggestion_id,
        kind="suggestion",
    )

    await handler.resume_session(
        session_id="sess-0",
        process_id=LIVE_PROCESS.process_id,
        last_cursor=None,
        last_chunk_index=None,
        kind="suggestion",
    )
    await handler._relay_live_suggestion_processes()

    assert LIVE_PROCESS.process_id in handler._relayed_suggestion_processes
    assert "process_started" not in _sent_events(websocket)


@pytest.mark.asyncio
async def test_a_resumed_live_process_still_completes_on_the_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous session's relay died with its socket; the resumed one waits."""
    handler, websocket = _build_handler(
        monkeypatch,
        client_state=WebSocketState.CONNECTED,
        repository=_SucceededRepository(),
    )
    store = handler.session_store
    store.create_session("sess-0", user_id="user-1")
    store.ensure_process("sess-0", LIVE_PROCESS.process_id)
    store.set_process_metadata(
        "sess-0",
        LIVE_PROCESS.process_id,
        suggestion_id=LIVE_PROCESS.suggestion_id,
        kind="suggestion",
    )

    try:
        await handler.resume_session(
            session_id="sess-0",
            process_id=LIVE_PROCESS.process_id,
            last_cursor=None,
            last_chunk_index=None,
            kind="suggestion",
        )
        for _ in range(50):
            if "process_completed" in _sent_events(websocket):
                break
            await asyncio.sleep(0.01)
    finally:
        await handler.close()

    events = _sent_events(websocket)
    assert "process_started" not in events
    assert "suggestion_chunk" in events
    assert events.count("process_completed") == 1


@pytest.mark.asyncio
async def test_a_process_the_tick_attached_first_is_not_replaced_by_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On reconnect the first tick usually wins the race against resume_session.

    The tick's result relay must keep running: replacing it would cancel it,
    and a cancelled relay reports the still-running process as canceled.
    """
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.CONNECTED
    )
    store = handler.session_store
    store.create_session("sess-0", user_id="user-1")
    store.ensure_process("sess-0", LIVE_PROCESS.process_id)
    store.set_process_metadata(
        "sess-0",
        LIVE_PROCESS.process_id,
        suggestion_id=LIVE_PROCESS.suggestion_id,
        kind="suggestion",
    )

    try:
        await handler._relay_live_suggestion_processes()
        await asyncio.sleep(0)  # let the tick's result relay start waiting
        await handler.resume_session(
            session_id="sess-0",
            process_id=LIVE_PROCESS.process_id,
            last_cursor=None,
            last_chunk_index=None,
            kind="suggestion",
        )
        await asyncio.sleep(0.05)
        events = _sent_events(websocket)
    finally:
        await handler.close()

    assert events.count("process_started") == 1
    assert "process_completed" not in events


def _bare_handler(
    monkeypatch: pytest.MonkeyPatch, *, session_id: str, db_path: Path
) -> tuple[WSOrchestrationHandler, MagicMock]:
    """A handler that passed the handshake; its relay has not been started."""
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    monkeypatch.setattr(suggestion_relay, "SUGGESTION_RELAY_TICK_SECONDS", 0.01)
    monkeypatch.setattr(
        suggestion_relay, "read_local_runtime_db_config", lambda: (db_path, 1000)
    )
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id=session_id,
        user_id="user-1",
    )
    repository = _ProcessingRepository()
    handler._get_suggestion_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    handler._get_action_state_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    return handler, websocket


def _migrated_db(tmp_path: Path) -> Path:
    from pantaray_agents.local_runtime.storage.migrations import (
        apply_migrations,
        load_default_migrations,
    )

    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES ('user-1', 'ja', '2026-09-30T00:00:00Z', '2026-09-30T00:00:00Z')"
        )
    return db_path


@pytest.mark.asyncio
async def test_a_welcome_waits_until_a_session_can_relay_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket is bound before its relay window starts; a welcome saved then is lost."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pantaray_agents.app.shared import install_common_exception_handlers
    from pantaray_agents.auth_http import get_current_user_id_from_token
    from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
        welcome_suggestion_id,
    )
    from pantaray_agents.routers import suggestion as suggestion_router

    db_path = _migrated_db(tmp_path)
    monkeypatch.setattr(
        suggestion_router, "read_local_runtime_db_config", lambda: (db_path, 1000)
    )
    monkeypatch.setattr(suggestion_router, "verify_current_owner", lambda _user: None)
    app = FastAPI()
    install_common_exception_handlers(app)
    app.include_router(suggestion_router.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: "user-1"
    client = TestClient(app)

    def _post_welcome() -> int:
        return client.post(
            "/v1/agents/users/user-1/suggestions/welcome", json={"answer": "Hello"}
        ).status_code

    def _stored() -> int:
        with sqlite3.connect(db_path) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM agent_suggestions"
            ).fetchone()[0]

    handler, websocket = _bare_handler(
        monkeypatch, session_id="sess-1", db_path=db_path
    )
    try:
        assert (_post_welcome(), _stored()) == (503, 0)

        handler.start_suggestion_relay()
        assert (_post_welcome(), _stored()) == (200, 1)
        for _ in range(100):
            if "process_started" in _sent_events(websocket):
                break
            await asyncio.sleep(0.01)
    finally:
        await handler.close()
        client.close()

    started = [
        call.args[0]
        for call in websocket.send_json.call_args_list
        if call.args[0].get("event") == "process_started"
    ]
    assert [event["data"]["suggestion_id"] for event in started] == [
        welcome_suggestion_id("user-1")
    ]
    assert not deliverable_sessions.owner_has_deliverable_session("user-1")


@pytest.mark.asyncio
async def test_a_session_whose_send_failed_no_longer_counts_as_deliverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pantaray_agents.schema.websocket import SessionStartedMessage

    db_path = _migrated_db(tmp_path)
    broken, broken_socket = _bare_handler(
        monkeypatch, session_id="sess-broken", db_path=db_path
    )
    healthy, _ = _bare_handler(monkeypatch, session_id="sess-healthy", db_path=db_path)
    broken_socket.send_json = AsyncMock(side_effect=RuntimeError("socket is gone"))
    message = SessionStartedMessage(session_id="sess-broken", issued_at="now")
    try:
        broken.start_suggestion_relay()
        assert deliverable_sessions.owner_has_deliverable_session("user-1")
        await broken._send(
            "session_started",
            message,
            store_in_session_store=False,
            persist_public_event=False,
        )
        assert not deliverable_sessions.owner_has_deliverable_session("user-1")

        healthy.start_suggestion_relay()
        assert deliverable_sessions.owner_has_deliverable_session("user-1")
    finally:
        await broken.close()
        await healthy.close()


async def _relay_until_completed(
    handler: WSOrchestrationHandler, websocket: MagicMock, *, ticks: int
) -> list[str]:
    for _ in range(ticks):
        if "process_completed" in _sent_events(websocket):
            break
        await asyncio.sleep(0.01)
    return _sent_events(websocket)


@pytest.mark.asyncio
async def test_a_held_suggestion_is_delivered_only_once_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _HeldRepository()
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.CONNECTED, repository=repository
    )
    try:
        await handler._relay_live_suggestion_processes()
        held_events = await _relay_until_completed(handler, websocket, ticks=10)

        repository.delivery_state = "released"
        released_events = await _relay_until_completed(handler, websocket, ticks=50)
    finally:
        await handler.close()

    assert held_events == ["process_started"]
    assert released_events == [
        "process_started",
        "suggestion_chunk",
        "process_completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_state", ["expired", "superseded"])
async def test_a_suggestion_that_ends_unshown_completes_as_no_suggestion(
    monkeypatch: pytest.MonkeyPatch, ended_state: str
) -> None:
    repository = _HeldRepository()
    handler, websocket = _build_handler(
        monkeypatch, client_state=WebSocketState.CONNECTED, repository=repository
    )
    try:
        await handler._relay_live_suggestion_processes()
        repository.delivery_state = ended_state
        events = await _relay_until_completed(handler, websocket, ticks=50)
    finally:
        await handler.close()

    assert events == ["process_started", "process_completed"]
    completed = websocket.send_json.call_args_list[-1].args[0]["data"]
    assert completed["has_suggestion"] is False
    assert completed.get("interaction_contract") is None


def _seed_finished_suggestion(
    db_path: Path,
    suggestion_id: str,
    delivery_state: str,
    *,
    finished_at: str,
    updated_at: str | None = None,
    user_reaction: str | None = None,
) -> None:
    """A stored Suggestion whose run (process) completed at `finished_at`."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer,"
            " has_suggestion, interaction_contract, user_reaction, delivery_state,"
            " created_at, updated_at) VALUES (?, 'user-1', 'success', 'Answer', 1,"
            " 'action_offer', ?, ?, ?, ?)",
            (
                suggestion_id,
                user_reaction,
                delivery_state,
                finished_at,
                updated_at or finished_at,
            ),
        )
        connection.execute(
            "INSERT INTO processes(process_id, user_id, kind, status, suggestion_id,"
            " started_at, updated_at, completed_at, heartbeat_at, next_event_seq)"
            " VALUES (?, 'user-1', 'suggestion', 'completed', ?, ?, ?, ?, ?, 1)",
            (f"p-{suggestion_id}", suggestion_id, *[finished_at] * 4),
        )


def test_discovery_finds_suggestions_held_or_released_since_the_session_began(
    tmp_path: Path,
) -> None:
    """A reconnect after the run finished still owes the user what was not shown."""
    db_path = _migrated_db(tmp_path)
    before, since, after = (f"2026-09-30T0{hour}:00:00.000Z" for hour in (0, 1, 2))
    _seed_finished_suggestion(db_path, "held", "held", finished_at=before)
    _seed_finished_suggestion(db_path, "shown", "released", finished_at=before)
    _seed_finished_suggestion(db_path, "expired", "expired", finished_at=before)
    _seed_finished_suggestion(
        db_path, "released-now", "released", finished_at=before, updated_at=after
    )
    # A reaction after the session began moves updated_at; it is not a release.
    _seed_finished_suggestion(
        db_path,
        "reacted-now",
        "released",
        finished_at=before,
        updated_at=after,
        user_reaction="rejected",
    )

    found = suggestion_relay.read_relayable_suggestion_processes(
        db_path=db_path, busy_timeout_ms=1000, user_id="user-1", since=since
    )

    assert sorted(found) == [
        LiveSuggestionProcess("p-held", "held"),
        LiveSuggestionProcess("p-released-now", "released-now"),
    ]


def _release_tick_then_state(db_path: Path) -> tuple[str, int]:
    """Run the periodic release task once; the row's state and Memory nodes."""
    _run_suggestion_release(
        PeriodicTaskContext(
            db_path=db_path,
            busy_timeout_ms=1000,
            artifact_root=db_path.parent / "artifacts",
            owner_user_id="user-1",
            worker_is_idle=True,
        )
    )
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT delivery_state, (SELECT COUNT(*) FROM memory_nodes"
            " WHERE source_record_id = 'held') FROM agent_suggestions"
            " WHERE suggestion_id = 'held'"
        ).fetchone()


def _seed_held_a_minute_ago(db_path: Path) -> None:
    held_at = format_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    _seed_finished_suggestion(db_path, "held", "held", finished_at=held_at)


@pytest.mark.asyncio
async def test_a_suggestion_held_while_no_session_is_open_is_never_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save, disconnect, release, reconnect: it ends unshown and unremembered."""
    db_path = _migrated_db(tmp_path)
    _seed_held_a_minute_ago(db_path)

    assert _release_tick_then_state(db_path) == ("expired", 0)
    handler, websocket = _bare_handler(
        monkeypatch, session_id="sess-reconnected", db_path=db_path
    )
    try:
        handler.start_suggestion_relay()
        await asyncio.sleep(0.1)
    finally:
        await handler.close()

    assert _sent_events(websocket) == []


@pytest.mark.asyncio
async def test_a_release_before_the_first_relay_tick_is_delivered_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnect, release, first discovery: the session still shows it, once."""
    db_path = _migrated_db(tmp_path)
    _seed_held_a_minute_ago(db_path)
    handler, websocket = _bare_handler(
        monkeypatch, session_id="sess-reconnected", db_path=db_path
    )
    handler._get_suggestion_repository = AsyncMock(  # type: ignore[method-assign]
        return_value=LocalSuggestionRepository(
            db_path=str(db_path), busy_timeout_ms=1000, activity_repository=MagicMock()
        )
    )
    monkeypatch.setattr(
        suggestion_job_timing, "SUGGESTION_JOB_POLL_INTERVAL_SECONDS", 0.01
    )
    try:
        handler.start_suggestion_relay()
        # The relay loop has not run yet: this release lands before its first tick.
        assert _release_tick_then_state(db_path) == ("released", 1)
        await _relay_until_completed(handler, websocket, ticks=100)
        await asyncio.sleep(0.1)  # later discovery ticks must not relay it again
    finally:
        await handler.close()

    assert _sent_events(websocket) == [
        "process_started",
        "suggestion_chunk",
        "process_completed",
    ]

from __future__ import annotations

from fastapi.testclient import TestClient


class _Repo:
    def __init__(
        self,
        *,
        suggestion_row: dict[str, object] | None,
        event_rows: list[dict[str, object]] | None,
    ) -> None:
        self._suggestion_row = suggestion_row
        self._event_rows = event_rows

    async def get_suggestion_state(self, *, user_id: str, suggestion_id: str):
        from pantaray_agents.schema.repositories.repository import RepositoryResult

        row = self._suggestion_row
        if row is None:
            return RepositoryResult(data=None)
        if (
            str(row.get("user_id")) != user_id
            or str(row.get("suggestion_id")) != suggestion_id
        ):
            return RepositoryResult(data=None)
        # The real repository always decodes the column, an empty list by default.
        return RepositoryResult(data={"artifacts_json": [], **row})

    async def get_process_events_for_detail(self, *, user_id: str, suggestion_id: str):
        from pantaray_agents.schema.repositories.repository import RepositoryResult

        rows = self._event_rows or []
        filtered = [
            dict(row)
            for row in rows
            if str(row.get("user_id")) == user_id
            and str(row.get("suggestion_id")) == suggestion_id
        ]
        return RepositoryResult(data=filtered)


def _event(
    *,
    sequence: int,
    event_name: str,
    created_at: str,
    data: dict[str, object],
    process_id: str = "proc-action-123",
    suggestion_id: str = "sug-123",
    user_id: str = "user-123",
    action_id: str | None = "action-123",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "event_name": event_name,
        "created_at": created_at,
        "process_id": process_id,
        "suggestion_id": suggestion_id,
        "user_id": user_id,
        "action_id": action_id,
        "payload": {
            "data": data,
            "meta": {
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "action_id": action_id,
            },
        },
    }


def test_history_overlay_bootstrap_returns_history_snapshot() -> None:
    from pantaray_agents.entrypoints.main_local import app
    from pantaray_agents.routers import history_overlay as router_module

    async def _fake_user() -> str:
        return "user-123"

    app.dependency_overrides[router_module.get_current_user_id_from_token] = _fake_user
    app.dependency_overrides[router_module._get_overlay_repository] = lambda: _Repo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "success",
            "action_id": "action-123",
            "action_process_id": "proc-action-123",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="proc-suggestion-123",
                data={"content": "suggestion text"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="proc-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=8,
                event_name="process_completed",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "proc-action-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "status": "success",
                    "completed_at": "2026-03-14T00:00:05Z",
                    "final_output": "action final output",
                },
            ),
        ],
    )

    client = TestClient(app)
    try:
        response = client.get(
            "/api/agent/history/sug-123/overlay-bootstrap",
            headers={"Authorization": "Bearer dummy"},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["suggestion_id"] == "sug-123"
    assert payload["snapshot"]["suggestionText"] == "suggestion text"
    assert payload["snapshot"]["actionStatus"] == "success"
    assert payload["live_resume"]["kind"] == "none"


def test_history_overlay_bootstrap_returns_processing_live_resume(monkeypatch) -> None:
    from pantaray_agents.entrypoints.main_local import app
    from pantaray_agents.orchestration import router as orch_router
    from pantaray_agents.orchestration.session.store import InMemorySessionStore
    from pantaray_agents.routers import history_overlay as router_module

    async def _fake_user() -> str:
        return "user-123"

    session_store = InMemorySessionStore(max_age_seconds=3600)
    session_store.create_session("session-123", user_id="user-123")
    session_store.set_process_metadata(
        "session-123",
        "proc-action-123",
        suggestion_id="sug-123",
        action_id="action-123",
        command_id="command-123",
        kind="action",
    )
    monkeypatch.setattr(orch_router, "SESSION_STORE", session_store)
    app.dependency_overrides[router_module.get_current_user_id_from_token] = _fake_user
    app.dependency_overrides[router_module._get_overlay_repository] = lambda: _Repo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "processing",
            "action_id": "action-123",
            "action_process_id": "proc-action-123",
            "action_process_status": "running",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="proc-suggestion-123",
                data={"content": "suggestion text"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="proc-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=5,
                event_name="process_started",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "proc-action-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
        ],
    )

    client = TestClient(app)
    try:
        response = client.get(
            "/api/agent/history/sug-123/overlay-bootstrap",
            headers={"Authorization": "Bearer dummy"},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot"]["actionPhase"] == "processing"
    assert payload["live_resume"]["kind"] == "action"
    assert payload["live_resume"]["process_id"] == "proc-action-123"


def test_history_overlay_bootstrap_returns_404_without_history_row() -> None:
    from pantaray_agents.entrypoints.main_local import app
    from pantaray_agents.routers import history_overlay as router_module

    async def _fake_user() -> str:
        return "user-123"

    app.dependency_overrides[router_module.get_current_user_id_from_token] = _fake_user
    app.dependency_overrides[router_module._get_overlay_repository] = lambda: _Repo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": None,
        },
        event_rows=None,
    )

    client = TestClient(app)
    try:
        response = client.get(
            "/api/agent/history/sug-123/overlay-bootstrap",
            headers={"Authorization": "Bearer dummy"},
        )
    finally:
        client.close()
        app.dependency_overrides.clear()

    assert response.status_code == 409

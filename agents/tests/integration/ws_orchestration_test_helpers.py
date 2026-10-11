from __future__ import annotations

import importlib
import json
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import anyio
import pytest
from starlette.testclient import TestClient
from tests.suggestion_relay_seed import (
    SeededSuggestion,
    seed_live_suggestion_process,
    seed_terminal_suggestion_row,
)

from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.local_api_auth import (
    clear_local_api_token,
    issue_local_api_token,
    read_local_api_token,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.settings_loader import (
    LOCAL_RUNTIME_SETTINGS_MODULE,
    clear_active_settings_module,
    register_active_settings_module,
)

if TYPE_CHECKING:
    from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler


class _WsMockSuggestionRepository(MockSuggestionAgentRepository):
    """WS integration tests 用に `agent_suggestions` 契約を満たすモック。"""

    @staticmethod
    def _suggestion_identity(row: dict[str, object]) -> tuple[str, str]:
        return (
            str(row.get("suggestion_id") or ""),
            str(row.get("user_id") or ""),
        )

    def _insert_suggestion_table_row_if_absent(
        self,
        *,
        table_name: str,
        row: dict[str, object],
    ) -> dict[str, object]:
        rows = self.data.setdefault(table_name, [])
        identity = self._suggestion_identity(row)
        copied = dict(row)
        for existing in rows:
            if (
                isinstance(existing, dict)
                and self._suggestion_identity(existing) == identity
            ):
                return dict(existing)
        rows.append(copied)
        return copied

    def _upsert_suggestion_table_row(
        self,
        *,
        table_name: str,
        row: dict[str, object],
    ) -> dict[str, object]:
        rows = self.data.setdefault(table_name, [])
        identity = self._suggestion_identity(row)
        copied = dict(row)
        for index, existing in enumerate(rows):
            if (
                isinstance(existing, dict)
                and self._suggestion_identity(existing) == identity
            ):
                rows[index] = copied
                return copied
        rows.append(copied)
        return copied

    def _sync_suggestion_mirror(
        self,
        *,
        suggestion_id: str,
        user_id: str,
    ) -> None:
        source = next(
            (
                row
                for row in self.data.get("suggestions", [])
                if isinstance(row, dict)
                and str(row.get("suggestion_id") or "") == suggestion_id
                and str(row.get("user_id") or "") == user_id
            ),
            None,
        )
        if source is None:
            return
        self._upsert_suggestion_table_row(table_name="agent_suggestions", row=source)

    async def create_processing_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        created_at: str | None = None,
    ) -> RepositoryResult[dict[str, object]]:
        now_iso = datetime.now(UTC).isoformat()
        row = {
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "status": "processing",
            "created_at": created_at or now_iso,
            "updated_at": now_iso,
            "answer": None,
            "thinking": None,
            "prompt_text": None,
            "response_text": None,
            "has_suggestion": None,
            "interaction_contract": None,
            "prompt_name": None,
            "prompt_version": None,
            "error": None,
            "user_reaction": None,
            "accepted_at": None,
            "rejected_at": None,
            "action_status": None,
            "action_failure_code": None,
            "action_command_id": None,
            "action_process_id": None,
            "action_id": None,
            "action_started_at": None,
        }
        saved = self._insert_suggestion_table_row_if_absent(
            table_name="suggestions",
            row=row,
        )
        self._insert_suggestion_table_row_if_absent(
            table_name="agent_suggestions",
            row=saved,
        )
        return RepositoryResult(data=saved)

    async def save_suggestion(
        self,
        suggestion,
        prompt_name: str,
        prompt_version: str,
        prompt_text: str | None = None,
        response_text: str | None = None,
        request_images_count: int = 0,
        used_images_count: int = 0,
    ):
        result = await super().save_suggestion(
            suggestion=suggestion,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_text=prompt_text,
            response_text=response_text,
            request_images_count=request_images_count,
            used_images_count=used_images_count,
        )
        row = result.data if isinstance(result.data, dict) else None
        if row is None:
            return result
        mirrored = self._upsert_suggestion_table_row(
            table_name="agent_suggestions",
            row=row,
        )
        return RepositoryResult(data=mirrored)

    async def get_suggestion(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, object]]:
        row = next(
            (
                candidate
                for candidate in self.data.get("agent_suggestions", [])
                if candidate.get("suggestion_id") == suggestion_id
                and candidate.get("user_id") == user_id
            ),
            None,
        )
        return RepositoryResult(data=dict(row) if isinstance(row, dict) else None)

    async def get_suggestion_state(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, object]]:
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)

    async def finalize_suggestion_start_error_if_processing(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ):
        row_result = await self.get_suggestion(
            user_id=user_id,
            suggestion_id=suggestion_id,
        )
        row = row_result.data if isinstance(row_result.data, dict) else None
        if row is None:
            return RepositoryResult(
                data=type(
                    "FinalizeSuggestionStartErrorResult",
                    (),
                    {"outcome": "not_found", "row": None},
                )()
            )
        if str(row.get("status") or "").strip().lower() != "processing":
            return RepositoryResult(
                data=type(
                    "FinalizeSuggestionStartErrorResult",
                    (),
                    {"outcome": "already_terminal", "row": dict(row)},
                )()
            )
        row["status"] = "error"
        row["answer"] = ""
        row["has_suggestion"] = False
        row["interaction_contract"] = None
        row["error"] = {
            "error_code": error_code,
            "error_message": error_message,
            "error_details": error_details,
            "metadata": metadata,
        }
        self._upsert_suggestion_table_row(table_name="suggestions", row=row)
        self._upsert_suggestion_table_row(table_name="agent_suggestions", row=row)
        return RepositoryResult(
            data=type(
                "FinalizeSuggestionStartErrorResult",
                (),
                {"outcome": "updated", "row": dict(row)},
            )()
        )

    async def mark_user_reaction(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        user_reaction: str,
        action_status: str | None = None,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
        accepted_at: str | None = None,
        rejected_at: str | None = None,
        dismiss_reason: str | None = None,
    ) -> RepositoryResult[dict[str, object]]:
        row_result = await self.get_suggestion(
            user_id=user_id,
            suggestion_id=suggestion_id,
        )
        row = row_result.data if isinstance(row_result.data, dict) else None
        if row is None:
            return RepositoryResult(error="Suggestion not found")
        reaction = str(user_reaction or "").strip().lower()
        row["user_reaction"] = reaction
        if reaction == "accepted":
            row["accepted_at"] = accepted_at or datetime.now(UTC).isoformat()
            row["rejected_at"] = None
        else:
            row["rejected_at"] = rejected_at or datetime.now(UTC).isoformat()
            row["accepted_at"] = None
            row["dismiss_reason"] = dismiss_reason
        if action_status is not None:
            row["action_status"] = action_status
        if update_action_failure_code:
            row["action_failure_code"] = action_failure_code
        self._upsert_suggestion_table_row(table_name="suggestions", row=row)
        self._upsert_suggestion_table_row(table_name="agent_suggestions", row=row)
        return RepositoryResult(data=dict(row))

    async def finalize_action_terminal_and_project_history(self, *, command):
        result = await super().finalize_action_terminal_and_project_history(
            command=command
        )
        if (
            result.error is None
            and result.data is not None
            and command.suggestion_id is not None
        ):
            self._sync_suggestion_mirror(
                suggestion_id=command.suggestion_id,
                user_id=command.user_id,
            )
        return result


@dataclass(frozen=True, slots=True)
class WsAppHarness:
    client: TestClient
    handler_class: type[WSOrchestrationHandler]
    repository: _WsMockSuggestionRepository


WS_APP_OWNER_ID = "user_test"


@pytest.fixture(scope="function")
def ws_app_harness() -> Iterator[WsAppHarness]:
    register_active_settings_module(LOCAL_RUNTIME_SETTINGS_MODULE)

    cfg = importlib.import_module("pantaray_agents.config")
    deps = importlib.import_module("pantaray_agents.dependencies")
    local_app_module = importlib.import_module("pantaray_agents.app.local_app")
    orchestration_router_module = importlib.import_module(
        "pantaray_agents.orchestration.router"
    )
    handler_class = orchestration_router_module.WSOrchestrationHandler

    repo_patch = pytest.MonkeyPatch()
    repo_patch.setitem(cfg.settings, "use_mocks", True)
    repo_patch.setattr(deps, "is_mock_mode", lambda: True)
    # The relay reads runtime processes, so give this app its own migrated DB.
    runtime_root = Path(tempfile.mkdtemp(prefix="ws-app-"))
    db_path = runtime_root / "runtime.sqlite3"
    repo_patch.setenv("LOCAL_DB_PATH", str(db_path))
    repo_patch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "5000")
    repo_patch.setenv("LOCAL_ARTIFACT_ROOT", str(runtime_root / "artifacts"))
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=5000,
        migrations=load_default_migrations(),
    )
    repository = _WsMockSuggestionRepository()
    repo_patch.setattr(
        handler_class,
        "_get_suggestion_repository",
        AsyncMock(return_value=repository),
    )
    app = local_app_module.create_local_app()
    # This harness drives the app without its lifespan, so mint what the
    # runtime would have minted at startup.
    issue_local_api_token()
    register_logged_out_owner(WS_APP_OWNER_ID)

    client = TestClient(app)
    try:
        yield WsAppHarness(
            client=client,
            handler_class=handler_class,
            repository=repository,
        )
    finally:
        try:
            client.close()
        finally:
            repo_patch.undo()
            clear_active_settings_module()
            reset_logged_out_owner()
            clear_local_api_token()


def _ws_connect(harness: WsAppHarness, user_id: str = WS_APP_OWNER_ID):
    headers = {"Authorization": f"Bearer {read_local_api_token()}"}
    return harness.client.websocket_connect(
        f"/v1/agents/users/{user_id}/orchestrations", headers=headers
    )


def _send_ws(ws, event: str, data: dict[str, object]) -> None:
    ws.send_json({"event": event, "data": data})


def _receive_ws_json(ws, *, timeout_s: float) -> dict[str, object]:
    async def _recv() -> object:
        with anyio.fail_after(timeout_s):
            return await ws._send_rx.receive()  # type: ignore[attr-defined]

    message = ws.portal.call(_recv)  # type: ignore[attr-defined]
    ws._raise_on_close(message)  # type: ignore[attr-defined]
    if (
        isinstance(message, dict)
        and "text" in message
        and message.get("text") is not None
    ):
        text = str(message["text"])
    elif (
        isinstance(message, dict)
        and "bytes" in message
        and message.get("bytes") is not None
    ):
        text = bytes(message["bytes"]).decode("utf-8")
    else:
        raise AssertionError(f"Unexpected WS message shape: {message!r}")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise AssertionError(f"Expected JSON object, got: {parsed!r}")
    return parsed


def _drain_until(
    ws,
    stop_events: set[str],
    timeout_s: float = 5.0,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            msg = _receive_ws_json(ws, timeout_s=remaining)
        except TimeoutError:
            break
        messages.append(msg)
        if str(msg.get("event")) in stop_events:
            break
    return messages


def _event_list(msgs: list[dict[str, object]]) -> list[str]:
    return [str(m.get("event")) for m in msgs]


def _assert_has_event(msgs: list[dict[str, object]], event: str) -> None:
    events = _event_list(msgs)
    assert event in events, f"Expected event '{event}' in {events}"


def start_relayed_suggestion(
    harness: WsAppHarness,
    *,
    user_id: str,
    terminal: bool = True,
) -> SeededSuggestion:
    """Create the runtime Suggestion process the WS session should relay."""
    seeded = seed_live_suggestion_process(user_id=user_id)
    if terminal:
        seed_terminal_suggestion_row(
            harness.repository,
            user_id=user_id,
            suggestion_id=seeded.suggestion_id,
        )
    return seeded

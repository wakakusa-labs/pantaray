from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from starlette.testclient import TestClient
from tests.integration.local_artifact_test_support import build_test_llm_client
from tests.integration.local_runtime_ws_test_fakes import (
    LocalWsSuggestionRepository,
    load_test_prompt_config,
)

import pantaray_agents.config as runtime_config
import pantaray_agents.config_local_runtime as local_runtime_config
import pantaray_agents.dependencies as deps
import pantaray_agents.dependencies_memory as memory_deps
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.runtime.local_api_auth import read_local_api_token
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
)
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.orchestration.ws import suggestion_job_timing
from pantaray_agents.schema.agent.suggestion import (
    SuggestionStructuredOutput,
)

TEST_USER_ID = "user-real"
TEST_DB_BUSY_TIMEOUT_MS = 1_000
TEST_SUGGESTION_JOB_POLL_INTERVAL_SECONDS = 0.05
TEST_SHORT_RUNTIME_PARENT = Path("/tmp")
TEST_DESKTOP_SESSION_EXPIRES_AT = "2099-01-01T00:00:00Z"


def recent_two_iso(*, seconds_ago: int = 600) -> tuple[str, str]:
    base = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    first = base.isoformat().replace("+00:00", "Z")
    second = (base + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    return first, second


def receive_ws_json(ws, *, timeout_s: float) -> dict[str, Any]:
    async def _recv() -> object:
        with anyio.fail_after(timeout_s):
            return await ws._send_rx.receive()  # type: ignore[attr-defined]

    message = ws.portal.call(_recv)  # type: ignore[attr-defined]
    ws._raise_on_close(message)  # type: ignore[attr-defined]
    if isinstance(message, dict) and message.get("text") is not None:
        payload = str(message["text"])
    elif isinstance(message, dict) and message.get("bytes") is not None:
        payload = bytes(message["bytes"]).decode("utf-8")
    else:
        raise AssertionError(f"Unexpected websocket message: {message!r}")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise AssertionError(f"Expected websocket JSON object, got: {parsed!r}")
    return parsed


def drain_ws_until(
    ws,
    *,
    stop_events: set[str],
    timeout_s: float,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            message = receive_ws_json(ws, timeout_s=remaining)
        except TimeoutError:
            break
        messages.append(message)
        if str(message.get("event")) in stop_events:
            return messages
    raise AssertionError(f"Expected stop events {stop_events}, got {messages!r}")


def ws_connect(client: TestClient, *, user_id: str = TEST_USER_ID):
    return client.websocket_connect(
        f"/v1/agents/users/{user_id}/orchestrations",
        headers={"Authorization": f"Bearer {read_local_api_token()}"},
    )


@dataclass(frozen=True)
class LocalRuntimeWsHarness:
    client: TestClient
    db_path: Path
    suggestion_repository: LocalWsSuggestionRepository


def _insert_user_if_missing(db_path: Path, *, user_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id,
                ui_language,
                created_at,
                updated_at
            ) VALUES (?, 'ja', '2026-03-30T00:00:00Z', '2026-03-30T00:00:00Z')
            """,
            (user_id,),
        )


def _pin_pipeline_dependency_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dependency_module: Any,
) -> None:
    sys.modules["pantaray_agents.dependencies"] = dependency_module

    from pantaray_agents.local_runtime.runtime import (
        activity_local_executor as activity_local_executor_module,
    )
    from pantaray_agents.orchestration.ws import base as ws_base_module
    from pantaray_agents.orchestration.ws import handler as ws_handler_module
    from pantaray_agents.tasks.internal_jobs import suggestion as suggestion_job_module

    target_modules = (
        activity_local_executor_module,
        ws_base_module,
        ws_handler_module,
        suggestion_job_module,
    )
    for module in target_modules:
        monkeypatch.setattr(module, "deps", dependency_module, raising=False)
    monkeypatch.setattr(memory_deps, "deps", dependency_module)


@pytest.fixture
def short_runtime_root() -> Iterator[Path]:
    """Use a short root so the real Unix control socket fits macOS limits."""
    with TemporaryDirectory(
        prefix="pantaray-ws-",
        dir=TEST_SHORT_RUNTIME_PARENT,
    ) as directory:
        yield Path(directory)


@pytest.fixture
def local_runtime_ws_harness(
    short_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[LocalRuntimeWsHarness]:
    db_path = short_runtime_root / "runtime.sqlite3"
    artifact_root = short_runtime_root / "artifacts"
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(TEST_DB_BUSY_TIMEOUT_MS))
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv(
        "LOCAL_RUNTIME_LOCK_PATH",
        str(short_runtime_root / "runtime.lock"),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    monkeypatch.setitem(local_runtime_config.settings, "use_mocks", False)
    monkeypatch.setitem(runtime_config.settings, "use_mocks", False)
    monkeypatch.setattr(
        suggestion_job_timing,
        "SUGGESTION_JOB_POLL_INTERVAL_SECONDS",
        TEST_SUGGESTION_JOB_POLL_INTERVAL_SECONDS,
    )
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    from pantaray_agents.local_runtime.suggestion_state.repository import (
        LocalSuggestionStateRepository,
    )

    def _build_local_suggestion_state_repository() -> LocalSuggestionStateRepository:
        return LocalSuggestionStateRepository(
            db_path=str(db_path),
            busy_timeout_ms=TEST_DB_BUSY_TIMEOUT_MS,
        )

    suggestion_repository = LocalWsSuggestionRepository(db_path=db_path)
    activity_llm = build_test_llm_client()
    activity_llm.responses["default"] = "Focused work in Finder and documents."
    suggestion_llm = build_test_llm_client()
    suggestion_llm.set_next_response(
        SuggestionStructuredOutput.model_validate(
            {
                "has_suggestion": True,
                "interaction_contract": "action_offer",
                "key_point": "The local runtime verification is half done.",
                "deliverable": "The rest of the local runtime verification.",
                "agent_session": False,
                "suggestion_summary": "Focused local runtime verification.",
                "target_context": {
                    "organization_name": None,
                    "project_name": None,
                },
            }
        ).model_dump()
    )

    # The other two lens runs find nothing; the selector picks the candidate.
    nothing = SuggestionStructuredOutput.model_validate(
        {
            "has_suggestion": False,
            "interaction_contract": None,
            "key_point": "",
            "agent_session": None,
            "suggestion_summary": None,
            "target_context": None,
        }
    ).model_dump()
    suggestion_llm.queued_responses = [
        nothing,
        nothing,
        {"tool_id": "select_suggestion", "args": {"choice": 1, "reason": "it"}},
    ]
    # The writer's call is the next plain-text call on this client.
    suggestion_llm.responses["default"] = (
        "Continue the focused local runtime verification."
    )
    monkeypatch.setattr(deps, "get_llm_client", lambda: activity_llm, raising=False)
    monkeypatch.setattr(
        deps,
        "get_suggestion_repository",
        AsyncMock(return_value=suggestion_repository),
        raising=False,
    )
    monkeypatch.setattr(
        deps,
        "get_local_suggestion_state_repository",
        _build_local_suggestion_state_repository,
        raising=False,
    )

    from pantaray_agents.agents.suggestion_agent import SuggestionAgent

    monkeypatch.setattr(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        load_test_prompt_config,
    )
    suggestion_agent = SuggestionAgent(
        config={"mode": "local_runtime", "llm_client": suggestion_llm},
        repository=suggestion_repository,
        research_tools=build_mock_suggestion_research_tools(),
        stable_memory=SuggestionStableMemoryContext(
            prompt="No stable memory roots are available.",
            has_facts=False,
            has_insights=False,
        ),
    )
    suggestion_agent.client = suggestion_llm
    monkeypatch.setattr(
        deps,
        "get_suggestion_agent",
        AsyncMock(return_value=suggestion_agent),
        raising=False,
    )
    _pin_pipeline_dependency_module(monkeypatch, dependency_module=deps)

    from pantaray_agents.app.local_app import create_local_app
    from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
    from pantaray_agents.routers import activity as activity_router_module

    app = create_local_app()

    async def _fake_agents_user_id() -> str:
        return TEST_USER_ID

    app.dependency_overrides[activity_router_module.get_current_user_id_from_token] = (
        _fake_agents_user_id
    )
    monkeypatch.setattr(
        WSOrchestrationHandler,
        "_get_suggestion_repository",
        AsyncMock(return_value=suggestion_repository),
    )

    with TestClient(app) as client:
        _insert_user_if_missing(db_path, user_id=TEST_USER_ID)
        import_desktop_session(
            db_path=db_path,
            busy_timeout_ms=TEST_DB_BUSY_TIMEOUT_MS,
            user_id=TEST_USER_ID,
            desktop_access_token="test.desktop.session",
            expires_at=TEST_DESKTOP_SESSION_EXPIRES_AT,
            session_version="1",
        )
        # The worker only claims once Electron main has applied `configure`.
        mark_configured()
        yield LocalRuntimeWsHarness(
            client=client,
            db_path=db_path,
            suggestion_repository=suggestion_repository,
        )

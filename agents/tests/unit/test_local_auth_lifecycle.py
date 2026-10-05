from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from types import ModuleType

import pytest
from starlette.testclient import TestClient

from pantaray_agents.local_runtime.runtime.action_message_models import (
    StartedActionMessageResult,
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
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.routers import action_messages

LOCAL_OWNER_ID = "local-owner-1"
MISSING_TOKEN = "missing"
INVALID_TOKEN = "invalid"
NON_ASCII_TOKEN = "non-ascii"
ISSUED_TOKEN = "issued"


@pytest.fixture
def local_app_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """The local app whose runtime start only mints the local API token."""
    from pantaray_agents.app import local_app

    monkeypatch.setattr(
        local_app, "start_local_runtime_if_enabled", issue_local_api_token
    )
    monkeypatch.setattr(
        local_app, "stop_local_runtime_if_enabled", clear_local_api_token
    )
    register_logged_out_owner(LOCAL_OWNER_ID)
    try:
        yield local_app
    finally:
        reset_logged_out_owner()
        clear_local_api_token()


def authorization(token_kind: str) -> dict[str, str | bytes]:
    if token_kind == MISSING_TOKEN:
        return {}
    if token_kind == INVALID_TOKEN:
        return {"Authorization": "Bearer not-the-issued-local-api-token"}
    if token_kind == NON_ASCII_TOKEN:
        # Raw bytes a non-httpx client can send; Starlette decodes them latin-1.
        return {"Authorization": "Bearer caf\u00e9".encode("latin-1")}
    return {"Authorization": f"Bearer {read_local_api_token()}"}


def record_submission(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop at the submission boundary while recording which owner reached it."""
    reached: list[str] = []

    def submit(
        command: action_messages.SubmitActionMessageCommand,
    ) -> StartedActionMessageResult:
        reached.append(command.user_id)
        return StartedActionMessageResult(
            disposition="started",
            action_id="action-1",
            message_id=command.message.message_id,
            user_step_id="step-1",
            action_status="processing",
            process_id="process-1",
            job_id="job-1",
            inserted=True,
        )

    monkeypatch.setattr(action_messages, "submit_canonical_action_message", submit)
    return reached


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("token_kind", "path_user_id", "expected", "expected_submissions"),
    [
        (MISSING_TOKEN, LOCAL_OWNER_ID, 401, []),
        (INVALID_TOKEN, LOCAL_OWNER_ID, 401, []),
        (NON_ASCII_TOKEN, LOCAL_OWNER_ID, 401, []),
        (ISSUED_TOKEN, "another-owner", 403, []),
        (ISSUED_TOKEN, LOCAL_OWNER_ID, 200, [LOCAL_OWNER_ID]),
    ],
)
def test_local_messages_require_the_issued_token_and_the_current_owner(
    monkeypatch: pytest.MonkeyPatch,
    local_app_module: ModuleType,
    resume: bool,
    token_kind: str,
    path_user_id: str,
    expected: int,
    expected_submissions: list[str],
) -> None:
    submissions = record_submission(monkeypatch)
    path = f"/v1/agents/users/{path_user_id}/actions/"
    body = (
        {"message_id": "message-1"}
        if resume
        else {
            "target": {"kind": "new"},
            "message": {
                "version": 1,
                "message_id": "message-1",
                "content": "hello",
                "images": [],
            },
        }
    )
    with TestClient(local_app_module.create_local_app()) as client:
        response = client.post(
            path + ("action-1/resume" if resume else "messages"),
            json=body,
            headers=authorization(token_kind),
        )
    assert response.status_code == expected
    assert submissions == expected_submissions


def test_local_mock_mode_does_not_bypass_local_api_token_authentication(
    monkeypatch: pytest.MonkeyPatch,
    local_app_module: ModuleType,
) -> None:
    monkeypatch.setitem(local_app_module.settings, "use_mocks", True)
    with TestClient(local_app_module.create_local_app()) as client:
        assert client.get("/api/agent/history").status_code == 401


def test_local_app_starts_without_supabase_or_cloud_configuration(
    monkeypatch: pytest.MonkeyPatch,
    local_app_module: ModuleType,
) -> None:
    for name in (
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_JWT_AUD",
        "SUPABASE_JWT_ISS",
        "LLM_PROXY_URL",
        "WEB_TOOLS_PROXY_URL",
    ):
        monkeypatch.delenv(name)
    with TestClient(local_app_module.create_local_app()) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("fail_start", [False, True])
def test_local_lifespan_stops_the_runtime_even_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
    local_app_module: ModuleType,
    fail_start: bool,
) -> None:
    events: list[str] = []

    def start() -> None:
        events.append("start")
        if fail_start:
            raise RuntimeError("start failed")

    monkeypatch.setattr(local_app_module, "start_local_runtime_if_enabled", start)
    monkeypatch.setattr(
        local_app_module, "stop_local_runtime_if_enabled", lambda: events.append("stop")
    )
    app = local_app_module.create_local_app()
    if fail_start:
        with pytest.raises(RuntimeError, match="start failed"), TestClient(app):
            pass
    else:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    assert events == ["start", "stop"]


def test_local_lifespan_logs_startup_failure_without_its_message(
    monkeypatch: pytest.MonkeyPatch,
    local_app_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_text = "api_key=sk-private-value"

    def start() -> None:
        raise MigrationError(f"LOCAL_RUNTIME_ALREADY_ACTIVE: {private_text}")

    monkeypatch.setattr(local_app_module, "start_local_runtime_if_enabled", start)
    app = local_app_module.create_local_app()
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(MigrationError),
        TestClient(app),
    ):
        pass

    startup_failures = [
        json.loads(record.getMessage())
        for record in caplog.records
        if "LOCAL_RUNTIME_STARTUP_FAILED" in record.getMessage()
    ]
    assert [
        failure["exception_chain"][0]["error_class"] for failure in startup_failures
    ] == ["MigrationError"]
    assert private_text not in caplog.text

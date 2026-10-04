from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ALLOWED_TOOL_IDS,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    capture_screen as capture_screen_module,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    CompletedToolControl,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import CAPTURE_SCREEN_TOOL
from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    SCREEN_CAPTURE_REQUESTED_EVENT,
    ScreenCaptureBroker,
    ScreenCaptureCaptured,
    ScreenCaptureRefusalCode,
    ScreenCaptureRefused,
)
from pantaray_agents.local_runtime.tooling import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    ToolInvocationStartInput,
    create_capability_grant,
    load_approval_session_by_request,
    record_tool_invocation_start,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    apply_approval_decision,
)
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.local_runtime.tooling.repository.action_approval_modes import (
    set_action_approval_mode,
)

CAPTURE_UUID = "6a6f1b2c-6e34-4d21-9a11-0f2c3d4e5f60"
STORAGE_PATH = f"user-1/2026-09-08/{CAPTURE_UUID}.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"screen pixels"
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
REQUESTED_AT = "2026-09-08T00:00:00Z"
TOOL_REQUEST_ID = "action-1:step-1:1"
APP_NAME = "Finder"


def _state(context: object) -> dict[str, object]:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at=REQUESTED_AT,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["manifest_id"] = context.manifest_id  # type: ignore[attr-defined]
    state["execution_session_id"] = context.execution_session_id  # type: ignore[attr-defined]
    return state


def _bootstrap(tmp_path: Path) -> tuple[Path, object]:
    return _bootstrap_runtime_db(
        tmp_path,
        allowed_tool_ids=(*BROKER_ALLOWED_TOOL_IDS, CAPTURE_SCREEN_TOOL.tool_id),
    )


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_path: Path,
    artifact_root: Path | None = None,
) -> ScreenCaptureBroker:
    """Point the tool at this test's database, artifact root, and broker."""

    broker = ScreenCaptureBroker()
    monkeypatch.setattr(
        capture_screen_module,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(capture_screen_module, "screen_capture_broker", lambda: broker)
    monkeypatch.setattr(
        capture_screen_module,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )
    if artifact_root is not None:
        monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    return broker


def _write_capture(artifact_root: Path, payload: bytes = PNG_BYTES) -> None:
    target = artifact_root / "generated" / "images" / STORAGE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _grant_screen_capture(db_path: Path) -> None:
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-capture",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("screen_capture",),
            created_at=REQUESTED_AT,
            updated_at=REQUESTED_AT,
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=1_000,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-screen-capture",
            user_id="user-1",
            preference_id="pref-capture",
            capability="screen_capture",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at=REQUESTED_AT,
        ),
    )


def _capture_request_events(db_path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM process_events WHERE event_name = ?",
            (SCREEN_CAPTURE_REQUESTED_EVENT,),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


async def _preflight(state: dict[str, object]):
    return await capture_screen_module.run_capture_screen_preflight(
        step_id="step-1",
        tool_def=CAPTURE_SCREEN_TOOL,
        state=state,
        app_name=APP_NAME,
        tool_request_id=TOOL_REQUEST_ID,
        requested_at=REQUESTED_AT,
    )


def _record_invocation(
    db_path: Path,
    context: object,
    *,
    invocation_id: str,
) -> str:
    """The audit row the Action runtime writes before a capture runs."""

    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=TOOL_REQUEST_ID,
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id=CAPTURE_SCREEN_TOOL.tool_id,
            manifest_id=context.manifest_id,  # type: ignore[attr-defined]
            execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
            cwd=".",
            timeout_ms=15_000,
            intent_class="screen_capture",
            network_policy="deny",
            command_summary_json={
                "summary_kind": "screen_capture",
                "app_name": APP_NAME,
            },
            capability_snapshot_json={"required_capabilities": ["screen_capture"]},
            request_json={},
            status="running",
            started_at=REQUESTED_AT,
        ),
    )
    return invocation_id


async def _run(
    state: dict[str, object],
    db_path: Path,
    context: object,
    *,
    invocation_id: str = "invocation-1",
):
    return await capture_screen_module.run_capture_screen_tool(
        step_id="step-1",
        tool_def=CAPTURE_SCREEN_TOOL,
        state=state,
        app_name=APP_NAME,
        tool_request_id=TOOL_REQUEST_ID,
        tool_invocation_id=_record_invocation(
            db_path, context, invocation_id=invocation_id
        ),
        requested_at=REQUESTED_AT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action_mode", [None, "always_allow"])
async def test_prompt_each_time_pauses_before_any_capture_is_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action_mode: ApprovalMode | None
) -> None:
    db_path, context = _bootstrap(tmp_path)
    if action_mode is not None:
        set_action_approval_mode(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            approval_mode=action_mode,
            updated_at=REQUESTED_AT,
        )
    _bind(monkeypatch, db_path=db_path)

    preparation = await _preflight(_state(context))

    assert isinstance(preparation.control, ApprovalRequiredToolControl)
    assert preparation.result.status == "processing"
    # The user approves the named app, which is exactly what Electron is asked for.
    pending = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id=TOOL_REQUEST_ID,
    )
    assert pending is not None
    assert pending.command_summary_json["app_name"] == APP_NAME
    # The capture is what needs consent, so nothing may have been asked of the
    # desktop client before the user answered.
    assert _capture_request_events(db_path) == []


@pytest.mark.asyncio
async def test_workspace_grant_alone_still_prompts_for_a_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="screen_capture")
    _bind(monkeypatch, db_path=db_path)

    preparation = await _preflight(_state(context))

    assert isinstance(preparation.control, ApprovalRequiredToolControl)


@pytest.mark.asyncio
async def test_denied_approval_reports_the_call_as_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _bind(monkeypatch, db_path=db_path)
    state = _state(context)
    await _preflight(state)
    _decide(db_path, decision="denied")

    preparation = await _preflight(state)

    assert isinstance(preparation.control, ApprovalDeniedToolControl)
    assert preparation.result.output["executed"] is False
    assert _capture_request_events(db_path) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action_mode", [None, "prompt_each_time"])
async def test_screen_capture_grant_authorizes_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action_mode: ApprovalMode | None
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    if action_mode is not None:
        set_action_approval_mode(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            approval_mode=action_mode,
            updated_at=REQUESTED_AT,
        )
    _bind(monkeypatch, db_path=db_path)

    preparation = await _preflight(_state(context))

    assert isinstance(preparation.control, CompletedToolControl)


@pytest.mark.asyncio
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_captured_image_becomes_a_verified_tool_attachment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    artifact_root = tmp_path / "artifacts"
    broker = _bind(monkeypatch, db_path=db_path, artifact_root=artifact_root)
    _write_capture(artifact_root)

    result = await _answer_with(
        broker,
        db_path=db_path,
        run=_run(_state(context), db_path, context),
        outcome=_captured(),
    )

    assert result.status == "success"
    assert result.attachments is not None
    attachment = result.attachments[0]
    assert attachment["ref"].startswith("tool_attachment:")
    assert attachment["source_kind"] == "local_image_blob"
    assert attachment["storage_path"] == STORAGE_PATH
    assert attachment["sha256"] == PNG_SHA256
    output = result.output
    assert isinstance(output, dict)
    assert output["captured_at"] == "2026-09-27T06:50+09:00"
    assert output["message"] == (
        f"Captured the window of Finder at 2026-09-27T06:50+09:00. {attachment['ref']}"
    )
    # The durable output is the only per-step record the conversation read model has.
    assert output["attachments"] == [
        {
            "type": "file",
            "source_kind": "local_image_blob",
            "mime_type": "image/png",
            "path": attachment["display_path"],
            "storage_path": STORAGE_PATH,
            "ref": attachment["ref"],
            "byte_size": len(PNG_BYTES),
        }
    ]
    # The bytes ride the file-input path; durable history that memory tools read
    # must never carry the image itself.
    assert "base64" not in json.dumps(output)
    assert PNG_BYTES.decode("latin-1") not in json.dumps(output)


@pytest.mark.parametrize("payload", (None, PNG_BYTES + b"replaced"))
@pytest.mark.asyncio
async def test_a_file_that_is_not_what_was_reported_fails_the_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: bytes | None
) -> None:
    """Electron's sha256 and byte_size are claims; the file on disk decides."""

    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    broker = _bind(monkeypatch, db_path=db_path, artifact_root=artifact_root)
    if payload is not None:
        _write_capture(artifact_root, payload=payload)

    result = await _answer_with(
        broker,
        db_path=db_path,
        run=_run(_state(context), db_path, context),
        outcome=_captured(),
    )

    assert result.status == "error"
    assert result.attachments is None
    assert result.output["error"]["error_type"] == "CAPTURE_INTEGRITY_FAILED"


@pytest.mark.asyncio
async def test_refusal_reaches_the_model_with_its_code_and_no_attachment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    broker = _bind(monkeypatch, db_path=db_path)

    result = await _answer_with(
        broker,
        db_path=db_path,
        run=_run(_state(context), db_path, context),
        outcome=ScreenCaptureRefused(
            code="CAPTURE_REFUSED_PASSWORD_MANAGER",
            details={"app_name": "1Password"},
        ),
    )

    assert result.status == "error"
    assert result.attachments is None
    error = result.output["error"]
    assert error["error_type"] == "CAPTURE_REFUSED_PASSWORD_MANAGER"
    assert error["details"] == {"app_name": "1Password"}


def test_every_refusal_code_carries_its_own_guidance() -> None:
    """A code without a message reaches the model as a bare KeyError instead."""

    assert set(capture_screen_module._REFUSAL_MESSAGES) == set(
        get_args(ScreenCaptureRefusalCode)
    )


@pytest.mark.asyncio
async def test_unanswered_request_times_out_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    _bind(monkeypatch, db_path=db_path)
    monkeypatch.setattr(capture_screen_module, "_CAPTURE_TIMEOUT_SECONDS", 0.01)

    result = await _run(_state(context), db_path, context)

    assert result.output["error"]["error_type"] == "CAPTURE_TIMED_OUT"
    event = _capture_request_events(db_path)[0]
    assert event["capture_request_id"]
    assert event["app_name"] == APP_NAME


@pytest.mark.asyncio
async def test_an_approved_capture_is_spent_and_cannot_run_a_second_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One consent is one capture, even if the Action replays the same step."""

    db_path, context = _bootstrap(tmp_path)
    _grant_screen_capture(db_path)
    artifact_root = tmp_path / "artifacts"
    broker = _bind(monkeypatch, db_path=db_path, artifact_root=artifact_root)
    _write_capture(artifact_root)

    first = await _answer_with(
        broker,
        db_path=db_path,
        run=_run(_state(context), db_path, context),
        outcome=_captured(),
    )
    assert first.status == "success"
    spent = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id=TOOL_REQUEST_ID,
    )
    assert spent is not None
    assert spent.tool_invocation_id == "invocation-1"
    assert spent.claimed_at == REQUESTED_AT

    replayed = await _run(
        _state(context), db_path, context, invocation_id="invocation-2"
    )

    assert replayed.status == "error"
    assert replayed.output["error"]["error_type"] == "CAPTURE_APPROVAL_STATE_CHANGED"
    # The replay must not reach the desktop client at all.
    assert len(_capture_request_events(db_path)) == 1


@pytest.mark.asyncio
async def test_consent_withdrawn_between_preflight_and_call_fails_without_capturing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, context = _bootstrap(tmp_path)
    _bind(monkeypatch, db_path=db_path)
    state = _state(context)
    await _preflight(state)
    _decide(db_path, decision="denied")

    result = await _run(state, db_path, context)

    assert result.status == "error"
    assert result.output["error"]["error_type"] == "CAPTURE_APPROVAL_STATE_CHANGED"
    assert _capture_request_events(db_path) == []


def _captured() -> ScreenCaptureCaptured:
    return ScreenCaptureCaptured(
        storage_path=STORAGE_PATH,
        mime_type="image/png",
        byte_size=len(PNG_BYTES),
        sha256=PNG_SHA256,
        width_px=1440,
        height_px=900,
        app_name="Finder",
        captured_at=datetime(2026, 9, 26, 21, 50, 1, tzinfo=UTC),
    )


async def _answer_with(
    broker: ScreenCaptureBroker,
    *,
    db_path: Path,
    run,
    outcome,
):
    """Run the tool and answer the request it announces, as Electron would."""

    task = asyncio.ensure_future(run)
    for _ in range(200):
        await asyncio.sleep(0.005)
        events = _capture_request_events(db_path)
        if not events:
            continue
        answered = broker.answer(
            capture_request_id=str(events[0]["capture_request_id"]),
            user_id="user-1",
            outcome=outcome,
        )
        if answered == "accepted":
            break
    return await task


def _decide(db_path: Path, *, decision: str) -> None:
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id=TOOL_REQUEST_ID,
        user_id="user-1",
        action_id="action-1",
        approval_session_id=None,
        decision=decision,  # type: ignore[arg-type]
        decided_at="2026-09-08T00:00:02Z",
    )

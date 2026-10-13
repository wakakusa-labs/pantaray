import base64
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.action_conversation.tool_output import (
    ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES,
    ACTION_TOOL_OUTPUT_MAX_READ_BYTES,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    ActionStepToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.routers.local.registry import register_local_routers
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import (
    FormalToolStepOutput,
    serialize_json_tool_output,
)

from .resource_recovery_test_support import bootstrap_runtime_db

TIMESTAMP = "2026-08-30T00:00:00Z"


def _persist_terminal_step(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    step_id: str,
    step_number: int,
    output: object,
    step_name: str = "tool::bash",
) -> None:
    finalized = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ToolResultFinalizationRequest(
            owner=ActionStepToolResultOwner(
                manifest_id=context.manifest_id,
                action_id="action-1",
                user_id="user-1",
                step_id=step_id,
            ),
            output=output,
        ),
    )
    envelope = FormalToolStepOutput(
        schema_version=1,
        status="success",
        output=finalized.output,
        output_storage_kind=finalized.storage_kind,
        output_owner_kind=finalized.owner_kind,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_action_steps(
                   step_id,action_id,user_id,step_number,step_type,step_name,
                   status,thinking,tool_args,tool_output,started_at,completed_at,
                   created_at
               ) VALUES (?, 'action-1', 'user-1', ?, 'tool_execution', ?,
                         'success', 'private thinking', '{"secret":"input"}',
                         ?, ?, ?, ?)""",
            (
                step_id,
                step_number,
                step_name,
                envelope.model_dump_json(),
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
            ),
        )


def test_tool_output_route_pages_owned_text_and_classifies_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, raw_context = bootstrap_runtime_db(tmp_path)
    assert isinstance(raw_context, ActionExecutionContext)
    raw_output: JSONValue = (
        "e\u0301" + "a" * (ACTION_TOOL_OUTPUT_MAX_READ_BYTES - 6) + "🙂"
    )
    for step_number, step_id, output, step_name in (
        (1, "step-text", raw_output, "tool::bash"),
        (2, "step-binary", b"\x00private binary", "tool::bash"),
        (3, "step-empty", None, "tool::bash"),
        (4, "step-hidden", None, "tool::submit_final_answer"),
    ):
        _persist_terminal_step(
            db_path=db_path,
            context=raw_context,
            step_id=step_id,
            step_number=step_number,
            output=output,
            step_name=step_name,
        )

    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: "user-1"
    client = TestClient(app)
    output_url = "/v1/agents/users/user-1/actions/action-1/steps/step-text/output"

    pages: list[str] = []
    cursor = None
    for _ in range(
        ACTION_TOOL_OUTPUT_MAX_READ_BYTES // ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES - 1
    ):
        params: dict[str, int | str] = {
            "limit_bytes": ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get(output_url, params=params)
        body = response.json()
        pages.append(body["content"])
        cursor = body["next_cursor"]
        assert cursor is not None
    assert isinstance(cursor, str)
    cursor_payload = json.loads(
        base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    )
    cursor_payload["content_sha256"] = "0" * 64
    forged_cursor = (
        base64.urlsafe_b64encode(
            json.dumps(cursor_payload, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    assert client.get(output_url, params={"cursor": forged_cursor}).status_code == 422
    response = client.get(
        output_url,
        params={
            "cursor": cursor,
            "limit_bytes": ACTION_TOOL_OUTPUT_MAX_PAGE_BYTES - 1,
        },
    )
    body = response.json()
    pages.append(body["content"])
    cursor = body["next_cursor"]
    assert cursor is not None
    response = client.get(output_url, params={"cursor": cursor})
    assert response.json() == {
        "content": "",
        "next_cursor": None,
        "truncated": True,
        "unavailable_reason": None,
    }
    expected = serialize_json_tool_output(raw_output).encode("utf-8")
    assert "".join(pages) == expected[:ACTION_TOOL_OUTPUT_MAX_READ_BYTES].decode(
        "utf-8", errors="ignore"
    )
    assert "private thinking" not in "".join(pages)
    assert '"secret":"input"' not in "".join(pages)
    text_path = next(raw_context.tool_results_path.rglob("*.txt"))
    with text_path.open("r+b") as handle:
        handle.write(b"\x80")
    assert client.get(output_url).status_code == 500

    for step_id, reason in (("step-binary", "binary"), ("step-empty", "no_output")):
        path = f"/v1/agents/users/user-1/actions/action-1/steps/{step_id}/output"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "content": "",
            "next_cursor": None,
            "truncated": False,
            "unavailable_reason": reason,
        }
        if reason == "binary":
            binary_path = next(raw_context.tool_results_path.rglob("*.bin"))
            binary_path.unlink()
            assert client.get(path).status_code == 500
            binary_path.write_bytes(b"size mismatch")
            assert client.get(path).status_code == 500
            binary_path.unlink()
            os.mkfifo(binary_path)
            assert client.get(path).status_code == 500
    for path in (
        "/v1/agents/users/user-1/actions/action-1/steps/step-hidden/output",
        "/v1/agents/users/user-1/actions/other/steps/step-text/output",
    ):
        assert client.get(path).status_code == 404
    assert (
        client.get(
            "/v1/agents/users/user-2/actions/action-1/steps/step-text/output"
        ).status_code
        == 403
    )
    assert client.get(output_url, params={"cursor": "0"}).status_code == 422

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantaray_agents.action_status import build_finalize_action_terminal_command
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.assistant_message import (
    project_persisted_assistant_messages,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    serialize_memory_draft,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobPreparation,
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_REQUEST_TEXT,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    ActionTerminalRepository,
)
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
    welcome_suggestion_id,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.suggestion_state.public_process_events import (
    append_public_process_event,
)
from pantaray_agents.local_runtime.tooling.agent_experience.action_history import (
    ActionTurnWindow,
    AgentExperienceActionHistoryTools,
)
from pantaray_agents.routers.local.registry import register_local_routers
from pantaray_agents.schema.action_conversation import (
    ActionConversationPage,
    UserEntry,
)
from pantaray_agents.schema.agent.action import RuntimeStateCheckpointPayload
from pantaray_agents.tasks.types import ActionJobRuntimePayload

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
PRIVATE_THINKING = "private-thinking-marker"
PRIVATE_TOOL_INPUT = "private-tool-input-marker"
VISIBLE_TOOL_SUBJECT = "src/app.py"
MESSAGES_PATH = f"/v1/agents/users/{USER_ID}/actions/messages"


@pytest.fixture
def runtime_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    _import_session(db_path)

    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER_ID
    with TestClient(app) as client:
        yield client, db_path


def _import_session(db_path: Path) -> None:
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        desktop_access_token="header.payload.signature",
        expires_at="2099-01-01T00:00:00Z",
        session_version="1",
    )
    # The worker only claims once Electron main has applied `configure`.
    mark_configured()


def _message(
    message_id: str,
    content: str,
    *,
    action_id: str | None = None,
    expected_process_id: str | None = None,
) -> dict[str, object]:
    return {
        "target": (
            {"kind": "new"}
            if action_id is None
            else {
                "kind": "existing",
                "action_id": action_id,
                "expected_process_id": expected_process_id,
            }
        ),
        "message": {
            "version": 1,
            "message_id": message_id,
            "content": content,
            "images": [],
            "language": "ja",
        },
    }


def _claim_and_prepare(
    db_path: Path, *, claimed_by: str
) -> tuple[ActionJobRuntimePayload, ActionJobPreparation]:
    payload = claim_next_pending_action_job(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id=USER_ID,
        claimed_by=claimed_by,
    )
    assert payload is not None
    preparation = ActionJobRuntimeRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).prepare_execution(
        payload=payload,
        started_at=now_utc_iso(),
    )
    assert preparation.skip_outcome is None
    return payload, preparation


def _read_state(
    client: TestClient, action_id: str, cursor: str | None = None
) -> dict[str, object]:
    params: dict[str, object] = {"limit": 100}
    if cursor is not None:
        params["cursor"] = cursor
    response = client.get(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/state", params=params
    )
    assert response.status_code == 200
    return response.json()


def _insert_reply_source(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            """INSERT INTO agent_suggestions(
                suggestion_id,user_id,status,has_suggestion,interaction_contract,
                answer,created_at,updated_at)
            VALUES ('comment-1',?,'success',1,'message_only','**先ほどの発言**',?,?)""",
            (USER_ID, now_utc_iso(), now_utc_iso()),
        )

        for name, data in (
            ("suggestion_chunk", {"content": "**先ほどの発言**"}),
            (
                "process_completed",
                {
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "message_only",
                },
            ),
        ):
            append_public_process_event(
                connection=connection,
                event_id=f"comment-{name}",
                suggestion_id="comment-1",
                user_id=USER_ID,
                action_id=None,
                event_name=name,
                payload={"data": data, "meta": {"suggestion_id": "comment-1"}},
                created_at=now_utc_iso(),
            )


def test_assistant_message_is_public_history_before_the_first_user(
    runtime_client: tuple[TestClient, Path],
) -> None:
    client, db_path = runtime_client
    _insert_reply_source(db_path)
    request = _message("reply", "その件を詳しく")
    request["target"] = {"kind": "new", "reply_to_suggestion_id": "comment-1"}
    response = client.post(MESSAGES_PATH, json=request)
    assert response.status_code == 200, response.text
    started = response.json()
    _, preparation = _claim_and_prepare(db_path, claimed_by="worker:reply")
    assert [
        message.content for message in preparation.context.preceding_assistant_messages
    ] == ["**先ほどの発言**"]
    state = _read_state(client, started["action_id"])
    page = ActionConversationPage.model_validate_json(json.dumps(state))
    assert page.action.suggestion_id is None
    assert page.next_cursor is None
    assert [
        (entry.step_kind, entry.step_number, entry.content)
        for entry in page.runs[0].entries
    ] == [
        ("user", 2, "その件を詳しく"),
        ("assistant", 1, "**先ほどの発言**"),
    ]
    assert client.post(MESSAGES_PATH, json=request).json() == {
        **started,
        "action_status": "processing",
    }
    bootstrap = client.get("/api/agent/history/comment-1/overlay-bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["snapshot"]["actionId"] == started["action_id"]
    assert bootstrap.json()["snapshot"]["reactionState"] is None
    history = client.get("/api/agent/history", params={"limit": 25, "status": "all"})
    assert [(item["kind"], item["action_id"]) for item in history.json()["items"]] == [
        ("conversation", started["action_id"])
    ]
    search = client.get(
        "/api/agent/history", params={"search_text": "先ほどの発言", "status": "all"}
    )
    assert search.status_code == 200, search.text
    assert [item["action_id"] for item in search.json()["items"]] == [
        started["action_id"]
    ]
    assert (
        client.get(
            "/api/agent/history", params={"search_text": "unmatched", "status": "all"}
        ).json()["items"]
        == []
    )
    duplicate = _message("another-reply", "別の返信")
    duplicate["target"] = request["target"]
    assert client.post(MESSAGES_PATH, json=duplicate).status_code == 409


def test_a_reply_to_the_welcome_continues_it_as_an_ordinary_conversation(
    runtime_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import suggestion as suggestion_router

    client, db_path = runtime_client
    welcome_path = f"/v1/agents/users/{USER_ID}/suggestions/welcome"
    welcome = "まずはあなたの仕事を理解するところから始めます。"
    # Until the desktop app's session is open, nothing could show the welcome.
    refused = client.post(welcome_path, json={"answer": welcome})
    assert refused.status_code == 503, refused.text
    assert client.get("/api/agent/history", params={"limit": 25}).json()["items"] == []
    monkeypatch.setattr(
        suggestion_router, "owner_has_deliverable_session", lambda _: True
    )
    created = client.post(welcome_path, json={"answer": welcome})
    assert created.status_code == 200, created.text
    assert created.json() == {"created": True}
    # The welcome is data of its own, so a repeat greets no one.
    assert client.post(welcome_path, json={"answer": "again"}).json() == {
        "created": False
    }
    # An unanswered welcome is listed in history instead of breaking it.
    history = client.get("/api/agent/history", params={"limit": 25, "status": "all"})
    assert history.status_code == 200, history.text
    assert [
        (item["kind"], item.get("suggestion_id")) for item in history.json()["items"]
    ] == [("suggestion", welcome_suggestion_id(USER_ID))]

    request = _message("welcome-reply", "今開いている資料を要約して")
    request["target"] = {
        "kind": "new",
        "reply_to_suggestion_id": welcome_suggestion_id(USER_ID),
    }
    response = client.post(MESSAGES_PATH, json=request)
    assert response.status_code == 200, response.text
    _, preparation = _claim_and_prepare(db_path, claimed_by="worker:welcome")
    assert [
        message.content for message in preparation.context.preceding_assistant_messages
    ] == [welcome]


def test_project_refs_are_stored_projected_and_given_to_the_model(
    runtime_client: tuple[TestClient, Path],
) -> None:
    client, db_path = runtime_client
    request = _message("message-refs", "\N{ROCKET} Check Demo App")
    message = request["message"]
    assert isinstance(message, dict)
    message["project_refs"] = [
        {
            "project_id": "project-1",
            "display_name": "Demo App",
            "paths": ["/workspace/demo-app"],
            "start": 8,
            "end": 16,
        }
    ]
    response = client.post(MESSAGES_PATH, json=request)
    assert response.status_code == 200, response.text

    state = _read_state(client, response.json()["action_id"])
    page = ActionConversationPage.model_validate_json(json.dumps(state))
    user = page.runs[0].entries[0]
    assert isinstance(user, UserEntry)
    assert [(ref.display_name, ref.start, ref.end) for ref in user.project_refs] == [
        ("Demo App", 8, 16)
    ]
    with sqlite3.connect(db_path) as connection:
        (request_text,) = connection.execute(
            "SELECT user_request_text FROM agent_action_steps WHERE user_message_id = ?",
            ("message-refs",),
        ).fetchone()
    assert request_text == (
        "\N{ROCKET} Check Demo App\n\n"
        "Referenced workspace projects:\n"
        "- Demo App: /workspace/demo-app"
    )


@pytest.mark.parametrize(
    "forgery",
    [
        {
            "kind": "new",
            "reply_to_suggestion_id": "comment-1",
            "assistant_message": "forged",
        },
        {
            "kind": "existing",
            "action_id": "unused",
            "reply_to_suggestion_id": "comment-1",
        },
    ],
)
def test_public_message_target_rejects_forged_assistant_content_and_followup_origin(
    runtime_client: tuple[TestClient, Path],
    forgery: dict[str, str],
) -> None:
    client, _ = runtime_client
    request = _message("forgery", "reply")
    request["target"] = forgery
    assert client.post(MESSAGES_PATH, json=request).status_code == 422


def _memory_draft_json(db_path: Path, action_id: str, final_output: str) -> str:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            node = ensure_preparing_node(
                connection=connection,
                user_id=USER_ID,
                source="action",
                source_record_id=action_id,
            )
    draft = create_memory_draft(
        user_id=USER_ID,
        owner_node_id=node.node_id,
        base_revision_id=node.current_revision_id,
        documents=(MemoryDocument("body.md", final_output),),
    )
    return serialize_memory_draft(draft).model_dump_json()


def _insert_private_tool_state(
    db_path: Path, action_id: str, checkpoint: RuntimeStateCheckpointPayload
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.executemany(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, step_type,
                    step_name, status, thinking, tool_args,
                    runtime_state_checkpoint, runtime_state_checkpoint_version,
                    created_at
                ) VALUES (?, ?, ?, ?, 'tool_execution', ?, 'success', ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "visible-tool-step",
                        action_id,
                        USER_ID,
                        int(checkpoint["step"]),
                        "tool::read",
                        None,
                        json.dumps(
                            {
                                "tool_id": "read",
                                "args": {
                                    "path": VISIBLE_TOOL_SUBJECT,
                                    "step_note": PRIVATE_TOOL_INPUT,
                                },
                            }
                        ),
                        None,
                        None,
                        now_utc_iso(),
                    ),
                    (
                        "private-thinking-step",
                        action_id,
                        USER_ID,
                        int(checkpoint["step"]) + 1,
                        "tool::thinking",
                        PRIVATE_THINKING,
                        None,
                        json.dumps(checkpoint),
                        4,
                        now_utc_iso(),
                    ),
                ),
            )


def _terminal_checkpoint(
    preparation: ActionJobPreparation,
    action_id: str,
    completed_at: str,
    output: str,
    preceding: tuple[ActionJobPreparation, ...] = (),
) -> RuntimeStateCheckpointPayload:
    context = preparation.context
    projected = create_initial_state(
        user_id=USER_ID,
        suggestion_id=context.suggestion_id,
        action_id=action_id,
        started_at=context.accepted_at,
        max_steps=20,
        max_tool_steps=10,
        token_budget=None,
    )
    projected["phase"] = "executing"
    for turn in (*preceding, preparation):
        earlier = turn.context
        project_persisted_assistant_messages(
            projected,
            earlier.preceding_assistant_messages,
            before_step_number=earlier.user_step_number,
        )
        projected = project_persisted_user_request_step(
            projected,
            step_id=earlier.user_step_id,
            step_number=earlier.user_step_number,
            local_step_number=earlier.user_step_local_step_number,
            short_step_id=earlier.user_step_short_id,
            request_text=earlier.user_message.content,
            occurred_at=earlier.accepted_at,
            history_phase="executing",
        )
    projected["status"] = "success"
    projected["updated_at"] = completed_at
    projected["final_output"] = output
    return build_runtime_state_checkpoint(projected)


@pytest.mark.parametrize("reply", [False, True])
async def test_success_then_followup_projects_two_runs_and_one_history_item(
    runtime_client: tuple[TestClient, Path],
    reply: bool,
) -> None:
    client, db_path = runtime_client
    fixture = (
        Path(__file__).resolve().parents[3]
        / "frontend/tests/fixtures/action_conversation_v2.json"
    ).read_text(encoding="utf-8")
    page = ActionConversationPage.model_validate_json(fixture, strict=True)
    assert json.loads(page.model_dump_json()) == json.loads(fixture)

    request = _message("message-1", "最初の依頼")
    if reply:
        _insert_reply_source(db_path)
        request["target"] = {"kind": "new", "reply_to_suggestion_id": "comment-1"}
    first = client.post(MESSAGES_PATH, json=request)
    assert first.status_code == 200
    first_result = first.json()
    first_payload, first_preparation = _claim_and_prepare(
        db_path, claimed_by="worker:first"
    )
    final_output = "最初の回答"
    completed_at = now_utc_iso()
    checkpoint = _terminal_checkpoint(
        first_preparation, first_payload["action_id"], completed_at, final_output
    )
    _insert_private_tool_state(db_path, first_result["action_id"], checkpoint)
    terminal = build_finalize_action_terminal_command(
        process_completed_event_id="event-first-completed",
        suggestion_id=first_preparation.context.suggestion_id,
        user_id=USER_ID,
        command_id=first_preparation.context.command_id,
        process_id=first_payload["process_id"],
        action_id=first_payload["action_id"],
        accepted_at=first_preparation.context.accepted_at,
        completed_at=completed_at,
        action_status="success",
        final_output=final_output,
        memory_draft_json=_memory_draft_json(
            db_path, first_payload["action_id"], final_output
        ),
    )
    await ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).finalize_action_job_terminal(
        command=terminal,
        job_id=first_payload["job_id"],
        runtime_state_checkpoint=checkpoint,
    )
    if reply:
        with sqlite3.connect(db_path) as connection:
            start, end = connection.execute(
                "SELECT turn_start_step_number,turn_end_step_number FROM memory_agent_triggers WHERE action_id=?",
                (first_result["action_id"],),
            ).fetchone()
        memory_tools = AgentExperienceActionHistoryTools(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id=USER_ID,
            turns=(ActionTurnWindow(first_result["action_id"], start, end),),
        )
        call = ReactToolCall(
            tool_name="history_fetch",
            tool_args={
                "action_id": first_result["action_id"],
                "refs": ["S-1-ASSISTANT"],
            },
            tool_call_envelope=ToolCallEnvelope(
                tool_id="history_fetch", reason=None, args={}
            ),
        )
        evidence = await memory_tools.fetch_history(call, 1)
        assert evidence.status == "success", evidence.output
        assert "**先ほどの発言**" in json.dumps(evidence.output, ensure_ascii=False)
    completed = client.get(
        f"/v1/agents/users/{USER_ID}/actions/{first_result['action_id']}/state",
        params={"limit": 100},
    )
    assert completed.status_code == 200
    completed_json = completed.json()
    assert completed_json["action"]["status"] == "success"
    assert completed_json["runs"][0]["final_output"] == final_output
    completed_history = client.get(
        "/api/agent/history", params={"limit": 25, "status": "all"}
    )
    assert completed_history.status_code == 200
    assert (
        completed_json["runs"][0]["completion_event_id"]
        == completed_history.json()["items"][0]["latest_completion_event_id"]
        == "event-first-completed"
    )
    entries = completed_json["runs"][0]["entries"]
    assert [(entry["step_kind"], entry.get("label")) for entry in entries] == [
        ("tool", "read"),
        ("user", None),
        *([("assistant", None)] if reply else []),
    ]
    # The row says what the step acted on; the rest of its arguments stay private.
    assert entries[0]["subject"] == VISIBLE_TOOL_SUBJECT
    assert [entry["content"] for entry in entries if entry["step_kind"] == "user"] == [
        "最初の依頼"
    ]
    assert PRIVATE_THINKING not in completed.text
    assert PRIVATE_TOOL_INPUT not in completed.text

    followup = client.post(
        MESSAGES_PATH,
        json=_message(
            "message-2",
            "追加の依頼",
            action_id=first_result["action_id"],
            expected_process_id=None,
        ),
    )
    assert followup.status_code == 200, followup.text
    followup_result = followup.json()
    second_payload, second_preparation = _claim_and_prepare(
        db_path, claimed_by="worker:followup"
    )
    if reply:
        assert [
            message.content
            for message in second_preparation.context.preceding_assistant_messages
        ] == ["**先ほどの発言**"]
    assert followup_result["process_id"] == second_payload["process_id"]

    current = _read_state(client, first_result["action_id"])
    assert current["action"] == {
        "action_id": first_result["action_id"],
        "suggestion_id": None,
        "approved_suggestion": None,
        "status": "processing",
        "latest_run_id": followup_result["process_id"],
        "resumable": False,
    }
    assert [(run["run_id"], run["status"]) for run in current["runs"]] == [
        (followup_result["process_id"], "running")
    ]
    assert current["runs"][0]["completion_event_id"] is None
    older = _read_state(client, first_result["action_id"], current["next_cursor"])
    assert older["runs"][0]["completion_event_id"] == "event-first-completed"
    assert [
        (run["run_id"], run["status"], run["final_output"]) for run in older["runs"]
    ] == [(first_result["process_id"], "success", final_output)]
    contents = [
        entry["content"]
        for page in (current, older)
        for run in page["runs"]
        for entry in run["entries"]
        if entry["step_kind"] == "user"
    ]
    assert contents == ["追加の依頼", "最初の依頼"]
    assert [
        entry["content"]
        for run in older["runs"]
        for entry in run["entries"]
        if entry["step_kind"] == "assistant"
    ] == (["**先ほどの発言**"] if reply else [])

    history = client.get("/api/agent/history", params={"limit": 25, "status": "all"})
    assert history.status_code == 200
    assert [
        (item["kind"], item["action_id"], item["title"], item["status"])
        for item in history.json()["items"]
    ] == [("conversation", first_result["action_id"], "最初の依頼", "running")]

    if reply:
        bootstrap = client.get("/api/agent/history/comment-1/overlay-bootstrap")
        assert bootstrap.json()["snapshot"]["actionId"] == first_result["action_id"]
        second_completed_at = now_utc_iso()
        await ActionTerminalRepository(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
        ).finalize_action_job_terminal(
            command=replace(
                terminal,
                process_completed_event_id="event-second-completed",
                command_id=second_preparation.context.command_id,
                process_id=second_payload["process_id"],
                accepted_at=second_preparation.context.accepted_at,
                completed_at=second_completed_at,
                final_output="次の回答",
                memory_draft_json=_memory_draft_json(
                    db_path, first_result["action_id"], "次の回答"
                ),
            ),
            job_id=second_payload["job_id"],
            runtime_state_checkpoint=_terminal_checkpoint(
                second_preparation,
                first_result["action_id"],
                second_completed_at,
                "次の回答",
                preceding=(first_preparation,),
            ),
        )
        with sqlite3.connect(db_path) as connection:
            starts = connection.execute(
                "SELECT turn_start_step_number FROM memory_agent_triggers WHERE action_id=? ORDER BY turn_start_step_number",
                (first_result["action_id"],),
            ).fetchall()
        assert starts == [(1,), (second_preparation.context.user_step_number,)]


def test_a_not_executed_followup_leaves_the_stopped_run_resumable(
    runtime_client: tuple[TestClient, Path],
) -> None:
    client, db_path = runtime_client
    started = client.post(
        MESSAGES_PATH, json=_message("message-running", "実行する")
    ).json()
    payload, preparation = _claim_and_prepare(db_path, claimed_by="worker:running")
    action_id = str(started["action_id"])
    _insert_private_tool_state(
        db_path,
        action_id,
        _terminal_checkpoint(preparation, str(payload["action_id"]), now_utc_iso(), ""),
    )
    canceled = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/cancel",
        json={"reason": "user_stop"},
    )
    assert canceled.status_code == 202, canceled.text
    assert _read_state(client, action_id)["action"]["resumable"] is True

    late = client.post(
        MESSAGES_PATH,
        json=_message(
            "message-late",
            "停止後の指示",
            action_id=action_id,
            expected_process_id=str(started["process_id"]),
        ),
    )
    # The stop fence never executes this message, so it cannot be the turn the
    # Action owes an answer to — the stop it replaced stays the one to continue.
    assert late.json()["disposition"] == "not_executed"
    assert _read_state(client, action_id)["action"]["resumable"] is True


def test_a_checkpoint_this_build_cannot_restore_is_not_resumable(
    runtime_client: tuple[TestClient, Path],
) -> None:
    """A Resume this build would refuse must not be offered in the first place.

    ``resume_service`` rejects any checkpoint whose version is not the current one,
    so a read model that only asks whether some checkpoint exists would show 「再開」
    on a conversation that cannot continue.
    """

    client, db_path = runtime_client
    started = client.post(
        MESSAGES_PATH, json=_message("message-running", "実行する")
    ).json()
    payload, preparation = _claim_and_prepare(db_path, claimed_by="worker:running")
    action_id = str(started["action_id"])
    _insert_private_tool_state(
        db_path,
        action_id,
        _terminal_checkpoint(preparation, str(payload["action_id"]), now_utc_iso(), ""),
    )
    canceled = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/cancel",
        json={"reason": "user_stop"},
    )
    assert canceled.status_code == 202, canceled.text
    assert _read_state(client, action_id)["action"]["resumable"] is True

    # The upgrade that raises the checkpoint version leaves the old rows behind;
    # resume_service refuses them with ACTION_RESUME_CHECKPOINT_INVALID.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint_version = ?
            WHERE action_id = ? AND runtime_state_checkpoint IS NOT NULL
            """,
            (RUNTIME_STATE_CHECKPOINT_VERSION - 1, action_id),
        )

    assert _read_state(client, action_id)["action"]["resumable"] is False
    # The read model and the submit boundary must agree: what the conversation
    # refuses to offer, the boundary refuses to accept.
    refused = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/resume",
        json={"message_id": "message-resume"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "ActionConflict"


def test_stop_fences_late_message_and_replay_does_not_create_runtime(
    runtime_client: tuple[TestClient, Path],
) -> None:
    client, db_path = runtime_client
    started_response = client.post(
        MESSAGES_PATH, json=_message("message-running", "実行する")
    )
    assert started_response.status_code == 200
    started = started_response.json()
    _claim_and_prepare(db_path, claimed_by="worker:running")
    steer_body = _message(
        "message-steer",
        "実行中の追加指示",
        action_id=started["action_id"],
        expected_process_id=started["process_id"],
    )
    steer = client.post(MESSAGES_PATH, json=steer_body)
    assert steer.status_code == 200
    assert steer.json()["disposition"] == "pending"

    canceled = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{started['action_id']}/cancel",
        json={"reason": "user_stop"},
    )
    assert canceled.status_code == 202
    late_body = _message(
        "message-late",
        "停止後の古い指示",
        action_id=started["action_id"],
        expected_process_id=started["process_id"],
    )
    late = client.post(MESSAGES_PATH, json=late_body)
    assert late.status_code == 200
    assert late.json()["disposition"] == "not_executed"
    with sqlite3.connect(db_path) as connection:
        before_replay = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM processes WHERE action_id = ?),
              (SELECT COUNT(*) FROM jobs WHERE logical_key = ?)
            """,
            (started["action_id"], started["action_id"]),
        ).fetchone()

    reset_desktop_session_store()
    _import_session(db_path)
    replay = client.post(MESSAGES_PATH, json=late_body)
    assert replay.status_code == 200
    assert replay.json() == late.json()
    with sqlite3.connect(db_path) as connection:
        after_replay = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM processes WHERE action_id = ?),
              (SELECT COUNT(*) FROM jobs WHERE logical_key = ?)
            """,
            (started["action_id"], started["action_id"]),
        ).fetchone()
    assert before_replay == after_replay == (1, 1)

    state = _read_state(client, started["action_id"])
    assert state["action"]["status"] == "canceled"
    assert state["runs"][0]["status"] == "canceled"
    unadopted = _read_state(client, started["action_id"], state["next_cursor"])[
        "unadopted_messages"
    ]
    assert [(item["message_id"], item["status"]) for item in unadopted] == [
        ("message-late", "not_executed"),
        ("message-steer", "not_executed"),
    ]


def _stop_a_started_action(
    client: TestClient, db_path: Path, *, message_id: str
) -> tuple[dict[str, object], ActionJobPreparation]:
    """Start one Action, give it a restorable checkpoint, and stop it."""

    started = client.post(MESSAGES_PATH, json=_message(message_id, "実行する")).json()
    payload, preparation = _claim_and_prepare(db_path, claimed_by="worker:running")
    action_id = str(started["action_id"])
    _insert_private_tool_state(
        db_path,
        action_id,
        _terminal_checkpoint(preparation, str(payload["action_id"]), now_utc_iso(), ""),
    )
    canceled = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/cancel",
        json={"reason": "user_stop"},
    )
    assert canceled.status_code == 202, canceled.text
    assert _read_state(client, action_id)["action"]["resumable"] is True
    return started, preparation


def _resume(client: TestClient, action_id: str, message_id: str) -> object:
    return client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/resume",
        json={"message_id": message_id},
    )


def test_resume_opens_a_new_run_the_stop_control_can_reach(
    runtime_client: tuple[TestClient, Path],
) -> None:
    """Stop must reach the run 「再開」 started, not the one it continued.

    The Stop control sends the conversation's ``latest_run_id``. A resume that
    kept the stopped run's identity would leave that field naming a canceled
    run, so pressing Stop again would do nothing to the run that is working.
    """

    client, db_path = runtime_client
    started, _ = _stop_a_started_action(client, db_path, message_id="message-running")
    action_id = str(started["action_id"])

    resumed = _resume(client, action_id, "message-resume")
    assert resumed.status_code == 200, resumed.text
    resumed_run = resumed.json()
    assert resumed_run["disposition"] == "started"
    assert resumed_run["process_id"] != started["process_id"]

    state = _read_state(client, action_id)
    assert state["action"]["status"] == "queued"
    assert state["action"]["latest_run_id"] == resumed_run["process_id"]
    assert state["action"]["resumable"] is False

    stopped_again = client.post(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/cancel",
        json={"reason": "user_stop"},
    )
    assert stopped_again.status_code == 202, stopped_again.text
    after_stop = _read_state(client, action_id)
    assert after_stop["action"]["status"] == "canceled"
    assert [(run["run_id"], run["status"]) for run in after_stop["runs"]] == [
        (resumed_run["process_id"], "canceled")
    ]


def test_resume_is_never_shown_as_a_message(
    runtime_client: tuple[TestClient, Path],
) -> None:
    """The user pressed a button, so no message may appear for it anywhere.

    The turn still needs its USER row — that is what opens a run — and the row's
    text is what the model reads, so it must be excluded by the projection
    rather than left unwritten.
    """

    client, db_path = runtime_client
    started, _ = _stop_a_started_action(client, db_path, message_id="message-running")
    action_id = str(started["action_id"])
    resumed_run = _resume(client, action_id, "message-resume").json()

    state = client.get(
        f"/v1/agents/users/{USER_ID}/actions/{action_id}/state", params={"limit": 100}
    )
    assert ACTION_RESUME_REQUEST_TEXT not in state.text
    page = state.json()
    assert [(run["run_id"], run["entries"]) for run in page["runs"]] == [
        (resumed_run["process_id"], [])
    ]
    older = _read_state(client, action_id, page["next_cursor"])
    assert [
        entry["content"]
        for run in older["runs"]
        for entry in run["entries"]
        if entry["step_kind"] == "user"
    ] == ["実行する"]
    assert older["unadopted_messages"] == []
    assert "message-resume" not in state.text + json.dumps(older)

    # Nor may it be found by searching the conversation list.
    history = client.get(
        "/api/agent/history",
        params={"limit": 25, "status": "all", "search_text": "stopped this run"},
    )
    assert history.status_code == 200, history.text
    assert history.json()["items"] == []


async def test_a_resumed_run_reaches_its_own_terminal(
    runtime_client: tuple[TestClient, Path],
) -> None:
    """The resumed run must be able to commit a terminal of its own.

    The Action-terminal Memory trigger is keyed by the turn's last step number,
    so a resume that continued the stopped turn would collide with the trigger
    that turn's cancel already wrote and the terminal could never commit.
    """

    client, db_path = runtime_client
    started, first = _stop_a_started_action(
        client, db_path, message_id="message-running"
    )
    action_id = str(started["action_id"])
    resumed_run = _resume(client, action_id, "message-resume").json()

    payload, preparation = _claim_and_prepare(db_path, claimed_by="worker:resume")
    assert payload["process_id"] == resumed_run["process_id"]
    assert preparation.context.user_message.content == ACTION_RESUME_REQUEST_TEXT
    final_output = "再開後の回答"
    completed_at = now_utc_iso()
    checkpoint = _terminal_checkpoint(
        preparation,
        str(payload["action_id"]),
        completed_at,
        final_output,
        preceding=(first,),
    )
    terminal = build_finalize_action_terminal_command(
        process_completed_event_id="event-resume-completed",
        suggestion_id=preparation.context.suggestion_id,
        user_id=USER_ID,
        command_id=preparation.context.command_id,
        process_id=str(payload["process_id"]),
        action_id=action_id,
        accepted_at=preparation.context.accepted_at,
        completed_at=completed_at,
        action_status="success",
        final_output=final_output,
        memory_draft_json=_memory_draft_json(db_path, action_id, final_output),
    )
    await ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).finalize_action_job_terminal(
        command=terminal,
        job_id=str(payload["job_id"]),
        runtime_state_checkpoint=checkpoint,
    )

    state = _read_state(client, action_id)
    assert state["action"]["status"] == "success"
    assert [
        (run["run_id"], run["status"], run["final_output"]) for run in state["runs"]
    ] == [(resumed_run["process_id"], "success", final_output)]
    with sqlite3.connect(db_path) as connection:
        source_ids = [
            row[0]
            for row in connection.execute(
                "SELECT source_id FROM memory_agent_triggers "
                "WHERE action_id = ? ORDER BY created_at, source_id",
                (action_id,),
            )
        ]
    # The stop and the resumed turn each own a turn, so neither key is the other.
    assert len(source_ids) == len(set(source_ids)) == 2


def test_resume_is_refused_when_the_action_is_not_stopped(
    runtime_client: tuple[TestClient, Path],
) -> None:
    client, db_path = runtime_client
    started = client.post(
        MESSAGES_PATH, json=_message("message-running", "実行する")
    ).json()
    _claim_and_prepare(db_path, claimed_by="worker:running")

    refused = _resume(client, str(started["action_id"]), "message-resume")
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "ExpectedProcessConflict"

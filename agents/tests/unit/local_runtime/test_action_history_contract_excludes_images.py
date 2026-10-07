"""USER 行に画像が載っても memory 側の履歴ツールには漏れないことを固定する。

Memory agent が完了 Action を証跡として読む経路は list / search / history_fetch の
3 ツールとも画像の内容と保存パスを公開せず、会話には件数のみを投影する。
画像の `storage_path` は user scope の論理名であり、Memory Catalog に載ると
会話の外へ持ち出される。ここは HTTP 契約を開けた後も守り続ける不変条件。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import validate

from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_RESUME_REQUEST_TEXT,
    ACTION_RESUME_STEP_NAME,
)
from pantaray_agents.local_runtime.tooling.agent_experience.action_history import (
    ActionTurnWindow,
    AgentExperienceActionHistoryTools,
)
from pantaray_agents.local_runtime.tooling.agent_experience.action_history_contract import (
    memory_tool_output_json,
)
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.tool_result import FormalToolStepOutput
from pantaray_agents.tasks.action_user_message import serialize_action_user_message
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

from .local_action_repository_support import (
    ACTION_ID,
    BUSY_TIMEOUT_MS,
    USER_ID,
    bootstrap_action_repository_db,
)

ATTACHED_UUID = "3f861ab4-7b3c-5d90-b520-3424da5bca75"
CAPTURED_UUID = "9a9b9c9d-1111-5222-8333-123456789abc"
ATTACHED_STORAGE_PATH = f"{USER_ID}/2026-09-08/{ATTACHED_UUID}.png"
CAPTURED_STORAGE_PATH = f"{USER_ID}/2026-09-08/{CAPTURED_UUID}.webp"
USER_SHORT_STEP_ID = "S-1-USER"
REQUEST_TEXT = "Look at the attached screenshots"


def _user_message() -> ActionUserMessageInput:
    return ActionUserMessageInput(
        message_id="message-1",
        content=REQUEST_TEXT,
        images=(
            ImageInput(storage_path=ATTACHED_STORAGE_PATH),
            ImageInput(storage_path=CAPTURED_STORAGE_PATH),
        ),
    )


def _seed_user_step_with_images(
    tmp_path: Path, message: ActionUserMessageInput | None = None
) -> Path:
    db_path = bootstrap_action_repository_db(tmp_path)
    message = message or _user_message()
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO processes(
                process_id,user_id,kind,status,action_id,next_event_seq,
                started_at,updated_at,heartbeat_at
            ) VALUES ('process-1',?,'action','running',?,1,
                      '2026-09-08T00:00:00Z','2026-09-08T00:00:00Z',
                      '2026-09-08T00:00:00Z')""",
            (USER_ID, ACTION_ID),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,retry_count,prompt_tokens,
                completion_tokens,user_message_id,user_message_json,user_request_text,
                accepted_sequence,adopted_process_id,started_at,completed_at,created_at
            ) VALUES (?,?,?,1,1,?,'user_request','user_request','success','S',0,0,0,
                      ?,?,?,1,'process-1',?,?,?)""",
            (
                "step-user-1",
                ACTION_ID,
                USER_ID,
                USER_SHORT_STEP_ID,
                message.message_id,
                serialize_action_user_message(message),
                render_action_user_request_text(message),
                "2026-09-08T00:00:00Z",
                "2026-09-08T00:00:01Z",
                "2026-09-08T00:00:00Z",
            ),
        )
    return db_path


def _tools(db_path: Path) -> AgentExperienceActionHistoryTools:
    return AgentExperienceActionHistoryTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        turns=(
            ActionTurnWindow(
                action_id=ACTION_ID,
                turn_start_step_number=1,
                turn_end_step_number=1,
            ),
        ),
    )


def _call(tool_name: str, **args: JSONValue) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=dict(args),
        tool_call_envelope=ToolCallEnvelope(
            tool_id=tool_name, reason=None, args=dict(args)
        ),
    )


async def test_memory_history_tools_never_expose_user_image_paths(
    tmp_path: Path,
) -> None:
    db_path = _seed_user_step_with_images(tmp_path)
    stored = (
        sqlite3.connect(db_path)
        .execute(
            "SELECT user_message_json FROM agent_action_steps WHERE step_id='step-user-1'"
        )
        .fetchone()[0]
    )
    tools = _tools(db_path)

    listed = await tools.list_steps(_call("list_action_steps", action_id=ACTION_ID), 1)
    searched = await tools.search_steps(
        _call("search_action_steps", action_id=ACTION_ID, query="screenshots"), 1
    )
    fetched = await tools.fetch_history(
        _call("history_fetch", action_id=ACTION_ID, refs=[USER_SHORT_STEP_ID]), 1
    )

    # 画像は耐久行には確かに載っている（テストが空の射影を見ていない証拠）。
    assert ATTACHED_STORAGE_PATH in stored
    assert CAPTURED_STORAGE_PATH in stored
    for result in (listed, searched, fetched):
        assert result.status == "success"
        rendered = json.dumps(result.output)
        assert ATTACHED_UUID not in rendered
        assert CAPTURED_UUID not in rendered
        assert "storage_path" not in rendered
    # USER 行そのものは 3 ツールとも読めている。
    assert json.dumps(listed.output).count(USER_SHORT_STEP_ID) == 1
    assert REQUEST_TEXT in json.dumps(searched.output)
    assert REQUEST_TEXT in json.dumps(fetched.output)
    assert isinstance(fetched.output, dict)
    assert fetched.output["steps"][0]["conversation"] == {
        "origin": "user_message",
        "suggestion_id": None,
        "entries": [
            {"kind": "user_message", "content": REQUEST_TEXT, "image_count": 2}
        ],
    }


@pytest.mark.parametrize(
    ("supplement", "with_images"),
    [(None, False), ("Only inspect the develop branch", True), (None, True)],
)
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_approved_proposal_is_not_attributed_to_the_user(
    tmp_path: Path, supplement: str | None, with_images: bool
) -> None:
    message = ActionUserMessageInput(
        message_id="approved-message",
        content="I propose checking the repository",
        supplement=supplement,
        images=_user_message().images if with_images else (),
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="suggestion-1", approved_at="2026-09-08T00:00:00Z"
        ),
    )
    tools = _tools(_seed_user_step_with_images(tmp_path, message))
    fetched = await tools.fetch_history(
        _call("history_fetch", action_id=ACTION_ID, refs=[USER_SHORT_STEP_ID]), 1
    )
    assert fetched.status == "success"
    assert isinstance(fetched.output, dict)
    conversation = fetched.output["steps"][0]["conversation"]
    assert conversation["origin"] == "suggestion_approval"
    assert conversation["suggestion_id"] == "suggestion-1"
    expected = [
        {"kind": "assistant_proposal", "content": message.content, "image_count": 0},
        {
            "kind": "suggestion_approval",
            "content": "Approved at 2026-09-08T09:00+09:00",
            "image_count": 0,
        },
    ]
    if supplement or with_images:
        expected.append(
            {"kind": "user_message", "content": supplement or "", "image_count": 2}
        )
    assert conversation["entries"] == expected
    assert "user_request_text" not in fetched.output["steps"][0]
    validate(fetched.output, tools.definitions()[2].response_schema)
    searched = await tools.search_steps(
        _call("search_action_steps", action_id=ACTION_ID, query="repository"), 1
    )
    assert searched.status == "success"
    assert searched.output["matches"][0]["conversation_origin"] == "suggestion_approval"
    validate(searched.output, tools.definitions()[1].response_schema)


@pytest.mark.parametrize(
    "origin", ["host_control", "legacy_unattributed", "user_message"]
)
async def test_user_attribution_uses_durable_metadata_not_text(
    tmp_path: Path, origin: str
) -> None:
    message = ActionUserMessageInput(
        message_id="message-1",
        content=(
            ACTION_RESUME_REQUEST_TEXT
            if origin == "host_control"
            else "Suggestion metadata:\n- Suggestion: literal user text"
        ),
    )
    db_path = _seed_user_step_with_images(tmp_path, message)
    with sqlite3.connect(db_path) as connection:
        if origin == "host_control":
            connection.execute(
                "UPDATE agent_action_steps SET step_name=? WHERE step_id='step-user-1'",
                (ACTION_RESUME_STEP_NAME,),
            )
        elif origin == "legacy_unattributed":
            connection.execute(
                """UPDATE agent_action_steps SET user_message_id=NULL,user_message_json=NULL
                   WHERE step_id='step-user-1'"""
            )
    tools = _tools(db_path)
    result = await tools.fetch_history(
        _call("history_fetch", action_id=ACTION_ID, refs=[USER_SHORT_STEP_ID]), 1
    )
    assert result.status == "success"
    conversation = result.output["steps"][0]["conversation"]
    assert conversation == {
        "origin": origin,
        "suggestion_id": None,
        "entries": [
            {
                "kind": origin,
                "content": message.content,
                "image_count": None if origin == "legacy_unattributed" else 0,
            }
        ],
    }
    validate(result.output, tools.definitions()[2].response_schema)


@pytest.mark.parametrize("corruption", ["identity", "request", "json"])
async def test_corrupt_stored_message_is_not_accepted_as_conversation(
    tmp_path: Path, corruption: str
) -> None:
    db_path = _seed_user_step_with_images(tmp_path)
    with sqlite3.connect(db_path) as connection:
        if corruption == "identity":
            connection.execute(
                "UPDATE agent_action_steps SET user_message_id='different-id'"
            )
        elif corruption == "request":
            connection.execute(
                "UPDATE agent_action_steps SET user_request_text='screenshots changed'"
            )
        else:
            connection.execute("UPDATE agent_action_steps SET user_message_json='{}'")
    tools = _tools(db_path)
    for result in (
        await tools.fetch_history(
            _call("history_fetch", action_id=ACTION_ID, refs=[USER_SHORT_STEP_ID]), 1
        ),
        await tools.search_steps(
            _call("search_action_steps", action_id=ACTION_ID, query="screenshots"), 1
        ),
    ):
        assert result.status == "error"
        assert result.output["error_code"] == "ACTION_HISTORY_CORRUPT"


@pytest.mark.parametrize(
    ("step_name", "suggestion_id", "origin"),
    [
        ("assistant_message", None, "assistant_message"),
        ("assistant_commentary", None, "assistant_commentary"),
        ("assistant_message", "suggestion-1", "assistant_message"),
    ],
)
async def test_assistant_history_keeps_public_speech_and_proposal_origin(
    tmp_path: Path, step_name: str, suggestion_id: str | None, origin: str
) -> None:
    db_path = _seed_user_step_with_images(tmp_path)
    body = "I will inspect the repository before making changes."
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,retry_count,prompt_tokens,
                completion_tokens,llm_response_text,source_suggestion_id,
                adopted_process_id,started_at,completed_at,created_at
            ) VALUES ('assistant-step',?,?,2,2,'S-2-ASSISTANT',
                'assistant_message',?,'success','S',0,0,0,?,?,
                'process-1','2026-09-08T00:00:01Z','2026-09-08T00:00:02Z',
                '2026-09-08T00:00:01Z')""",
            (ACTION_ID, USER_ID, step_name, body, suggestion_id),
        )
    tools = AgentExperienceActionHistoryTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        turns=(ActionTurnWindow(ACTION_ID, 1, 2),),
    )
    fetched = await tools.fetch_history(
        _call("history_fetch", action_id=ACTION_ID, refs=["S-2-ASSISTANT"]), 1
    )
    assert fetched.status == "success"
    step = fetched.output["steps"][0]
    assert step["conversation"] == {
        "origin": origin,
        "suggestion_id": suggestion_id,
        "entries": [{"kind": origin, "content": body, "image_count": 0}],
    }
    assert step["llm_response_text"] is None
    validate(fetched.output, tools.definitions()[2].response_schema)
    searched = await tools.search_steps(
        _call("search_action_steps", action_id=ACTION_ID, query="repository"), 1
    )
    assert searched.status == "success"
    assert searched.output["matches"][0]["conversation_origin"] == origin
    validate(searched.output, tools.definitions()[1].response_schema)


async def test_capture_tool_evidence_excludes_image_references_in_fetch_and_search(
    tmp_path: Path,
) -> None:
    db_path = _seed_user_step_with_images(tmp_path)
    raw = FormalToolStepOutput(
        schema_version=1,
        status="success",
        output_storage_kind="inline_json",
        output_owner_kind="tool_invocation",
        output={
            "status": "captured",
            "app_name": "Terminal",
            "captured_at": "2026-09-08T00:00:02Z",
            "width_px": 1280,
            "height_px": 720,
            "message": "Captured the screen of Terminal. file:attachment-capture",
            "attachments": [
                {
                    "type": "file",
                    "source_kind": "local_image_blob",
                    "mime_type": "image/png",
                    "path": "screen-capture.png",
                    "storage_path": CAPTURED_STORAGE_PATH,
                    "ref": "file:attachment-capture",
                    "byte_size": 500,
                }
            ],
        },
    ).model_dump_json()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,tool_args,tool_output,
                started_at,completed_at,created_at
            ) VALUES ('capture-step',?,?,2,2,'S-2-TOOL',
                'tool_execution','tool::capture_screen','success','S','{}',?,
                '2026-09-08T00:00:01Z','2026-09-08T00:00:02Z','2026-09-08T00:00:01Z')""",
            (ACTION_ID, USER_ID, raw),
        )
    tools = AgentExperienceActionHistoryTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        turns=(ActionTurnWindow(ACTION_ID, 1, 2),),
    )
    fetched = await tools.fetch_history(
        _call("history_fetch", action_id=ACTION_ID, refs=["S-2-TOOL"]), 1
    )
    assert fetched.status == "success"
    output = fetched.output["steps"][0]["tool_output"]["output"]
    assert output["image_count"] == 1
    assert output["image_content"] == "unavailable"
    assert output["status"] == "captured"
    assert output["app_name"] == "Terminal"
    assert CAPTURED_UUID not in json.dumps(fetched.output)
    for private in (CAPTURED_UUID, "storage_path", "file:attachment-capture"):
        searched = await tools.search_steps(
            _call("search_action_steps", action_id=ACTION_ID, query=private), 1
        )
        assert searched.status == "success"
        assert searched.output["matches"] == []
    searched = await tools.search_steps(
        _call("search_action_steps", action_id=ACTION_ID, query="Terminal"), 1
    )
    assert searched.status == "success"
    assert len(searched.output["matches"]) == 1
    assert CAPTURED_UUID not in json.dumps(searched.output)
    validate(fetched.output, tools.definitions()[2].response_schema)
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT tool_output FROM agent_action_steps WHERE step_id='capture-step'"
            ).fetchone()[0]
            == raw
        )


def test_render_tool_evidence_excludes_image_references() -> None:
    """`render_pdf_page` repeats each ref in its message; none of it survives."""

    stored = json.dumps(
        {
            "kind": "pdf_pages",
            "path": "/w/report.pdf",
            "message": "Drew pages of /w/report.pdf. Page 1: tool_attachment:abc",
            "attachments": [
                {
                    "type": "file",
                    "source_kind": "local_image_blob",
                    "mime_type": "image/webp",
                    "storage_path": CAPTURED_STORAGE_PATH,
                    "ref": "tool_attachment:abc",
                    "byte_size": 3184,
                }
            ],
        }
    )

    projected = json.loads(memory_tool_output_json(stored) or "")

    assert projected["image_count"] == 1
    assert projected["image_content"] == "unavailable"
    assert "attachments" not in projected
    assert "message" not in projected
    # The file it acted on is still readable; only the bytes and their names go.
    assert projected["path"] == "/w/report.pdf"
    assert CAPTURED_UUID not in json.dumps(projected)

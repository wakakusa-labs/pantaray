import sqlite3
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.action_conversation.cursor_codec import (
    OpaqueCursorError,
)
from pantaray_agents.local_runtime.action_conversation.repository import (
    ActionConversationCursorConflictError,
    ActionConversationNotFoundError,
)
from pantaray_agents.local_runtime.action_conversation.run_projection import (
    read_action_conversation_page_in_connection,
)
from pantaray_agents.local_runtime.action_conversation.sqlite_visibility import (
    register_action_tool_visibility_sqlite,
)
from pantaray_agents.local_runtime.runtime import runtime_env
from pantaray_agents.local_runtime.storage import sqlite_vector as vector
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.routers.action_cancel_service import execute_action_cancel
from pantaray_agents.schema.action_conversation import ActionConversationPage

router = APIRouter(prefix="/v1/agents/users", tags=["Action Agent"])

# Conversation pages count logical runs (USER turns), never rows: a page holds
# whole runs, so a turn's tool rows can never push its own message off the page.
ACTION_CONVERSATION_DEFAULT_PAGE_SIZE = 25
ACTION_CONVERSATION_MAX_PAGE_SIZE = 100
_ACTION_CANCEL_CLEANUP_INCOMPLETE_DETAIL = "Action cancel cleanup incomplete"


class ActionCancelRequest(BaseModel):
    """Action キャンセル API のリクエストボディ。

    ユーザー操作に起因する STOP 要求などの理由を任意で付加できる。
    """

    reason: str | None = Field(
        default=None,
        description="キャンセル理由（ログ用途。省略可）",
    )
    expected_process_id: str | None = Field(
        default=None,
        description=(
            "停止したい run の process_id。いまの run が別の run なら 409 を返し、"
            "画面が見ていない run を止めない（省略時は照合しない）"
        ),
    )


class ActionCancelResponse(BaseModel):
    """Action キャンセル API のレスポンス。"""

    status: Literal["accepted"] = Field(
        description='キャンセル要求の処理結果: 常に "accepted"'
    )


@router.get(
    "/{user_id}/actions/{action_id}/state",
    response_model=ActionConversationPage,
)
def action_state(
    user_id: str,
    action_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(
        ACTION_CONVERSATION_DEFAULT_PAGE_SIZE,
        ge=1,
        le=ACTION_CONVERSATION_MAX_PAGE_SIZE,
    ),
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionConversationPage:
    if resolved_user_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user_id mismatch")
    try:
        db_path, busy_timeout_ms = runtime_env.read_local_runtime_db_config()
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, busy_timeout_ms)
            register_action_tool_visibility_sqlite(connection)
            connection.execute("BEGIN")
            return read_action_conversation_page_in_connection(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
                cursor=cursor,
                limit=limit,
            )
    except OpaqueCursorError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid Action conversation cursor",
        ) from exc
    except ActionConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found") from exc
    except ActionConversationCursorConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Action conversation cursor is stale",
        ) from exc
    except (MigrationError, sqlite3.Error, vector.SQLiteVectorExtensionError) as exc:
        raise PublicAgentHTTPError(
            "Action conversation read failed",
            error_code="ACTION_CONVERSATION_INTEGRITY_ERROR",
        ) from exc


@router.post(
    "/{user_id}/actions/{action_id}/cancel",
    response_model=ActionCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_action(
    user_id: str,
    action_id: str,
    body: ActionCancelRequest | None = None,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionCancelResponse:
    """Action 実行中のアクションをキャンセルする内部専用 API。

    - デスクトップの「停止」（メインウィンドウと小ウィンドウ）が呼ぶ唯一の停止経路。
    - DB-first で canonical terminal を確定し、その後に WebSocket 側が
      `process_completed(status="canceled")` を配信する。
    """

    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )

    cleanup_complete = await execute_action_cancel(
        user_id=user_id,
        action_id=action_id,
        reason=body.reason if body else None,
        expected_process_id=body.expected_process_id if body else None,
    )
    if not cleanup_complete:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_ACTION_CANCEL_CLEANUP_INCOMPLETE_DETAIL,
        )

    return ActionCancelResponse(status="accepted")

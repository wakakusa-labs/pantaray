from __future__ import annotations

import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.action_conversation.cursor_codec import (
    OpaqueCursorError,
)
from pantaray_agents.local_runtime.action_conversation.history_candidates import (
    register_conversation_history_casefold_sqlite,
)
from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    ConversationBusyError,
    HistoryItemKind,
    delete_history_item,
)
from pantaray_agents.local_runtime.action_conversation.history_repository import (
    read_conversation_history_item_in_connection,
    read_conversation_history_page_in_connection,
)
from pantaray_agents.local_runtime.runtime import runtime_env
from pantaray_agents.local_runtime.storage import sqlite_vector as vector
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.conversation_history import (
    ConversationHistoryListItem,
    ConversationHistoryPage,
)

CONVERSATION_HISTORY_DEFAULT_PAGE_SIZE = 25
CONVERSATION_HISTORY_MAX_PAGE_SIZE = 100
CONVERSATION_HISTORY_MAX_SEARCH_CODE_POINTS = 256

router = APIRouter(prefix="/api/agent/history", tags=["Conversation History"])


@router.get("", response_model=ConversationHistoryPage)
def list_conversation_history(
    cursor: str | None = Query(default=None),
    limit: int = Query(
        CONVERSATION_HISTORY_DEFAULT_PAGE_SIZE,
        ge=1,
        le=CONVERSATION_HISTORY_MAX_PAGE_SIZE,
    ),
    search_text: str = Query(
        default="",
        max_length=CONVERSATION_HISTORY_MAX_SEARCH_CODE_POINTS,
    ),
    user_id: str = Depends(get_current_user_id_from_token),
) -> ConversationHistoryPage:
    try:
        db_path, busy_timeout_ms = runtime_env.read_local_runtime_db_config()
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, busy_timeout_ms)
            register_conversation_history_casefold_sqlite(connection)
            connection.execute("BEGIN")
            return read_conversation_history_page_in_connection(
                connection=connection,
                user_id=user_id,
                search_text=search_text,
                cursor=cursor,
                limit=limit,
            )
    except OpaqueCursorError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid conversation history cursor",
        ) from exc
    except (MigrationError, sqlite3.Error, vector.SQLiteVectorExtensionError) as exc:
        raise PublicAgentHTTPError(
            "Conversation history read failed",
            error_code="CONVERSATION_HISTORY_INTEGRITY_ERROR",
        ) from exc


@router.get("/items/{kind}/{item_id}", response_model=ConversationHistoryListItem)
def read_conversation_history_item(
    kind: HistoryItemKind,
    item_id: str,
    user_id: str = Depends(get_current_user_id_from_token),
) -> ConversationHistoryListItem:
    """The list's row for one item, as the list shows it; 404 when the list does
    not hold it for this user (gone, another user's, or a suggestion now a task)."""

    try:
        db_path, busy_timeout_ms = runtime_env.read_local_runtime_db_config()
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            configure_connection(connection, busy_timeout_ms)
            register_conversation_history_casefold_sqlite(connection)
            connection.execute("BEGIN")
            item = read_conversation_history_item_in_connection(
                connection=connection, user_id=user_id, item=(kind, item_id)
            )
    except (MigrationError, sqlite3.Error, vector.SQLiteVectorExtensionError) as exc:
        raise PublicAgentHTTPError(
            "Conversation history read failed",
            error_code="CONVERSATION_HISTORY_INTEGRITY_ERROR",
        ) from exc
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "history item not found")
    return item


@router.delete("/items/{kind}/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation_history_item(
    kind: HistoryItemKind,
    item_id: str,
    user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    """204 also when already gone; 409 ``CONVERSATION_BUSY`` while its run is active."""

    try:
        db_path, busy_timeout_ms = runtime_env.read_local_runtime_db_config()
        delete_history_item(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=runtime_env.read_local_runtime_artifact_root(),
            user_id=user_id,
            kind=kind,
            item_id=item_id,
        )
    except ConversationBusyError as exc:
        raise PublicAgentHTTPError(
            "Conversation is in use",
            error_code="CONVERSATION_BUSY",
            status_code=status.HTTP_409_CONFLICT,
            error_type=ErrorType.CONFLICT_ERROR,
            public_message="This conversation is in use. Try again later.",
        ) from exc
    except (MigrationError, sqlite3.Error, vector.SQLiteVectorExtensionError) as exc:
        raise PublicAgentHTTPError(
            "Conversation history deletion failed",
            error_code="CONVERSATION_HISTORY_DELETE_FAILED",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)

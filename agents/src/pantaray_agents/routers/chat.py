from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.chat.store import (
    ChatItemReferenceError,
    ChatMessageIdentityConflictError,
    append_chat_item,
    read_chat_page,
    read_chat_turn_marks,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.schema.chat import (
    ChatItem,
    ChatItemPage,
    ChatMessageConflict,
    ChatMessageHttpRequest,
    ChatMessageRejected,
    ChatMessageRejectedField,
    ChatTurnRetryHttpRequest,
    ChatTurnRetryStale,
    UserMessageContent,
)
from pantaray_agents.tasks.chat_turns import request_chat_turn

CHAT_PAGE_DEFAULT_SIZE = 50
CHAT_PAGE_MAX_SIZE = 200
_REJECTABLE_FIELDS: dict[str, ChatMessageRejectedField] = {
    "text": "text",
    "quote_item_id": "quote_item_id",
    "images": "images",
    "files": "files",
    "project_refs": "project_refs",
}

router = APIRouter(prefix="/v1/agents/users", tags=["Chat"])


def _require_path_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


@router.post(
    "/{user_id}/chat/messages",
    description=(
        "Append the user's message to their chat and start the turn that "
        "answers it. `message_id` is a user-wide idempotency key: resending it "
        "with the same message returns the item already appended; with a "
        "different message, 409."
    ),
    response_model=ChatItem,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ChatMessageRejected},
        status.HTTP_409_CONFLICT: {"model": ChatMessageConflict},
    },
)
async def post_chat_message(
    user_id: str,
    request: Request,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ChatItem | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    try:
        body = ChatMessageHttpRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        return _rejected(_rejected_field(exc))
    content = UserMessageContent(
        kind="user_message",
        text=body.text,
        quote_item_id=body.quote_item_id,
        images=body.images,
        files=body.files,
        project_refs=body.project_refs,
    )
    try:
        item = await asyncio.to_thread(
            append_chat_item,
            user_id=user_id,
            message_id=body.message_id,
            content=content,
        )
    except ChatItemReferenceError as exc:
        return _rejected(exc.field)
    except ChatMessageIdentityConflictError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ChatMessageConflict().model_dump(mode="json"),
        )
    except (MigrationError, sqlite3.Error) as exc:
        raise PublicAgentHTTPError(
            "Chat message append failed", error_code="CHAT_APPEND_FAILED"
        ) from exc
    # Also on a resend: a turn the message started may not have ended.
    request_chat_turn(user_id)
    return item


@router.post(
    "/{user_id}/chat/turns/retry",
    description=(
        "Run the turn that ended in `failure_item_id` again, reading the same "
        "items. 409 once another turn has ended since."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_409_CONFLICT: {"model": ChatTurnRetryStale}},
)
async def retry_chat_turn(
    user_id: str,
    body: ChatTurnRetryHttpRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    _require_path_user(user_id, resolved_user_id)
    try:
        marks = await asyncio.to_thread(read_chat_turn_marks, user_id=user_id)
    except (MigrationError, sqlite3.Error) as exc:
        raise PublicAgentHTTPError(
            "Chat turn read failed", error_code="CHAT_READ_FAILED"
        ) from exc
    failure = marks.last_failure
    if failure is None or failure.item_id != body.failure_item_id:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ChatTurnRetryStale().model_dump(mode="json"),
        )
    request_chat_turn(user_id, retry_of=body.failure_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{user_id}/chat/items", response_model=ChatItemPage)
async def list_chat_items(
    user_id: str,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(CHAT_PAGE_DEFAULT_SIZE, ge=1, le=CHAT_PAGE_MAX_SIZE),
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ChatItemPage:
    """Newest first; pass the page's `next_cursor` as `before` for older items."""

    _require_path_user(user_id, resolved_user_id)
    try:
        return await asyncio.to_thread(
            read_chat_page, user_id=user_id, before=before, limit=limit
        )
    except (MigrationError, sqlite3.Error) as exc:
        raise PublicAgentHTTPError(
            "Chat items read failed", error_code="CHAT_READ_FAILED"
        ) from exc


def _rejected_field(exc: ValidationError) -> ChatMessageRejectedField:
    """Name the request field the first error is in, when the UI can point at it."""

    locator = exc.errors(include_input=False, include_url=False)[0]["loc"]
    return _REJECTABLE_FIELDS.get(str(locator[0]) if locator else "", "body")


def _rejected(field: ChatMessageRejectedField) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ChatMessageRejected(field=field).model_dump(mode="json"),
    )

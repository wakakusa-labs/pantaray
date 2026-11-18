"""Electron's answer to one browser operation.

The operation request id is the whole authority: the runtime issued it, it belongs
to one user, and it is consumed once. A PNG or PDF never crosses this endpoint;
Electron wrote it where the id says, and the waiting runtime re-reads it and checks
the claims made here before it uses a byte of it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationAnswer,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    browser_operation_broker,
)

router = APIRouter(prefix="/local", tags=["Action Browser"])


class ActionBrowserResponse(BaseModel):
    accepted: bool


@router.post("/action-browser", response_model=ActionBrowserResponse)
async def answer_action_browser(
    request: BrowserOperationAnswer,
    user_id: str = Depends(get_current_user_id_from_token),
) -> ActionBrowserResponse:
    answer = browser_operation_broker().answer(
        request_id=request.operation_request_id,
        user_id=str(user_id),
        outcome=request.result,
    )
    if answer == "accepted":
        return ActionBrowserResponse(accepted=True)
    if answer == "unknown_request":
        # Electron deletes its temporary file on this answer: nobody will read it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no browser operation is waiting for this request id",
        )
    if answer == "user_mismatch":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="browser operation belongs to another user",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="browser operation has already been answered",
    )


__all__ = ["router"]

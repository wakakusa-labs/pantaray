"""Electron's answer to one HTML render request.

The request id is the whole authority: it was issued by the runtime, it belongs to
one user, and it is consumed once. The PDF itself never crosses this endpoint;
Electron wrote it where the id says, and the waiting runtime re-reads it and checks
the ``byte_size`` and ``sha256`` claimed here before using a byte of it.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.html_render_relay import (
    BlockedRequest,
    BlockedRequestReason,
    HtmlRenderAnswer,
    HtmlRenderClaimed,
    HtmlRendererFailureCode,
    HtmlRenderFailed,
    html_render_broker,
)

router = APIRouter(prefix="/local", tags=["Action HTML Render"])

# The relay contract caps the blocked sub-resources Electron reports.
_MAX_BLOCKED_REQUESTS = 10


class BlockedRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: BlockedRequestReason
    url: str


class RenderedHtmlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["rendered"]
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blocked_requests: list[BlockedRequestBody] = Field(max_length=_MAX_BLOCKED_REQUESTS)


class FailedHtmlRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["failed"]
    code: HtmlRendererFailureCode
    message: str
    blocked_requests: list[BlockedRequestBody] = Field(max_length=_MAX_BLOCKED_REQUESTS)


class ActionHtmlRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    render_request_id: str = Field(min_length=1, pattern=r"\S")
    result: RenderedHtmlResult | FailedHtmlRenderResult = Field(discriminator="status")


class ActionHtmlRenderResponse(BaseModel):
    accepted: bool


@router.post("/action-html-render", response_model=ActionHtmlRenderResponse)
async def answer_action_html_render(
    request: ActionHtmlRenderRequest,
    user_id: str = Depends(get_current_user_id_from_token),
) -> ActionHtmlRenderResponse:
    answer = html_render_broker().answer(
        request_id=request.render_request_id,
        user_id=str(user_id),
        outcome=_build_answer(request.result),
    )
    if answer == "accepted":
        return ActionHtmlRenderResponse(accepted=True)
    if answer == "unknown_request":
        # Electron deletes its temporary PDF on this answer: nobody will read it.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no render is waiting for this request id",
        )
    if answer == "user_mismatch":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="render request belongs to another user",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="render request has already been answered",
    )


def _build_answer(
    result: RenderedHtmlResult | FailedHtmlRenderResult,
) -> HtmlRenderAnswer:
    blocked_requests = tuple(
        BlockedRequest(reason=blocked.reason, url=blocked.url)
        for blocked in result.blocked_requests
    )
    if isinstance(result, FailedHtmlRenderResult):
        return HtmlRenderFailed(
            code=result.code,
            message=result.message,
            blocked_requests=blocked_requests,
        )
    return HtmlRenderClaimed(
        byte_size=result.byte_size,
        sha256=result.sha256,
        blocked_requests=blocked_requests,
    )


__all__ = ["router"]

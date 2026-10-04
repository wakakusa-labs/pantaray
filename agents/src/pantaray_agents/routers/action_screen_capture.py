"""Electron's answer to one ``capture_screen`` request.

The runtime cannot call into Electron, so a capture is a request pushed over the
orchestration WebSocket and answered here. The request id is the whole authority:
it was issued by the runtime, it belongs to one user, and it is consumed once.

Nothing in the payload is trusted. The waiting tool re-reads the artifact this
endpoint was told about and recomputes its content identity before the image
reaches a model, so a wrong ``storage_path`` or ``sha256`` fails the capture
rather than substituting one image for another.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    ScreenCaptureCaptured,
    ScreenCaptureOutcome,
    ScreenCaptureRefusalCode,
    ScreenCaptureRefused,
    screen_capture_broker,
)
from pantaray_agents.schema.agent.base import JSONValue

router = APIRouter(prefix="/local", tags=["Action Screen Capture"])


class CapturedScreenResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["captured"]
    storage_path: str = Field(min_length=1, pattern=r"\S")
    mime_type: str = Field(min_length=1, pattern=r"\S")
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    app_name: str = Field(min_length=1, pattern=r"\S")
    captured_at: AwareDatetime


class RefusedScreenCaptureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["refused"]
    code: ScreenCaptureRefusalCode
    # Refusal details may name the application or the browser host, never the
    # window title: a title is often the private thing the filter is protecting.
    axis: Literal["app", "website", "file", "editing"] | None = None
    app_name: str | None = None
    host: str | None = None
    # Only apps the filter admits; never an excluded app or a window title.
    available_apps: list[str] | None = None


class ActionScreenCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_request_id: str = Field(min_length=1, pattern=r"\S")
    result: CapturedScreenResult | RefusedScreenCaptureResult = Field(
        discriminator="status"
    )


class ActionScreenCaptureResponse(BaseModel):
    accepted: bool


@router.post("/action-screen-capture", response_model=ActionScreenCaptureResponse)
async def answer_action_screen_capture(
    request: ActionScreenCaptureRequest,
    user_id: str = Depends(get_current_user_id_from_token),
) -> ActionScreenCaptureResponse:
    answer = screen_capture_broker().answer(
        capture_request_id=request.capture_request_id,
        user_id=str(user_id),
        outcome=_build_outcome(request.result),
    )
    if answer == "accepted":
        return ActionScreenCaptureResponse(accepted=True)
    if answer == "unknown_request":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no capture is waiting for this request id",
        )
    if answer == "user_mismatch":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="capture request belongs to another user",
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="capture request has already been answered",
    )


def _build_outcome(
    result: CapturedScreenResult | RefusedScreenCaptureResult,
) -> ScreenCaptureOutcome:
    if isinstance(result, RefusedScreenCaptureResult):
        details: dict[str, JSONValue] = {}
        if result.axis is not None:
            details["axis"] = result.axis
        if result.app_name is not None:
            details["app_name"] = result.app_name
        if result.host is not None:
            details["host"] = result.host
        if result.available_apps is not None:
            details["available_apps"] = list(result.available_apps)
        return ScreenCaptureRefused(code=result.code, details=details)
    return ScreenCaptureCaptured(
        storage_path=result.storage_path,
        mime_type=result.mime_type,
        byte_size=result.byte_size,
        sha256=result.sha256,
        width_px=result.width_px,
        height_px=result.height_px,
        app_name=result.app_name,
        captured_at=result.captured_at,
    )


__all__ = ["router"]

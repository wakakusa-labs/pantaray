"""Requests from the ``capture_screen`` tool to Electron main.

Electron answers with ``POST /local/action-screen-capture``; the rendezvous itself
is ``OneShotRequests``. A request that is never answered times out and reports
``CAPTURE_TIMED_OUT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.websocket.server_messages import (
    ScreenCaptureRequestedMessage,
)

from .deliverable_sessions import deliver_desktop_request
from .one_shot_requests import OneShotRequests


@dataclass(frozen=True, slots=True)
class ScreenCaptureCaptured:
    """Electron's account of an image it wrote under the artifact root.

    Every field is a claim. The tool re-reads the artifact and recomputes the
    content identity before it hands anything to the LLM.
    """

    storage_path: str
    mime_type: str
    byte_size: int
    sha256: str
    width_px: int
    height_px: int
    app_name: str
    captured_at: datetime


ScreenCaptureRefusalCode = Literal[
    "SCREEN_RECORDING_PERMISSION_REQUIRED",
    "CAPTURE_TARGET_NOT_FOUND",
    "CAPTURE_REFUSED_BY_PRIVACY_FILTER",
    "CAPTURE_REFUSED_PASSWORD_MANAGER",
    "CAPTURE_REFUSED_URL_UNAVAILABLE",
    "CAPTURE_REFUSED_PRIVATE_WINDOW",
    "CAPTURE_REFUSED_SENSITIVE_PAGE",
]
"""Every reason a capture can be refused. Closed so the model is never handed an
unexplained failure, and so each code has exactly one guidance message."""


@dataclass(frozen=True, slots=True)
class ScreenCaptureRefused:
    """A refusal decided before any pixels were read."""

    code: ScreenCaptureRefusalCode
    details: dict[str, JSONValue]


type ScreenCaptureOutcome = ScreenCaptureCaptured | ScreenCaptureRefused


async def announce_screen_capture_request(
    *,
    user_id: str,
    action_id: str,
    process_id: str,
    tool_request_id: str,
    capture_request_id: str,
    app_name: str,
) -> None:
    """Ask the owner's desktop app for the capture.

    The message names the approved app and carries no capture policy: what may be
    captured is decided in Electron main, against settings the runtime never sees.
    """

    await deliver_desktop_request(
        user_id,
        ScreenCaptureRequestedMessage(
            kind="action",
            process_id=process_id,
            action_id=action_id,
            tool_request_id=tool_request_id,
            capture_request_id=capture_request_id,
            app_name=app_name,
        ),
    )


_BROKER = OneShotRequests[ScreenCaptureOutcome]()


def screen_capture_broker() -> OneShotRequests[ScreenCaptureOutcome]:
    """The process-wide broker shared by the tool and the HTTP endpoint."""

    return _BROKER


__all__ = [
    "ScreenCaptureCaptured",
    "ScreenCaptureOutcome",
    "ScreenCaptureRefusalCode",
    "ScreenCaptureRefused",
    "announce_screen_capture_request",
    "screen_capture_broker",
]

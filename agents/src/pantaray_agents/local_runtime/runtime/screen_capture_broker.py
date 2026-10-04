"""Rendezvous between the ``capture_screen`` tool and Electron main.

The Action runtime and the local backend HTTP app share one uvicorn process, so a
capture request is held in memory: the tool opens a request, the runtime pushes the
request id to Electron over the orchestration WebSocket, and Electron answers it with
``POST /local/action-screen-capture``.

A request id is consumed exactly once. A second answer is rejected rather than
overwriting the first, so a replayed POST cannot swap the artifact a tool already
verified, and an answer for another user is refused before it is read.

Design limit: a pending request does not survive a local backend restart. The waiting
tool then times out and reports ``CAPTURE_TIMED_OUT``, which is the same fail-closed
result as an Electron that never answers.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from .process_events import append_local_process_event

SCREEN_CAPTURE_REQUESTED_EVENT = "screen_capture_requested"

_ROOT_PROCESS_SQL = """
SELECT COALESCE(process.parent_process_id, process.process_id) AS root_process_id
FROM processes AS process
WHERE process.process_id = :process_id AND process.user_id = :user_id
"""


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

type ScreenCaptureAnswerStatus = Literal[
    "accepted",
    "unknown_request",
    "user_mismatch",
    "already_answered",
]


@dataclass(slots=True)
class _PendingCapture:
    user_id: str
    outcome: asyncio.Future[ScreenCaptureOutcome] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class ScreenCaptureBroker:
    """One-shot capture requests keyed by a server-issued id."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingCapture] = {}

    def open(self, *, user_id: str) -> str:
        capture_request_id = str(uuid.uuid4())
        self._pending[capture_request_id] = _PendingCapture(user_id=user_id)
        return capture_request_id

    async def wait(
        self,
        *,
        capture_request_id: str,
        timeout_seconds: float,
    ) -> ScreenCaptureOutcome | None:
        """Await the answer, or return None once the deadline passes.

        The request is closed either way: a late answer finds nothing to resolve
        instead of resolving a request nobody is waiting on any more.
        """

        pending = self._pending.get(capture_request_id)
        if pending is None:
            raise KeyError(capture_request_id)
        try:
            return await asyncio.wait_for(pending.outcome, timeout=timeout_seconds)
        except TimeoutError:
            return None
        finally:
            self.close(capture_request_id=capture_request_id)

    def answer(
        self,
        *,
        capture_request_id: str,
        user_id: str,
        outcome: ScreenCaptureOutcome,
    ) -> ScreenCaptureAnswerStatus:
        pending = self._pending.get(capture_request_id)
        if pending is None:
            return "unknown_request"
        if pending.user_id != user_id:
            return "user_mismatch"
        if pending.outcome.done():
            return "already_answered"
        pending.outcome.set_result(outcome)
        return "accepted"

    def close(self, *, capture_request_id: str) -> None:
        pending = self._pending.pop(capture_request_id, None)
        if pending is not None and not pending.outcome.done():
            pending.outcome.cancel()


def announce_screen_capture_request(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    process_id: str,
    tool_request_id: str,
    capture_request_id: str,
    app_name: str,
) -> None:
    """Publish the request on the stream Electron actually reads.

    Only the root Action process is forwarded to the desktop client, so a
    subagent's request is announced on its parent's stream. The payload names the
    approved app and carries no capture policy: what may be captured is decided
    in Electron main, against settings the runtime never sees.
    """

    append_local_process_event(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        process_id=_resolve_root_process_id(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            process_id=process_id,
        ),
        event_type=SCREEN_CAPTURE_REQUESTED_EVENT,
        payload={
            "action_id": action_id,
            "process_id": process_id,
            "tool_request_id": tool_request_id,
            "capture_request_id": capture_request_id,
            "app_name": app_name,
        },
    )


def _resolve_root_process_id(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    process_id: str,
) -> str:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            _ROOT_PROCESS_SQL,
            {"process_id": process_id, "user_id": user_id},
        ).fetchone()
    if row is None:
        raise MigrationError(
            f"screen capture request has no owning process: {process_id}"
        )
    return str(row[0])


_BROKER = ScreenCaptureBroker()


def screen_capture_broker() -> ScreenCaptureBroker:
    """The process-wide broker shared by the tool and the HTTP endpoint."""

    return _BROKER


__all__ = [
    "SCREEN_CAPTURE_REQUESTED_EVENT",
    "ScreenCaptureAnswerStatus",
    "ScreenCaptureBroker",
    "ScreenCaptureCaptured",
    "ScreenCaptureOutcome",
    "ScreenCaptureRefusalCode",
    "ScreenCaptureRefused",
    "announce_screen_capture_request",
    "screen_capture_broker",
]

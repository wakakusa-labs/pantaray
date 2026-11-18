"""Run browser operations in Electron main, in the one session each run owns.

A run, named by its ``actor_process_id``, owns at most one browser session. The
runtime mints the session id; Electron opens the session on the first operation
that names it and refuses every operation naming an id it was told to close. The
registry is this process's memory under a lock: runs call in from worker threads
with loops of their own, and run ends and the periodic sweep from others.

An operation is a one-shot request like an HTML render. It goes to the user's
newest desktop session, and Electron answers it with ``POST /local/action-browser``.
A PNG or PDF Electron says it wrote to
``{artifact_root}/.tmp/browser/{operation_request_id}.{png|pdf}`` is re-read and
checked against the claimed ``byte_size`` and ``sha256``. The file is removed on
every path, answered or not.

Once a run's end has begun, no session is opened and no operation is sent for it.
A close is idempotent and unanswered; the periodic sweep retries one that reached
no desktop session, and drops an owner only once its run is over and every close
it needed was delivered.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_regular_file_descriptor,
)
from pantaray_agents.schema.websocket.server_messages import (
    BrowserOperation,
    BrowserOperationRequestedMessage,
    BrowserPagePolicy,
    BrowserSessionCloseRequestedMessage,
)

from ..storage.migrations.connection import configure_connection
from .browser_contract import (
    BrowserArtifactClaim,
    BrowserContactedHost,
    BrowserEngineFailureCode,
    BrowserOperationFailed,
    BrowserOperationSucceeded,
)
from .deliverable_sessions import deliver_desktop_request
from .one_shot_requests import OneShotRequests
from .runtime_env import read_local_runtime_artifact_root

BROWSER_TEMP_DIRECTORY = ".tmp/browser"
# Coupled to Electron's per-operation budgets (frontend/electron/src/browser): the
# longest is navigate's 20 s, and a ``wait`` stays under it because its bound is
# BROWSER_MAX_WAIT_SECONDS. The rest covers dispatch and the answer's round trip.
# Raise this whenever Electron's longest budget grows.
_ELECTRON_LONGEST_OPERATION_BUDGET_SECONDS = 20.0
BROWSER_RELAY_TIMEOUT_SECONDS = _ELECTRON_LONGEST_OPERATION_BUDGET_SECONDS + 10.0

BrowserRelayFailureCode = (
    BrowserEngineFailureCode
    | Literal["BROWSER_RELAY_TIMED_OUT", "BROWSER_INTEGRITY_FAILED"]
)

type BrowserAnswer = BrowserOperationSucceeded | BrowserOperationFailed

# Electron has dropped the session, so the run's next operation needs a new id.
_SESSION_ENDING_CODES: frozenset[BrowserEngineFailureCode] = frozenset(
    {"BROWSER_RENDERER_CRASHED", "BROWSER_SESSION_GONE"}
)
_ARTIFACT_KIND_BY_ACTION: dict[str, Literal["png", "pdf"]] = {
    "screenshot": "png",
    "print": "pdf",
}
_LIVE_OWNERS_SQL = """
SELECT process_id FROM processes
WHERE status IN ('enqueued', 'running', 'paused') AND process_id IN ({placeholders})
"""


@dataclass(frozen=True, slots=True)
class BrowserOperationDone:
    """Electron's answer, its artifact verified. ``artifact`` holds the bytes of the
    PNG (``screenshot``) or PDF (``print``) that ``result.artifact`` describes, already
    removed from disk; it is None for every other operation."""

    result: BrowserOperationSucceeded
    artifact: bytes | None


@dataclass(frozen=True, slots=True)
class BrowserRelayFailed:
    code: BrowserRelayFailureCode
    message: str
    # Hosts the page reached before it failed, still to be reported.
    network: tuple[BrowserContactedHost, ...]


type BrowserOperationOutcome = BrowserOperationDone | BrowserRelayFailed


@dataclass(frozen=True, slots=True)
class _Session:
    user_id: str
    session_id: str


@dataclass(slots=True)
class _Owner:
    ending: bool = False
    current: _Session | None = None
    # Sessions whose close no desktop session has received yet.
    unclosed: set[_Session] = field(default_factory=set)


_BROKER = OneShotRequests[BrowserAnswer]()
_LOCK = threading.Lock()
_OWNERS: dict[str, _Owner] = {}


def browser_operation_broker() -> OneShotRequests[BrowserAnswer]:
    """The process-wide broker shared by the relay and the HTTP endpoint."""

    return _BROKER


async def request_browser_operation(
    *,
    user_id: str,
    action_id: str,
    owner_process_id: str,
    tool_request_id: str,
    operation: BrowserOperation,
    policy: BrowserPagePolicy,
) -> BrowserOperationOutcome:
    """Run ``operation`` in the owner's session, opening one if it has none.

    The caller has validated the operation and resolved its local paths, and
    ``policy`` is what the page may load and write to from now on.
    """

    session = _current_session(owner_process_id=owner_process_id, user_id=user_id)
    if session is None:
        return BrowserRelayFailed(
            code="BROWSER_SESSION_GONE",
            message="This run is ending, so its browser is closed.",
            network=(),
        )
    artifact_root = read_local_runtime_artifact_root()
    broker = browser_operation_broker()
    operation_request_id = broker.open(user_id=user_id)
    try:
        await deliver_desktop_request(
            user_id,
            BrowserOperationRequestedMessage(
                operation_request_id=operation_request_id,
                session_id=session.session_id,
                action_id=action_id,
                process_id=owner_process_id,
                tool_request_id=tool_request_id,
                operation=operation,
                policy=policy,
            ),
        )
        answer = await broker.wait(
            request_id=operation_request_id,
            timeout_seconds=BROWSER_RELAY_TIMEOUT_SECONDS,
        )
        if answer is None:
            return BrowserRelayFailed(
                code="BROWSER_RELAY_TIMED_OUT",
                message=(
                    "The desktop app did not answer the browser operation in "
                    f"{int(BROWSER_RELAY_TIMEOUT_SECONDS)}s."
                ),
                network=(),
            )
        if isinstance(answer, BrowserOperationFailed):
            if answer.code in _SESSION_ENDING_CODES:
                _forget_session(owner_process_id=owner_process_id, session=session)
            return BrowserRelayFailed(
                code=answer.code, message=answer.message, network=tuple(answer.network)
            )
        # Off the event loop: a PDF may be large, and this loop may also serve the
        # local HTTP API and the WebSocket.
        return await asyncio.to_thread(
            _verify_artifact,
            artifact_root=artifact_root,
            operation_request_id=operation_request_id,
            expected_kind=_ARTIFACT_KIND_BY_ACTION.get(operation.action),
            answer=answer,
        )
    finally:
        # Closing is idempotent; it matters when the delivery itself failed.
        broker.close(request_id=operation_request_id)
        for kind in ("png", "pdf"):
            (artifact_root / _artifact_path(operation_request_id, kind)).unlink(
                missing_ok=True
            )


async def close_browser_session(*, owner_process_id: str) -> None:
    """Close the owner's session, if it has one. The run may open a new one."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None:
            _retire_current(owner)
    await _deliver_closes(owner_process_id)


async def end_browser_sessions(*, owner_process_ids: tuple[str, ...]) -> None:
    """At the end of these runs: refuse their operations from now on, then close
    their sessions.

    The refusal is in place before the first await, so a caller cancelled during
    delivery still leaves it set, and the sweep delivers what is left.
    """

    with _LOCK:
        for owner_process_id in owner_process_ids:
            owner = _OWNERS.setdefault(owner_process_id, _Owner())
            owner.ending = True
            _retire_current(owner)
    for owner_process_id in owner_process_ids:
        await _deliver_closes(owner_process_id)


async def sweep_browser_sessions(*, db_path: Path, busy_timeout_ms: int) -> None:
    """End the owners whose run is no longer live, retry every undelivered close,
    and forget owners with nothing left to close and no live run."""

    with _LOCK:
        owner_process_ids = tuple(_OWNERS)
    if not owner_process_ids:
        return
    # Only the owners listed above are judged: one registered since then is not
    # in the query, and its absence from ``live`` says nothing about its run.
    live = _live_owner_ids(db_path, busy_timeout_ms, owner_process_ids)
    ended = tuple(
        owner_process_id
        for owner_process_id in owner_process_ids
        if owner_process_id not in live
    )
    await end_browser_sessions(owner_process_ids=ended)
    for owner_process_id in live:
        await _deliver_closes(owner_process_id)
    with _LOCK:
        for owner_process_id in ended:
            owner = _OWNERS.get(owner_process_id)
            if owner is not None and not owner.unclosed:
                del _OWNERS[owner_process_id]


def _current_session(*, owner_process_id: str, user_id: str) -> _Session | None:
    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if owner.ending:
            return None
        if owner.current is None:
            owner.current = _Session(user_id=user_id, session_id=str(uuid.uuid4()))
        return owner.current


def _forget_session(*, owner_process_id: str, session: _Session) -> None:
    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None and owner.current == session:
            owner.current = None


def _retire_current(owner: _Owner) -> None:
    """Under the lock: the current session now only waits for its close."""

    if owner.current is not None:
        owner.unclosed.add(owner.current)
        owner.current = None


async def _deliver_closes(owner_process_id: str) -> None:
    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        unclosed = tuple(owner.unclosed) if owner is not None else ()
    for session in unclosed:
        delivered = await deliver_desktop_request(
            session.user_id,
            BrowserSessionCloseRequestedMessage(session_id=session.session_id),
        )
        if delivered:
            with _LOCK:
                owner = _OWNERS.get(owner_process_id)
                if owner is not None:
                    owner.unclosed.discard(session)


def _live_owner_ids(
    db_path: Path, busy_timeout_ms: int, owner_process_ids: tuple[str, ...]
) -> frozenset[str]:
    placeholders = ", ".join("?" for _ in owner_process_ids)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            _LIVE_OWNERS_SQL.format(placeholders=placeholders), owner_process_ids
        ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _artifact_path(operation_request_id: str, kind: Literal["png", "pdf"]) -> str:
    """Relative to the artifact root; Electron derives the same path from the id."""

    return f"{BROWSER_TEMP_DIRECTORY}/{operation_request_id}.{kind}"


def _verify_artifact(
    *,
    artifact_root: Path,
    operation_request_id: str,
    expected_kind: Literal["png", "pdf"] | None,
    answer: BrowserOperationSucceeded,
) -> BrowserOperationOutcome:
    def integrity_failed(reason: str) -> BrowserRelayFailed:
        return BrowserRelayFailed(
            code="BROWSER_INTEGRITY_FAILED",
            message=(
                "The browser's output does not match the desktop app's answer: "
                f"{reason}."
            ),
            # The page reached these hosts whether or not its output holds up.
            network=tuple(answer.network),
        )

    claim: BrowserArtifactClaim | None = answer.artifact
    if (None if claim is None else claim.kind) != expected_kind:
        return integrity_failed("the answer names a different kind of output")
    if claim is None:
        return BrowserOperationDone(result=answer, artifact=None)
    try:
        descriptor = open_regular_file_descriptor(
            root_path=artifact_root,
            relative_path=_artifact_path(operation_request_id, claim.kind),
        )
    except DescriptorPathError:
        return integrity_failed("no regular file was written")
    try:
        # Checked before reading, so a wrong claim never pulls an unbounded file
        # into memory.
        if os.fstat(descriptor).st_size != claim.byte_size:
            return integrity_failed("byte size differs")
        with open(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != claim.sha256:
        return integrity_failed("sha256 differs")
    return BrowserOperationDone(result=answer, artifact=data)


__all__ = [
    "BROWSER_RELAY_TIMEOUT_SECONDS",
    "BROWSER_TEMP_DIRECTORY",
    "BrowserAnswer",
    "BrowserOperationDone",
    "BrowserOperationOutcome",
    "BrowserRelayFailed",
    "BrowserRelayFailureCode",
    "browser_operation_broker",
    "close_browser_session",
    "end_browser_sessions",
    "request_browser_operation",
    "sweep_browser_sessions",
]

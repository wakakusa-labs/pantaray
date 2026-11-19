"""Run browser operations in Electron main, in the one session each run owns.

A run (its ``actor_process_id``) owns at most one session, whose id the runtime
mints; Electron refuses every operation naming an id it was told to close. The
registry is in memory under a lock, since callers run on several threads.

Each operation is one request and one answer (``OneShotRequests``), which
Electron posts to ``POST /local/action-browser``. A PNG or PDF at
``{artifact_root}/.tmp/browser/{operation_request_id}.{png|pdf}`` is checked
against the claimed size and sha256 and removed on every path.

No session is opened and no operation sent for a run whose end has begun: an
in-memory fence until its status is written, the status after. Closes are
idempotent and unanswered; the sweep retries undelivered ones and drops an owner
once its run is over and its closes were delivered.

The registry also keeps the hosts an approved ``allow_writes`` let the run's
pages write to: every operation's policy carries them, they outlast a close,
and the run's end drops them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
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
    BrowserNavigateOperation,
    BrowserOperation,
    BrowserOperationRequestedMessage,
    BrowserPagePolicy,
    BrowserSessionCloseRequestedMessage,
)

from ..storage.migrations.connection import configure_connection
from .browser_contract import (
    BrowserContactedHost,
    BrowserEngineFailureCode,
    BrowserOperationFailed,
    BrowserOperationSucceeded,
    BrowserRelayFailureCode,
)
from .deliverable_sessions import deliver_desktop_request
from .one_shot_requests import OneShotRequests
from .runtime_env import read_local_runtime_artifact_root

logger = logging.getLogger(__name__)

BROWSER_TEMP_DIRECTORY = ".tmp/browser"
# Coupled to Electron's per-operation budgets (frontend/electron/src/browser): the
# longest is navigate's 20 s, and a ``wait`` stays under it because its bound is
# BROWSER_MAX_WAIT_SECONDS. The rest covers dispatch and the answer's round trip.
# Raise this whenever Electron's longest budget grows.
_ELECTRON_LONGEST_OPERATION_BUDGET_SECONDS = 20.0
BROWSER_RELAY_TIMEOUT_SECONDS = _ELECTRON_LONGEST_OPERATION_BUDGET_SECONDS + 10.0
# A close is one small frame: still pending after this, the socket is stuck, and
# the sweep on the shared worker must not wait on it.
BROWSER_CLOSE_SEND_TIMEOUT_SECONDS = 5.0

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
    """Electron's answer, verified. ``artifact`` is the PNG or PDF ``result.artifact``
    describes, already removed from disk, for ``screenshot`` and ``print`` only."""

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
    # Hosts the run's pages may write to; it only grows until the run ends.
    write_hosts: set[str] = field(default_factory=set)
    # The host the current session's last navigate opened, as Electron parsed
    # the requested URL; never read from the page. None for a file.
    page_host: str | None = None


_BROKER = OneShotRequests[BrowserAnswer]()
_LOCK = threading.Lock()
_OWNERS: dict[str, _Owner] = {}


def browser_operation_broker() -> OneShotRequests[BrowserAnswer]:
    """The process-wide broker shared by the relay and the HTTP endpoint."""

    return _BROKER


async def request_browser_operation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    owner_process_id: str,
    operation: BrowserOperation,
    policy: BrowserPagePolicy,
) -> BrowserOperationOutcome:
    """Run ``operation`` in the owner's session, opening one if it has none.

    The caller has validated the operation and resolved its local paths, and
    ``policy`` is what the page may load from now on; the page may write to the
    run's approved hosts, whatever ``policy`` names.
    """

    session = _current_session(
        db_path, busy_timeout_ms, owner_process_id=owner_process_id, user_id=user_id
    )
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
                operation=operation,
                policy=policy.model_copy(
                    update={"write_hosts": list(browser_write_hosts(owner_process_id))}
                ),
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
                with _LOCK:
                    owner = _OWNERS.get(owner_process_id)
                    if owner is not None and owner.current == session:
                        owner.current = None
                        owner.page_host = None
            return BrowserRelayFailed(
                code=answer.code, message=answer.message, network=tuple(answer.network)
            )
        if isinstance(operation, BrowserNavigateOperation):
            with _LOCK:
                owner = _OWNERS.get(owner_process_id)
                if owner is not None and owner.current == session:
                    owner.page_host = answer.opened_host
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


def allow_browser_writes(*, owner_process_id: str, host: str) -> None:
    """Let the owner's pages write to ``host`` from its next operation on."""

    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if not owner.ending:
            owner.write_hosts.add(host)


def browser_write_hosts(owner_process_id: str) -> tuple[str, ...]:
    """The hosts the owner's pages may write to, sorted."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return () if owner is None else tuple(sorted(owner.write_hosts))


def browser_page_host(owner_process_id: str) -> str | None:
    """The host the owner's open page was navigated to; None for a local file or
    no page. The page may have followed links since."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return None if owner is None else owner.page_host


async def close_browser_session(*, owner_process_id: str) -> None:
    """Close the owner's session, if it has one. The run may open a new one."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None:
            _retire_current(owner)
    await _deliver_closes(owner_process_id)


async def end_browser_sessions(*, owner_process_ids: tuple[str, ...]) -> None:
    """Refuse these runs' operations from now on, then close their sessions. The
    refusal is set before the first await, so it survives a cancelled caller."""

    with _LOCK:
        for owner_process_id in owner_process_ids:
            owner = _OWNERS.setdefault(owner_process_id, _Owner())
            owner.ending = True
            owner.write_hosts.clear()
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


def _current_session(
    db_path: Path, busy_timeout_ms: int, *, owner_process_id: str, user_id: str
) -> _Session | None:
    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None and owner.ending:
            return None
        if owner is not None and owner.current is not None:
            return owner.current
    # The sweep forgets ended owners: past that, the run's status is the fence.
    if owner_process_id not in _live_owner_ids(
        db_path, busy_timeout_ms, (owner_process_id,)
    ):
        return None
    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if owner.ending:
            return None
        if owner.current is None:
            owner.current = _Session(user_id=user_id, session_id=str(uuid.uuid4()))
        return owner.current


def _retire_current(owner: _Owner) -> None:
    """Under the lock: the current session now only waits for its close."""

    if owner.current is not None:
        owner.unclosed.add(owner.current)
        owner.current = None
    owner.page_host = None


async def _deliver_closes(owner_process_id: str) -> None:
    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        unclosed = tuple(owner.unclosed) if owner is not None else ()
    for session in unclosed:
        try:
            delivered = await asyncio.wait_for(
                deliver_desktop_request(
                    session.user_id,
                    BrowserSessionCloseRequestedMessage(session_id=session.session_id),
                ),
                timeout=BROWSER_CLOSE_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # Left unclosed for the sweep; the next owner's closes still go out.
            logger.warning("Browser session close not sent in time; will retry")
            return
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
    # Relative to the artifact root; Electron derives the same path from the id.
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

    claim = answer.artifact
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
    "BROWSER_CLOSE_SEND_TIMEOUT_SECONDS",
    "BROWSER_RELAY_TIMEOUT_SECONDS",
    "BROWSER_TEMP_DIRECTORY",
    "BrowserAnswer",
    "BrowserOperationDone",
    "BrowserOperationOutcome",
    "BrowserRelayFailed",
    "allow_browser_writes",
    "browser_operation_broker",
    "browser_page_host",
    "browser_write_hosts",
    "close_browser_session",
    "end_browser_sessions",
    "request_browser_operation",
    "sweep_browser_sessions",
]

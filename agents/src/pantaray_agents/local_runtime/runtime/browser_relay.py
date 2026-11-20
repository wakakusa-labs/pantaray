"""Run browser operations in Electron main, in the sessions each run owns.

A run (its ``actor_process_id``) has two pages, kept apart: the app's hidden page
(``"pantaray"``) and one tab of its own in the user's browser (``"user"``). Each
page has at most one session, whose id the runtime mints; Electron refuses every
operation naming an id it was told to close. The registry is in memory under a
lock, since callers run on several threads.

Each operation is one request and one answer (``OneShotRequests``), which
Electron posts to ``POST /local/action-browser``. A PNG or PDF at
``{artifact_root}/.tmp/browser/{operation_request_id}.{png|pdf}`` is checked
against the claimed size and sha256 and removed on every path.

No session is opened and no operation sent for a run whose end has begun: an
in-memory fence until its status is written, the status after. Closes are
idempotent and unanswered; the sweep retries undelivered ones and drops an owner
once its run is over and its closes were delivered.

The registry also keeps, per page, the hosts an approved ``allow_writes`` let it
write to and, for the user's page, the hosts the user approved it to open: every
operation on that page carries them, a host approved on one page never reaches
the other, they outlast a close, and the run's end drops them. The user's page
is bound to the user browser of its first operation for the whole run, since
its hosts were approved for that browser.

The user's page waits for its first answer without a deadline, since the user
must allow the connection in their browser; once one answer came, Electron's
budgets bound each operation as on the app's page.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, overload

from pantaray_agents.schema.user_browser import BrowserChoice, UserBrowserId
from pantaray_agents.schema.websocket.server_messages import (
    BrowserNavigateOperation,
    BrowserOperation,
    BrowserOperationRequestedMessage,
    BrowserPagePolicy,
    BrowserSessionCloseRequestedMessage,
    BrowserUserTarget,
)

from ..storage.migrations.connection import configure_connection
from .browser_artifact import (
    BrowserArtifactKind,
    BrowserArtifactMismatchError,
    browser_artifact_path,
    read_claimed_artifact,
)
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
# A user's browser that could not be connected to leaves no session either, and
# the next try connects, and asks the user, again.
_SESSION_ENDING_CODES: frozenset[BrowserEngineFailureCode] = frozenset(
    {
        "BROWSER_RENDERER_CRASHED",
        "BROWSER_SESSION_GONE",
        "BROWSER_USER_NOT_RUNNING",
        "BROWSER_USER_FILE_BLOCKED",
        "BROWSER_USER_DENIED",
        "BROWSER_USER_UNAVAILABLE",
    }
)
_ARTIFACT_KIND_BY_ACTION: dict[str, BrowserArtifactKind] = {
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
class _Page:
    current: _Session | None = None
    # Hosts the page may write to; it only grows until the run ends.
    write_hosts: set[str] = field(default_factory=set)
    # The user's page only: hosts it may open, kept like ``write_hosts``.
    read_hosts: set[str] = field(default_factory=set)
    # The host the current session's last navigate opened, as Electron parsed
    # the requested URL; never read from the page. None for a file.
    page_host: str | None = None
    # Whether the current session has answered once, so its connection is up.
    connected: bool = False
    # The user's page only: the browser its first operation named, for the run.
    user_browser: UserBrowserId | None = None


@dataclass(frozen=True, slots=True)
class _Opened:
    """A page's session, with what the page held when it was picked."""

    session: _Session
    connected: bool
    write_hosts: tuple[str, ...]
    read_hosts: tuple[str, ...]


def _pages() -> dict[BrowserChoice, _Page]:
    return {"pantaray": _Page(), "user": _Page()}


@dataclass(slots=True)
class _Owner:
    ending: bool = False
    # Sessions whose close no desktop session has received yet.
    unclosed: set[_Session] = field(default_factory=set)
    pages: dict[BrowserChoice, _Page] = field(default_factory=_pages)


_BROKER = OneShotRequests[BrowserAnswer]()
_LOCK = threading.Lock()
_OWNERS: dict[str, _Owner] = {}


def browser_operation_broker() -> OneShotRequests[BrowserAnswer]:
    """The process-wide broker shared by the relay and the HTTP endpoint."""

    return _BROKER


@overload
async def request_browser_operation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    owner_process_id: str,
    browser: Literal["pantaray"],
    user_browser: None,
    operation: BrowserOperation,
    policy: BrowserPagePolicy,
    action_id: None = None,
) -> BrowserOperationOutcome: ...


@overload
async def request_browser_operation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    owner_process_id: str,
    browser: Literal["user"],
    user_browser: UserBrowserId,
    operation: BrowserOperation,
    policy: BrowserPagePolicy,
    action_id: str,
) -> BrowserOperationOutcome: ...


async def request_browser_operation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    owner_process_id: str,
    browser: BrowserChoice,
    user_browser: UserBrowserId | None,
    operation: BrowserOperation,
    policy: BrowserPagePolicy,
    action_id: str | None = None,
) -> BrowserOperationOutcome:
    """Run ``operation`` in the session of the owner's ``browser`` page, opening
    one if it has none.

    The caller has validated the operation and resolved its local paths, and
    ``policy`` is what the page may load from now on; the page may write to the
    hosts approved for it, whatever ``policy`` names. ``user_browser`` is the
    user's browser the ``"user"`` page runs in; naming another than the one the
    page is bound to (:func:`bound_user_browser`) raises ``ValueError``.
    ``action_id`` is the run's Action, whose chat shows the wait while the user
    is asked to allow the connection.
    """

    opened = _current_session(
        db_path,
        busy_timeout_ms,
        owner_process_id=owner_process_id,
        user_id=user_id,
        browser=browser,
        user_browser=user_browser,
    )
    if opened is None:
        return BrowserRelayFailed(
            code="BROWSER_SESSION_GONE",
            message="This run is ending, so its browser is closed.",
            network=(),
        )
    session = opened.session
    user_target = None
    if user_browser is not None:
        # The overloads require it with the user's browser.
        assert action_id is not None
        user_target = BrowserUserTarget(
            browser=user_browser,
            action_id=action_id,
            read_hosts=list(opened.read_hosts),
        )
    # Until the user's browser answers once, the user is being asked to turn
    # remote debugging on and allow the connection, which takes as long as they
    # take; stopping the run ends it.
    timeout_seconds = (
        None
        if browser == "user" and not opened.connected
        else BROWSER_RELAY_TIMEOUT_SECONDS
    )
    artifact_root = read_local_runtime_artifact_root()
    broker = browser_operation_broker()
    operation_request_id = broker.open(user_id=user_id)
    try:
        delivered = await deliver_desktop_request(
            user_id,
            BrowserOperationRequestedMessage(
                operation_request_id=operation_request_id,
                session_id=session.session_id,
                operation=operation,
                policy=policy.model_copy(
                    update={"write_hosts": list(opened.write_hosts)}
                ),
                user_browser=user_target,
            ),
        )
        if not delivered and timeout_seconds is None:
            # No answer can come, and nothing would end the wait.
            return BrowserRelayFailed(
                code="BROWSER_RELAY_TIMED_OUT",
                message="The desktop app did not receive the browser operation.",
                network=(),
            )
        answer = await broker.wait(
            request_id=operation_request_id, timeout_seconds=timeout_seconds
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
        with _LOCK:
            owner = _OWNERS.get(owner_process_id)
            page = None if owner is None else owner.pages[browser]
            if page is not None and page.current == session:
                if (
                    isinstance(answer, BrowserOperationFailed)
                    and answer.code in _SESSION_ENDING_CODES
                ):
                    page.current = None
                    page.page_host = None
                    page.connected = False
                else:
                    page.connected = True
                    if isinstance(answer, BrowserOperationSucceeded) and isinstance(
                        operation, BrowserNavigateOperation
                    ):
                        page.page_host = answer.opened_host
        if isinstance(answer, BrowserOperationFailed):
            return BrowserRelayFailed(
                code=answer.code, message=answer.message, network=tuple(answer.network)
            )
        try:
            # Off the event loop: a PDF may be large, and this loop may also serve
            # the local HTTP API and the WebSocket.
            artifact = await asyncio.to_thread(
                read_claimed_artifact,
                artifact_root=artifact_root,
                operation_request_id=operation_request_id,
                expected_kind=_ARTIFACT_KIND_BY_ACTION.get(operation.action),
                claim=answer.artifact,
            )
        except BrowserArtifactMismatchError as mismatch:
            return BrowserRelayFailed(
                code="BROWSER_INTEGRITY_FAILED",
                message=(
                    "The browser's output does not match the desktop app's answer: "
                    f"{mismatch}."
                ),
                # The page reached these hosts whether or not its output holds up.
                network=tuple(answer.network),
            )
        return BrowserOperationDone(result=answer, artifact=artifact)
    finally:
        # Closing is idempotent; it matters when the delivery itself failed.
        broker.close(request_id=operation_request_id)
        for kind in ("png", "pdf"):
            (artifact_root / browser_artifact_path(operation_request_id, kind)).unlink(
                missing_ok=True
            )


def allow_browser_writes(
    *, owner_process_id: str, browser: BrowserChoice, host: str
) -> None:
    """Let the owner's ``browser`` page write to ``host`` from its next operation
    on."""

    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if not owner.ending:
            owner.pages[browser].write_hosts.add(host)


def allow_user_browser_reads(*, owner_process_id: str, host: str) -> None:
    """Let the owner's page in the user's browser open ``host`` from its next
    operation on."""

    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if not owner.ending:
            owner.pages["user"].read_hosts.add(host)


def browser_write_hosts(
    owner_process_id: str, browser: BrowserChoice
) -> tuple[str, ...]:
    """The hosts the owner's ``browser`` page may write to, sorted."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return () if owner is None else tuple(sorted(owner.pages[browser].write_hosts))


def user_browser_read_hosts(owner_process_id: str) -> tuple[str, ...]:
    """The hosts the owner's page in the user's browser may open, sorted."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return () if owner is None else tuple(sorted(owner.pages["user"].read_hosts))


def bound_user_browser(owner_process_id: str) -> UserBrowserId | None:
    """The user browser the owner's page in it is bound to for the run; None
    before its first operation."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return None if owner is None else owner.pages["user"].user_browser


def browser_page_host(owner_process_id: str, browser: BrowserChoice) -> str | None:
    """The host the owner's open ``browser`` page was navigated to; None for a
    local file or no page. The page may have followed links since."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        return None if owner is None else owner.pages[browser].page_host


async def close_browser_session(
    *, owner_process_id: str, browser: BrowserChoice
) -> None:
    """Close the session of the owner's ``browser`` page, if it has one. The run
    may open a new one."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None:
            _retire_current(owner, owner.pages[browser])
    await _deliver_closes(owner_process_id)


async def end_browser_sessions(*, owner_process_ids: tuple[str, ...]) -> None:
    """Refuse these runs' operations from now on, then close both pages'
    sessions. The refusal is set before the first await, so it survives a
    cancelled caller."""

    with _LOCK:
        for owner_process_id in owner_process_ids:
            owner = _OWNERS.setdefault(owner_process_id, _Owner())
            owner.ending = True
            for page in owner.pages.values():
                page.write_hosts.clear()
                page.read_hosts.clear()
                _retire_current(owner, page)
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
    db_path: Path,
    busy_timeout_ms: int,
    *,
    owner_process_id: str,
    user_id: str,
    browser: BrowserChoice,
    user_browser: UserBrowserId | None,
) -> _Opened | None:
    """The ``browser`` page's session and the hosts it carries, taken together;
    None once the run is ending."""

    with _LOCK:
        owner = _OWNERS.get(owner_process_id)
        if owner is not None and owner.ending:
            return None
        if owner is not None:
            page = owner.pages[browser]
            _bind(page, user_browser)
            if page.current is not None:
                return _opened(page, page.current)
    # The sweep forgets ended owners: past that, the run's status is the fence.
    if owner_process_id not in _live_owner_ids(
        db_path, busy_timeout_ms, (owner_process_id,)
    ):
        return None
    with _LOCK:
        owner = _OWNERS.setdefault(owner_process_id, _Owner())
        if owner.ending:
            return None
        page = owner.pages[browser]
        _bind(page, user_browser)
        if page.current is None:
            page.current = _Session(user_id=user_id, session_id=str(uuid.uuid4()))
        return _opened(page, page.current)


def _bind(page: _Page, user_browser: UserBrowserId | None) -> None:
    """Under the lock: the page's first operation binds it for the run."""

    if page.user_browser is not None and page.user_browser != user_browser:
        raise ValueError(
            f"the run's page is bound to {page.user_browser}, not {user_browser}"
        )
    page.user_browser = user_browser


def _opened(page: _Page, session: _Session) -> _Opened:
    """Under the lock."""

    return _Opened(
        session=session,
        connected=page.connected,
        write_hosts=tuple(sorted(page.write_hosts)),
        read_hosts=tuple(sorted(page.read_hosts)),
    )


def _retire_current(owner: _Owner, page: _Page) -> None:
    """Under the lock: the page's session now only waits for its close."""

    if page.current is not None:
        owner.unclosed.add(page.current)
        page.current = None
    page.page_host = None
    page.connected = False


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


__all__ = [
    "BROWSER_CLOSE_SEND_TIMEOUT_SECONDS",
    "BROWSER_RELAY_TIMEOUT_SECONDS",
    "BrowserAnswer",
    "BrowserOperationDone",
    "BrowserOperationOutcome",
    "BrowserRelayFailed",
    "allow_browser_writes",
    "allow_user_browser_reads",
    "bound_user_browser",
    "browser_operation_broker",
    "browser_page_host",
    "browser_write_hosts",
    "close_browser_session",
    "end_browser_sessions",
    "request_browser_operation",
    "sweep_browser_sessions",
    "user_browser_read_hosts",
]

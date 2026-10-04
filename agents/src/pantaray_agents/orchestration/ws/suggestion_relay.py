"""Relay Suggestion processes started by the local runtime to the live WS session.

The Suggestion is triggered by the short Insight's `reconsideration_reason`
inside the runtime worker, so the WebSocket is no longer the starting owner: it
forwards what the runtime already owns. A Suggestion that finishes while no
session is connected is discarded by the job before it is stored. One stored
is held until the release task decides, so a later session owes only a held
Suggestion, which the row itself records; the relay keeps no ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Final, NamedTuple

from starlette.websockets import WebSocketState

from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.orchestration.common.errors import error_from_payload
from pantaray_agents.orchestration.ws.background_task import spawn_ws_background_task
from pantaray_agents.orchestration.ws.deliverable_sessions import (
    register_deliverable_session,
    unregister_deliverable_session,
)
from pantaray_agents.orchestration.ws.error_meta import build_suggestion_error_meta
from pantaray_agents.orchestration.ws.suggestion_stream.events import (
    SuggestionStreamEventsMixin,
)
from pantaray_agents.orchestration.ws.suggestion_stream.repository_wait import (
    SuggestionRepositoryWaitMixin,
)
from pantaray_agents.orchestration.ws.suggestion_stream.types import (
    SUGGESTION_DB_DEPENDENCY,
    SUGGESTION_DB_GET_OP,
    SuggestionRowFetchError,
    SuggestionTerminalRow,
)
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.utils.public_error import public_ws_error

logger = logging.getLogger(__name__)

SUGGESTION_RELAY_TASK_KEY: Final[str] = "suggestion_relay_tick"
# One discovery read per second per live session; a Suggestion run includes an
# LLM call, so sub-second discovery buys nothing and the row wait below already
# polls at the job interval once a process is attached.
SUGGESTION_RELAY_TICK_SECONDS: Final[float] = 1.0
_RELAY_FAILURE_LOG_INTERVAL_SECONDS: Final[float] = 60.0

# `processes.status` values a suggestion process can still move out of.
LIVE_SUGGESTION_PROCESS_STATUSES: Final[tuple[str, str]] = ("enqueued", "running")


class LiveSuggestionProcess(NamedTuple):
    """One runtime suggestion process that has not reached a terminal status."""

    process_id: str
    suggestion_id: str


def read_relayable_suggestion_processes(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    since: str,
) -> list[LiveSuggestionProcess]:
    """Read the user's suggestion processes this session still owes a delivery.

    Live processes are always candidates. A process that already finished is a
    candidate only when it finished after `since` (the session start), so a run
    shorter than one relay tick is still delivered exactly once to this session
    without keeping a delivery ledger. So is a process whose Suggestion is still
    held, or was released after `since` (possibly before this session's first
    tick); a reaction also moves `updated_at`, so a reacted one is not.
    """
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            """
            SELECT process_id, suggestion_id
            FROM processes
            WHERE user_id = ?
              AND kind = 'suggestion'
              AND suggestion_id IS NOT NULL
              AND (
                status IN (?, ?)
                OR (completed_at IS NOT NULL
                    AND julianday(completed_at) >= julianday(?))
                OR suggestion_id IN (
                    SELECT suggestion_id FROM agent_suggestions
                    WHERE user_id = ?
                      AND (
                        delivery_state = 'held'
                        OR (delivery_state = 'released'
                            AND julianday(updated_at) >= julianday(?)
                            AND user_reaction IS NULL
                            AND action_status IS NULL)
                      )
                )
              )
            ORDER BY started_at ASC
            """,
            (user_id, *LIVE_SUGGESTION_PROCESS_STATUSES, since, user_id, since),
        ).fetchall()
    return [LiveSuggestionProcess(str(row[0]), str(row[1])) for row in rows]


class SuggestionRelayMixin(
    SuggestionRepositoryWaitMixin,
    SuggestionStreamEventsMixin,
):
    """Forward runtime-owned Suggestion processes over the live WS session."""

    def _init_suggestion_relay(self) -> None:
        # Session-scoped: a process delivered once is never delivered again, and
        # a new session re-discovers whatever the runtime still has in flight.
        self._relayed_suggestion_processes: set[str] = set()
        self._relay_since = now_utc_iso()

    def _register_suggestion_process(
        self,
        process_id: str,
        suggestion_id: str | None,
    ) -> None:
        self._process_metadata[process_id] = {
            "kind": "suggestion",
            "suggestion_id": suggestion_id,
        }
        if suggestion_id:
            # クライアント申告の suggestion_id は信用しないため、
            # このセッションが観測した ID を所有検証の材料として残す。
            self._issued_suggestion_ids.add(str(suggestion_id))

    def _release_suggestion_process(self, process_id: str) -> None:
        self._process_metadata.pop(process_id, None)

    def mark_suggestion_process_relayed(self, process_id: str) -> None:
        """A process this session already follows must not be relayed again.

        `resume_session` re-attaches a live process on reconnect; without this
        the next discovery tick would deliver its lifecycle a second time.
        """
        self._relayed_suggestion_processes.add(process_id)

    def follow_resumed_suggestion_process(
        self, process_id: str, suggestion_id: str
    ) -> None:
        """Deliver the terminal state of a process the client resumed mid-run.

        The previous session's result relay died with its socket, and the
        discovery tick skips a resumed process, so this session must wait for
        the row itself. `process_started` is not repeated: the client already
        holds the process.
        """
        self._register_suggestion_process(process_id, suggestion_id)
        spawn_ws_background_task(
            owner=self,
            task_key=process_id,
            coro=self._relay_suggestion_result(
                LiveSuggestionProcess(process_id, suggestion_id)
            ),
        )

    def can_deliver(self) -> bool:
        """Whether this session can still send; a failed send closes it for good."""
        return (
            not self._is_closed
            and self.websocket.client_state == WebSocketState.CONNECTED
        )

    def start_suggestion_relay(self) -> None:
        """Start forwarding runtime Suggestion processes for this session."""
        spawn_ws_background_task(
            owner=self,
            task_key=SUGGESTION_RELAY_TASK_KEY,
            coro=self._suggestion_relay_loop(),
        )
        # Only now: `_relay_since` is fixed, so whatever finishes from here on
        # falls inside this session's relay window.
        register_deliverable_session(str(self.user_id), self)

    def stop_suggestion_relay(self) -> None:
        """Stop the relay tick (called from the handler's close path)."""
        unregister_deliverable_session(str(self.user_id), self)
        self._task_supervisor.cancel(SUGGESTION_RELAY_TASK_KEY)

    async def _suggestion_relay_loop(self) -> None:
        poll_interval = SUGGESTION_RELAY_TICK_SECONDS
        try:
            while not self._is_closed:
                try:
                    await self._relay_live_suggestion_processes()
                except Exception as exc:  # noqa: BLE001
                    # A tick failure must not end the session; the unattached
                    # process is retried on the next tick.
                    if self._rate_limiter.should_log(
                        "suggestion_relay:tick",
                        interval_seconds=_RELAY_FAILURE_LOG_INTERVAL_SECONDS,
                    ):
                        logger.warning(
                            "Suggestion relay tick failed: session_id=%s error=%s",
                            self.session_id,
                            exc,
                            exc_info=True,
                        )
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return

    async def _relay_live_suggestion_processes(self) -> None:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        for process in read_relayable_suggestion_processes(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=str(self.user_id),
            since=self._relay_since,
        ):
            if process.process_id in self._relayed_suggestion_processes:
                continue
            await self._attach_suggestion_process(process)

    async def _attach_suggestion_process(self, process: LiveSuggestionProcess) -> None:
        self.session_store.ensure_process(self.session_id, process.process_id)
        self._register_suggestion_process(process.process_id, process.suggestion_id)
        started = await self._emit_process_started(
            process.process_id,
            process.suggestion_id,
            kind="suggestion",
        )
        if not started:
            # The socket is gone: leave no half-started public process behind.
            self._release_suggestion_process(process.process_id)
            self.session_store.clear_process(self.session_id, process.process_id)
            return
        self._relayed_suggestion_processes.add(process.process_id)
        self._sync_process_metadata_to_current_session(
            process.process_id,
            suggestion_id=process.suggestion_id,
            kind="suggestion",
        )
        spawn_ws_background_task(
            owner=self,
            task_key=process.process_id,
            coro=self._relay_suggestion_result(process),
        )

    async def _relay_suggestion_result(self, process: LiveSuggestionProcess) -> None:
        """Deliver the process's terminal state, whatever ends it.

        A process that emitted `process_started` must always emit
        `process_completed`, so every exit path terminates it explicitly.
        """
        process_id, suggestion_id = process
        try:
            row = await self._wait_for_suggestion_row(
                user_id=str(self.user_id),
                suggestion_id=suggestion_id,
                process_id=process_id,
            )
            await self._deliver_terminal_suggestion(process, row)
        except asyncio.CancelledError:
            await self._complete_relayed_suggestion(
                process_id,
                suggestion_id,
                status="canceled",
            )
        except SuggestionRowFetchError:
            await self.send_error(
                public_ws_error(
                    error_code="WS_DEPENDENCY_UNAVAILABLE",
                    request_id=self.session_id,
                    error_type=ErrorType.INTERNAL_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="Suggestion DB read failed (dependency unavailable).",
                    extra_error_details={
                        "dependency": SUGGESTION_DB_DEPENDENCY,
                        "operation": SUGGESTION_DB_GET_OP,
                    },
                ),
                process_id=process_id,
                meta=build_suggestion_error_meta(
                    suggestion_id=suggestion_id,
                    process_id=process_id,
                    stage="running_failed",
                    error_code="WS_DEPENDENCY_UNAVAILABLE",
                ),
            )
            await self._complete_relayed_suggestion(
                process_id,
                suggestion_id,
                status="error",
            )
        except Exception:
            logger.exception(
                "Suggestion relay failed: process_id=%s suggestion_id=%s",
                process_id,
                suggestion_id,
            )
            await self._complete_relayed_suggestion(
                process_id,
                suggestion_id,
                status="error",
            )

    async def _deliver_terminal_suggestion(
        self,
        process: LiveSuggestionProcess,
        row: SuggestionTerminalRow,
    ) -> None:
        process_id, suggestion_id = process
        status_val = row["status"]
        # Only a released Suggestion is shown; one that expired or was
        # superseded while held completes like a run that found nothing.
        withheld = status_val == "success" and row.get("delivery_state") != "released"
        has_suggestion = bool(row.get("has_suggestion")) and not withheld
        answer = str(row.get("answer") or "")
        interaction_contract_raw = row.get("interaction_contract")
        interaction_contract = (
            interaction_contract_raw.strip()
            if not withheld
            and isinstance(interaction_contract_raw, str)
            and interaction_contract_raw.strip()
            else None
        )

        if status_val != "success":
            error_payload = _parse_suggestion_error(row.get("error"))
            if error_payload is not None:
                await self.send_error(
                    error_from_payload(error_payload, "SUGGESTION_JOB_ERROR"),
                    process_id=process_id,
                    meta=build_suggestion_error_meta(
                        suggestion_id=suggestion_id,
                        process_id=process_id,
                        stage="running_failed",
                        error_code=str(
                            error_payload.get("error_code") or "SUGGESTION_JOB_ERROR"
                        ),
                    ),
                )
        elif has_suggestion and answer:
            await self._emit_completion_chunk(
                process_id,
                suggestion_id,
                answer,
                kind="suggestion",
            )

        await self._complete_relayed_suggestion(
            process_id,
            suggestion_id,
            status=status_val,
            has_suggestion=has_suggestion,
            interaction_contract=interaction_contract,
        )

    async def _complete_relayed_suggestion(
        self,
        process_id: str,
        suggestion_id: str,
        *,
        status: str,
        has_suggestion: bool | None = None,
        interaction_contract: str | None = None,
    ) -> None:
        self._release_suggestion_process(process_id)
        await self._emit_process_completed(
            process_id,
            suggestion_id,
            status=status,
            has_suggestion=has_suggestion,
            kind="suggestion",
            interaction_contract=interaction_contract,
        )


def _parse_suggestion_error(raw: object) -> dict[str, object] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return dict(raw) if isinstance(raw, dict) else None

"""Suggestion terminal row polling against the repository SSOT."""

from __future__ import annotations

import asyncio
import logging

from pantaray_agents.orchestration.ws import suggestion_job_timing
from pantaray_agents.orchestration.ws.suggestion_stream.types import (
    SUGGESTION_DB_DEPENDENCY,
    SUGGESTION_DB_FAILURE_LOG_INTERVAL_SECONDS,
    SUGGESTION_DB_GET_OP,
    SuggestionRowFetchError,
    SuggestionTerminalRow,
    SuggestionTerminalStatus,
    coerce_suggestion_terminal_row,
)
from pantaray_agents.utils.metrics import (
    record_ws_dependency_circuit_open,
    record_ws_dependency_fail,
)
from pantaray_agents.utils.ws_observability import RateLimiter

logger = logging.getLogger(__name__)


class SuggestionRepositoryWaitMixin:
    """Repository polling helpers for suggestion jobs."""

    def _resolve_suggestion_job_poll_interval_seconds(self) -> float:
        """Resolve the repository polling interval in seconds."""
        return suggestion_job_timing.suggestion_job_poll_interval_seconds()

    async def _wait_for_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        process_id: str | None = None,
    ) -> SuggestionTerminalRow:
        """Wait for a terminal suggestion row from the repository SSOT.

        A held Suggestion is not finished for the relay: it is shown, or ends
        unshown, only once the release task decides.
        """
        repo = await self._get_suggestion_repository()  # type: ignore[attr-defined]
        if repo is None:
            raise SuggestionRowFetchError("Suggestion DB repository is unavailable.")

        poll_interval = self._resolve_suggestion_job_poll_interval_seconds()

        limiter: RateLimiter | None = getattr(self, "_rate_limiter", None)
        if not isinstance(limiter, RateLimiter):
            limiter = RateLimiter()

        circuit_key = f"{SUGGESTION_DB_DEPENDENCY}:{SUGGESTION_DB_GET_OP}"
        terminal_statuses: set[SuggestionTerminalStatus] = {
            "success",
            "error",
            "timeout",
            "canceled",
        }

        while True:
            circuit = self._suggestion_db_circuit()
            if not circuit.allow(circuit_key):
                record_ws_dependency_circuit_open(dependency=SUGGESTION_DB_DEPENDENCY)
                raise SuggestionRowFetchError(
                    "Suggestion DB read circuit is open (dependency unhealthy)."
                )
            try:
                result = await repo.get_suggestion(
                    user_id=str(user_id),
                    suggestion_id=str(suggestion_id),
                )
                circuit.record_success(circuit_key)
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                record_ws_dependency_fail(
                    dependency=SUGGESTION_DB_DEPENDENCY,
                    operation=SUGGESTION_DB_GET_OP,
                )
                opened = circuit.record_failure(circuit_key)
                if limiter.should_log(
                    circuit_key,
                    interval_seconds=SUGGESTION_DB_FAILURE_LOG_INTERVAL_SECONDS,
                ):
                    logger.warning(
                        "Suggestion DB read failed: user_id=%s suggestion_id=%s process_id=%s error=%s",
                        user_id,
                        suggestion_id,
                        process_id,
                        exc,
                        exc_info=True,
                    )
                if opened:
                    record_ws_dependency_circuit_open(
                        dependency=SUGGESTION_DB_DEPENDENCY
                    )
                    raise SuggestionRowFetchError(
                        "Suggestion DB read failed repeatedly (circuit opened)."
                    ) from exc
                result = None
            row = coerce_suggestion_terminal_row(
                getattr(result, "data", None) if result is not None else None
            )
            if (
                row
                and row["status"] in terminal_statuses
                and row.get("delivery_state") != "held"
            ):
                return row
            await asyncio.sleep(float(poll_interval))

    def _suggestion_db_circuit(self):  # noqa: ANN202
        from pantaray_agents.orchestration.ws import suggestion_stream as ss

        return ss._suggestion_db_circuit

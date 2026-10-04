"""How the local runtime ends a Suggestion row that is still `processing`."""

from __future__ import annotations

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult

from .shared import encode_json_column


class LocalSuggestionTerminationMixin(LocalSuggestionStateRepository):
    async def finalize_suggestion_start_error_if_processing(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RepositoryResult[DBRow]:
        error_payload: dict[str, object] = {
            "error_code": error_code,
            "error_message": error_message,
            "error_type": "runtime_error",
            "severity": "error",
        }
        if error_details:
            error_payload["error_details"] = error_details
        if metadata:
            error_payload["metadata"] = metadata
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE agent_suggestions
                    SET status = 'error',
                        error = ?,
                        updated_at = ?
                    WHERE user_id = ? AND suggestion_id = ? AND status = 'processing'
                    """,
                    (
                        encode_json_column(error_payload),
                        now_utc_iso(),
                        user_id,
                        suggestion_id,
                    ),
                )
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)

    async def discard_suggestion_if_processing(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[DBRow]:
        """End a Suggestion that will not be published, keeping none of its text.

        Every exit that produces nothing, or discards what the run produced,
        ends here: no session can deliver it, recording was turned off, the
        activity permit was revoked, or a newer review superseded it. The
        enqueueing transaction created the row as `processing`, which the
        suggestion route reports as a stream still in progress, so it is ended
        as `canceled`. The run steps hold the prompt and any answer the run
        produced, so they go in the same transaction.
        """
        with self._connect() as connection, immediate_transaction(connection):
            ended = connection.execute(
                """
                UPDATE agent_suggestions SET status = 'canceled', updated_at = ?
                WHERE user_id = ? AND suggestion_id = ? AND status = 'processing'
                """,
                (now_utc_iso(), user_id, suggestion_id),
            ).rowcount
            if ended:
                connection.execute(
                    "DELETE FROM agent_suggestion_run_steps WHERE suggestion_id = ?",
                    (suggestion_id,),
                )
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)

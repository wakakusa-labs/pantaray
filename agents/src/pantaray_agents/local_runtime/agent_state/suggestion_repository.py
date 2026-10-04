from __future__ import annotations

import sqlite3
from datetime import timedelta

from pantaray_agents.local_runtime.runtime.utc_timestamps import (
    format_utc_iso,
    now_utc_iso,
    utc_now,
)
from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
    WELCOME_SUGGESTION_PROMPT_NAME,
)
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
)
from pantaray_agents.repositories.runtime_ports import (
    ActivityRepositoryPort,
    PatchRunStepKind,
    PatchRunStepStatus,
    SuggestionRepositoryPort,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.suggestion_reactions import (
    normalize_public_suggestion_reaction_for_write,
)

from .recent_suggestion_history import load_recent_suggestion_history
from .shared import build_audit_timestamps, encode_json_column
from .suggestion_termination import LocalSuggestionTerminationMixin

MESSAGE_ONLY_INTERACTION_CONTRACT = "message_only"


def _normalize_interaction_contract(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


class LocalSuggestionRepository(
    LocalSuggestionTerminationMixin,
    SuggestionRepositoryPort,
):
    def __init__(
        self,
        *,
        db_path: str,
        busy_timeout_ms: int,
        activity_repository: ActivityRepositoryPort,
    ) -> None:
        super().__init__(db_path=db_path, busy_timeout_ms=busy_timeout_ms)
        self._activity_repository = activity_repository

    async def create_processing_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        created_at: str | None = None,
    ) -> RepositoryResult[DBRow]:
        row_created_at, row_updated_at = build_audit_timestamps(created_at=created_at)
        with self._connect() as connection:
            with immediate_transaction(connection):
                connection.execute(
                    """
                    INSERT INTO agent_suggestions(
                        suggestion_id,
                        user_id,
                        status,
                        answer,
                        thinking,
                        suggestion_summary,
                        target_context_json,
                        error,
                        prompt_text,
                        response_text,
                        prompt_name,
                        prompt_version,
                        has_suggestion,
                        interaction_contract,
                        action_request_payload,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, 'processing', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(suggestion_id) DO NOTHING
                    """,
                    (suggestion_id, user_id, row_created_at, row_updated_at),
                )
            row = connection.execute(
                """
                SELECT *
                FROM agent_suggestions
                WHERE user_id = ? AND suggestion_id = ?
                LIMIT 1
                """,
                (user_id, suggestion_id),
            ).fetchone()
        if row is None:
            return RepositoryResult(error="suggestion row not found after create")
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)

    async def save_suggestion(
        self,
        suggestion: SuggestionAgentResponse,
        prompt_name: str,
        prompt_version: str,
        prompt_text: str | None = None,
        response_text: str | None = None,
        request_images_count: int = 0,
        used_images_count: int = 0,
    ) -> RepositoryResult[DBRow]:
        error_payload = (
            suggestion.error.model_dump() if suggestion.error is not None else None
        )
        # A new Suggestion is held until the release task shows it, and it
        # replaces the one held before: an owner holds at most one.
        held = str(suggestion.status) == "success" and suggestion.has_suggestion
        updated_at = now_utc_iso()
        with self._connect() as connection:
            with immediate_transaction(connection):
                if held:
                    connection.execute(
                        """
                        UPDATE agent_suggestions
                        SET delivery_state = 'superseded', updated_at = ?
                        WHERE user_id = ? AND delivery_state = 'held'
                          AND EXISTS (
                              SELECT 1 FROM agent_suggestions AS saved
                              WHERE saved.user_id = ? AND saved.suggestion_id = ?
                                AND saved.status = 'processing'
                          )
                        """,
                        (
                            updated_at,
                            suggestion.user_id,
                            suggestion.user_id,
                            suggestion.suggestion_id,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE agent_suggestions
                    SET status = ?,
                        answer = ?,
                        thinking = ?,
                        suggestion_summary = ?,
                        target_context_json = ?,
                        error = ?,
                        prompt_text = ?,
                        response_text = ?,
                        prompt_name = ?,
                        prompt_version = ?,
                        has_suggestion = ?,
                        request_images_count = ?,
                        used_images_count = ?,
                        interaction_contract = ?,
                        delivery_state = ?,
                        updated_at = ?
                    WHERE user_id = ? AND suggestion_id = ? AND status = 'processing'
                    """,
                    (
                        str(suggestion.status),
                        suggestion.answer,
                        suggestion.thinking,
                        suggestion.suggestion_summary,
                        encode_json_column(
                            suggestion.target_context.model_dump()
                            if suggestion.target_context is not None
                            else None
                        ),
                        encode_json_column(error_payload),
                        prompt_text,
                        response_text,
                        prompt_name,
                        prompt_version,
                        1 if suggestion.has_suggestion else 0,
                        request_images_count,
                        used_images_count,
                        suggestion.interaction_contract,
                        "held" if held else None,
                        updated_at,
                        suggestion.user_id,
                        suggestion.suggestion_id,
                    ),
                )
        return await self.get_suggestion(
            user_id=str(suggestion.user_id),
            suggestion_id=str(suggestion.suggestion_id),
        )

    async def save_suggestion_run_step(
        self,
        *,
        suggestion_id: str,
        step_number: int,
        step_kind: PatchRunStepKind,
        status: PatchRunStepStatus,
        llm_prompt_text: str | None = None,
        llm_response_text: str | None = None,
        tool_name: str | None = None,
        tool_call_envelope: JSONValue = None,
        tool_output: JSONValue = None,
        error_code: str | None = None,
        error_message: str | None = None,
        created_at: str | None = None,
    ) -> RepositoryResult[DBRow]:
        row_created_at, _ = build_audit_timestamps(created_at=created_at)
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO agent_suggestion_run_steps(
                        suggestion_id,
                        step_number,
                        step_kind,
                        status,
                        llm_prompt_text,
                        llm_response_text,
                        tool_name,
                        tool_input_json,
                        tool_output_json,
                        error_code,
                        error_message,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(suggestion_id, step_number) DO UPDATE SET
                        step_kind = excluded.step_kind,
                        status = excluded.status,
                        llm_prompt_text = excluded.llm_prompt_text,
                        llm_response_text = excluded.llm_response_text,
                        tool_name = excluded.tool_name,
                        tool_input_json = excluded.tool_input_json,
                        tool_output_json = excluded.tool_output_json,
                        error_code = excluded.error_code,
                        error_message = excluded.error_message
                    """,
                    (
                        suggestion_id,
                        step_number,
                        step_kind,
                        status,
                        llm_prompt_text,
                        llm_response_text,
                        tool_name,
                        encode_json_column(tool_call_envelope),
                        encode_json_column(tool_output),
                        error_code,
                        error_message,
                        row_created_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM agent_suggestion_run_steps
                    WHERE suggestion_id = ? AND step_number = ?
                    """,
                    (suggestion_id, step_number),
                ).fetchone()
        if row is None:
            return RepositoryResult(error="suggestion run step not found")
        return RepositoryResult(data=dict(row))

    async def get_recent_suggestions(
        self,
        user_id: str,
        days: int = 7,
        limit: int = 20,
    ) -> RepositoryResult[list[DBRow]]:
        cutoff_iso = format_utc_iso(utc_now() - timedelta(days=days))
        with self._connect() as connection:
            history = load_recent_suggestion_history(
                connection,
                user_id=user_id,
                excluded_prompt_name=WELCOME_SUGGESTION_PROMPT_NAME,
                cutoff_iso=cutoff_iso,
                limit=limit,
            )
        return RepositoryResult(data=history)

    async def get_recent_activity_descriptions(
        self,
        user_id: str,
        limit: int = 3,
    ) -> RepositoryResult[list[DBRow]]:
        result = await self._activity_repository.get_recent_activity_logs(
            user_id=user_id,
            limit=limit,
        )
        return result

    async def get_recent_activity_summary_1h(
        self,
        user_id: str,
        limit: int = 1,
    ) -> RepositoryResult[list[DBRow]]:
        return await self.get_recent_activity_summary(
            user_id,
            summary_type="1h",
            limit=limit,
        )

    async def get_recent_activity_summary(
        self,
        user_id: str,
        *,
        summary_type: str,
        limit: int = 1,
    ) -> RepositoryResult[list[DBRow]]:
        result = await self._activity_repository.get_recent_activity_summary(
            user_id=user_id,
            summary_type=summary_type,
            limit=limit,
        )
        return result

    async def mark_user_reaction(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        user_reaction: str,
        action_status: str | None = None,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
        accepted_at: str | None = None,
        rejected_at: str | None = None,
    ) -> RepositoryResult[DBRow]:
        try:
            stored_user_reaction = normalize_public_suggestion_reaction_for_write(
                user_reaction
            )
        except ValueError:
            return RepositoryResult(error="user_reaction must be accepted or dismiss")
        update_columns: list[str] = ["user_reaction = ?", "updated_at = ?"]
        params: list[object] = [stored_user_reaction, now_utc_iso()]
        if stored_user_reaction == "accepted":
            update_columns.extend(["accepted_at = ?", "rejected_at = NULL"])
            params.append(accepted_at or now_utc_iso())
        elif stored_user_reaction == "rejected":
            update_columns.extend(["rejected_at = ?", "accepted_at = NULL"])
            params.append(rejected_at or now_utc_iso())
        if action_status is not None:
            update_columns.append("action_status = ?")
            params.append(action_status)
        if update_action_failure_code:
            update_columns.append("action_failure_code = ?")
            params.append(action_failure_code)
        with self._connect() as connection:
            with connection:
                mutation_guard = _reject_message_only_action_lane_mutation(
                    connection=connection,
                    user_id=user_id,
                    suggestion_id=suggestion_id,
                    operation_name="mark_user_reaction",
                )
                if mutation_guard is not None:
                    return RepositoryResult(error=mutation_guard)
                connection.execute(
                    f"""
                    UPDATE agent_suggestions
                    SET {", ".join(update_columns)}
                    WHERE user_id = ? AND suggestion_id = ?
                    """,
                    (*params, user_id, suggestion_id),
                )
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)

    async def update_action_status(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        action_status: str,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
    ) -> RepositoryResult[DBRow]:
        update_columns: list[str] = ["action_status = ?", "updated_at = ?"]
        params: list[object] = [action_status, now_utc_iso()]
        if update_action_failure_code:
            update_columns.append("action_failure_code = ?")
            params.append(action_failure_code)
        with self._connect() as connection:
            with connection:
                mutation_guard = _reject_message_only_action_lane_mutation(
                    connection=connection,
                    user_id=user_id,
                    suggestion_id=suggestion_id,
                    operation_name="update_action_status",
                )
                if mutation_guard is not None:
                    return RepositoryResult(error=mutation_guard)
                connection.execute(
                    f"""
                    UPDATE agent_suggestions
                    SET {", ".join(update_columns)}
                    WHERE user_id = ? AND suggestion_id = ?
                    """,
                    (*params, user_id, suggestion_id),
                )
        return await self.get_suggestion(user_id=user_id, suggestion_id=suggestion_id)


def _reject_message_only_action_lane_mutation(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    suggestion_id: str,
    operation_name: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT interaction_contract
        FROM agent_suggestions
        WHERE user_id = ? AND suggestion_id = ?
        LIMIT 1
        """,
        (user_id, suggestion_id),
    ).fetchone()
    if row is None:
        return "Suggestion not found"
    interaction_contract = _normalize_interaction_contract(row["interaction_contract"])
    if interaction_contract == MESSAGE_ONLY_INTERACTION_CONTRACT:
        return f"{operation_name} is not allowed for message_only suggestions"
    return None

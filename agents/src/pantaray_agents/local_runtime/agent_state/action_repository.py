from __future__ import annotations

import sqlite3
from typing import Protocol

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.repositories.budget_validation import (
    validate_optional_positive_int,
)
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort
from pantaray_agents.schema.agent.action import ActionAgentResponse
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)

from ..suggestion_state.repository import LocalSuggestionStateRepository
from .action_repository_context import LocalActionRepositoryContextMixin
from .action_repository_memory import LocalActionRepositoryMemoryMixin
from .action_repository_steps import LocalActionRepositoryStepsMixin
from .shared import (
    build_audit_timestamps,
    encode_json_column,
    normalize_row,
)

_ACTION_JSON_COLUMNS = {"error", "execution_target_json"}


class _RepositoryFailureResult(Protocol):
    error: str | None
    error_kind: RepositoryErrorKind | None
    retryable: bool | None


class LocalActionRepository(
    LocalActionRepositoryStepsMixin,
    LocalActionRepositoryContextMixin,
    LocalActionRepositoryMemoryMixin,
    LocalSuggestionStateRepository,
    ActionRepositoryPort,
):
    @staticmethod
    def _repository_failure(
        *,
        message: str,
        error_kind: RepositoryErrorKind,
        retryable: bool,
    ) -> RepositoryResult[DBRow]:
        return RepositoryResult(
            error=message,
            error_kind=error_kind,
            retryable=retryable,
        )

    @staticmethod
    def _propagate_repository_failure[T](
        result: _RepositoryFailureResult,
        *,
        prefix: str | None = None,
    ) -> RepositoryResult[T]:
        if result.error is None:
            raise ValueError("repository failure result is required")
        message = f"{prefix}: {result.error}" if prefix else result.error
        return RepositoryResult(
            error=message,
            error_kind=result.error_kind or RepositoryErrorKind.UNKNOWN,
            retryable=False if result.retryable is None else result.retryable,
        )

    @staticmethod
    def _classify_sqlite_integrity_error(
        exc: sqlite3.IntegrityError,
    ) -> RepositoryResult[DBRow]:
        message = str(exc).strip() or exc.__class__.__name__
        normalized = message.lower()
        error_kind = (
            RepositoryErrorKind.CONFLICT
            if "unique constraint failed" in normalized
            else RepositoryErrorKind.CONSTRAINT
        )
        return LocalActionRepository._repository_failure(
            message=message,
            error_kind=error_kind,
            retryable=False,
        )

    @staticmethod
    def _classify_sqlite_operational_error(
        exc: sqlite3.OperationalError,
    ) -> RepositoryResult[DBRow]:
        message = str(exc).strip() or exc.__class__.__name__
        normalized = message.lower()
        retryable = any(
            hint in normalized
            for hint in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
                "database busy",
                "unable to open database file",
            )
        )
        error_kind = (
            RepositoryErrorKind.TRANSIENT if retryable else RepositoryErrorKind.UNKNOWN
        )
        return LocalActionRepository._repository_failure(
            message=message,
            error_kind=error_kind,
            retryable=retryable,
        )

    async def _ensure_action_owned(
        self,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[None]:
        try:
            action_result = await self.get_action(user_id=user_id, action_id=action_id)
        except sqlite3.OperationalError as exc:
            classified = self._classify_sqlite_operational_error(exc)
            return RepositoryResult(
                error=classified.error,
                error_kind=classified.error_kind,
                retryable=classified.retryable,
            )
        if action_result.error:
            return self._propagate_repository_failure(action_result)
        if not isinstance(action_result.data, dict):
            return RepositoryResult(
                error="Action not found",
                error_kind=RepositoryErrorKind.NOT_FOUND,
                retryable=False,
            )
        return RepositoryResult(data=None)

    async def save_action(
        self,
        response: ActionAgentResponse,
        prompt_name: str,
        prompt_version: str,
        final_prompt_text: str | None = None,
        *,
        steps_budget: int | None = None,
        llm_steps_budget: int | None = None,
        tool_steps_budget: int | None = None,
        token_budget: int | None = None,
        total_steps: int | None = None,
        total_llm_steps: int | None = None,
        total_tool_steps: int | None = None,
        total_prompt_tokens: int | None = None,
        total_completion_tokens: int | None = None,
    ) -> RepositoryResult[DBRow]:
        budget_error = validate_optional_positive_int(
            field_name="token_budget",
            value=token_budget,
        )
        if budget_error is not None:
            return RepositoryResult(error=budget_error)

        existing_result = await self.get_action(
            user_id=str(response.user_id),
            action_id=str(response.action_id),
        )
        if existing_result.error:
            return RepositoryResult(error=existing_result.error)
        existing_row = (
            existing_result.data if isinstance(existing_result.data, dict) else None
        )
        if existing_row is None:
            return RepositoryResult(
                error="Action not found",
                error_kind=RepositoryErrorKind.NOT_FOUND,
                retryable=False,
            )
        if existing_row.get("suggestion_id") != response.suggestion_id:
            return RepositoryResult(
                error="Action suggestion relation cannot be changed",
                error_kind=RepositoryErrorKind.CONFLICT,
                retryable=False,
            )
        raw_generation = existing_row.get("generation")
        generation = (
            int(raw_generation)
            if isinstance(raw_generation, int) and not isinstance(raw_generation, bool)
            else 1
        )
        resolved_total_steps = total_steps or 0
        resolved_total_llm_steps = total_llm_steps or 0
        resolved_total_tool_steps = total_tool_steps or 0
        resolved_total_prompt_tokens = total_prompt_tokens or 0
        resolved_total_completion_tokens = total_completion_tokens or 0
        _, updated_at = build_audit_timestamps(
            created_at=str(existing_row["created_at"])
        )

        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE agent_actions
                    SET status = ?,
                        final_output = ?,
                        error = ?,
                        final_prompt_text = ?,
                        generation = ?,
                        steps_budget = ?,
                        llm_steps_budget = ?,
                        tool_steps_budget = ?,
                        token_budget = ?,
                        total_steps = ?,
                        total_llm_steps = ?,
                        total_tool_steps = ?,
                        total_prompt_tokens = ?,
                        total_completion_tokens = ?,
                        total_tokens = ?,
                        prompt_name = ?,
                        prompt_version = ?,
                        updated_at = ?
                    WHERE action_id = ? AND user_id = ?
                    """,
                    (
                        str(response.status),
                        response.final_output,
                        encode_json_column(
                            response.error.model_dump()
                            if response.error is not None
                            else None
                        ),
                        final_prompt_text,
                        generation,
                        steps_budget,
                        llm_steps_budget,
                        tool_steps_budget,
                        token_budget,
                        resolved_total_steps,
                        resolved_total_llm_steps,
                        resolved_total_tool_steps,
                        resolved_total_prompt_tokens,
                        resolved_total_completion_tokens,
                        resolved_total_prompt_tokens + resolved_total_completion_tokens,
                        prompt_name,
                        prompt_version,
                        updated_at,
                        response.action_id,
                        response.user_id,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM agent_actions
                    WHERE user_id = ? AND action_id = ?
                    LIMIT 1
                    """,
                    (response.user_id, response.action_id),
                ).fetchone()
        if row is None:
            return RepositoryResult(error="action row not found after upsert")
        return RepositoryResult(
            data=normalize_row(row, json_columns=_ACTION_JSON_COLUMNS)
        )

    async def update_action_status_if_processing(
        self,
        *,
        user_id: str,
        action_id: str,
        status: str,
    ) -> RepositoryResult[bool]:
        if not user_id or not action_id:
            return RepositoryResult(
                error="update_action_status_if_processing: user_id/action_id is required"
            )
        with self._connect() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE agent_actions
                    SET status = ?, updated_at = ?
                    WHERE user_id = ? AND action_id = ? AND status = 'processing'
                    """,
                    (status, now_utc_iso(), user_id, action_id),
                )
        return RepositoryResult(data=cursor.rowcount > 0)

    async def increment_action_generation(
        self,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[int]:
        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(ownership_result)
        with self._connect() as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE agent_actions
                    SET generation = generation + 1,
                        updated_at = ?
                    WHERE user_id = ? AND action_id = ?
                    """,
                    (now_utc_iso(), user_id, action_id),
                )
                row = connection.execute(
                    """
                    SELECT generation
                    FROM agent_actions
                    WHERE user_id = ? AND action_id = ?
                    LIMIT 1
                    """,
                    (user_id, action_id),
                ).fetchone()
        if row is None:
            return RepositoryResult(error="Action not found")
        return RepositoryResult(data=int(row["generation"]))

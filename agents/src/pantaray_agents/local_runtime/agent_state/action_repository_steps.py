from __future__ import annotations

import sqlite3
from typing import Protocol

from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    ActionSubagentWaitAuthorityError,
    ActionSubagentWaitInputError,
    collect_action_subagent_results_in_connection,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.repository.tool_invocation_links import (
    link_tool_invocations_to_action_step_in_connection,
)
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionRuntimeResumeContext,
    ActionRuntimeResumeContractError,
    build_action_runtime_resume_context,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import (
    ActionProviderTurnRecord,
    ActionStepRecord,
    StepType,
)
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import JSONValue, StepStatusType
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_llm.contracts.conversation import LlmProviderTurn

from ..runtime.action_checkpoint_retention import prune_action_checkpoints_in_connection
from .action_llm_turn_commit import (
    ActionLlmTurnCommitError,
    save_action_llm_turn_in_connection,
)
from .action_provider_turns import read_action_provider_turns_in_connection
from .shared import encode_json_column, normalize_row

_ACTION_STEP_JSON_COLUMNS = {
    "tool_args",
    "tool_output",
    "provider_turn",
    "runtime_state_checkpoint",
    "error",
}


class _RepositoryFailureResult(Protocol):
    error: str | None
    error_kind: RepositoryErrorKind | None
    retryable: bool | None


class _ActionRepositoryStepsState(Protocol):
    def _connect(self) -> sqlite3.Connection: ...

    async def _ensure_action_owned(
        self,
        *,
        user_id: str,
        action_id: str,
    ) -> RepositoryResult[None]: ...

    @staticmethod
    def _repository_failure(
        *,
        message: str,
        error_kind: RepositoryErrorKind,
        retryable: bool,
    ) -> RepositoryResult[DBRow]: ...

    @staticmethod
    def _propagate_repository_failure[T](
        result: _RepositoryFailureResult,
        *,
        prefix: str | None = None,
    ) -> RepositoryResult[T]: ...

    @staticmethod
    def _classify_sqlite_integrity_error(
        exc: sqlite3.IntegrityError,
    ) -> RepositoryResult[DBRow]: ...

    @staticmethod
    def _classify_sqlite_operational_error(
        exc: sqlite3.OperationalError,
    ) -> RepositoryResult[DBRow]: ...


class LocalActionRepositoryStepsMixin:
    async def save_action_step(
        self: _ActionRepositoryStepsState,
        step_id: str,
        action_id: str,
        step_number: int,
        step_name: str,
        step_type: StepType,
        user_request_text: str | None = None,
        llm_prompt_text: str | None = None,
        llm_response_text: str | None = None,
        tool_args: dict[str, JSONValue] | None = None,
        tool_output: dict[str, JSONValue] | None = None,
        thinking: str | None = None,
        runtime_state_checkpoint: dict[str, JSONValue] | None = None,
        runtime_state_checkpoint_version: int | None = None,
        status: StepStatusType = StepStatusType.PROCESSING,
        error: dict[str, JSONValue] | None = None,
        execution_time_ms: int | None = None,
        retry_count: int = 0,
        *,
        parent_step_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        goal_handle: str,
        user_id: str | None = None,
        short_step_id: str,
        local_step_number: int,
        tool_invocation_ids: tuple[str, ...] = (),
        subagent_collection_receipt: ActionSubagentCollectionReceipt | None = None,
        llm_turn: ActionLlmTurnCommit | None = None,
        origin: ActionToolCallOrigin | None = None,
        provider_turn: ActionProviderTurnRecord | None = None,
    ) -> RepositoryResult[DBRow]:
        if not isinstance(user_id, str) or not user_id:
            return self._repository_failure(
                message="save_action_step: user_id is required",
                error_kind=RepositoryErrorKind.VALIDATION,
                retryable=False,
            )
        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(
                ownership_result,
                prefix="save_action_step",
            )

        record = ActionStepRecord(
            step_id=step_id,
            action_id=action_id,
            step_number=step_number,
            step_name=step_name,
            step_type=step_type,
            user_request_text=user_request_text,
            llm_prompt_text=llm_prompt_text,
            llm_response_text=llm_response_text,
            tool_args=tool_args,
            tool_output=tool_output,
            thinking=thinking,
            call_id=origin.call_id if origin is not None else None,
            llm_step_id=origin.llm_step_id if origin is not None else None,
            provider_turn=None if provider_turn is None else provider_turn.turn,
            provider_turn_identity=(
                None if provider_turn is None else provider_turn.identity
            ),
            runtime_state_checkpoint=runtime_state_checkpoint,
            runtime_state_checkpoint_version=runtime_state_checkpoint_version,
            status=status,
            error=error,
            execution_time_ms=execution_time_ms,
            parent_step_id=parent_step_id,
            goal_handle=goal_handle,
            retry_count=retry_count,
            started_at=started_at,
            completed_at=completed_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            created_at=now_utc_iso(),
            short_step_id=short_step_id,
            local_step_number=local_step_number,
        )
        payload = record.model_dump(exclude_none=False)
        payload["user_id"] = user_id
        payload["prompt_tokens"] = payload["prompt_tokens"] or 0
        payload["completion_tokens"] = payload["completion_tokens"] or 0
        payload["tool_args"] = encode_json_column(payload["tool_args"])
        payload["tool_output"] = encode_json_column(payload["tool_output"])
        payload["provider_turn"] = encode_json_column(payload["provider_turn"])
        payload["runtime_state_checkpoint"] = encode_json_column(
            payload["runtime_state_checkpoint"]
        )
        payload["error"] = encode_json_column(payload["error"])
        if step_type == StepType.ASSISTANT_MESSAGE:
            payload["adopted_process_id"] = None

        columns = tuple(payload.keys())
        placeholders = ", ".join("?" for _ in columns)
        # One durable row per step identity. Re-persisting a step_id settles the
        # earlier projection of the same step (an approval pause writes the step
        # before its tool result exists) instead of forking a second row.
        settlement = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"step_id", "created_at"}
        )
        sql = f"""
            INSERT INTO agent_action_steps({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(step_id) DO UPDATE SET {settlement}
        """
        with self._connect() as connection:
            try:
                with connection:
                    connection.execute("BEGIN")
                    if step_type == StepType.ASSISTANT_MESSAGE:
                        # Bind the utterance to the same adopted USER that owns
                        # this timeline position; checkpoint and message commit together.
                        owner = connection.execute(
                            """SELECT adopted_process_id FROM agent_action_steps
                            WHERE user_id=? AND action_id=? AND step_type='user_request'
                              AND status='success' AND adopted_process_id IS NOT NULL
                              AND step_number<=?
                            ORDER BY step_number DESC,step_id DESC LIMIT 1""",
                            (user_id, action_id, step_number),
                        ).fetchone()
                        if owner is None:
                            return self._repository_failure(
                                message="assistant message requires an adopted USER owner",
                                error_kind=RepositoryErrorKind.CONSTRAINT,
                                retryable=False,
                            )
                        payload["adopted_process_id"] = owner["adopted_process_id"]
                    if subagent_collection_receipt is not None:
                        if (
                            step_type != StepType.TOOL_EXECUTION
                            or step_name
                            not in {"tool::wait_subagents", "tool::cancel_subagent"}
                            or (
                                step_name == "tool::cancel_subagent"
                                and len(
                                    subagent_collection_receipt.request.child_process_ids
                                )
                                != 1
                            )
                            or status != StepStatusType.SUCCESS
                            or user_id != subagent_collection_receipt.request.user_id
                            or action_id
                            != subagent_collection_receipt.request.action_id
                            or completed_at != subagent_collection_receipt.collected_at
                        ):
                            return self._repository_failure(
                                message="invalid Action subagent collection receipt",
                                error_kind=RepositoryErrorKind.VALIDATION,
                                retryable=False,
                            )
                        collect_action_subagent_results_in_connection(
                            connection,
                            receipt=subagent_collection_receipt,
                        )
                    if llm_turn is None:
                        connection.execute(
                            sql, tuple(payload[column] for column in columns)
                        )
                    else:
                        save_action_llm_turn_in_connection(
                            connection, payload=payload, turn=llm_turn
                        )
                    if runtime_state_checkpoint is not None:
                        prune_action_checkpoints_in_connection(
                            connection, user_id=user_id, action_id=action_id
                        )
                    if (
                        step_type
                        in {StepType.TOOL_EXECUTION, StepType.ASSISTANT_MESSAGE}
                        and tool_invocation_ids
                    ):
                        link_tool_invocations_to_action_step_in_connection(
                            connection,
                            action_id=action_id,
                            invocation_ids=tool_invocation_ids,
                            step_id=step_id,
                        )
                    row = connection.execute(
                        "SELECT * FROM agent_action_steps WHERE step_id = ? LIMIT 1",
                        (step_id,),
                    ).fetchone()
            except sqlite3.IntegrityError as exc:
                return self._classify_sqlite_integrity_error(exc)
            except sqlite3.OperationalError as exc:
                return self._classify_sqlite_operational_error(exc)
            except (
                ActionSubagentWaitAuthorityError,
                ActionSubagentWaitInputError,
                ActionLlmTurnCommitError,
            ) as exc:
                return self._repository_failure(
                    message=str(exc),
                    error_kind=RepositoryErrorKind.CONSTRAINT,
                    retryable=False,
                )
        if row is None:
            return self._repository_failure(
                message="action step row not found after insert",
                error_kind=RepositoryErrorKind.UNKNOWN,
                retryable=True,
            )
        return RepositoryResult(
            data=normalize_row(row, json_columns=_ACTION_STEP_JSON_COLUMNS)
        )

    async def get_action_provider_turns(
        self: _ActionRepositoryStepsState,
        *,
        user_id: str,
        action_id: str,
        identity: str,
    ) -> RepositoryResult[dict[str, LlmProviderTurn]]:
        """The turns of this Action that ``identity`` may be handed back.

        A run keeps the turns it received in memory; this reloads them after a
        restart so the projection can hand them back to the same provider.
        """

        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(
                ownership_result,
                prefix="get_action_provider_turns",
            )
        with self._connect() as connection:
            return RepositoryResult(
                data=read_action_provider_turns_in_connection(
                    connection,
                    user_id=user_id,
                    action_id=action_id,
                    identity=identity,
                )
            )

    async def get_action_steps_by_short_step_ids(
        self: _ActionRepositoryStepsState,
        *,
        user_id: str,
        action_id: str,
        short_step_ids: tuple[str, ...],
    ) -> RepositoryResult[list[DBRow]]:
        if not short_step_ids:
            return self._repository_failure(
                message="get_action_steps_by_short_step_ids: refs are required",
                error_kind=RepositoryErrorKind.VALIDATION,
                retryable=False,
            )
        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(
                ownership_result,
                prefix="get_action_steps_by_short_step_ids",
            )
        placeholders = ", ".join("?" for _ in short_step_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    step_id,
                    action_id,
                    step_number,
                    step_name,
                    step_type,
                    user_request_text,
                    llm_prompt_text,
                    llm_response_text,
                    tool_args,
                    tool_output,
                    thinking,
                    status,
                    error,
                    execution_time_ms,
                    parent_step_id,
                    goal_handle,
                    retry_count,
                    started_at,
                    completed_at,
                    prompt_tokens,
                    completion_tokens,
                    created_at,
                    short_step_id,
                    local_step_number
                FROM agent_action_steps
                WHERE user_id = ?
                  AND action_id = ?
                  AND short_step_id IN ({placeholders})
                ORDER BY
                    short_step_id,
                    completed_at IS NULL ASC,
                    completed_at DESC,
                    created_at DESC,
                    step_id DESC
                """,
                (user_id, action_id, *short_step_ids),
            ).fetchall()
        latest_by_ref: dict[str, DBRow] = {}
        for row in rows:
            ref = str(row["short_step_id"] or "")
            if ref and ref not in latest_by_ref:
                latest_by_ref[ref] = normalize_row(
                    row,
                    json_columns=_ACTION_STEP_JSON_COLUMNS,
                )
        return RepositoryResult(
            data=[latest_by_ref[ref] for ref in short_step_ids if ref in latest_by_ref]
        )

    async def get_runtime_resume_context_for_user_step(
        self: _ActionRepositoryStepsState,
        *,
        user_id: str,
        action_id: str,
        current_user_step_number: int,
    ) -> RepositoryResult[ActionRuntimeResumeContext]:
        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(ownership_result)
        with self._connect() as connection:
            checkpoint = connection.execute(
                """
                SELECT
                    step_id,
                    step_number,
                    step_name,
                    completed_at,
                    created_at,
                    runtime_state_checkpoint,
                    runtime_state_checkpoint_version
                FROM agent_action_steps
                WHERE user_id = ?
                  AND action_id = ?
                  AND runtime_state_checkpoint IS NOT NULL
                ORDER BY
                    step_number DESC,
                    completed_at IS NULL ASC,
                    completed_at DESC,
                    created_at DESC,
                    step_id DESC
                LIMIT 1
                """,
                (user_id, action_id),
            ).fetchone()
            checkpoint_row = (
                normalize_row(checkpoint, json_columns=_ACTION_STEP_JSON_COLUMNS)
                if checkpoint is not None
                else None
            )
            checkpoint_step_number = (
                checkpoint_row.get("step_number") if checkpoint_row is not None else 0
            )
            if not isinstance(checkpoint_step_number, int):
                raise ActionRuntimeResumeContractError(
                    "Action runtime checkpoint step number is invalid"
                )
            intervening_rows = connection.execute(
                """
                SELECT step_id, user_id, action_id, accepted_sequence, step_number,
                       local_step_number, short_step_id, step_type, status,
                       user_message_id, user_message_json, user_request_text,
                       adopted_process_id, created_at
                FROM agent_action_steps
                WHERE user_id = ? AND action_id = ?
                  AND step_type = 'user_request' AND status = 'success'
                  AND adopted_process_id IS NOT NULL
                  AND step_number > ? AND step_number < ?
                ORDER BY step_number, accepted_sequence, step_id
                LIMIT 2
                """,
                (
                    user_id,
                    action_id,
                    checkpoint_step_number,
                    current_user_step_number,
                ),
            ).fetchall()
        return RepositoryResult(
            data=build_action_runtime_resume_context(
                checkpoint_row=checkpoint_row,
                intervening_rows=tuple(dict(row) for row in intervening_rows),
                expected_user_id=user_id,
                expected_action_id=action_id,
            )
        )

    async def get_runtime_checkpoint_for_approval_resume(
        self: _ActionRepositoryStepsState,
        *,
        user_id: str,
        action_id: str,
        approval_session_id: str,
        tool_request_id: str,
    ) -> RepositoryResult[DBRow]:
        ownership_result = await self._ensure_action_owned(
            user_id=user_id, action_id=action_id
        )
        if ownership_result.error:
            return self._propagate_repository_failure(ownership_result)
        with self._connect() as connection:
            row = connection.execute(
                """
                WITH approval_anchor AS (
                SELECT
                    step_id,
                    step_number
                FROM agent_action_steps AS steps
                WHERE steps.user_id = ?
                  AND steps.action_id = ?
                  AND steps.runtime_state_checkpoint IS NOT NULL
                  AND (
                    (
                        json_extract(
                            steps.runtime_state_checkpoint,
                            '$.pending_approval_request.approval_session_id'
                        ) = ?
                        AND json_extract(
                            steps.runtime_state_checkpoint,
                            '$.pending_approval_request.tool_request_id'
                        ) = ?
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM json_each(
                            steps.runtime_state_checkpoint,
                            '$.current_approval_blockers'
                        ) AS blocker
                        WHERE json_extract(
                            blocker.value,
                            '$.approval_session_id'
                        ) = ?
                          AND json_extract(
                            blocker.value,
                            '$.tool_request_id'
                        ) = ?
                    )
                  )
                ORDER BY
                    steps.step_number DESC,
                    steps.completed_at IS NULL ASC,
                    steps.completed_at DESC,
                    steps.created_at DESC,
                    steps.step_id DESC
                LIMIT 1
                )
                SELECT
                    steps.step_id,
                    steps.step_number,
                    steps.step_name,
                    steps.completed_at,
                    steps.created_at,
                    steps.runtime_state_checkpoint,
                    steps.runtime_state_checkpoint_version,
                    approval_anchor.step_id AS approval_anchor_step_id
                FROM approval_anchor
                JOIN agent_action_steps AS steps
                  ON steps.user_id = ?
                 AND steps.action_id = ?
                 AND steps.runtime_state_checkpoint IS NOT NULL
                 AND steps.step_number >= approval_anchor.step_number
                ORDER BY
                    steps.step_number DESC,
                    steps.completed_at IS NULL ASC,
                    steps.completed_at DESC,
                    steps.created_at DESC,
                    steps.step_id DESC
                LIMIT 1
                """,
                (
                    user_id,
                    action_id,
                    approval_session_id,
                    tool_request_id,
                    approval_session_id,
                    tool_request_id,
                    user_id,
                    action_id,
                ),
            ).fetchone()
        if row is None:
            return RepositoryResult(data=None)
        data = normalize_row(row, json_columns=_ACTION_STEP_JSON_COLUMNS)
        approval_anchor_step_id = str(data.pop("approval_anchor_step_id"))
        return RepositoryResult(
            data=data,
            metadata={"approval_anchor_step_id": approval_anchor_step_id},
        )


__all__ = ["LocalActionRepositoryStepsMixin"]

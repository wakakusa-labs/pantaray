"""ActionAgent execution service."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, cast

from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.log_safety import exception_type_name
from pantaray_agents.agents.action_agent.runtime.models import ExecutionContextModel
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    EXECUTION_CONTEXT_STATE_FIELDS,
    execution_context_from_state,
    execution_context_state_patch,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    ActionAgentStateConfig,
)
from pantaray_agents.agents.action_agent.runtime.state.context import ensure_context
from pantaray_agents.application.action.ports import ActionStepEmitter
from pantaray_agents.config_tunables import ActionAgentTunables
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ActionExecutionContextError,
    VerifiedActionExecutionContextLeafError,
    build_local_tool_definition_seeds,
    ensure_action_scratch_execution_context,
    validate_reusable_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import ExecutionSessionTerminalStatus
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
    ActionRuntimeResumeContext,
)
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.base import AgentError, JSONValue
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

from .failure_recovery import (
    ActionRuntimeApplicationFailure,
    ActionRuntimeFailureStage,
    build_action_runtime_failure_error,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime


_ACTION_STATUS_TO_EXECUTION_SESSION_TERMINAL_STATUS: dict[
    str, ExecutionSessionTerminalStatus
] = {
    "success": "completed",
    "error": "failed",
    "canceled": "canceled",
}

type GraphRunner = Callable[[ActionAgentState], Awaitable[ActionAgentState]]


class InitialStateResolver(Protocol):
    def __call__(
        self,
        *,
        request: ActionAgentRequest,
        started_at: str,
        state_config: ActionAgentStateConfig,
        token_budget: int | None,
        checkpoint_row: object,
        intervening_user_step: ActionResumeUserStep | None,
    ) -> ActionAgentState: ...


class GraphRuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        request: ActionAgentRequest,
        state_config: ActionAgentStateConfig,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
        intervening_user_step: ActionResumeUserStep | None,
    ) -> ActionGraphRuntime: ...


class GraphRunnerFactory(Protocol):
    def __call__(self, runtime: ActionGraphRuntime) -> GraphRunner: ...


@dataclass(frozen=True)
class ExecutionDeps:
    """Dependencies required by the execution service."""

    repository: ActionRepositoryPort
    logger: logging.Logger
    now_provider: Callable[[], str]
    action_agent_tunables: ActionAgentTunables
    coerce_action_token_budget: Callable[[object], int | None]
    build_agent_error: Callable[..., AgentError]
    error_code_prefix: str
    read_local_runtime_db_config: Callable[[], tuple[Path, int]]
    resolve_initial_state: InitialStateResolver
    create_graph_runtime: GraphRuntimeFactory
    build_graph_runner: GraphRunnerFactory


class ActionRuntimeExecutionService:
    """LangGraph 実行と execution session 制御を扱う service。"""

    def __init__(self, deps: ExecutionDeps) -> None:
        self._deps = deps

    async def run_graph(
        self,
        request: ActionAgentRequest,
        *,
        executing_prompt_name: str,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionAgentState:
        started_at = self._deps.now_provider()
        initial_state: ActionAgentState | None = None
        try:
            state_config = self.build_state_config(
                executing_prompt_name=executing_prompt_name,
            )
            existing_action = await self._deps.repository.get_action(
                user_id=str(request.user_id),
                action_id=str(request.action_id),
            )
            if existing_action.error:
                raise RuntimeError(existing_action.error)
            if not isinstance(existing_action.data, dict):
                raise RuntimeError(
                    "Action header must exist before action graph execution starts"
                )
            token_budget = self._deps.coerce_action_token_budget(
                existing_action.data.get("token_budget")
            )
            state_config["token_budget"] = token_budget
            (
                checkpoint_row,
                intervening_user_step,
            ) = await self.load_runtime_checkpoint_for_request(request)
            runtime = self._deps.create_graph_runtime(
                request=request,
                state_config=state_config,
                emit_action_step=emit_action_step,
                emit_error=emit_error,
                intervening_user_step=intervening_user_step,
            )

            initial_state = self._deps.resolve_initial_state(
                request=request,
                started_at=started_at,
                state_config=state_config,
                token_budget=token_budget,
                checkpoint_row=checkpoint_row,
                intervening_user_step=intervening_user_step,
            )
            if initial_state.get("status") == "processing":
                self.attach_local_execution_context_if_required(
                    request=request,
                    started_at=started_at,
                    state=initial_state,
                )
        except Exception as exc:  # noqa: BLE001
            self.raise_action_runtime_failure(
                request=request,
                stage="prepare",
                exc=exc,
                state=initial_state,
            )

        try:
            runner = self._deps.build_graph_runner(runtime)
        except Exception as exc:  # noqa: BLE001
            self.raise_action_runtime_failure(
                request=request,
                stage="build",
                exc=exc,
                state=initial_state,
            )

        try:
            final_state = await runner(initial_state)
        except Exception as exc:  # noqa: BLE001
            self.raise_action_runtime_failure(
                request=request,
                stage="execute",
                exc=exc,
                state=initial_state,
            )

        execution_session_status = self.resolve_execution_session_terminal_status(
            final_state
        )
        if execution_session_status is not None:
            self.try_complete_local_execution_session_if_present(
                state=final_state,
                status=execution_session_status,
                completed_at=self._deps.now_provider(),
            )
        return final_state

    async def load_runtime_checkpoint_for_request(
        self,
        request: ActionAgentRequest,
    ) -> tuple[RepositoryResult[object], ActionResumeUserStep | None]:
        approval_session_id = request.approval_resume_session_id
        tool_request_id = request.approval_resume_tool_request_id
        if approval_session_id is not None and tool_request_id is not None:
            checkpoint = (
                await self._deps.repository.get_runtime_checkpoint_for_approval_resume(
                    user_id=str(request.user_id),
                    action_id=str(request.action_id),
                    approval_session_id=approval_session_id,
                    tool_request_id=tool_request_id,
                )
            )
            return cast(RepositoryResult[object], checkpoint), None
        resume = await self._deps.repository.get_runtime_resume_context_for_user_step(
            user_id=str(request.user_id),
            action_id=str(request.action_id),
            current_user_step_number=request.user_step_number,
        )
        context = resume.data
        if not isinstance(context, ActionRuntimeResumeContext):
            return cast(RepositoryResult[object], resume), None
        return RepositoryResult(
            data=context.checkpoint_row
        ), context.intervening_user_step

    def build_state_config(
        self,
        *,
        executing_prompt_name: str,
    ) -> ActionAgentStateConfig:
        tunables = self._deps.action_agent_tunables
        return {
            "max_steps": tunables.max_steps,
            "max_tool_steps": tunables.max_tool_steps,
            "token_budget": None,
            "prompt_name": executing_prompt_name,
            "prompt_version": "1.0",
            "max_parallel_memory_queries": tunables.max_parallel_memory_queries,
            "cancel_check_max_consecutive_failures": (
                tunables.cancel_check_max_consecutive_failures
            ),
            "cancel_check_failure_grace_seconds": (
                tunables.cancel_check_failure_grace_seconds
            ),
        }

    def raise_action_runtime_failure(
        self,
        *,
        request: ActionAgentRequest,
        stage: ActionRuntimeFailureStage,
        exc: BaseException,
        state: ActionAgentState | None,
    ) -> NoReturn:
        self._deps.logger.exception(
            "Action runtime failed: action_id=%s suggestion_id=%s user_id=%s "
            "stage=%s exception_type=%s",
            request.action_id,
            request.suggestion_id,
            request.user_id,
            stage,
            exception_type_name(exc),
        )

        if is_retryable_repository_exception(exc):
            raise exc

        execution_session_id: str | None = None
        if state is not None:
            raw_execution_session_id = state.get("execution_session_id")
            if isinstance(raw_execution_session_id, str) and raw_execution_session_id:
                execution_session_id = raw_execution_session_id
            self.try_complete_local_execution_session_if_present(
                state=state,
                status="failed",
                completed_at=self._deps.now_provider(),
            )
        error = build_action_runtime_failure_error(
            request=request,
            stage=stage,
            exc=exc,
            build_agent_error=self._deps.build_agent_error,
            error_code_prefix=self._deps.error_code_prefix,
        )
        raise ActionRuntimeApplicationFailure(
            stage=stage,
            error=error,
            execution_session_id=execution_session_id,
        ) from exc

    def attach_local_execution_context_if_required(
        self,
        *,
        request: ActionAgentRequest,
        started_at: str,
        state: ActionAgentState,
    ) -> None:
        try:
            existing_context = execution_context_from_state(state)
            if existing_context is not None:
                db_path, busy_timeout_ms = self._deps.read_local_runtime_db_config()
                validate_reusable_action_scratch_execution_context(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    user_id=str(request.user_id),
                    action_id=str(request.action_id),
                    **existing_context.model_dump(),
                )
        except (ValidationError, ActionExecutionContextError, sqlite3.Error) as exc:
            if isinstance(exc, VerifiedActionExecutionContextLeafError):
                complete_execution_session(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    execution_session_id=exc.execution_session_id,
                    status="failed",
                    completed_at=started_at,
                )
            mutable_state = cast(MutableMapping[str, object], state)
            for field_name in EXECUTION_CONTEXT_STATE_FIELDS:
                mutable_state.pop(field_name, None)
            if isinstance(exc, ValidationError):
                raise RuntimeError(
                    "Action runtime checkpoint must preserve manifest_id, "
                    "execution_session_id, execution_network_policy, "
                    "action_temp_dir, app_runtime_python, and read_access_scope together: "
                    f"{exc}"
                ) from exc
            raise
        if existing_context is not None:
            state.update(execution_context_state_patch(existing_context))
            ensure_context(state)["read_access_scope"] = (
                existing_context.read_access_scope
            )
            return
        db_path, busy_timeout_ms = self._deps.read_local_runtime_db_config()
        allowed_tool_ids = tuple(
            seed.tool_id for seed in build_local_tool_definition_seeds()
        )
        local_context = ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=str(request.user_id),
            action_id=str(request.action_id),
            started_at=started_at,
            allowed_tool_ids=allowed_tool_ids,
        )
        resolved_context = ExecutionContextModel(
            manifest_id=local_context.manifest_id,
            execution_session_id=local_context.execution_session_id,
            execution_network_policy=local_context.network_policy,
            action_temp_dir=str(local_context.action_temp_dir),
            app_runtime_python=str(local_context.app_runtime_python),
            read_access_scope=local_context.read_access_scope,
        )
        state.update(execution_context_state_patch(resolved_context))
        ensure_context(state)["read_access_scope"] = resolved_context.read_access_scope

    def try_complete_local_execution_session_if_present(
        self,
        *,
        state: ActionAgentState,
        status: ExecutionSessionTerminalStatus,
        completed_at: str,
    ) -> None:
        execution_session_id = state.get("execution_session_id")
        if not isinstance(execution_session_id, str) or not execution_session_id:
            return
        db_path, busy_timeout_ms = self._deps.read_local_runtime_db_config()
        try:
            complete_execution_session(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                execution_session_id=execution_session_id,
                status=status,
                completed_at=completed_at,
            )
        except Exception as exc:  # noqa: BLE001
            self._deps.logger.warning(
                "Failed to complete local execution session: execution_session_id=%s status=%s exception_type=%s",
                execution_session_id,
                status,
                exception_type_name(exc),
            )

    @staticmethod
    def resolve_execution_session_terminal_status(
        state: ActionAgentState,
    ) -> ExecutionSessionTerminalStatus | None:
        normalized_status = str(state.get("status") or "").strip().lower()
        return _ACTION_STATUS_TO_EXECUTION_SESSION_TERMINAL_STATUS.get(
            normalized_status
        )

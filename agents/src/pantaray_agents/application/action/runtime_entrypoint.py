"""Shared Action runtime public entrypoint helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pantaray_agents.action_status import (
    ACTION_FAILURE_MESSAGE_RUNNING_FAILED,
    ACTION_FAILURE_STAGE_RUNNING_FAILED,
    action_failure_message_public,
)
from pantaray_agents.agents.action_agent.agent_runtime_facade import (
    ActionGraphRuntimeCreateInput,
    coerce_action_token_budget,
    create_action_graph_runtime,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.application.action.execution_service import (
    ActionRuntimeExecutionService,
    ExecutionDeps,
)
from pantaray_agents.application.action.failure_recovery import (
    ActionRuntimeApplicationFailure,
    action_connection_failure_code,
)
from pantaray_agents.application.action.ports import ActionStepEmitter
from pantaray_agents.application.action.response_service import ActionResponseService
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionAgentResponse,
    ActionExecutionResult,
    ActionRunResult,
)
from pantaray_agents.schema.agent.base import AgentRequest, JSONValue
from pantaray_agents.utils.trace_context import TraceContextManager

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent.agent import ActionAgent

type RequestIdFactory = Callable[[], str]
type CoerceRequest = Callable[[AgentRequest], Awaitable[ActionAgentRequest]]


class ActionRuntimeEntrypointAgentDeps(Protocol):
    """Agent surface required by the shared Action runtime entrypoint."""

    @property
    def response_service(self) -> ActionResponseService: ...

    @property
    def executing_prompt_name(self) -> str: ...

    async def coerce_request(self, request: AgentRequest) -> ActionAgentRequest: ...


@dataclass(frozen=True)
class ActionRuntimeEntrypointDeps:
    """Dependencies required by the shared Action runtime entrypoint."""

    response_service: ActionResponseService
    execution_service: ActionRuntimeExecutionService
    executing_prompt_name: str
    coerce_request: CoerceRequest
    now_provider: Callable[[], str]
    request_id_factory: RequestIdFactory


def build_action_runtime_execution_service(
    agent: ActionAgent,
) -> ActionRuntimeExecutionService:
    """Build the shared LangGraph execution service for Action runtime."""

    response_service = agent.response_service
    return ActionRuntimeExecutionService(
        ExecutionDeps(
            repository=agent.repository,
            logger=agent._logger,
            now_provider=now_utc_iso,
            action_agent_tunables=load_local_runtime_tunables().action_agent,
            coerce_action_token_budget=coerce_action_token_budget,
            build_agent_error=response_service.build_agent_error,
            error_code_prefix=agent.get_error_code_prefix(),
            read_local_runtime_db_config=agent.read_local_runtime_db_config,
            resolve_initial_state=lambda **kwargs: (
                agent.resume_service.resolve_initial_state(**kwargs)
            ),
            create_graph_runtime=lambda **kwargs: create_action_graph_runtime(
                agent,
                agent.runtime_services,
                input=ActionGraphRuntimeCreateInput(
                    request=kwargs["request"],
                    state_config=kwargs["state_config"],
                    intervening_user_step=kwargs["intervening_user_step"],
                ),
                emit_action_step=kwargs["emit_action_step"],
                emit_error=kwargs["emit_error"],
            ),
            build_graph_runner=agent.build_graph_runner,
        )
    )


def build_action_runtime_entrypoint(
    agent: ActionRuntimeEntrypointAgentDeps,
    *,
    execution_service: ActionRuntimeExecutionService,
) -> ActionRuntimeEntrypoint:
    return ActionRuntimeEntrypoint(
        ActionRuntimeEntrypointDeps(
            response_service=agent.response_service,
            execution_service=execution_service,
            executing_prompt_name=agent.executing_prompt_name,
            coerce_request=agent.coerce_request,
            now_provider=now_utc_iso,
            request_id_factory=lambda: str(uuid4()),
        )
    )


class ActionRuntimeEntrypoint:
    """Shared public entrypoint for Action runtime callers."""

    def __init__(self, deps: ActionRuntimeEntrypointDeps) -> None:
        self._deps = deps

    @property
    def now_provider(self) -> Callable[[], str]:
        return self._deps.now_provider

    async def process(self, request: AgentRequest) -> ActionAgentResponse:
        action_request = await self._deps.coerce_request(request)
        try:
            state = await self.run_graph(
                action_request,
                emit_action_step=self._noop_event,
                emit_error=self._noop_event,
            )
        except ActionRuntimeApplicationFailure as exc:
            return self._deps.response_service.build_public_error_response(
                request=action_request,
                created_at=self._deps.now_provider(),
                error=exc.error,
            )
        return self._deps.response_service.state_to_response(state)

    async def stream_action(
        self,
        request: ActionAgentRequest,
        *,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionAgentResponse:
        try:
            state = await self.run_graph(
                request,
                emit_action_step=emit_action_step,
                emit_error=emit_error,
            )
        except ActionRuntimeApplicationFailure as exc:
            public_response = self._deps.response_service.build_public_error_response(
                request=request,
                created_at=self._deps.now_provider(),
                error=exc.error,
            )
            assert public_response.error is not None
            await emit_error(public_response.error.model_dump())
            return public_response
        return self._deps.response_service.state_to_response(state)

    async def execute_action_runtime(
        self,
        request: ActionAgentRequest,
        *,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionExecutionResult:
        try:
            state = await self.run_graph(
                request,
                emit_action_step=emit_action_step,
                emit_error=emit_error,
            )
        except ActionRuntimeApplicationFailure as exc:
            return self.build_application_failure_execution_result(
                request=request,
                failure=exc,
            )
        return self._deps.response_service.state_to_execution_result(state)

    async def run_graph(
        self,
        request: ActionAgentRequest,
        *,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionAgentState:
        with TraceContextManager(
            user_id=str(request.user_id),
            action_id=str(request.action_id),
            suggestion_id=request.suggestion_id,
            request_id=self._deps.request_id_factory(),
        ):
            return await self._deps.execution_service.run_graph(
                request=request,
                executing_prompt_name=self._deps.executing_prompt_name,
                emit_action_step=emit_action_step,
                emit_error=emit_error,
            )

    def build_application_failure_execution_result(
        self,
        *,
        request: ActionAgentRequest,
        failure: ActionRuntimeApplicationFailure,
    ) -> ActionExecutionResult:
        error = failure.error
        # The public terminal carries a code and one sentence, so a cause the
        # user can repair has to be named here; `error_payload` keeps the
        # diagnostic error exactly as the agent raised it.
        failure_code = action_connection_failure_code(error) or error.error_code
        return ActionExecutionResult(
            run_result=ActionRunResult(
                action_id=str(request.action_id),
                suggestion_id=request.suggestion_id,
                user_id=str(request.user_id),
                completed_at=self._deps.now_provider(),
                status="error",
                final_output="",
                action_failure_code=failure_code,
                error_payload=error.model_dump(),
                failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
                failure_message_public=action_failure_message_public(
                    failure_code=failure_code,
                    language=request.user_message.language,
                )
                or ACTION_FAILURE_MESSAGE_RUNNING_FAILED,
                final_prompt_text=None,
                prompt_name=self._deps.executing_prompt_name,
                prompt_version="1.0",
            ),
            execution_session_id=failure.execution_session_id,
            runtime_state_checkpoint=None,
        )

    @staticmethod
    async def _noop_event(_event: object) -> None:
        return None


__all__ = [
    "ActionRuntimeEntrypoint",
    "ActionRuntimeEntrypointDeps",
    "ActionRuntimeEntrypointAgentDeps",
    "build_action_runtime_entrypoint",
    "build_action_runtime_execution_service",
]

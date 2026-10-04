"""ActionAgent の LangGraph 実装。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from pantaray_agents.agents.action_agent.agent_runtime_facade import (
    coerce_action_token_budget,
    load_runtime_approval_session_by_request,
)
from pantaray_agents.agents.action_agent.runtime.graph import (
    ActionGraphRuntime,
    RuntimeServices,
    build_action_agent_graph,
)
from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
    PromptRenderingDeps,
    PromptRenderingService,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    ActionTokenAccountingService,
    TokenAccountingDeps,
)
from pantaray_agents.agents.action_agent.support.formatter import (
    ActionAgentFormatter,
)
from pantaray_agents.agents.core import BaseAgent
from pantaray_agents.application.action.cancellation_service import (
    ActionCancellationService,
    CancellationDeps,
)
from pantaray_agents.application.action.execution_service import (
    ActionRuntimeExecutionService,
    GraphRunner,
)
from pantaray_agents.application.action.persistence import ActionAgentPersistence
from pantaray_agents.application.action.ports import ActionStepEmitter
from pantaray_agents.application.action.response_service import (
    ActionResponseService,
    ResponseDeps,
)
from pantaray_agents.application.action.resume_service import (
    ActionResumeService,
    ResumeDeps,
)
from pantaray_agents.application.action.runtime_entrypoint import (
    ActionRuntimeEntrypoint,
    build_action_runtime_entrypoint,
    build_action_runtime_execution_service,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession
from pantaray_agents.repositories.runtime_ports import ActionRepositoryPort
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionAgentResponse,
    ActionExecutionResult,
)
from pantaray_agents.schema.agent.base import AgentError, AgentRequest, JSONValue
from pantaray_llm.profiles import (
    ACTION_EXECUTING_PROFILE_ID,
    ACTION_PLANNING_PROFILE_ID,
    TOOL_THINKING_PROFILE_ID,
)

logger = logging.getLogger(__name__)
type ActionAgentConfig = Mapping[str, JSONValue]
type ContextDataPayload = dict[str, JSONValue]


class ActionAgent(
    BaseAgent[ActionAgentResponse],
):
    """LangGraph を用いた ActionAgent 実装。"""

    LLM_STAGE_PROFILE_IDS = {
        "planning": ACTION_PLANNING_PROFILE_ID,
        "executing": ACTION_EXECUTING_PROFILE_ID,
        "tool_thinking": TOOL_THINKING_PROFILE_ID,
    }

    EXECUTING_PROMPT_NAME = "action/executing"

    def __init__(
        self,
        config: ActionAgentConfig,
        repository: ActionRepositoryPort | None = None,
    ) -> None:
        super().__init__(dict(config))
        if repository is None:
            raise ValueError("ActionAgent requires a repository.")
        self.repository = repository
        # プロンプト設定のロード（system_instruction と prompt を分離）
        self._executing_config = self._load_prompt_config(self.EXECUTING_PROMPT_NAME)
        formatter = ActionAgentFormatter()
        self._prompt_rendering_service = PromptRenderingService(
            PromptRenderingDeps(formatter=formatter)
        )
        self._logger = logger
        self._initialize_runtime_support()

    def _now_iso(self) -> str:
        return now_utc_iso()

    @property
    def executing_prompt_name(self) -> str:
        return self.EXECUTING_PROMPT_NAME

    @property
    def prompt_rendering_service(self) -> PromptRenderingService:
        return self._prompt_rendering_service

    @property
    def token_accounting_service(self) -> ActionTokenAccountingService:
        return self._token_accounting_service

    @property
    def response_service(self) -> ActionResponseService:
        return self._response_service

    @property
    def persistence(self) -> ActionAgentPersistence:
        return self._persistence

    @property
    def resume_service(self) -> ActionResumeService:
        return self._resume_service

    @property
    def cancellation_service(self) -> ActionCancellationService:
        return self._cancellation_service

    @property
    def runtime_services(self) -> RuntimeServices:
        return self._runtime_services

    @property
    def execution_service(self) -> ActionRuntimeExecutionService:
        return self._execution_service

    @property
    def runtime_entrypoint(self) -> ActionRuntimeEntrypoint:
        return self._runtime_entrypoint

    async def coerce_request(self, request: AgentRequest) -> ActionAgentRequest:
        return await self._validate_request(request)

    def read_local_runtime_db_config(self) -> tuple[Path, int]:
        return read_local_runtime_db_config()

    def load_approval_session_by_request(
        self,
        user_id: str,
        tool_request_id: str,
    ) -> StoredApprovalSession | None:
        return load_runtime_approval_session_by_request(
            self.read_local_runtime_db_config,
            user_id,
            tool_request_id,
        )

    def build_graph_runner(self, runtime: ActionGraphRuntime) -> GraphRunner:
        return build_action_agent_graph(runtime)

    def _initialize_runtime_support(self) -> None:
        self._persistence = ActionAgentPersistence(
            default_prompt_name=self.executing_prompt_name,
            default_prompt_version="1.0",
            logger=self._logger,
            now_provider=now_utc_iso,
        )
        self._response_service = ActionResponseService(
            ResponseDeps(persistence=self._persistence)
        )
        self._token_accounting_service = ActionTokenAccountingService(
            TokenAccountingDeps(
                build_agent_error=self._response_service.build_agent_error,
            )
        )
        self._resume_service = ActionResumeService(
            ResumeDeps(
                logger=self._logger,
                load_approval_session_by_request=self.load_approval_session_by_request,
                build_agent_error=self._response_service.build_agent_error,
                now_provider=now_utc_iso,
            )
        )
        self._cancellation_service = ActionCancellationService(
            CancellationDeps(
                repository=self.repository,
                logger=self._logger,
                now_provider=now_utc_iso,
                build_agent_error=self._response_service.build_agent_error,
            )
        )
        self._runtime_services = RuntimeServices(
            resume=self._resume_service,
            cancellation=self._cancellation_service,
            response=self._response_service,
            rendering=self.prompt_rendering_service,
            token_accounting=self.token_accounting_service,
        )
        self._execution_service = build_action_runtime_execution_service(self)
        self._runtime_entrypoint = build_action_runtime_entrypoint(
            self,
            execution_service=self._execution_service,
        )

    def _coerce_action_token_budget(self, raw: object) -> int | None:
        return coerce_action_token_budget(raw)

    @property
    def executing_prompt(self) -> str:
        """Executing プロンプト（prompt部分のみ）を返す。"""
        return self._executing_config.prompt

    @property
    def executing_system_instruction(self) -> str | None:
        """Executing の system_instruction を返す。"""
        return self._executing_config.system_instruction

    def executing_role_rule(self, key: str) -> str:
        """Executing prompt の role rule を返す。"""
        return self._executing_config.require_role_rule(key)

    def executing_world_state_update(self, key: str) -> str:
        """Executing prompt の world state 更新テンプレートを返す。"""
        return self._executing_config.require_world_state_update(key)

    def get_response_class(self) -> type[ActionAgentResponse]:
        return ActionAgentResponse

    def get_error_code_prefix(self) -> str:
        return "ACTION"

    async def _validate_request(self, request: AgentRequest) -> ActionAgentRequest:
        """リクエストを ActionAgentRequest に整形する。"""

        if isinstance(request, ActionAgentRequest):
            return request
        model_dump = getattr(request, "model_dump", None)
        if callable(model_dump):
            payload = model_dump()
            if isinstance(payload, dict):
                return ActionAgentRequest(**payload)
        raise TypeError("ActionAgentRequest input is required.")

    async def _fetch_context_data(self, request: AgentRequest) -> ContextDataPayload:
        return {}

    def _build_prompt(self, context_data: ContextDataPayload) -> str:
        raise NotImplementedError(
            "_build_prompt is not used directly in the LangGraph implementation."
        )

    async def _process_llm_response(self, prompt: str) -> ContextDataPayload:
        raise NotImplementedError(
            "LangGraph 実装では _process_llm_response を直接使用しません"
        )

    def _create_success_response(
        self,
        request: AgentRequest,
        extracted_data: ContextDataPayload,
    ) -> ActionAgentResponse:
        raise NotImplementedError(
            "LangGraph 実装では _create_success_response を直接使用しません"
        )

    async def _save_response(self, response: ActionAgentResponse) -> None:
        raise NotImplementedError(
            "LangGraph 実装では _save_response を直接使用しません"
        )

    def _get_id_attrs(self) -> dict[str, str]:
        return {"action_id": "action_id", "suggestion_id": "suggestion_id"}

    async def _handle_agent_error(
        self,
        error: AgentError,
        response_params: dict[str, object],
    ) -> ActionAgentResponse:
        raise NotImplementedError(
            "LangGraph 実装では _handle_agent_error を直接使用しません"
        )

    async def process(self, request: AgentRequest) -> ActionAgentResponse:
        return await self._runtime_entrypoint.process(request)

    async def stream_action(
        self,
        request: ActionAgentRequest,
        *,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionAgentResponse:
        return await self._runtime_entrypoint.stream_action(
            request,
            emit_action_step=emit_action_step,
            emit_error=emit_error,
        )

    async def execute_action_runtime(
        self,
        request: ActionAgentRequest,
        *,
        emit_action_step: ActionStepEmitter,
        emit_error: Callable[[Mapping[str, JSONValue]], Awaitable[None]],
    ) -> ActionExecutionResult:
        return await self._runtime_entrypoint.execute_action_runtime(
            request,
            emit_action_step=emit_action_step,
            emit_error=emit_error,
        )

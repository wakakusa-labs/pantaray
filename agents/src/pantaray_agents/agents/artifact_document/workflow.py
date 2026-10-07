from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from pantaray_agents.agents.artifact_react import (
    ARTIFACT_PATCH_TOOL_NAME,
    PatchCommitResult,
    ReactLoopResult,
    ReactLoopStep,
    build_artifact_react_response_format,
    build_artifact_react_tools_definition_block,
)
from pantaray_agents.agents.core.error_contract import (
    AgentErrorPhase,
    AgentPhaseError,
    build_agent_error,
)
from pantaray_agents.agents.core.llm_file_inputs import LlmFileInput
from pantaray_agents.agents.core.mixins.llm_usage import CountingSink, TokenSink
from pantaray_agents.schema.agent import AgentRequest, AgentResponse
from pantaray_agents.schema.agent.base import (
    AgentError,
    JSONValue,
)
from pantaray_agents.tools.contract import ReactToolCall, ReactToolDefinition
from pantaray_agents.utils.artifact_patch.errors import ArtifactPatchConflictError
from pantaray_agents.utils.structured_artifact_patch import (
    StructuredArtifactPatch,
    apply_structured_artifact_patch,
)
from pantaray_agents.utils.trace_context import TraceContextManager

from .runner import ReActAgentRunner
from .types import ReActAgentRunInput, ReActAgentRunResult

type ArtifactContextPayload = dict[str, JSONValue]


class ReActAgentHost(Protocol):
    DEFAULT_SYSTEM_INSTRUCTION: str
    _current_language: str | None

    def _compose_system_instruction(
        self, *, base_instruction: str, language: str | None
    ) -> str: ...

    async def _generate_llm_response(
        self,
        prompt: str,
        *,
        sink: TokenSink,
        system_instruction: str | None = None,
        file_inputs: list[LlmFileInput] | None = None,
        stage: str | None = None,
        response_schema: object | None = None,
    ) -> str | object: ...

    def _consume_llm_thoughts(self) -> str | None: ...


class ReActAgent[
    RequestT: AgentRequest,
    ResponseT: AgentResponse,
    ContextT: ArtifactContextPayload,
](ABC):
    agent_name: str
    prompt_name: str
    prompt_version: str
    logical_path: str
    host: ReActAgentHost
    patch_loop_stage: str
    error_code_prefix: str

    async def process(self, request: AgentRequest) -> ResponseT:
        validated_request: RequestT | None = None
        try:
            validated_request = self.validate_request(request)
        except Exception as exc:
            return await self.handle_process_error(
                error=self.build_error(AgentErrorPhase.VALIDATION, exc),
                request=request,
                cause=exc,
            )

        try:
            language = self.request_language(validated_request)
            user_id = self.request_user_id(validated_request)
        except Exception as exc:
            return await self.handle_process_error(
                error=self.build_error(AgentErrorPhase.VALIDATION, exc),
                request=validated_request,
                cause=exc,
            )

        try:
            self.host._current_language = language
            with TraceContextManager(user_id=user_id):
                return await ReActAgentRunner().run(
                    definition=self, request=validated_request
                )
        except AgentPhaseError as exc:
            if exc.phase is AgentErrorPhase.ERROR_PERSISTENCE:
                raise exc.cause from exc
            return await self.handle_process_error(
                error=self.build_error(exc.phase, exc.cause),
                request=validated_request,
                cause=exc.cause,
            )
        except Exception as exc:
            return await self.handle_process_error(
                error=self.build_error(AgentErrorPhase.UNEXPECTED, exc),
                request=validated_request,
                cause=exc,
            )

    async def prepare(
        self, request: RequestT
    ) -> ReActAgentRunInput[RequestT, ResponseT]:
        try:
            context_data = await self.load_context(request)
            base_text = self.base_text_from_context(context_data)
        except Exception as exc:
            raise AgentPhaseError(AgentErrorPhase.CONTEXT_FETCH, exc) from exc

        try:
            await self.start_run(
                request=request, context_data=context_data, base_text=base_text
            )
            run_id = self.run_id_from_context(
                request=request, context_data=context_data
            )
        except Exception as exc:
            raise AgentPhaseError(AgentErrorPhase.RUN_START, exc) from exc

        tool_definitions = self.react_tools_for_context(
            request=request,
            context_data=context_data,
        )
        response_schema = build_artifact_react_response_format(
            tool_definitions=tool_definitions
        )
        sink = CountingSink()

        async def call_llm(prompt: str) -> str | object:
            return await self.host._generate_llm_response(
                prompt=prompt,
                sink=sink,
                system_instruction=self.host._compose_system_instruction(
                    base_instruction=self.patch_loop_base_instruction(),
                    language=self.host._current_language,
                ),
                stage=self.patch_loop_stage,
                response_schema=response_schema,
            )

        async def commit_patch(
            tool_base_text: str,
            updated_text: str,
            base_sha256: str,
            call: ReactToolCall,
            step_number: int,
        ) -> PatchCommitResult:
            try:
                return await self.commit_domain_patch(
                    request=request,
                    context_data=context_data,
                    tool_base_text=tool_base_text,
                    updated_text=updated_text,
                    base_sha256=base_sha256,
                    call=call,
                    step_number=step_number,
                )
            except Exception as exc:
                raise AgentPhaseError(self.commit_error_phase(exc), exc) from exc

        async def record_step(step: ReactLoopStep) -> None:
            try:
                await self.record_step(
                    request=request,
                    context_data=context_data,
                    step=step,
                )
            except Exception as exc:
                raise AgentPhaseError(AgentErrorPhase.STEP_RECORD, exc) from exc

        async def build_success_response(
            response_request: RequestT, result: ReActAgentRunResult
        ) -> ResponseT:
            try:
                return await self.build_success_response(response_request, result)
            except Exception as exc:
                raise AgentPhaseError(AgentErrorPhase.RESPONSE_BUILD, exc) from exc

        async def build_error_response(
            response_request: RequestT, error: AgentError
        ) -> ResponseT:
            return await self.complete_error_response(response_request, error)

        async def commit_completed_without_patch(completed_base_text: str) -> None:
            try:
                await self.commit_completed_without_patch(
                    request=request,
                    context_data=context_data,
                    base_text=completed_base_text,
                )
            except Exception as exc:
                raise AgentPhaseError(self.commit_error_phase(exc), exc) from exc

        return ReActAgentRunInput(
            run_id=run_id,
            logical_path=self.logical_path,
            base_text=base_text,
            build_prompt=lambda current_text, last_error: self.build_prompt(
                context_data=context_data,
                current_text=current_text,
                last_error=last_error,
                tool_definitions=tool_definitions,
            ),
            call_llm=call_llm,
            apply_patch=self.apply_patch,
            commit_patch=commit_patch,
            record_step=record_step,
            build_success_response=build_success_response,
            build_error_response=build_error_response,
            tools=tool_definitions,
            commit_completed_without_patch=commit_completed_without_patch,
            consume_llm_thoughts=self.host._consume_llm_thoughts,
        )

    def build_prompt(
        self,
        *,
        context_data: ContextT,
        current_text: str,
        last_error: str | None,
        tool_definitions: tuple[ReactToolDefinition, ...] = (),
    ) -> str:
        tools_definitions = build_artifact_react_tools_definition_block(
            logical_path=self.logical_path,
            tool_definitions=tool_definitions,
        )
        domain_prompt = self.build_domain_prompt(
            context_data=context_data,
            current_text=current_text,
            tools_definitions=tools_definitions,
        )
        prompt = domain_prompt
        if last_error:
            return f"{prompt}\n\n# Previous Error\n{last_error}\n"
        return prompt

    def apply_patch(self, base_text: str, patch: StructuredArtifactPatch) -> str:
        return apply_structured_artifact_patch(base_text=base_text, patch=patch)

    def react_tools_for_context(
        self,
        *,
        request: RequestT,
        context_data: ContextT,
    ) -> tuple[ReactToolDefinition, ...]:
        _ = request, context_data
        return ()

    @abstractmethod
    def validate_request(self, request: AgentRequest) -> RequestT: ...

    def request_language(self, request: RequestT) -> str | None:
        language = getattr(request, "language", None)
        return language if isinstance(language, str) else None

    def request_user_id(self, request: RequestT) -> str:
        user_id = getattr(request, "user_id", None)
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id is required")
        return user_id

    def build_error(
        self, phase: AgentErrorPhase, exception: Exception | None = None
    ) -> AgentError:
        return build_agent_error(
            prefix=self.error_code_prefix,
            phase=phase,
            exception=exception,
        )

    async def handle_process_error(
        self,
        *,
        error: AgentError,
        request: AgentRequest,
        cause: Exception,
    ) -> ResponseT:
        try:
            return await self.complete_error_response(request, error)
        except AgentPhaseError as handler_exc:
            raise handler_exc.cause from cause

    def error_from_loop_result(self, loop_result: ReactLoopResult) -> AgentError:
        return self.build_error(self.error_phase_from_loop_result(loop_result))

    def error_phase_from_loop_result(
        self, loop_result: ReactLoopResult
    ) -> AgentErrorPhase:
        last_error = loop_result.last_error or ""
        if _all_recorded_steps_are_llm(loop_result):
            return AgentErrorPhase.LLM
        terminal_tool_error = _terminal_tool_error(loop_result)
        if terminal_tool_error is not None:
            if terminal_tool_error.tool_name == ARTIFACT_PATCH_TOOL_NAME:
                return AgentErrorPhase.PATCH
        if _has_artifact_patch_tool_error(loop_result):
            return AgentErrorPhase.PATCH
        if "max_llm_turns" in last_error or "max_tool_calls" in last_error:
            return AgentErrorPhase.LLM
        return AgentErrorPhase.UNEXPECTED

    def commit_error_phase(self, exc: Exception) -> AgentErrorPhase:
        return (
            AgentErrorPhase.CONFLICT
            if _has_cause(exc, ArtifactPatchConflictError)
            else AgentErrorPhase.COMMIT
        )

    async def complete_error_response(
        self, request: AgentRequest, error: AgentError
    ) -> ResponseT:
        response_params = self.response_params_from_request(request)
        try:
            await self.persist_error(error, response_params)
        except Exception as exc:
            raise AgentPhaseError(AgentErrorPhase.ERROR_PERSISTENCE, exc) from exc
        try:
            return await self.build_error_response(request, error)
        except Exception as exc:
            raise AgentPhaseError(AgentErrorPhase.RESPONSE_BUILD, exc) from exc

    @abstractmethod
    def response_params_from_request(
        self, request: AgentRequest
    ) -> dict[str, object]: ...

    @abstractmethod
    async def persist_error(
        self, error: AgentError, response_params: dict[str, object]
    ) -> None: ...

    @abstractmethod
    async def load_context(self, request: RequestT) -> ContextT: ...

    @abstractmethod
    def base_text_from_context(self, context_data: ContextT) -> str: ...

    @abstractmethod
    def run_id_from_context(
        self, *, request: RequestT, context_data: ContextT
    ) -> str: ...

    @abstractmethod
    async def start_run(
        self,
        *,
        request: RequestT,
        context_data: ContextT,
        base_text: str,
    ) -> None: ...

    @abstractmethod
    def build_domain_prompt(
        self, *, context_data: ContextT, current_text: str, tools_definitions: str
    ) -> str: ...

    @abstractmethod
    def patch_loop_base_instruction(self) -> str: ...

    @abstractmethod
    async def commit_domain_patch(
        self,
        *,
        request: RequestT,
        context_data: ContextT,
        tool_base_text: str,
        updated_text: str,
        base_sha256: str,
        call: ReactToolCall,
        step_number: int,
    ) -> PatchCommitResult: ...

    @abstractmethod
    async def record_step(
        self,
        *,
        request: RequestT,
        context_data: ContextT,
        step: ReactLoopStep,
    ) -> None: ...

    @abstractmethod
    async def commit_completed_without_patch(
        self,
        *,
        request: RequestT,
        context_data: ContextT,
        base_text: str,
    ) -> None: ...

    @abstractmethod
    async def build_success_response(
        self, request: RequestT, result: ReActAgentRunResult
    ) -> ResponseT: ...

    @abstractmethod
    async def build_error_response(
        self, request: RequestT, error: AgentError
    ) -> ResponseT: ...


def _all_recorded_steps_are_llm(loop_result: ReactLoopResult) -> bool:
    return bool(loop_result.steps) and all(
        step.step_kind == "llm" for step in loop_result.steps
    )


def _terminal_tool_error(loop_result: ReactLoopResult) -> ReactLoopStep | None:
    if not loop_result.last_error:
        return None
    for step in reversed(loop_result.steps):
        if (
            step.step_kind == "tool"
            and step.status == "error"
            and step.error_message == loop_result.last_error
        ):
            return step
    return None


def _has_artifact_patch_tool_error(loop_result: ReactLoopResult) -> bool:
    return any(
        step.step_kind == "tool"
        and step.status == "error"
        and step.tool_name == ARTIFACT_PATCH_TOOL_NAME
        for step in loop_result.steps
    )


def _has_cause(exc: BaseException, expected_type: type[BaseException]) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, expected_type):
            return True
        current = current.__cause__
    return False

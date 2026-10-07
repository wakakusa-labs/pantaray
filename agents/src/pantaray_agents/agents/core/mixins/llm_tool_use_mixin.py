from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, overload

from pantaray_agents.agents.core.llm_file_inputs import (
    LlmFileInput,
    build_blob_file_block,
    interleave_file_inputs,
)
from pantaray_agents.utils.llm_types import types
from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
)
from pantaray_llm.contracts.conversation import (
    AnthropicProviderTurn,
    LlmConversation,
    LlmProviderTurn,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.tool_use import (
    AnthropicToolContinuation,
    LlmToolCall,
    LlmToolContinuation,
    LlmToolDefinition,
    LlmToolResult,
    LlmToolUseRequest,
    OpenAiToolContinuation,
)
from pantaray_llm.errors import LlmProxyExecutionError

from .llm_generation_mixin import (
    _LLM_THOUGHTS_CONTEXT,
    LLMGenerationMixin,
)
from .llm_usage import (
    LlmUsage,
    TokenSink,
    add_usage_metadata,
)


@dataclass(frozen=True, slots=True)
class LlmToolCallTurn:
    calls: tuple[LlmToolCall, ...]
    continuation: LlmToolContinuation | None
    dropped_call_names: tuple[str, ...] = ()

    @property
    def call(self) -> LlmToolCall:
        return self.calls[0]


@dataclass(frozen=True, slots=True)
class ActionTurnReply:
    """One Action turn, and the provider output the next turn may hand back."""

    response: LlmActionTurnResponse
    # Beside the response rather than inside it: ``LlmActionTurnResponse`` is
    # validated closed and shared with the cloud. None on a request that sent
    # no conversation, and from a cloud deployment that predates the field.
    provider_turn: LlmProviderTurn | None


class LlmToolUseResponseError(RuntimeError):
    pass


def _validate_tool_call_response(
    *, response: object, continuation_mode: Literal["disabled", "stateless"]
) -> LlmToolCallTurn:
    calls = getattr(response, "tool_calls", ())
    if not isinstance(calls, tuple) or not calls:
        raise LlmToolUseResponseError("LLM proxy returned no native tool call")
    dropped_call_names = getattr(response, "dropped_tool_call_names", ())
    continuation = getattr(response, "tool_continuation", None)
    if continuation is not None and not isinstance(
        continuation, OpenAiToolContinuation | AnthropicToolContinuation
    ):
        raise LlmToolUseResponseError("LLM proxy returned invalid continuation state")
    if continuation_mode == "stateless" and continuation is None:
        raise LlmToolUseResponseError("LLM proxy omitted required continuation state")
    if continuation_mode == "disabled" and continuation is not None:
        raise LlmToolUseResponseError(
            "LLM proxy returned unexpected continuation state"
        )
    return LlmToolCallTurn(
        calls=calls,
        continuation=continuation,
        dropped_call_names=dropped_call_names,
    )


class LlmToolUseMixin(LLMGenerationMixin):
    async def _generate_llm_tool_call(
        self,
        *,
        sink: TokenSink,
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        continuation_mode: Literal["disabled", "stateless"],
        continuation: LlmToolContinuation | None = None,
        tool_result: LlmToolResult | None = None,
        conversation: LlmConversation | None = None,
        max_parallel_tool_calls: int = 1,
        system_instruction: str | None = None,
        file_inputs: list[LlmFileInput] | None = None,
        stage: str | None = None,
        before_attempt: Callable[[], None] | None = None,
    ) -> LlmToolCallTurn:
        return await self._generate_llm_native_turn(
            sink=sink,
            prompt=prompt,
            request=LlmToolUseRequest(
                tools=list(tools),
                continuation_mode=continuation_mode,
                continuation=continuation,
                tool_result=tool_result,
                conversation=conversation,
                max_parallel_tool_calls=max_parallel_tool_calls,
            ),
            system_instruction=system_instruction,
            file_inputs=file_inputs,
            stage=stage,
            before_attempt=before_attempt,
        )

    async def _generate_llm_action_turn(
        self,
        *,
        sink: TokenSink,
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        max_parallel_tool_calls: int = 1,
        system_instruction: str | None = None,
        file_inputs: list[LlmFileInput] | None = None,
        conversation: LlmConversation | None = None,
        stage: str | None = None,
        before_attempt: Callable[[], None] | None = None,
    ) -> ActionTurnReply:
        return await self._generate_llm_native_turn(
            sink=sink,
            prompt=prompt,
            request=LlmActionTurnRequest(
                mode="action_turn",
                tools=list(tools),
                max_parallel_tool_calls=max_parallel_tool_calls,
                conversation=conversation,
            ),
            system_instruction=system_instruction,
            file_inputs=file_inputs,
            stage=stage,
            before_attempt=before_attempt,
        )

    @overload
    async def _generate_llm_native_turn(
        self,
        *,
        sink: TokenSink,
        prompt: str,
        request: LlmToolUseRequest,
        system_instruction: str | None,
        file_inputs: list[LlmFileInput] | None,
        stage: str | None,
        before_attempt: Callable[[], None] | None,
    ) -> LlmToolCallTurn: ...

    @overload
    async def _generate_llm_native_turn(
        self,
        *,
        sink: TokenSink,
        prompt: str,
        request: LlmActionTurnRequest,
        system_instruction: str | None,
        file_inputs: list[LlmFileInput] | None,
        stage: str | None,
        before_attempt: Callable[[], None] | None,
    ) -> ActionTurnReply: ...

    async def _generate_llm_native_turn(
        self,
        *,
        sink: TokenSink,
        prompt: str,
        request: LlmToolUseRequest | LlmActionTurnRequest,
        system_instruction: str | None,
        file_inputs: list[LlmFileInput] | None,
        stage: str | None,
        before_attempt: Callable[[], None] | None,
    ) -> LlmToolCallTurn | ActionTurnReply:
        sink.guard()
        use_system_instruction = self._resolve_system_instruction(system_instruction)
        inference_profile = self._resolve_inference_profile_id(stage=stage)
        sends_conversation = request.conversation is not None
        contents = (
            # The conversation's own items place this media, so the request's
            # message stays text only -- which is also what keeps that message
            # byte-identical from turn to turn.
            [prompt, *(build_blob_file_block(file) for file in file_inputs or ())]
            if sends_conversation
            else interleave_file_inputs(prompt=prompt, file_inputs=file_inputs)
        )

        response = None
        last_exc: Exception | None = None
        usage_ledger = LlmUsage(None, None)
        for attempt in range(self._LLM_RETRY_MAX_ATTEMPTS):
            if before_attempt is not None:
                before_attempt()
            try:
                response = await self.client.aio.models.generate_content(
                    contents=contents,
                    config=types.GenerateContentConfig(
                        inference_profile=inference_profile,
                        system_instruction=use_system_instruction,
                        tool_use=request,
                    ),
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                usage = (
                    exc.usage_metadata
                    if isinstance(exc, LlmProxyExecutionError)
                    else None
                )
                usage_ledger = add_usage_metadata(usage_ledger, usage)
                should_retry = self._is_retryable_llm_error(exc) and (
                    attempt < self._LLM_RETRY_MAX_ATTEMPTS - 1
                )
                if not should_retry:
                    sink.record(usage_ledger, stage=stage, may_raise=False)
                    self._raise_llm_upstream_error(exc)
                await self._sleep_llm_retry_backoff(attempt)

        if last_exc is not None or response is None:
            if last_exc is None:
                raise RuntimeError("LLM upstream error: unknown failure")
            self._raise_llm_upstream_error(last_exc)
            raise AssertionError("LLM upstream error must raise")

        usage = getattr(response, "usage_metadata", None)
        usage_ledger = add_usage_metadata(usage_ledger, usage)
        thoughts = self._extract_thoughts_best_effort(response)
        self._last_llm_thoughts = thoughts
        _LLM_THOUGHTS_CONTEXT.set(thoughts)
        try:
            turn: LlmToolCallTurn | ActionTurnReply
            if isinstance(request, LlmActionTurnRequest):
                action_turn = getattr(response, "action_turn", None)
                if not isinstance(action_turn, LlmActionTurnResponse):
                    raise LlmToolUseResponseError("LLM proxy returned no Action turn")
                provider_turn = getattr(response, "provider_turn", None)
                turn = ActionTurnReply(
                    response=action_turn,
                    provider_turn=(
                        provider_turn
                        if isinstance(
                            provider_turn, OpenAiProviderTurn | AnthropicProviderTurn
                        )
                        else None
                    ),
                )
            else:
                turn = _validate_tool_call_response(
                    response=response,
                    continuation_mode=request.continuation_mode,
                )
        except LlmToolUseResponseError:
            sink.record(usage_ledger, stage=stage, may_raise=False)
            raise
        sink.record(usage_ledger, stage=stage, may_raise=True)
        return turn


__all__ = [
    "ActionTurnReply",
    "LlmToolCallTurn",
    "LlmToolUseMixin",
    "LlmToolUseResponseError",
]

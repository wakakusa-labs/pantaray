"""ActionAgent token accounting service."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

from pantaray_agents.agents.action_agent.runtime.log_safety import safe_value_shape
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.coercion import (
    coerce_int_with_invalid_reason,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.core.mixins.llm_usage import (
    LlmUsage,
    TokenBudgetExceeded,
    add_llm_usage,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import AgentError

type AgentErrorBuilder = Callable[..., AgentError]

_ACCOUNTING_COUNTER_FIELDS = (
    "total_prompt_tokens",
    "total_completion_tokens",
    "tokens_used",
    "token_budget",
)


@dataclass(frozen=True)
class TokenAccountingDeps:
    """Dependencies required by the token accounting service."""

    build_agent_error: AgentErrorBuilder


class ActionTokenAccountingService:
    """Action-state token accounting."""

    def __init__(self, deps: TokenAccountingDeps) -> None:
        self._deps = deps

    def enforce_merged_token_budget_once(
        self,
        state: ActionAgentState,
        *,
        stage: str,
    ) -> bool:
        """Recheck the budget once after Goal Worker deltas are merged."""

        return (
            self._apply_token_usage(
                state,
                prompt_tokens=None,
                completion_tokens=None,
                stage=stage,
            )
            is not None
        )

    def _apply_token_usage(
        self,
        state: ActionAgentState,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        stage: str,
    ) -> AgentError | None:
        raw_state = cast("Mapping[str, object]", state)

        def _record_token_anomaly(
            *,
            error_code: str,
            field_name: str,
            raw_value: object,
            reason: str,
        ) -> None:
            warning_error = self._deps.build_agent_error(
                error_type="internal_error",
                error_code=error_code,
                error_message=(
                    "Token accounting used a sanitized fallback due to invalid state value."
                ),
                severity="warning",
                error_details={
                    "stage": stage,
                    "field_name": field_name,
                    "reason": reason,
                    "raw_type": type(raw_value).__name__,
                    "raw_shape": safe_value_shape(raw_value),
                },
                metadata={
                    "action_id": str(state.get("action_id") or ""),
                    "suggestion_id": str(state.get("suggestion_id") or ""),
                    "user_id": str(state.get("user_id") or ""),
                },
            )
            append_state_error(state, error=warning_error)

        def _coerce_token_counter(field_name: str) -> int:
            raw = raw_state.get(field_name)

            def _record_invalid(reason: str) -> None:
                _record_token_anomaly(
                    error_code="ACTION_TOKEN_COUNTER_INVALID",
                    field_name=field_name,
                    raw_value=raw,
                    reason=reason,
                )

            return coerce_int_with_invalid_reason(raw, on_invalid=_record_invalid)

        total_prompt = _coerce_token_counter("total_prompt_tokens")
        total_completion = _coerce_token_counter("total_completion_tokens")
        tokens_used = _coerce_token_counter("tokens_used")

        if prompt_tokens is not None:
            total_prompt += prompt_tokens
            tokens_used += prompt_tokens
        if completion_tokens is not None:
            total_completion += completion_tokens
            tokens_used += completion_tokens

        state["total_prompt_tokens"] = total_prompt
        state["total_completion_tokens"] = total_completion
        state["tokens_used"] = tokens_used

        raw_budget = raw_state.get("token_budget")
        budget: int | None = None
        if raw_budget not in (None, ""):

            def _record_invalid_budget(reason: str) -> None:
                _record_token_anomaly(
                    error_code="ACTION_TOKEN_BUDGET_INVALID",
                    field_name="token_budget",
                    raw_value=raw_budget,
                    reason=reason,
                )

            parsed = coerce_int_with_invalid_reason(
                raw_budget,
                on_invalid=_record_invalid_budget,
            )
            budget = parsed if parsed > 0 else None

        state["token_budget"] = budget

        if budget is not None and tokens_used > budget:
            excess_error = self._deps.build_agent_error(
                error_type="rate_limit_error",
                error_code="ACTION_TOKEN_BUDGET_EXCEEDED",
                error_message=f"Token budget exceeded while processing {stage}.",
                error_details={
                    "tokens_used": tokens_used,
                    "token_budget": budget,
                },
            )
            append_state_error(state, error=excess_error)
            set_status_with_updated_at(
                state,
                status="error",
                updated_at=now_utc_iso(),
            )
            state["final_output"] = ""
            state["next_action"] = None
            return excess_error
        return None


class StateTokenSink:
    """Action-state sink with run-local delta and sticky budget exhaustion."""

    def __init__(
        self,
        service: ActionTokenAccountingService,
        state: ActionAgentState,
    ) -> None:
        self._service = service
        self._state = state
        self._delta = LlmUsage(None, None)
        self._budget_error: AgentError | None = None

    @property
    def state(self) -> ActionAgentState:
        return self._state

    @property
    def delta(self) -> LlmUsage:
        return self._delta

    @property
    def budget_error(self) -> AgentError | None:
        return self._budget_error

    def guard(self) -> None:
        if self._budget_error is not None:
            raise TokenBudgetExceeded(self._budget_error, self)

    def record(
        self,
        usage: LlmUsage,
        *,
        stage: str | None,
        may_raise: bool,
    ) -> None:
        excess_error = self._service._apply_token_usage(  # noqa: SLF001
            self._state,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            stage=stage or "LLM call",
        )
        self._delta = add_llm_usage(self._delta, usage)
        if excess_error is None:
            return
        self._budget_error = excess_error
        if may_raise:
            raise TokenBudgetExceeded(excess_error, self)

    def rebind_state(self, state: ActionAgentState) -> None:
        """Project accounting-owned values to a replacement state without re-adding delta."""

        previous_state = self._state
        for field_name in _ACCOUNTING_COUNTER_FIELDS:
            state[field_name] = previous_state[field_name]  # type: ignore[literal-required]

        if self._budget_error is not None:
            raw_errors = state.get("errors")
            budget_error_present = isinstance(raw_errors, list) and any(
                error.get("error_code") == self._budget_error.error_code
                for error in raw_errors
            )
            if not budget_error_present:
                append_state_error(state, error=self._budget_error)
            state["status"] = "error"
            state["final_output"] = ""
            state["next_action"] = None

        self._state = state

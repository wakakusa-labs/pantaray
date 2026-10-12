"""Typed Action runtime failures and their pure error mapping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pantaray_agents.action_status import (
    ACTION_FAILURE_CODE_CONNECTION_NOT_CONFIGURED,
    ACTION_FAILURE_CODE_CONNECTION_REJECTED,
    ACTION_FAILURE_CODE_IMAGE_INPUT_INVALID,
    ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE,
    ACTION_FAILURE_CODE_LLM_REFUSED,
    ACTION_FAILURE_CODE_SIGN_IN_EXPIRED,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    CounterInvariantError,
)
from pantaray_agents.proxy_errors import build_llm_proxy_agent_error
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.base import AgentError
from pantaray_llm.errors import (
    MODEL_REFUSAL_RESPONSE_STATUS,
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    PROXY_INVALID_INPUT,
    PROXY_UPSTREAM_FORBIDDEN,
    LlmProxyExecutionError,
)

type AgentErrorBuilder = Callable[..., AgentError]
type ActionRuntimeFailureStage = Literal["prepare", "build", "execute"]

_UNEXPECTED_FAILURE_CODE_BY_STAGE: dict[ActionRuntimeFailureStage, str] = {
    "prepare": "ACTION_RUNTIME_PREPARE_FAILED",
    "build": "ACTION_GRAPH_BUILD_FAILED",
    "execute": "ACTION_GRAPH_EXECUTION_FAILED",
}
# `details.reason` values the proxy reports when the request could not carry
# its images. Without this mapping the user only sees the generic
# ACTION_PROCESSING_ERROR and cannot tell that the attachments were the cause.
_IMAGE_INPUT_FAILURE_CODE_BY_PROXY_REASON: dict[str, str] = {
    "active_media_too_large": ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE,
    "media_too_large": ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE,
    "media_references_too_large": ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE,
    "invalid_image": ACTION_FAILURE_CODE_IMAGE_INPUT_INVALID,
}
# A refused route says which credential the user must repair, and only the
# raiser knows: an API key is re-entered, a lapsed sign-in is signed in again
# (design 6.4). `build_llm_proxy_agent_error` folds both into
# ACTION_LLM_RESPONSE_ERROR and keeps the distinction in `error_details`, so the
# public terminal reads it back from there. Without this the user is told the
# Action failed and nothing about what to fix.
_CONNECTION_FAILURE_CODE_BY_PROXY_DETAILS: dict[tuple[str, str], str] = {
    (
        PROXY_CONNECTION_NOT_CONFIGURED,
        "configure_connection",
    ): ACTION_FAILURE_CODE_CONNECTION_NOT_CONFIGURED,
    (
        PROXY_AUTHENTICATION_FAILED,
        "configure_connection",
    ): ACTION_FAILURE_CODE_CONNECTION_REJECTED,
    (
        PROXY_AUTHENTICATION_FAILED,
        "reauthenticate",
    ): ACTION_FAILURE_CODE_SIGN_IN_EXPIRED,
}


def action_public_failure_code(error: AgentError) -> str | None:
    """Classify a terminal the user can act on: a refusal or a broken connection.

    A refusal is left to the user, who can change the model or start over. An
    authentication failure the raiser left generic (`abort`, or no suggested
    action at all) names no remedy, so it keeps the generic terminal.
    """

    details = error.error_details
    if details is None:
        return None
    proxy_error_code = details.get("proxy_error_code")
    if (
        proxy_error_code == PROXY_UPSTREAM_FORBIDDEN
        and details.get("response_status") == MODEL_REFUSAL_RESPONSE_STATUS
    ):
        return ACTION_FAILURE_CODE_LLM_REFUSED
    suggested_action = details.get("suggested_action")
    if not isinstance(proxy_error_code, str) or not isinstance(suggested_action, str):
        return None
    return _CONNECTION_FAILURE_CODE_BY_PROXY_DETAILS.get(
        (proxy_error_code, suggested_action)
    )


def action_image_input_failure_code(exc: BaseException) -> str | None:
    """Classify a proxy failure that the attached images caused."""

    if (
        not isinstance(exc, LlmProxyExecutionError)
        or exc.error_code != PROXY_INVALID_INPUT
        or exc.media_failure_reason is None
    ):
        return None
    return _IMAGE_INPUT_FAILURE_CODE_BY_PROXY_REASON.get(exc.media_failure_reason)


class ActionRuntimeApplicationFailure(RuntimeError):
    """A non-retryable runtime failure ready for the canonical terminal writer."""

    def __init__(
        self,
        *,
        stage: ActionRuntimeFailureStage,
        error: AgentError,
        execution_session_id: str | None,
    ) -> None:
        self.stage = stage
        self.error = error
        self.execution_session_id = execution_session_id
        super().__init__(f"Action runtime failed during {stage}")


def build_action_runtime_failure_error(
    *,
    request: ActionAgentRequest,
    stage: ActionRuntimeFailureStage,
    exc: BaseException,
    build_agent_error: AgentErrorBuilder,
    error_code_prefix: str,
) -> AgentError:
    if isinstance(exc, CounterInvariantError):
        return build_agent_error(
            error_type="internal_error",
            error_code="ACTION_COUNTER_INVARIANT_VIOLATION",
            error_message="Action processing encountered an internal error.",
            error_details={
                "reason": "counter_invariant_violation",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
            metadata={
                "action_id": str(request.action_id),
                "suggestion_id": request.suggestion_id,
                "user_id": str(request.user_id),
            },
        )

    if isinstance(exc, LlmProxyExecutionError):
        image_input_failure_code = action_image_input_failure_code(exc)
        if image_input_failure_code is not None:
            return build_agent_error(
                error_type="validation_error",
                error_code=image_input_failure_code,
                error_message="Action image input was rejected by the model.",
                error_details={
                    "reason": exc.media_failure_reason,
                    "proxy_error_code": exc.error_code,
                },
                metadata={
                    "action_id": str(request.action_id),
                    "suggestion_id": request.suggestion_id,
                    "user_id": str(request.user_id),
                },
            )
        return build_llm_proxy_agent_error(
            exception=exc,
            error_code_prefix=error_code_prefix,
        )

    return build_agent_error(
        error_type="internal_error",
        error_code=_UNEXPECTED_FAILURE_CODE_BY_STAGE[stage],
        error_message="Action processing encountered an internal error.",
        error_details={
            "stage": stage,
            "exception_type": type(exc).__name__,
        },
        metadata={
            "action_id": str(request.action_id),
            "suggestion_id": request.suggestion_id,
            "user_id": str(request.user_id),
        },
    )


__all__ = [
    "ActionRuntimeApplicationFailure",
    "action_public_failure_code",
    "action_image_input_failure_code",
    "ActionRuntimeFailureStage",
    "build_action_runtime_failure_error",
]

"""ActionAgent の永続化/レスポンス生成ヘルパー。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import ValidationError

from pantaray_agents.action_status import (
    RESUME_FAILURE_CODES,
    build_resume_failure,
    derive_action_terminal_failure,
    is_action_terminal_status,
    normalize_action_runtime_status,
)
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.error_redaction import (
    redact_agent_error_for_client,
)
from pantaray_agents.agents.action_agent.runtime.log_safety import (
    safe_mapping_keys,
    summarize_validation_error,
)
from pantaray_agents.agents.action_agent.runtime.models.approval import (
    PendingApprovalRequestModel,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.support.severity import (
    FATAL_SEVERITIES,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.action import (
    ActionAgentResponse,
    ActionApprovalBlocker,
    ActionExecutionResult,
    ActionRunResult,
    build_action_agent_response,
)
from pantaray_agents.schema.agent.base import AgentError, JSONValue
from pantaray_agents.utils.public_error import PUBLIC_INTERNAL_ERROR_MESSAGE
from pantaray_agents.utils.strict_numbers import is_strict_int

_STATE_KEY_ACTION_ID = "action_id"
_STATE_KEY_CONTEXT = "context"
_STATE_KEY_ERRORS = "errors"
_STATE_KEY_FINAL_OUTPUT = "final_output"
_STATE_KEY_LLM_STEPS_TAKEN = "llm_steps_taken"
_STATE_KEY_MAX_STEPS = "max_steps"
_STATE_KEY_MAX_TOOL_STEPS = "max_tool_steps"
_STATE_KEY_STARTED_AT = "started_at"
_STATE_KEY_STATUS = "status"
_STATE_KEY_STEPS_TAKEN = "steps_taken"
_STATE_KEY_SUGGESTION_ID = "suggestion_id"
_STATE_KEY_TOKEN_BUDGET = "token_budget"
_STATE_KEY_TOOL_STEPS_TAKEN = "tool_steps_taken"
_STATE_KEY_TOTAL_COMPLETION_TOKENS = "total_completion_tokens"
_STATE_KEY_TOTAL_PROMPT_TOKENS = "total_prompt_tokens"
_STATE_KEY_UPDATED_AT = "updated_at"
_STATE_KEY_USER_ID = "user_id"
_STATE_KEY_CURRENT_APPROVAL_BLOCKERS = "current_approval_blockers"
_STATE_KEY_PENDING_APPROVAL_REQUEST = "pending_approval_request"
_CONTEXT_KEY_LAST_SUPERVISOR_PROMPT = "last_supervisor_prompt"
_CONTEXT_KEY_PROMPT_NAME = "prompt_name"
_CONTEXT_KEY_PROMPT_VERSION = "prompt_version"
_ERROR_SUMMARY_KEY_EXCEPTION_TYPE = "exception_type"
_ERROR_SUMMARY_KEY_ERROR_COUNT = "error_count"
_ERROR_SUMMARY_KEY_MISSING_KEYS = "missing_keys"
_ERROR_SUMMARY_KEY_INVALID_KEYS = "invalid_keys"
_ERROR_DETAILS_KEY_RAW_ERROR_KEYS = "raw_error_keys"
_STEP_KEY_SEVERITY = "severity"
_STATUS_ERROR = "error"
_STATUS_PROCESSING = "processing"
_ERROR_TYPE_INTERNAL = "internal_error"
_ERROR_CODE_ACTION_AGENT_ERROR_PAYLOAD_INVALID = "ACTION_AGENT_ERROR_PAYLOAD_INVALID"
_REPOSITORY_RESULT_ATTR_DATA = "data"

type AgentErrorPayload = dict[str, JSONValue]
type FinalLlmInputPayload = dict[str, JSONValue]


class ActionAgentPersistence:
    """ActionAgent のステート永続化とレスポンス生成責務を受け持つクラス。"""

    def __init__(
        self,
        *,
        default_prompt_name: str,
        default_prompt_version: str,
        logger: logging.Logger | None = None,
        now_provider: Callable[[], str] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._now_provider = now_provider or now_utc_iso
        self._default_prompt_name = default_prompt_name
        self._default_prompt_version = default_prompt_version

    def _state_to_error_object(
        self,
        state: ActionAgentState,
        *,
        redact_for_client: bool,
    ) -> AgentError | None:
        error_obj: AgentError | None = None
        if state.get(_STATE_KEY_STATUS) == _STATUS_ERROR and state.get(
            _STATE_KEY_ERRORS
        ):
            raw_errors = state[_STATE_KEY_ERRORS]
            raw_error = next(
                (
                    item
                    for item in raw_errors
                    if isinstance(item, dict)
                    and str(item.get(_STEP_KEY_SEVERITY, "")).lower()
                    in FATAL_SEVERITIES
                ),
                raw_errors[0],
            )
            try:
                error_obj = AgentError.model_validate(raw_error)
            except ValidationError as exc:
                error_summary = summarize_validation_error(exc)
                raw_error_keys = safe_mapping_keys(raw_error)
                self._logger.warning(
                    "state.errors[0] is not a valid AgentError "
                    "(action_id=%s exception_type=%s error_count=%s "
                    "raw_error_keys=%s missing_keys=%s invalid_keys=%s)",
                    state.get(_STATE_KEY_ACTION_ID),
                    error_summary[_ERROR_SUMMARY_KEY_EXCEPTION_TYPE],
                    error_summary[_ERROR_SUMMARY_KEY_ERROR_COUNT],
                    raw_error_keys,
                    error_summary[_ERROR_SUMMARY_KEY_MISSING_KEYS],
                    error_summary[_ERROR_SUMMARY_KEY_INVALID_KEYS],
                )
                error_obj = AgentError(
                    error_type=_ERROR_TYPE_INTERNAL,
                    error_code=_ERROR_CODE_ACTION_AGENT_ERROR_PAYLOAD_INVALID,
                    error_message=PUBLIC_INTERNAL_ERROR_MESSAGE,
                    error_details={
                        _ERROR_SUMMARY_KEY_EXCEPTION_TYPE: error_summary[
                            _ERROR_SUMMARY_KEY_EXCEPTION_TYPE
                        ],
                        _ERROR_SUMMARY_KEY_ERROR_COUNT: error_summary[
                            _ERROR_SUMMARY_KEY_ERROR_COUNT
                        ],
                        _ERROR_DETAILS_KEY_RAW_ERROR_KEYS: raw_error_keys,
                        _ERROR_SUMMARY_KEY_MISSING_KEYS: error_summary[
                            _ERROR_SUMMARY_KEY_MISSING_KEYS
                        ],
                        _ERROR_SUMMARY_KEY_INVALID_KEYS: error_summary[
                            _ERROR_SUMMARY_KEY_INVALID_KEYS
                        ],
                    },
                    severity=_STATUS_ERROR,
                    metadata={
                        _STATE_KEY_ACTION_ID: str(
                            state.get(_STATE_KEY_ACTION_ID) or ""
                        ),
                        _STATE_KEY_SUGGESTION_ID: str(
                            state.get(_STATE_KEY_SUGGESTION_ID) or ""
                        ),
                        _STATE_KEY_USER_ID: str(state.get(_STATE_KEY_USER_ID) or ""),
                    },
                )
        if redact_for_client and error_obj is not None:
            error_obj = redact_agent_error_for_client(error_obj)
        return error_obj

    def state_to_response(
        self,
        state: ActionAgentState,
        *,
        redact_for_client: bool = True,
    ) -> ActionAgentResponse:
        """ActionAgentState から ActionAgentResponse を生成する。

        注意:
            `ActionAgentResponse.created_at` は DB の `agent_actions.created_at` と同義で扱うため、
            実行のたびに変動する `updated_at` ではなく、原則として `started_at` を採用する。
            `redact_for_client=True` の場合のみ `error_details` / `metadata` を除去して返す。
        """

        error_obj = self._state_to_error_object(
            state, redact_for_client=redact_for_client
        )

        final_output = state.get(_STATE_KEY_FINAL_OUTPUT) or ""
        created_at = (
            state.get(_STATE_KEY_STARTED_AT)
            or state.get(_STATE_KEY_UPDATED_AT)
            or self._now_provider()
        )

        return build_action_agent_response(
            action_id=state[_STATE_KEY_ACTION_ID],
            suggestion_id=state[_STATE_KEY_SUGGESTION_ID],
            user_id=state[_STATE_KEY_USER_ID],
            final_output=final_output,
            status=normalize_action_runtime_status(
                state.get(_STATE_KEY_STATUS, _STATUS_PROCESSING)
            ),
            created_at=created_at,
            error=error_obj,
        )

    def state_to_run_result(self, state: ActionAgentState) -> ActionRunResult:
        """state から worker 内部用の terminal result を組み立てる。"""

        response = self.state_to_response(state, redact_for_client=False)
        error_obj = response.error
        context = state.get(_STATE_KEY_CONTEXT, {})
        prompt_name = (
            str(context.get(_CONTEXT_KEY_PROMPT_NAME)).strip()
            if isinstance(context, dict)
            and isinstance(context.get(_CONTEXT_KEY_PROMPT_NAME), str)
            and str(context.get(_CONTEXT_KEY_PROMPT_NAME)).strip()
            else self._default_prompt_name
        )
        prompt_version = (
            str(context.get(_CONTEXT_KEY_PROMPT_VERSION)).strip()
            if isinstance(context, dict)
            and isinstance(context.get(_CONTEXT_KEY_PROMPT_VERSION), str)
            and str(context.get(_CONTEXT_KEY_PROMPT_VERSION)).strip()
            else self._default_prompt_version
        )

        def _optional_int(key: str) -> int | None:
            value = state.get(key)
            return value if is_strict_int(value) else None

        completed_at = (
            state.get(_STATE_KEY_UPDATED_AT)
            or state.get(_STATE_KEY_STARTED_AT)
            or self._now_provider()
        )
        action_failure_code: str | None = None
        failure_stage: str | None = None
        failure_message_public: str | None = None
        normalized_status = str(response.status).strip().lower()
        if is_action_terminal_status(normalized_status):
            failure = derive_action_terminal_failure(normalized_status)
            if (
                error_obj is not None
                and error_obj.error_type == "resume_error"
                and error_obj.error_code in RESUME_FAILURE_CODES
            ):
                failure = build_resume_failure(failure_code=error_obj.error_code)
            if failure is not None:
                action_failure_code = failure.failure_code
                failure_stage = failure.failure_stage
                failure_message_public = failure.failure_message_public

        approval_blockers = self._state_to_approval_blockers(state)

        return ActionRunResult(
            action_id=response.action_id,
            suggestion_id=response.suggestion_id,
            user_id=response.user_id,
            completed_at=str(completed_at),
            status=str(response.status),
            final_output=str(response.final_output or ""),
            memory_draft=state.get("supervisor_memory_draft"),
            action_failure_code=(
                error_obj.error_code if error_obj is not None else action_failure_code
            ),
            failure_stage=failure_stage,
            failure_message_public=failure_message_public,
            final_prompt_text=self.build_final_prompt_text(state),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            total_steps=_optional_int(_STATE_KEY_STEPS_TAKEN),
            total_llm_steps=_optional_int(_STATE_KEY_LLM_STEPS_TAKEN),
            total_tool_steps=_optional_int(_STATE_KEY_TOOL_STEPS_TAKEN),
            total_prompt_tokens=_optional_int(_STATE_KEY_TOTAL_PROMPT_TOKENS),
            total_completion_tokens=_optional_int(_STATE_KEY_TOTAL_COMPLETION_TOKENS),
            approval_blockers=approval_blockers,
        )

    @staticmethod
    def _state_to_approval_blockers(
        state: ActionAgentState,
    ) -> list[ActionApprovalBlocker]:
        raw_blockers = state.get(_STATE_KEY_CURRENT_APPROVAL_BLOCKERS)
        if not raw_blockers:
            pending_request = state.get(_STATE_KEY_PENDING_APPROVAL_REQUEST)
            raw_blockers = [pending_request] if pending_request is not None else []
        action_id = str(state.get(_STATE_KEY_ACTION_ID) or "").strip()
        blockers: list[ActionApprovalBlocker] = []
        for raw_blocker in raw_blockers:
            if raw_blocker is None:
                continue
            parsed_blocker = PendingApprovalRequestModel.model_validate(raw_blocker)
            tool_call = parsed_blocker.tool_call
            blockers.append(
                ActionApprovalBlocker(
                    action_id=action_id,
                    approval_session_id=parsed_blocker.approval_session_id,
                    tool_request_id=parsed_blocker.tool_request_id,
                    tool_id=tool_call.tool_id,
                    intent_class=parsed_blocker.intent_class,
                    command_summary=dict(parsed_blocker.command_summary),
                )
            )
        return blockers

    def state_to_execution_result(
        self, state: ActionAgentState
    ) -> ActionExecutionResult:
        """worker 実行系へ返す内部結果を組み立てる。"""

        run_result = self.state_to_run_result(state)
        error_obj = self._state_to_error_object(state, redact_for_client=False)
        return ActionExecutionResult(
            run_result=run_result,
            execution_session_id=(
                str(state.get("execution_session_id") or "").strip() or None
            ),
            runtime_state_checkpoint=(
                build_runtime_state_checkpoint(state)
                if run_result.status in {"success", "error"}
                and (error_obj is None or error_obj.error_type != "resume_error")
                else None
            ),
        )

    def build_final_prompt_text(self, state: ActionAgentState) -> str | None:
        """最終的な Action 全体入力として保存するプロンプト本文を返す。"""

        context = state.get(_STATE_KEY_CONTEXT, {})
        prompt = context.get(_CONTEXT_KEY_LAST_SUPERVISOR_PROMPT)
        if isinstance(prompt, str) and prompt.strip():
            return prompt
        return None

    def build_agent_error(
        self,
        *,
        error_type: str,
        error_code: str,
        error_message: str,
        severity: str = _STATUS_ERROR,
        error_details: AgentErrorPayload | None = None,
        metadata: AgentErrorPayload | None = None,
    ) -> AgentError:
        """AgentError インスタンスを生成する。"""

        return AgentError(
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            error_details=error_details,
            severity=severity,
            metadata=metadata,
        )

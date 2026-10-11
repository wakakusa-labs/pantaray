from __future__ import annotations

import logging
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.action_status import (
    ACTION_FAILURE_CODE_ORCHESTRATION_MODE_RETIRED,
    ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
    ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
    ACTION_FAILURE_CODE_RESUME_NOT_ALLOWED,
)
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    restore_runtime_state_checkpoint,
    validate_runtime_checkpoint_payload,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.assistant_message import (
    project_persisted_assistant_messages,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    has_persisted_user_request_step,
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.models import (
    ResumeFailureCode,
    ResumeFailureException,
    ResumeFailureModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    ActionAgentStateConfig,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.user_image_attachments import (
    build_user_image_attachments,
)
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession
from pantaray_agents.repositories import action_runtime_resume_contract as resume
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.schema.repository_errors import repository_data_or_raise

from .approval_resume_restoration import (
    normalize_approval_resume_state,
    select_approval_resume_target,
)
from .new_user_turn import reset_state_for_new_user_turn
from .resume_errors import ResumeStateError
from .resume_routing import (
    ResumeRouteName,
    graph_route_from_execution_think_state,
    graph_route_from_resume_state,
)
from .retired_orchestration import is_retired_orchestration_checkpoint

_RUNTIME_CHECKPOINT_NOT_FOUND_ERROR = "No data found"
_RUNTIME_CHECKPOINT_ROW_SHAPE_ERROR = "Runtime checkpoint row must be an object."
_RUNTIME_CHECKPOINT_PAYLOAD_MISSING_ERROR = "Runtime checkpoint payload is missing."
_RESTORED_OPTIONAL_STATE_FIELDS: tuple[tuple[str, object], ...] = (
    ("next_action", None),
    ("final_output", None),
    ("superseded_reason", None),
    ("superseded_at", None),
)


@dataclass(frozen=True)
class ResumeDeps:
    logger: logging.Logger
    load_approval_session_by_request: Callable[[str, str], StoredApprovalSession | None]
    build_agent_error: Callable[..., AgentError]
    now_provider: Callable[[], str]


class ActionResumeService:
    def __init__(self, deps: ResumeDeps) -> None:
        self._deps = deps

    def resolve_initial_state(
        self,
        *,
        request: ActionAgentRequest,
        started_at: str,
        state_config: ActionAgentStateConfig,
        token_budget: int | None,
        checkpoint_row: RepositoryResult[object],
        intervening_user_step: resume.ActionResumeUserStep | None = None,
    ) -> ActionAgentState:
        try:
            checkpoint_state = self.load_checkpoint_state_or_none(
                checkpoint_row=checkpoint_row,
                request=request,
            )
            if checkpoint_state is not None:
                is_new_user_turn = self._is_new_user_turn(
                    checkpoint_state=checkpoint_state,
                    request=request,
                )
                if is_new_user_turn:
                    next_user_step_number = (
                        intervening_user_step.step_number
                        if intervening_user_step is not None
                        else request.user_step_number
                    )
                    project_persisted_assistant_messages(
                        checkpoint_state,
                        request.preceding_assistant_messages,
                        before_step_number=next_user_step_number,
                    )
                    checkpoint_state = self._open_new_user_turn(
                        checkpoint_state=checkpoint_state,
                        expected_step_number=next_user_step_number,
                        started_at=(
                            intervening_user_step.created_at
                            if intervening_user_step is not None
                            else request.user_step_created_at
                        ),
                    )
                    checkpoint_state = self._project_intervening_user_step(
                        checkpoint_state,
                        intervening_user_step,
                        user_id=str(request.user_id),
                    )
                elif is_retired_orchestration_checkpoint(checkpoint_state):
                    raise ResumeStateError(
                        failure_code=ACTION_FAILURE_CODE_ORCHESTRATION_MODE_RETIRED,
                        message="The persisted Action orchestration mode is retired.",
                    )
                raw_anchor = checkpoint_row.metadata.get("approval_anchor_step_id")
                anchor_step_id = raw_anchor if isinstance(raw_anchor, str) else None
                # The approval applies only to the checkpoint that still names
                # it; without an anchor the resumed run already consumed it.
                approval_resume_advanced = self.is_approval_resume_request(
                    request
                ) and anchor_step_id != cast(
                    dict[str, object], checkpoint_row.data
                ).get("step_id")
                restored_state = self.apply_latest_runtime_state_config(
                    checkpoint_state=checkpoint_state,
                    state_config=state_config,
                    request=None if approval_resume_advanced else request,
                    paused_tool_step_id=(
                        None if approval_resume_advanced else anchor_step_id
                    ),
                )
                self.graph_route_from_resume(restored_state)
                return restored_state
            if self.is_approval_resume_request(request):
                raise ResumeStateError(
                    failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
                    message=(
                        "approval resume checkpoint not found for "
                        f"approval_session_id={request.approval_resume_session_id!r} "
                        "and "
                        f"tool_request_id={request.approval_resume_tool_request_id!r}"
                    ),
                )
        except ResumeStateError as exc:
            self._deps.logger.error(
                "Action resume failed: action_id=%s suggestion_id=%s user_id=%s failure_code=%s message=%s",
                request.action_id,
                request.suggestion_id,
                request.user_id,
                exc.failure_code,
                str(exc),
            )
            return self.build_resume_failure_state(
                request=request,
                started_at=started_at,
                state_config=state_config,
                token_budget=token_budget,
                failure_code=exc.failure_code,
                error_message=str(exc),
            )
        state = create_initial_state(
            user_id=request.user_id,
            suggestion_id=request.suggestion_id,
            action_id=request.action_id,
            started_at=started_at,
            max_steps=state_config["max_steps"],
            max_tool_steps=state_config["max_tool_steps"],
            token_budget=token_budget,
        )
        if intervening_user_step is not None:
            project_persisted_assistant_messages(
                state,
                request.preceding_assistant_messages,
                before_step_number=intervening_user_step.step_number,
            )
        state = self._project_intervening_user_step(
            state,
            intervening_user_step,
            user_id=str(request.user_id),
        )
        return self.apply_latest_runtime_state_config(
            checkpoint_state=state,
            state_config=state_config,
        )

    @staticmethod
    def is_approval_resume_request(request: ActionAgentRequest) -> bool:
        return (
            request.approval_resume_session_id is not None
            or request.approval_resume_tool_request_id is not None
        )

    @classmethod
    def _is_new_user_turn(
        cls,
        *,
        checkpoint_state: ActionAgentState,
        request: ActionAgentRequest,
    ) -> bool:
        if cls.is_approval_resume_request(request):
            return False
        return not has_persisted_user_request_step(
            checkpoint_state,
            step_id=request.user_step_id,
        )

    @staticmethod
    def _open_new_user_turn(
        *,
        checkpoint_state: ActionAgentState,
        expected_step_number: int,
        started_at: str,
    ) -> ActionAgentState:
        if not any(checkpoint_state.get("history_by_scope", {}).values()):
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_NOT_ALLOWED,
                message="A new USER turn requires prior persisted Action history.",
            )
        if checkpoint_state.get("step") != expected_step_number:
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message="USER step number does not follow the runtime checkpoint.",
            )
        legacy_temp_dir = Path(str(checkpoint_state.get("action_temp_dir") or ""))
        if legacy_temp_dir.is_absolute() and legacy_temp_dir.name == ".runtime-temp":
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message="A legacy Action-wide temp path cannot open a new USER turn.",
            )
        return reset_state_for_new_user_turn(checkpoint_state, started_at=started_at)

    @staticmethod
    def _project_intervening_user_step(
        state: ActionAgentState,
        user_step: resume.ActionResumeUserStep | None,
        *,
        user_id: str,
    ) -> ActionAgentState:
        if user_step is None:
            return state
        return project_persisted_user_request_step(
            state,
            step_id=user_step.step_id,
            step_number=user_step.step_number,
            local_step_number=user_step.local_step_number,
            short_step_id=user_step.short_step_id,
            request_text=render_action_user_request_text(user_step.message),
            occurred_at=user_step.created_at,
            history_phase="init",
            attachments=build_user_image_attachments(
                user_id=user_id,
                images=user_step.message.images,
            ),
        )

    def build_resume_failure_state(
        self,
        *,
        request: ActionAgentRequest,
        started_at: str,
        state_config: ActionAgentStateConfig,
        token_budget: int | None,
        failure_code: ResumeFailureCode,
        error_message: str,
    ) -> ActionAgentState:
        state = create_initial_state(
            user_id=request.user_id,
            suggestion_id=request.suggestion_id,
            action_id=request.action_id,
            started_at=started_at,
            max_steps=state_config["max_steps"],
            max_tool_steps=state_config["max_tool_steps"],
            token_budget=token_budget,
        )
        self.apply_latest_runtime_state_config(
            checkpoint_state=state,
            state_config=state_config,
        )
        failure = ResumeFailureModel.from_failure_code(failure_code)
        state["phase"] = "finalizing"
        state["final_output"] = ""
        append_state_error(
            state,
            error=self._deps.build_agent_error(
                error_type="resume_error",
                error_code=failure.failure_code,
                error_message=error_message,
                error_details={"failure_stage": failure.failure_stage},
            ),
            updated_at=started_at,
        )
        set_status_with_updated_at(state, status="error", updated_at=started_at)
        return state

    def apply_latest_runtime_state_config(
        self,
        *,
        checkpoint_state: ActionAgentState,
        state_config: ActionAgentStateConfig,
        request: ActionAgentRequest | None = None,
        paused_tool_step_id: str | None = None,
    ) -> ActionAgentState:
        mutable_state = cast(MutableMapping[str, object], checkpoint_state)
        for field_name, default_value in _RESTORED_OPTIONAL_STATE_FIELDS:
            mutable_state.setdefault(field_name, default_value)
        select_approval_resume_target(
            checkpoint_state,
            approval_session_id=(
                request.approval_resume_session_id if request is not None else None
            ),
            tool_request_id=(
                request.approval_resume_tool_request_id if request is not None else None
            ),
        )
        checkpoint_state["max_steps"] = state_config["max_steps"]
        checkpoint_state["max_tool_steps"] = state_config["max_tool_steps"]
        checkpoint_state["token_budget"] = state_config["token_budget"]
        checkpoint_state["cancel_check_max_consecutive_failures"] = state_config[
            "cancel_check_max_consecutive_failures"
        ]
        checkpoint_state["cancel_check_failure_grace_seconds"] = state_config[
            "cancel_check_failure_grace_seconds"
        ]
        return normalize_approval_resume_state(
            checkpoint_state,
            load_approval_session_by_request=(
                self._deps.load_approval_session_by_request
            ),
            now_provider=self._deps.now_provider,
            paused_tool_step_id=paused_tool_step_id,
        )

    def load_checkpoint_state_or_none(
        self,
        *,
        checkpoint_row: RepositoryResult[object],
        request: ActionAgentRequest,
    ) -> ActionAgentState | None:
        checkpoint_error = checkpoint_row.error
        if checkpoint_error == _RUNTIME_CHECKPOINT_NOT_FOUND_ERROR:
            return None
        checkpoint_data = repository_data_or_raise(
            checkpoint_row, safe_message=str(checkpoint_error)
        )
        if checkpoint_data is None:
            return None
        if not isinstance(checkpoint_data, dict):
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message=_RUNTIME_CHECKPOINT_ROW_SHAPE_ERROR,
            )

        checkpoint_version = checkpoint_data.get("runtime_state_checkpoint_version")
        if checkpoint_version != RUNTIME_STATE_CHECKPOINT_VERSION:
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message=(
                    "Unsupported action runtime checkpoint version: "
                    f"{checkpoint_version!r}"
                ),
            )
        checkpoint_payload = checkpoint_data.get("runtime_state_checkpoint")
        if not isinstance(checkpoint_payload, dict):
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message=_RUNTIME_CHECKPOINT_PAYLOAD_MISSING_ERROR,
            )

        try:
            validate_runtime_checkpoint_payload(checkpoint_payload)
            return restore_runtime_state_checkpoint(
                checkpoint_payload,
                expected_action_id=str(request.action_id),
                expected_suggestion_id=request.suggestion_id,
                expected_user_id=str(request.user_id),
            )
        except ResumeFailureException as exc:
            raise ResumeStateError(
                failure_code=exc.failure_code,
                message=str(exc),
            ) from exc
        except ValueError as exc:
            raise ResumeStateError(
                failure_code=ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
                message=str(exc),
            ) from exc

    @staticmethod
    def graph_route_from_execution_think(
        state: ActionAgentState,
    ) -> ResumeRouteName:
        return cast(ResumeRouteName, graph_route_from_execution_think_state(state))

    @staticmethod
    def graph_route_from_resume(state: ActionAgentState) -> ResumeRouteName:
        return graph_route_from_resume_state(state)

"""ActionAgent runtime checkpoint の構築・復元ヘルパー。"""

from __future__ import annotations

import copy
from typing import cast

from pydantic import ValidationError

from pantaray_agents.schema.agent.base import JSONValue

from .models.checkpoint import HistoryEntryModel, RuntimeCheckpointModel
from .models.execution_context import (
    execution_context_from_state,
    execution_context_state_patch,
)
from .models.failure import ResumeFailureCode, ResumeFailureException
from .state import ActionAgentState
from .state.context import (
    get_context_view,
)

RUNTIME_STATE_CHECKPOINT_VERSION = 4

_REQUIRED_CHECKPOINT_STATE_FIELDS = (
    "action_id",
    "suggestion_id",
    "user_id",
    "phase",
    "step",
    "steps_taken",
    "llm_steps_taken",
    "tool_steps_taken",
    "max_steps",
    "max_tool_steps",
    "status",
    "started_at",
    "updated_at",
    "tokens_used",
    "total_prompt_tokens",
    "total_completion_tokens",
    "chunks_sent",
    "history_by_scope",
    "goal_conversations",
    "next_action",
    "final_output",
    "errors",
    "run_authority",
    "skip_persist",
    "superseded_reason",
    "superseded_at",
)


def build_runtime_state_checkpoint(
    state: ActionAgentState,
) -> dict[str, JSONValue]:
    """現在の runtime state を JSONB 保存可能な checkpoint に変換する。"""

    validated = _build_runtime_checkpoint_model(state)
    checkpoint = validated.model_dump(mode="json")
    checkpoint["history_by_scope"] = _dump_history_by_scope(validated)
    _strip_optional_runtime_fields(checkpoint)
    return cast("dict[str, JSONValue]", checkpoint)


def restore_runtime_state_checkpoint(
    checkpoint: dict[str, JSONValue],
    *,
    expected_action_id: str,
    expected_suggestion_id: str | None,
    expected_user_id: str,
) -> ActionAgentState:
    """保存済み checkpoint から runtime state を復元する。"""

    if not isinstance(checkpoint, dict):
        raise ResumeFailureException(
            failure_code="ACTION_RESUME_CHECKPOINT_INVALID",
            message="runtime_state_checkpoint must be an object.",
        )

    restored = validate_runtime_checkpoint_payload(copy.deepcopy(checkpoint))
    restored_state = _restore_runtime_state_from_model(restored)
    _require_matching_id(
        restored_state,
        field_name="action_id",
        expected_value=expected_action_id,
    )
    _require_matching_optional_id(
        restored_state,
        field_name="suggestion_id",
        expected_value=expected_suggestion_id,
    )
    _require_matching_id(
        restored_state,
        field_name="user_id",
        expected_value=expected_user_id,
    )
    return restored_state


def validate_runtime_checkpoint_payload(
    checkpoint_payload: dict[str, JSONValue],
) -> RuntimeCheckpointModel:
    # Checkpoints saved before the Action stopped carrying its memory artifact
    # path list still hold that key. It is dropped from this read copy, never from
    # the stored row, so those Actions can still take a follow-up message.
    payload = {
        key: value
        for key, value in checkpoint_payload.items()
        if key != "memory_artifact_references"
    }
    try:
        return RuntimeCheckpointModel.model_validate(payload)
    except ResumeFailureException:
        raise
    except ValidationError as exc:
        raise ResumeFailureException(
            failure_code=_classify_checkpoint_validation_error(exc),
            message=str(exc),
        ) from exc


def _restore_runtime_state_from_model(
    checkpoint_model: RuntimeCheckpointModel,
) -> ActionAgentState:
    restored_payload = checkpoint_model.model_dump(mode="json")
    restored_payload["history_by_scope"] = _dump_history_by_scope(checkpoint_model)
    _strip_optional_runtime_fields(restored_payload)
    restored_state = cast(ActionAgentState, restored_payload)
    if checkpoint_model.next_action is not None:
        restored_state["next_action"] = checkpoint_model.next_action.model_copy(
            deep=True
        )
    if checkpoint_model.pending_approval_request is not None:
        restored_state["pending_approval_request"] = (
            checkpoint_model.pending_approval_request.model_copy(deep=True)
        )
    if checkpoint_model.current_approval_blockers:
        restored_state["current_approval_blockers"] = [
            blocker.model_copy(deep=True)
            for blocker in checkpoint_model.current_approval_blockers
        ]
    if checkpoint_model.supervisor_memory_draft is not None:
        restored_state["supervisor_memory_draft"] = (
            checkpoint_model.supervisor_memory_draft.model_copy(deep=True)
        )
    if checkpoint_model.memory_context_epoch is not None:
        restored_state["memory_context_epoch"] = (
            checkpoint_model.memory_context_epoch.model_copy(deep=True)
        )
    restored_state["goal_conversations"] = {
        goal_id: conversation.model_copy(deep=True)
        for goal_id, conversation in checkpoint_model.goal_conversations.items()
    }
    return restored_state


def _dump_history_by_scope(
    checkpoint_model: RuntimeCheckpointModel,
) -> dict[str, list[dict[str, JSONValue]]]:
    """optional payload の未設定状態を保ったまま履歴を JSON 化する。"""

    return {
        scope_handle: [_dump_history_entry(entry) for entry in entries]
        for scope_handle, entries in checkpoint_model.history_by_scope.items()
    }


def _dump_history_entry(entry: HistoryEntryModel) -> dict[str, JSONValue]:
    payload = cast(
        "dict[str, JSONValue]",
        entry.model_dump(mode="json", exclude_none=True),
    )
    # tool_id は必須フィールドで、LLM THINK では None が canonical value。
    payload["tool_id"] = entry.tool_id
    # JSON null は有効な tool result。未設定と区別して履歴へ残す。
    if "output" in entry.model_fields_set:
        payload["output"] = entry.output
    return payload


def _build_runtime_checkpoint_model(state: ActionAgentState) -> RuntimeCheckpointModel:
    context = copy.deepcopy(dict(get_context_view(state)))
    try:
        execution_context = execution_context_from_state(state)
    except ValidationError as exc:
        raise ValueError(
            "ActionAgent state invariant violated: invalid execution context state: "
            f"{exc}"
        ) from exc
    checkpoint_payload: dict[str, object] = {
        "action_id": _require_state_field(state, "action_id"),
        "suggestion_id": _require_state_field(state, "suggestion_id"),
        "user_id": _require_state_field(state, "user_id"),
        "phase": _require_state_field(state, "phase"),
        "step": _require_state_field(state, "step"),
        "steps_taken": _require_state_field(state, "steps_taken"),
        "llm_steps_taken": _require_state_field(state, "llm_steps_taken"),
        "tool_steps_taken": _require_state_field(state, "tool_steps_taken"),
        "max_steps": _require_state_field(state, "max_steps"),
        "max_tool_steps": _require_state_field(state, "max_tool_steps"),
        "status": _require_state_field(state, "status"),
        "started_at": _require_state_field(state, "started_at"),
        "updated_at": _require_state_field(state, "updated_at"),
        "token_budget": copy.deepcopy(state.get("token_budget")),
        "tokens_used": _require_state_field(state, "tokens_used"),
        "total_prompt_tokens": _require_state_field(state, "total_prompt_tokens"),
        "total_completion_tokens": _require_state_field(
            state, "total_completion_tokens"
        ),
        "chunks_sent": _require_state_field(state, "chunks_sent"),
        "context": cast(dict[str, JSONValue], context),
        "history_by_scope": _require_state_field(state, "history_by_scope"),
        "goal_conversations": _require_state_field(state, "goal_conversations"),
        "next_action": _require_state_field(state, "next_action"),
        "final_output": _require_state_field(state, "final_output"),
        "supervisor_pending_final_answer": copy.deepcopy(
            state.get("supervisor_pending_final_answer")
        ),
        "supervisor_memory_draft": copy.deepcopy(state.get("supervisor_memory_draft")),
        "memory_context_epoch": copy.deepcopy(state.get("memory_context_epoch")),
        "errors": _require_state_field(state, "errors"),
        "run_authority": _require_state_field(state, "run_authority"),
        "skip_persist": _require_state_field(state, "skip_persist"),
        "superseded_reason": _require_state_field(state, "superseded_reason"),
        "superseded_at": _require_state_field(state, "superseded_at"),
        "pending_approval_request": copy.deepcopy(
            state.get("pending_approval_request")
        ),
        "current_approval_blockers": copy.deepcopy(
            state.get("current_approval_blockers") or []
        ),
        "manifest_id": None,
        "execution_session_id": None,
        "execution_network_policy": None,
        "action_temp_dir": None,
        "app_runtime_python": None,
        "read_access_scope": None,
    }
    if execution_context is not None:
        checkpoint_payload.update(execution_context_state_patch(execution_context))
    try:
        return RuntimeCheckpointModel.model_validate(checkpoint_payload)
    except ResumeFailureException:
        raise
    except ValidationError as exc:
        raise ValueError(
            "ActionAgent state invariant violated: "
            f"invalid runtime checkpoint state: {exc}"
        ) from exc


def _require_state_field(state: ActionAgentState, field_name: str) -> object:
    if field_name not in state:
        raise ValueError(
            "ActionAgent state invariant violated: "
            f"{field_name} is required for runtime checkpoint."
        )
    return copy.deepcopy(dict(state)[field_name])


def _require_matching_id(
    state: ActionAgentState,
    *,
    field_name: str,
    expected_value: str,
) -> None:
    raw_value = state.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ResumeFailureException(
            failure_code="ACTION_RESUME_CHECKPOINT_INVALID",
            message=(
                f"runtime_state_checkpoint.{field_name} must be a non-empty string."
            ),
        )
    if raw_value != expected_value:
        raise ResumeFailureException(
            failure_code="ACTION_RESUME_CHECKPOINT_INVALID",
            message=(
                f"runtime_state_checkpoint.{field_name} mismatch: "
                f"expected={expected_value!r} actual={raw_value!r}"
            ),
        )


def _require_matching_optional_id(
    state: ActionAgentState,
    *,
    field_name: str,
    expected_value: str | None,
) -> None:
    raw_value = state.get(field_name)
    if raw_value is None and expected_value is None:
        return
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ResumeFailureException(
            failure_code="ACTION_RESUME_CHECKPOINT_INVALID",
            message=(
                f"runtime_state_checkpoint.{field_name} must match the "
                "optional Action provenance."
            ),
        )
    if raw_value != expected_value:
        raise ResumeFailureException(
            failure_code="ACTION_RESUME_CHECKPOINT_INVALID",
            message=(
                f"runtime_state_checkpoint.{field_name} mismatch: "
                f"expected={expected_value!r} actual={raw_value!r}"
            ),
        )


def _classify_checkpoint_validation_error(exc: ValidationError) -> ResumeFailureCode:
    runtime_context_fields = {
        "manifest_id",
        "execution_session_id",
        "execution_network_policy",
        "action_temp_dir",
        "app_runtime_python",
        "read_access_scope",
    }
    # Retired Goal Worker checkpoints still carry these keys. The model forbids extra
    # fields, so they fail closed here; keep the mapping so the failure stays typed.
    goal_worker_fields = {
        "goal_worker_current_goal_id",
        "goal_worker_generation",
        "goal_worker_stop_reason",
        "goal_worker_pending_tool_call",
        "goal_worker_pending_tool_thinking",
        "goal_worker_pending_tool_thinking_summary",
    }
    for error in exc.errors():
        loc = tuple(str(part) for part in error.get("loc", ()))
        if not loc:
            continue
        head = loc[0]
        if head == "pending_approval_request":
            return "ACTION_RESUME_APPROVAL_STATE_INVALID"
        if head == "current_approval_blockers":
            return "ACTION_RESUME_APPROVAL_STATE_INVALID"
        if head in goal_worker_fields:
            return "ACTION_RESUME_GOAL_WORKER_STATE_INVALID"
        if head in runtime_context_fields:
            return "ACTION_RESUME_RUNTIME_CONTEXT_INVALID"
    return "ACTION_RESUME_CHECKPOINT_INVALID"


def _strip_optional_runtime_fields(payload: dict[str, JSONValue]) -> None:
    for field_name in (
        "next_action",
        "final_output",
        "supervisor_pending_final_answer",
        "supervisor_memory_draft",
        "memory_context_epoch",
        "token_budget",
        "superseded_reason",
        "superseded_at",
        "manifest_id",
        "execution_session_id",
        "execution_network_policy",
        "action_temp_dir",
        "app_runtime_python",
        "read_access_scope",
        "pending_approval_request",
        "current_approval_blockers",
    ):
        if payload.get(field_name) is None:
            payload.pop(field_name, None)

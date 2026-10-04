"""Action 実行状態と terminal failure detail/command の共通定義。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypedDict, TypeGuard

from pantaray_agents.suggestion_reactions import (
    SUGGESTION_USER_REACTION_ACCEPTED,
    SUGGESTION_USER_REACTION_REJECTED,
    SuggestionUserReaction,
    normalize_public_suggestion_reaction_for_write,
    parse_stored_suggestion_user_reaction,
    require_stored_suggestion_user_reaction,
)
from pantaray_agents.utils.strict_numbers import is_strict_int
from pantaray_agents.utils.timestamps import normalize_iso8601_utc_z_microseconds

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

ActionTerminalStatus = Literal["success", "error", "canceled"]
FailedActionTerminalStatus = Literal["error", "canceled"]
ActionRuntimeStatus = Literal["idle", "processing"] | ActionTerminalStatus
ActionUiPhase = Literal[
    "idle",
    "accepted_pending_start",
    "processing",
    "terminal",
]

ACTION_STATUS_SUCCESS: Final[Literal["success"]] = "success"
ACTION_STATUS_ERROR: Final[Literal["error"]] = "error"
ACTION_STATUS_CANCELED: Final[Literal["canceled"]] = "canceled"
ACTION_STATUS_IDLE: Final[Literal["idle"]] = "idle"
ACTION_STATUS_PROCESSING: Final[Literal["processing"]] = "processing"
PROCESS_STATUS_ENQUEUED: Final[Literal["enqueued"]] = "enqueued"
PROCESS_STATUS_RUNNING: Final[Literal["running"]] = "running"
JOB_STATUS_QUEUED: Final[Literal["queued"]] = "queued"
JOB_STATUS_RUNNING: Final[Literal["running"]] = "running"
ACTION_FAILURE_STAGE_RUNNING_FAILED: Final[str] = "running_failed"
ACTION_FAILURE_CODE_PROCESSING_ERROR: Final[str] = "ACTION_PROCESSING_ERROR"
ACTION_FAILURE_CODE_CANCELED: Final[str] = "ACTION_CANCELED_BY_USER"
ACTION_FAILURE_CODE_APPROVAL_DENIED: Final[str] = "ACTION_APPROVAL_DENIED_BY_USER"
ACTION_FAILURE_STAGE_APPROVAL_DENIED: Final[str] = "approval_denied"
ACTION_FAILURE_CODE_TIMEOUT: Final[str] = "ACTION_PROCESSING_TIMEOUT"
ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE: Final[str] = "ACTION_IMAGE_INPUT_TOO_LARGE"
ACTION_FAILURE_CODE_IMAGE_INPUT_INVALID: Final[str] = "ACTION_IMAGE_INPUT_INVALID"
ACTION_FAILURE_CODE_CONNECTION_NOT_CONFIGURED: Final[str] = (
    "ACTION_CONNECTION_NOT_CONFIGURED"
)
ACTION_FAILURE_CODE_CONNECTION_REJECTED: Final[str] = "ACTION_CONNECTION_REJECTED"
ACTION_FAILURE_CODE_SIGN_IN_EXPIRED: Final[str] = "ACTION_SIGN_IN_EXPIRED"
# `build_llm_proxy_agent_error` folds PROXY_INSUFFICIENT_BALANCE into this code by
# prefixing the agent's own code prefix, so the public message keys on it directly.
ACTION_FAILURE_CODE_INSUFFICIENT_BALANCE: Final[str] = "ACTION_INSUFFICIENT_BALANCE"
ACTION_FAILURE_CODE_HEARTBEAT_STALE: Final[str] = "ACTION_PROCESS_HEARTBEAT_STALE"
ACTION_FAILURE_STAGE_RESUME_FAILED: Final[str] = "resume_failed"
ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID: Final[
    Literal["ACTION_RESUME_CHECKPOINT_INVALID"]
] = "ACTION_RESUME_CHECKPOINT_INVALID"
ACTION_FAILURE_CODE_RESUME_STATE_INCONSISTENT: Final[
    Literal["ACTION_RESUME_STATE_INCONSISTENT"]
] = "ACTION_RESUME_STATE_INCONSISTENT"
ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID: Final[
    Literal["ACTION_RESUME_APPROVAL_STATE_INVALID"]
] = "ACTION_RESUME_APPROVAL_STATE_INVALID"
ACTION_FAILURE_CODE_RESUME_GOAL_WORKER_STATE_INVALID: Final[
    Literal["ACTION_RESUME_GOAL_WORKER_STATE_INVALID"]
] = "ACTION_RESUME_GOAL_WORKER_STATE_INVALID"
ACTION_FAILURE_CODE_RESUME_RUNTIME_CONTEXT_INVALID: Final[
    Literal["ACTION_RESUME_RUNTIME_CONTEXT_INVALID"]
] = "ACTION_RESUME_RUNTIME_CONTEXT_INVALID"
ACTION_FAILURE_CODE_RESUME_NOT_ALLOWED: Final[Literal["ACTION_RESUME_NOT_ALLOWED"]] = (
    "ACTION_RESUME_NOT_ALLOWED"
)
ACTION_FAILURE_CODE_ORCHESTRATION_MODE_RETIRED: Final[
    Literal["ACTION_ORCHESTRATION_MODE_RETIRED"]
] = "ACTION_ORCHESTRATION_MODE_RETIRED"
RESUME_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID,
        ACTION_FAILURE_CODE_RESUME_STATE_INCONSISTENT,
        ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
        ACTION_FAILURE_CODE_RESUME_GOAL_WORKER_STATE_INVALID,
        ACTION_FAILURE_CODE_RESUME_RUNTIME_CONTEXT_INVALID,
        ACTION_FAILURE_CODE_RESUME_NOT_ALLOWED,
        ACTION_FAILURE_CODE_ORCHESTRATION_MODE_RETIRED,
    }
)
ACTION_FAILURE_MESSAGE_CANCELED: Final[str] = "Action execution was canceled."
ACTION_FAILURE_MESSAGE_HEARTBEAT_STALE: Final[str] = (
    "Action execution became unrecoverable before completion."
)
ACTION_FAILURE_MESSAGE_RUNNING_FAILED: Final[str] = "Action execution failed."
# Failure codes whose cause the user can act on, and the sentence that says how.
# Every other terminal keeps ACTION_FAILURE_MESSAGE_RUNNING_FAILED: a message
# that names no remedy is worse than the plain one.
_ACTION_PUBLIC_FAILURE_MESSAGES: Final[dict[str, dict[str, str]]] = {
    ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE: {
        "en": (
            "The attached images exceed the model's limit. "
            "Send fewer or smaller images."
        ),
        "ja": "添付画像がモデルの上限を超えました。枚数を減らすか、小さい画像で送り直してください。",
    },
    ACTION_FAILURE_CODE_IMAGE_INPUT_INVALID: {
        "en": "An attached image could not be read. Attach it again.",
        "ja": "添付画像を読み取れませんでした。もう一度添付してください。",
    },
    ACTION_FAILURE_CODE_CONNECTION_NOT_CONFIGURED: {
        "en": "No AI connection is set up. Choose one in Settings, then try again.",
        "ja": "AI接続が設定されていません。設定のAI接続で接続方法を選んでください。",
    },
    ACTION_FAILURE_CODE_CONNECTION_REJECTED: {
        "en": (
            "The provider rejected the saved AI connection. "
            "Check it in Settings, then try again."
        ),
        "ja": "保存したAI接続がプロバイダーに拒否されました。設定のAI接続で確認してください。",
    },
    ACTION_FAILURE_CODE_SIGN_IN_EXPIRED: {
        "en": "Your sign-in expired. Log in again, then try again.",
        "ja": "ログインの有効期限が切れました。ログインし直してください。",
    },
    # The same sentence has to hold for a user's own provider account and for a
    # Pantaray account, so it names neither an amount nor a way to pay.
    ACTION_FAILURE_CODE_INSUFFICIENT_BALANCE: {
        "en": (
            "The AI you are using has no usage left, so trying again "
            "will not help. Check the usage on that AI service."
        ),
        "ja": (
            "ご利用中のAIの利用枠が残っていないため、もう一度試しても実行できません。"
            "AIサービス側で利用状況をご確認ください。"
        ),
    },
}
ACTION_TERMINAL_STATUSES: Final[frozenset[ActionTerminalStatus]] = frozenset(
    {
        ACTION_STATUS_SUCCESS,
        ACTION_STATUS_ERROR,
        ACTION_STATUS_CANCELED,
    }
)
ACTION_RUNTIME_STATUSES: Final[frozenset[ActionRuntimeStatus]] = frozenset(
    {
        ACTION_STATUS_IDLE,
        ACTION_STATUS_PROCESSING,
        ACTION_STATUS_SUCCESS,
        ACTION_STATUS_ERROR,
        ACTION_STATUS_CANCELED,
    }
)
FAILED_ACTION_TERMINAL_STATUSES: Final[frozenset[FailedActionTerminalStatus]] = (
    frozenset(
        {
            ACTION_STATUS_ERROR,
            ACTION_STATUS_CANCELED,
        }
    )
)
PENDING_START_LANE_STATUS_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        (PROCESS_STATUS_ENQUEUED, JOB_STATUS_QUEUED),
        (PROCESS_STATUS_RUNNING, JOB_STATUS_RUNNING),
    }
)


def derive_action_phase(
    *,
    user_reaction: str | None,
    action_status: str | None,
    process_status: str | None,
    job_status: str | None,
) -> ActionUiPhase:
    normalized_user_reaction = parse_stored_suggestion_user_reaction(user_reaction)
    normalized_status = action_status.strip().lower() if action_status else None
    normalized_process_status = (
        process_status.strip().lower() if process_status else None
    )
    normalized_job_status = job_status.strip().lower() if job_status else None

    if normalized_status == ACTION_STATUS_PROCESSING:
        return "processing"
    if normalized_status in ACTION_TERMINAL_STATUSES:
        return "terminal"
    if (
        normalized_user_reaction != SUGGESTION_USER_REACTION_ACCEPTED
        or normalized_status != ACTION_STATUS_IDLE
    ):
        return "idle"
    if (
        normalized_process_status,
        normalized_job_status,
    ) in PENDING_START_LANE_STATUS_PAIRS:
        return "accepted_pending_start"
    return "idle"


@dataclass(frozen=True)
class ActionTerminalFailure:
    """Action non-success terminal の canonical failure reason。"""

    failure_code: str
    failure_stage: str
    failure_message_public: str


class ActionProcessCompletedData(TypedDict, total=False):
    kind: str
    process_id: str
    suggestion_id: str
    action_id: str
    command_id: str
    status: str
    completed_at: str
    final_output: str
    error: dict[str, JSONValue]
    failure_code: str
    failure_stage: str
    failure_message_public: str


class ActionProcessCompletedMeta(TypedDict, total=False):
    kind: str
    process_id: str
    suggestion_id: str
    action_id: str
    command_id: str
    failure_code: str


class ActionProcessCompletedPayload(TypedDict):
    data: ActionProcessCompletedData
    meta: ActionProcessCompletedMeta


@dataclass(frozen=True)
class FinalizeActionTerminalCommand:
    """Action terminalization RPC へ渡す正規化済みコマンド。"""

    process_completed_event_id: str
    suggestion_id: str | None
    user_id: str
    command_id: str
    process_id: str
    action_id: str
    accepted_at: str
    completed_at: str
    action_status: ActionTerminalStatus
    failure_code: str | None = None
    error_payload: dict[str, JSONValue] | None = None
    final_output: str | None = None
    memory_draft_json: str | None = None
    failure_stage: str | None = None
    failure_message_public: str | None = None
    final_prompt_text: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    total_steps: int | None = None
    total_llm_steps: int | None = None
    total_tool_steps: int | None = None
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "process_completed_event_id",
            "user_id",
            "command_id",
            "process_id",
            "action_id",
        ):
            _normalize_required_non_empty_str(self, field_name)
        object.__setattr__(
            self,
            "suggestion_id",
            _normalize_optional_str(self.suggestion_id),
        )
        for field_name in ("accepted_at", "completed_at"):
            _normalize_timestamp_str(self, field_name)

        object.__setattr__(
            self,
            "action_status",
            _normalize_terminal_status(self.action_status),
        )
        object.__setattr__(
            self, "failure_code", _normalize_optional_str(self.failure_code)
        )
        if self.error_payload is not None and not isinstance(self.error_payload, dict):
            raise ValueError(
                "FinalizeActionTerminalCommand: error_payload must be an object or null"
            )
        object.__setattr__(
            self, "final_output", _normalize_optional_str(self.final_output)
        )
        object.__setattr__(
            self, "memory_draft_json", _normalize_optional_str(self.memory_draft_json)
        )
        object.__setattr__(
            self,
            "failure_stage",
            _normalize_optional_str(self.failure_stage),
        )
        object.__setattr__(
            self,
            "failure_message_public",
            _normalize_optional_str(self.failure_message_public),
        )
        object.__setattr__(
            self,
            "final_prompt_text",
            _normalize_optional_str(self.final_prompt_text),
        )
        object.__setattr__(
            self, "prompt_name", _normalize_optional_str(self.prompt_name)
        )
        object.__setattr__(
            self,
            "prompt_version",
            _normalize_optional_str(self.prompt_version),
        )

        for field_name in (
            "total_steps",
            "total_llm_steps",
            "total_tool_steps",
            "total_prompt_tokens",
            "total_completion_tokens",
        ):
            _validate_optional_non_negative_int(self, field_name)

        if self.action_status == ACTION_STATUS_SUCCESS:
            if self.final_output is None:
                raise ValueError(
                    "FinalizeActionTerminalCommand: success requires final_output"
                )
            if (
                self.failure_code is not None
                or self.failure_stage is not None
                or self.failure_message_public is not None
            ):
                raise ValueError(
                    "FinalizeActionTerminalCommand: success cannot include failure details"
                )
            return

        if (
            self.failure_code is None
            or self.failure_stage is None
            or self.failure_message_public is None
        ):
            raise ValueError(
                "FinalizeActionTerminalCommand: non-success requires "
                "failure_code/failure_stage/failure_message_public"
            )
        if self.final_output is not None:
            raise ValueError(
                "FinalizeActionTerminalCommand: non-success cannot include final_output"
            )

    @property
    def process_completed_payload(self) -> ActionProcessCompletedPayload:
        """Action terminal process_completed の canonical payload を返す。"""

        data: ActionProcessCompletedData = {
            "kind": "action",
            "process_id": self.process_id,
            "action_id": self.action_id,
            "command_id": self.command_id,
            "status": self.action_status,
            "completed_at": self.completed_at,
        }
        meta: ActionProcessCompletedMeta = {
            "kind": "action",
            "process_id": self.process_id,
            "action_id": self.action_id,
            "command_id": self.command_id,
        }
        if self.suggestion_id is not None:
            data["suggestion_id"] = self.suggestion_id
            meta["suggestion_id"] = self.suggestion_id
        if self.action_status == ACTION_STATUS_SUCCESS:
            if self.final_output is not None:
                data["final_output"] = self.final_output
        else:
            if self.error_payload is not None:
                data["error"] = dict(self.error_payload)
            if self.failure_code is not None:
                data["failure_code"] = self.failure_code
                meta["failure_code"] = self.failure_code
            if self.failure_stage is not None:
                data["failure_stage"] = self.failure_stage
            if self.failure_message_public is not None:
                data["failure_message_public"] = self.failure_message_public
        return {"data": data, "meta": meta}


def _normalize_required_non_empty_str(
    command: FinalizeActionTerminalCommand, field_name: str
) -> None:
    value = getattr(command, field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"FinalizeActionTerminalCommand: {field_name} must be a non-empty string"
        )
    object.__setattr__(command, field_name, value.strip())


def _normalize_timestamp_str(
    command: FinalizeActionTerminalCommand, field_name: str
) -> None:
    # Stored terminal payloads are re-derived and compared in microseconds.
    try:
        canonical = normalize_iso8601_utc_z_microseconds(getattr(command, field_name))
    except ValueError as exc:
        raise ValueError(
            f"FinalizeActionTerminalCommand: {field_name} must be a "
            f"timezone-aware ISO 8601 timestamp: {exc}"
        ) from exc
    object.__setattr__(command, field_name, canonical)


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_terminal_status(value: ActionTerminalStatus) -> ActionTerminalStatus:
    normalized = str(value).strip().lower()
    if not is_action_terminal_status(normalized):
        raise ValueError(
            f"FinalizeActionTerminalCommand: unsupported terminal status {value!r}"
        )
    return normalized


def _validate_optional_non_negative_int(
    command: FinalizeActionTerminalCommand, field_name: str
) -> None:
    value = getattr(command, field_name)
    if value is None:
        return
    if not is_strict_int(value) or value < 0:
        raise ValueError(
            f"FinalizeActionTerminalCommand: {field_name} must be a non-negative integer or null"
        )


def build_finalize_action_terminal_command(
    *,
    process_completed_event_id: str,
    suggestion_id: str | None,
    user_id: str,
    command_id: str,
    process_id: str,
    action_id: str,
    accepted_at: str,
    completed_at: str,
    action_status: ActionTerminalStatus,
    final_output: str | None = None,
    memory_draft_json: str | None = None,
    failure: ActionTerminalFailure | None = None,
    failure_code: str | None = None,
    error_payload: dict[str, JSONValue] | None = None,
    failure_stage: str | None = None,
    failure_message_public: str | None = None,
    final_prompt_text: str | None = None,
    prompt_name: str | None = None,
    prompt_version: str | None = None,
    total_steps: int | None = None,
    total_llm_steps: int | None = None,
    total_tool_steps: int | None = None,
    total_prompt_tokens: int | None = None,
    total_completion_tokens: int | None = None,
) -> FinalizeActionTerminalCommand:
    """Action terminalization 用の正規コマンドを構築する。"""

    return FinalizeActionTerminalCommand(
        process_completed_event_id=process_completed_event_id,
        suggestion_id=suggestion_id,
        user_id=user_id,
        command_id=command_id,
        process_id=process_id,
        action_id=action_id,
        accepted_at=accepted_at,
        completed_at=completed_at,
        action_status=action_status,
        failure_code=failure_code
        if failure_code is not None
        else (failure.failure_code if failure is not None else None),
        error_payload=error_payload,
        final_output=final_output,
        memory_draft_json=memory_draft_json,
        failure_stage=failure_stage
        if failure_stage is not None
        else (failure.failure_stage if failure is not None else None),
        failure_message_public=(
            failure_message_public
            if failure_message_public is not None
            else (failure.failure_message_public if failure is not None else None)
        ),
        final_prompt_text=final_prompt_text,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        total_steps=total_steps,
        total_llm_steps=total_llm_steps,
        total_tool_steps=total_tool_steps,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
    )


def is_action_terminal_status(value: object) -> TypeGuard[ActionTerminalStatus]:
    return isinstance(value, str) and value in ACTION_TERMINAL_STATUSES


def is_action_runtime_status(value: object) -> TypeGuard[ActionRuntimeStatus]:
    return isinstance(value, str) and value in ACTION_RUNTIME_STATUSES


def normalize_action_runtime_status(value: object) -> ActionRuntimeStatus:
    normalized = str(value).strip().lower()
    if not is_action_runtime_status(normalized):
        raise ValueError(f"Unsupported action runtime status: {value!r}")
    return normalized


def action_failure_message_public(
    *,
    failure_code: str,
    language: str | None,
) -> str | None:
    """Return the user-facing message for an actionable failure code."""

    messages = _ACTION_PUBLIC_FAILURE_MESSAGES.get(failure_code)
    if messages is None:
        return None
    return messages["ja"] if language == "ja" else messages["en"]


def derive_action_terminal_failure(
    action_status: ActionTerminalStatus,
) -> ActionTerminalFailure | None:
    """Action terminal status に対応する canonical failure reason を返す。"""

    if action_status == ACTION_STATUS_SUCCESS:
        return None
    if action_status == ACTION_STATUS_CANCELED:
        return ActionTerminalFailure(
            failure_code=ACTION_FAILURE_CODE_CANCELED,
            failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
            failure_message_public=ACTION_FAILURE_MESSAGE_CANCELED,
        )
    if action_status == ACTION_STATUS_ERROR:
        return ActionTerminalFailure(
            failure_code=ACTION_FAILURE_CODE_PROCESSING_ERROR,
            failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
            failure_message_public=ACTION_FAILURE_MESSAGE_RUNNING_FAILED,
        )
    raise RuntimeError(f"Unsupported terminal action_status: {action_status!r}")


def build_hard_stale_action_failure() -> ActionTerminalFailure:
    """hard-stale / 再開不能の Action を public `error` へ正規化する。"""

    return ActionTerminalFailure(
        failure_code=ACTION_FAILURE_CODE_HEARTBEAT_STALE,
        failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
        failure_message_public=ACTION_FAILURE_MESSAGE_HEARTBEAT_STALE,
    )


def build_action_timeout_failure() -> ActionTerminalFailure:
    """step budget / 実行時間超過を public `error` に正規化する。"""

    return ActionTerminalFailure(
        failure_code=ACTION_FAILURE_CODE_TIMEOUT,
        failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
        failure_message_public=ACTION_FAILURE_MESSAGE_RUNNING_FAILED,
    )


def build_resume_failure(*, failure_code: str) -> ActionTerminalFailure:
    """resume failure を public `error` に正規化する。"""

    if failure_code not in RESUME_FAILURE_CODES:
        raise ValueError(f"Unsupported resume failure_code: {failure_code!r}")
    return ActionTerminalFailure(
        failure_code=failure_code,
        failure_stage=ACTION_FAILURE_STAGE_RESUME_FAILED,
        failure_message_public=ACTION_FAILURE_MESSAGE_RUNNING_FAILED,
    )


__all__ = [
    "ACTION_FAILURE_CODE_HEARTBEAT_STALE",
    "ACTION_FAILURE_CODE_IMAGE_INPUT_INVALID",
    "ACTION_FAILURE_CODE_IMAGE_INPUT_TOO_LARGE",
    "ACTION_FAILURE_CODE_CONNECTION_NOT_CONFIGURED",
    "ACTION_FAILURE_CODE_CONNECTION_REJECTED",
    "ACTION_FAILURE_CODE_SIGN_IN_EXPIRED",
    "action_failure_message_public",
    "ACTION_FAILURE_CODE_CANCELED",
    "ACTION_FAILURE_CODE_PROCESSING_ERROR",
    "ACTION_FAILURE_CODE_TIMEOUT",
    "ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID",
    "ACTION_FAILURE_CODE_RESUME_CHECKPOINT_INVALID",
    "ACTION_FAILURE_CODE_RESUME_GOAL_WORKER_STATE_INVALID",
    "ACTION_FAILURE_CODE_RESUME_NOT_ALLOWED",
    "ACTION_FAILURE_CODE_RESUME_RUNTIME_CONTEXT_INVALID",
    "ACTION_FAILURE_CODE_RESUME_STATE_INCONSISTENT",
    "ACTION_FAILURE_CODE_ORCHESTRATION_MODE_RETIRED",
    "RESUME_FAILURE_CODES",
    "ACTION_STATUS_CANCELED",
    "ACTION_FAILURE_MESSAGE_HEARTBEAT_STALE",
    "ACTION_FAILURE_MESSAGE_CANCELED",
    "ACTION_FAILURE_MESSAGE_RUNNING_FAILED",
    "ACTION_FAILURE_STAGE_RESUME_FAILED",
    "ACTION_FAILURE_STAGE_RUNNING_FAILED",
    "FAILED_ACTION_TERMINAL_STATUSES",
    "ACTION_STATUS_ERROR",
    "ACTION_STATUS_IDLE",
    "ACTION_STATUS_PROCESSING",
    "ACTION_STATUS_SUCCESS",
    "ACTION_TERMINAL_STATUSES",
    "ACTION_RUNTIME_STATUSES",
    "ActionProcessCompletedPayload",
    "FailedActionTerminalStatus",
    "ActionTerminalFailure",
    "FinalizeActionTerminalCommand",
    "ActionRuntimeStatus",
    "SuggestionUserReaction",
    "SUGGESTION_USER_REACTION_ACCEPTED",
    "SUGGESTION_USER_REACTION_REJECTED",
    "ActionTerminalStatus",
    "build_action_timeout_failure",
    "build_hard_stale_action_failure",
    "build_resume_failure",
    "build_finalize_action_terminal_command",
    "derive_action_terminal_failure",
    "is_action_runtime_status",
    "is_action_terminal_status",
    "normalize_public_suggestion_reaction_for_write",
    "parse_stored_suggestion_user_reaction",
    "require_stored_suggestion_user_reaction",
    "normalize_action_runtime_status",
]

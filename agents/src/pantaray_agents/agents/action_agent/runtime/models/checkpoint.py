"""Runtime checkpoint boundary model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from pantaray_agents.schema.agent.action import (
    MemoryContextEpochCheckpoint,
    MemoryDraftCheckpointModel,
    StepType,
)
from pantaray_agents.schema.agent.base import AgentError, JSONValue

from .approval import PendingApprovalRequestModel
from .conversation import GoalConversationStateModel
from .execution_context import ExecutionContextModel
from .failure import ResumeFailureException
from .memory_reference import MemoryArtifactReferenceModel
from .tool_call import ActionLifecyclePhaseModel, NextActionModel

ActionStatusModel = Literal["processing", "success", "error", "canceled"]
RunAuthorityModel = Literal["authoritative", "superseded"]
HistoryLifecyclePhaseModel = Literal["init", "planning", "executing", "finalizing"]


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must be a non-empty string.")
    return value


class _HistoryAttachmentBaseModel(BaseModel):
    """履歴に保持する Action attachment の厳格境界。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["file"]
    ref: str
    blob_ref: str
    display_path: str
    mime_type: str
    byte_size: int = Field(ge=0)
    sha256: str

    _validate_ref = field_validator("ref")(_require_non_blank)
    _validate_blob_ref = field_validator("blob_ref")(_require_non_blank)
    _validate_display_path = field_validator("display_path")(_require_non_blank)
    _validate_mime_type = field_validator("mime_type")(_require_non_blank)
    _validate_sha256 = field_validator("sha256")(_require_non_blank)


class HistoryWorkspaceAttachmentModel(_HistoryAttachmentBaseModel):
    source_kind: Literal["workspace_file"]
    workspace_root_path: str
    workspace_relative_path: str

    _validate_workspace_root_path = field_validator("workspace_root_path")(
        _require_non_blank
    )
    _validate_workspace_relative_path = field_validator("workspace_relative_path")(
        _require_non_blank
    )


class HistoryLocalImageAttachmentModel(_HistoryAttachmentBaseModel):
    source_kind: Literal["local_image_blob"]
    storage_path: str

    _validate_storage_path = field_validator("storage_path")(_require_non_blank)


HistoryAttachmentModel = Annotated[
    HistoryWorkspaceAttachmentModel | HistoryLocalImageAttachmentModel,
    Field(discriminator="source_kind"),
]


class HistoryEntryModel(BaseModel):
    """checkpoint v4 の履歴エントリ境界。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    step_id: str
    step_number: int = Field(ge=1)
    phase: HistoryLifecyclePhaseModel
    step_type: StepType
    summary: str
    user_request_text: str | None = None
    assistant_message_text: str | None = None
    assistant_phase: Literal["commentary"] | None = None
    tool_id: str | None
    started_at: str
    completed_at: str
    thinking: str | None = None
    result_line: str | None = None
    args: dict[str, JSONValue] | None = None
    output: JSONValue | None = None
    attachments: list[HistoryAttachmentModel] | None = None
    short_step_id: str | None = None
    # Optional so a checkpoint written before these were recorded still resumes;
    # the projection falls back to rendered text for a span that lacks them.
    call_id: str | None = None
    llm_step_id: str | None = None
    turn_context: str | None = None
    world_state: dict[str, str] | None = None
    agents_md: str | None = None

    _validate_step_id = field_validator("step_id")(_require_non_blank)
    _validate_started_at = field_validator("started_at")(_require_non_blank)
    _validate_completed_at = field_validator("completed_at")(_require_non_blank)

    @field_validator("step_type", mode="before")
    @classmethod
    def _normalize_step_type(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return StepType(value)
            except ValueError:
                return value
        return value

    @field_validator("short_step_id")
    @classmethod
    def _validate_short_step_id(cls, value: str | None) -> str | None:
        return _require_non_blank(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_assistant_message_shape(self) -> HistoryEntryModel:
        if self.step_type is StepType.ASSISTANT_MESSAGE:
            if (
                not self.assistant_message_text
                or not self.assistant_message_text.strip()
            ):
                raise ValueError("assistant history requires non-empty message text")
            if (
                self.summary
                or self.tool_id is not None
                or any(
                    value is not None
                    for value in (
                        self.thinking,
                        self.result_line,
                        self.args,
                        self.attachments,
                    )
                )
                or "output" in self.model_fields_set
            ):
                raise ValueError("assistant history cannot contain execution payloads")
            if self.short_step_id is None or not self.short_step_id.endswith(
                "-ASSISTANT"
            ):
                raise ValueError(
                    "assistant history requires an ASSISTANT short_step_id"
                )
        elif (
            self.assistant_message_text is not None or self.assistant_phase is not None
        ):
            raise ValueError("assistant message fields require assistant history")
        return self

    @model_validator(mode="after")
    def _validate_user_request_shape(self) -> HistoryEntryModel:
        if self.step_type is StepType.USER_REQUEST:
            if self.user_request_text is None or not self.user_request_text.strip():
                raise ValueError(
                    "user_request history requires non-empty user_request_text"
                )
            if self.summary or self.tool_id is not None:
                raise ValueError(
                    "user_request history requires an empty summary and no tool_id"
                )
            if self.short_step_id is None or not self.short_step_id.endswith("-USER"):
                raise ValueError(
                    "user_request history requires a numbered *-USER short_step_id"
                )
            if (
                any(
                    value is not None
                    for value in (self.thinking, self.result_line, self.args)
                )
                or "output" in self.model_fields_set
            ):
                raise ValueError(
                    "user_request history cannot contain LLM or tool payload fields"
                )
            if self.attachments is not None and any(
                attachment.source_kind != "local_image_blob"
                for attachment in self.attachments
            ):
                raise ValueError(
                    "user_request history carries only local image attachments"
                )
        elif self.user_request_text is not None:
            raise ValueError(
                "user_request_text is available only for user_request history"
            )
        return self


class RuntimeCheckpointModel(BaseModel):
    """resume 判定に必要な checkpoint 境界。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action_id: str
    suggestion_id: str | None
    user_id: str
    phase: ActionLifecyclePhaseModel
    step: int
    steps_taken: int
    llm_steps_taken: int
    tool_steps_taken: int
    max_steps: int
    max_tool_steps: int
    status: ActionStatusModel
    started_at: str
    updated_at: str
    token_budget: int | None = None
    tokens_used: int
    total_prompt_tokens: int
    total_completion_tokens: int
    chunks_sent: int
    context: dict[str, JSONValue]
    history_by_scope: dict[str, list[HistoryEntryModel]]
    goal_conversations: dict[str, GoalConversationStateModel]
    memory_artifact_references: tuple[MemoryArtifactReferenceModel, ...]
    next_action: NextActionModel | None = None
    final_output: str | None = None
    supervisor_pending_final_answer: str | None = None
    supervisor_memory_draft: MemoryDraftCheckpointModel | None = None
    memory_context_epoch: MemoryContextEpochCheckpoint | None = None
    errors: list[AgentError]
    run_authority: RunAuthorityModel
    skip_persist: bool
    superseded_reason: str | None = None
    superseded_at: str | None = None
    pending_approval_request: PendingApprovalRequestModel | None = None
    current_approval_blockers: list[PendingApprovalRequestModel] = Field(
        default_factory=list
    )
    manifest_id: str | None = None
    execution_session_id: str | None = None
    execution_network_policy: str | None = None
    action_temp_dir: str | None = None
    app_runtime_python: str | None = None
    read_access_scope: str | None = None

    @field_validator("memory_artifact_references", mode="before")
    @classmethod
    def _normalize_json_memory_references(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("step")
    @classmethod
    def _validate_step(cls, value: int) -> int:
        if value < 1:
            raise ValueError("step must be >= 1")
        return value

    @model_validator(mode="after")
    def _validate_resume_boundary(self) -> RuntimeCheckpointModel:
        try:
            ExecutionContextModel.from_optional_values(
                manifest_id=self.manifest_id,
                execution_session_id=self.execution_session_id,
                execution_network_policy=self.execution_network_policy,
                action_temp_dir=self.action_temp_dir,
                app_runtime_python=self.app_runtime_python,
                read_access_scope=self.read_access_scope,
            )
        except ValidationError as exc:
            raise ResumeFailureException(
                failure_code="ACTION_RESUME_RUNTIME_CONTEXT_INVALID",
                message=f"Invalid execution context in runtime checkpoint: {exc}",
            ) from exc
        return self

    def execution_context_or_none(self) -> ExecutionContextModel | None:
        return ExecutionContextModel.from_optional_values(
            manifest_id=self.manifest_id,
            execution_session_id=self.execution_session_id,
            execution_network_policy=self.execution_network_policy,
            action_temp_dir=self.action_temp_dir,
            app_runtime_python=self.app_runtime_python,
            read_access_scope=self.read_access_scope,
        )

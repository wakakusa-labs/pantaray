"""アクション実行関連のスキーマ定義。"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    model_validator,
)

from pantaray_agents.action_status import ActionRuntimeStatus, ActionTerminalStatus
from pantaray_agents.schema.memory_catalog import MemoryCatalogSource
from pantaray_llm.contracts.conversation import LlmProviderTurn

from .action_assistant_message import ActionAssistantMessageStep
from .action_message import ActionUserMessageInput
from .action_message import SuggestionApprovalInput as SuggestionApprovalInput
from .base import (
    AgentError,
    AgentRequest,
    AgentResponse,
    JSONValue,
    StepStatusType,
)

type ActionPayload = dict[str, JSONValue]
type RuntimeStateCheckpointPayload = dict[str, JSONValue]


class MemoryDocumentCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_path: str
    content: str


class MemoryDraftLinkCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    local_ref_id: str
    target_fragment_id: str
    source_path: str
    source_anchor_text: str
    source_anchor_occurrence: int
    reference_note: str
    created_at: str
    state: Literal["carried", "pending", "removed"]


class AppliedMemoryDraftCommandCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_invocation_id: str
    local_ref_id: str | None
    draft_revision: str


class MemoryDraftCheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    draft_session_id: str
    user_id: str
    owner_node_id: str
    base_revision_id: str | None
    draft_revision: str
    documents: list[MemoryDocumentCheckpoint]
    links: list[MemoryDraftLinkCheckpoint]
    applied_commands: list[AppliedMemoryDraftCommandCheckpoint]


class MemoryContextItemCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    context_handle: str
    source: MemoryCatalogSource
    label: str
    source_path: str
    heading_path: str | None
    content: str
    observed_at: str
    user_id: str
    fragment_id: str
    revision_id: str
    node_id: str
    reference_depth: Literal[0, 1] = 0


class MemoryContextEpochCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    epoch_id: str
    run_id: str
    user_id: str
    items: list[MemoryContextItemCheckpoint]


class ActionScratchExecutionTarget(BaseModel):
    """Use an app-managed scratch workspace for non-project actions."""

    kind: Literal["scratch"] = "scratch"


ActionExecutionTarget = ActionScratchExecutionTarget


class ActionAgentRequest(AgentRequest):
    """Durable USER stepを実行するAction runtime request。"""

    user_id: str = Field(description="対象ユーザーのID")
    suggestion_id: str | None = Field(
        default=None,
        description="起点になった提案ID。提案を経由しないActionではNone。",
    )
    action_id: str = Field(description="アクションのID（オーケストレーターが生成）")
    user_step_id: str = Field(description="実行対象の永続化済みUSER step ID")
    user_step_number: PositiveInt = Field(description="Action内の論理step番号")
    user_step_local_step_number: PositiveInt = Field(
        description="Supervisor scope内のUSER step番号"
    )
    user_step_short_id: str = Field(description="LLM履歴で使う短縮USER step ID")
    user_step_created_at: str = Field(description="USER stepの永続化時刻")
    user_message: ActionUserMessageInput = Field(
        description="USER stepから復元した型付きメッセージ"
    )
    preceding_assistant_messages: tuple[ActionAssistantMessageStep, ...] = ()
    execution_target: ActionExecutionTarget = Field(
        default_factory=ActionScratchExecutionTarget,
        description="Action tool execution target.",
    )
    approval_resume_session_id: str | None = Field(
        default=None,
        description="Approval resume が対象にする approval session ID。",
    )
    approval_resume_tool_request_id: str | None = Field(
        default=None,
        description="Approval resume が対象にする tool request ID。",
    )

    @model_validator(mode="after")
    def validate_user_message_provenance(self) -> Self:
        expected_short_id = f"S-{self.user_step_local_step_number}-USER"
        if self.user_step_short_id != expected_short_id:
            raise ValueError(
                "user_step_short_id must match user_step_local_step_number"
            )
        approval = self.user_message.suggestion_approval
        if approval is None:
            return self
        if self.suggestion_id != approval.suggestion_id:
            raise ValueError(
                "suggestion_id must match user_message.suggestion_approval"
            )
        return self


class ActionAgentResponse(AgentResponse):
    """アクションエージェントのレスポンス

    API仕様書5.1章とDBスキーマ（agent_actions）に準拠
    APIの応答は最終的な出力（結果、完了通知、要約等）のみを示す

    Attributes:
        action_id (str): アクションのID
        suggestion_id (str): 関連する提案のID
        user_id (str): 対象ユーザーのID
        final_output (str): 最終ステップでユーザーに送信された出力内容
        created_at (str): レスポンス生成時刻（基底クラスから継承）
        status (str): 処理ステータス（基底クラスから継承）
            - Action は "idle" / "processing" / "success" / "error" / "canceled" を返す。
        error (AgentError | None): エラー情報（基底クラスから継承）
    """

    action_id: str = Field(description="アクションのID")
    suggestion_id: str | None = Field(description="起点になった提案のID")
    user_id: str = Field(description="対象ユーザーのID")
    final_output: str = Field(description="最終ステップでユーザーに送信された出力内容")
    status: ActionRuntimeStatus = Field(
        description="Action status: idle, processing, success, error, canceled"
    )


def build_action_agent_response(
    *,
    action_id: str,
    suggestion_id: str | None,
    user_id: str,
    final_output: str,
    created_at: str,
    status: ActionRuntimeStatus,
    error: AgentError | None,
) -> ActionAgentResponse:
    return ActionAgentResponse(
        action_id=str(action_id),
        suggestion_id=suggestion_id,
        user_id=str(user_id),
        final_output=str(final_output),
        created_at=str(created_at),
        status=status,
        error=error,
    )


class ActionHeaderRecord(BaseModel):
    """`agent_actions` 親レコード upsert 用の検証モデル。"""

    action_id: str
    user_id: str
    suggestion_id: str | None
    prompt_name: str
    prompt_version: str
    status: ActionRuntimeStatus = Field(default="processing")
    final_output: str = Field(default="")
    final_prompt_text: str | None = None
    error: AgentError | None = None
    created_at: str
    updated_at: str
    steps_budget: int | None = None
    llm_steps_budget: int | None = None
    tool_steps_budget: int | None = None
    token_budget: int | None = None
    total_steps: int | None = None
    total_llm_steps: int | None = None
    total_tool_steps: int | None = None
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None


class ActionTerminalStatusUpdate(BaseModel):
    """processing から terminal へ遷移させる更新コマンド。"""

    user_id: str
    action_id: str
    status: ActionTerminalStatus


class ActionApprovalBlocker(BaseModel):
    """Runtime が現在停止している approval blocker。"""

    action_id: str
    approval_session_id: str
    tool_request_id: str
    tool_id: str
    intent_class: str
    command_summary: ActionPayload


class ActionRunResult(BaseModel):
    """Action runtime の内部実行結果。

    worker が DB-first terminal RPC または local runtime pause へ渡すための
    canonical payload を表す。
    外部 API 応答の `ActionAgentResponse` とは分離し、terminal writer の責務を
    worker/RPC 側へ寄せる。
    """

    action_id: str
    suggestion_id: str | None
    user_id: str
    completed_at: str = Field(description="runtime result 確定時刻（ISO 8601）")
    status: ActionRuntimeStatus = Field(description="Action runtime status")
    final_output: str = Field(description="最終出力。非 success では空文字列。")
    memory_draft: MemoryDraftCheckpointModel | None = Field(
        default=None,
        description="成功Actionのfinal_outputと同じbodyを持つ検証済みMemory draft。",
    )
    action_failure_code: str | None = Field(
        default=None,
        description="公開可能な failure code。status!=success の場合に必須。",
    )
    error_payload: ActionPayload | None = Field(
        default=None,
        description="内部保持用の構造化 AgentError payload。terminal persist と診断ログで利用する。",
    )
    failure_stage: str | None = Field(
        default=None,
        description="公開 failure stage。非 success の場合に設定されうる。",
    )
    failure_message_public: str | None = Field(
        default=None,
        description="公開 failure message。非 success の場合に設定されうる。",
    )
    final_prompt_text: str | None = Field(
        default=None,
        description="最終プロンプト本文（保存用）。",
    )
    prompt_name: str = Field(description="Action header に保存する prompt 名")
    prompt_version: str = Field(description="Action header に保存する prompt version")
    total_steps: int | None = Field(default=None, description="総ステップ数")
    total_llm_steps: int | None = Field(default=None, description="LLM ステップ総数")
    total_tool_steps: int | None = Field(
        default=None,
        description="ツールステップ総数",
    )
    total_prompt_tokens: int | None = Field(
        default=None,
        description="プロンプトトークン総数",
    )
    total_completion_tokens: int | None = Field(
        default=None,
        description="コンプリーショントークン総数",
    )
    approval_blockers: list[ActionApprovalBlocker] = Field(
        default_factory=list,
        description="status=processing のとき runtime を停止している承認 blocker 一覧。",
    )


class ActionExecutionResult(BaseModel):
    """Action runtime から worker task へ渡す内部結果。"""

    run_result: ActionRunResult
    execution_session_id: str | None = None
    runtime_state_checkpoint: RuntimeStateCheckpointPayload | None


class StepType(StrEnum):
    """アクションステップ種別（永続化のSSOT）。"""

    USER_REQUEST = "user_request"
    ASSISTANT_MESSAGE = "assistant_message"
    LLM_OUTPUT = "llm_output"
    TOOL_EXECUTION = "tool_execution"


class ShortStepSuffix(StrEnum):
    """short_step_id のサフィックス種別。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    THINK = "THINK"
    TOOL = "TOOL"


SHORT_STEP_SUFFIX_PATTERN = "|".join(suffix.value for suffix in ShortStepSuffix)
# short_step_id の suffix パターン。


class ActionProviderTurnRecord(BaseModel):
    """One THINK's provider turn, bound to the account that issued it.

    The opaque part of a turn -- an OpenAI ``encrypted_content``, an Anthropic
    thinking signature -- is readable only by the provider account that produced
    it, so the turn is never stored without the identity it may be handed back
    to. ``identity`` is ``route_identity.ConnectionIdentity`` rendered as one
    comparable string and carries no secret.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: LlmProviderTurn
    identity: str = Field(min_length=1)
    # What the request that produced the turn sent before the turn's place --
    # ``conversation_projection.request_fingerprint`` chained over its items. A
    # turn goes back only behind the same prefix: a thinking block is bound to
    # everything before it, and a request that rewrote any of it is refused.
    fingerprint: str = Field(min_length=1)


class ActionStepRecord(BaseModel):
    """agent_action_steps へ挿入するレコードの検証モデル。"""

    step_id: str
    action_id: str
    step_number: int
    step_name: str
    step_type: StepType
    user_request_text: str | None = None
    llm_prompt_text: str | None = None
    llm_response_text: str | None = None
    tool_args: ActionPayload | None = None
    tool_output: ActionPayload | None = None
    thinking: str | None = None
    # The call identity a tool step executed: the model's own ``call_id`` and the
    # ``step_id`` of the THINK that declared it. Grouping tool rows by
    # ``llm_step_id`` reconstructs one turn's batch in declaration order.
    call_id: str | None = None
    llm_step_id: str | None = None
    # The provider turn an LLM step produced, kept as it arrived so a later turn
    # can hand it back, and the provider account that may receive it back.
    provider_turn: LlmProviderTurn | None = None
    provider_turn_identity: str | None = None
    provider_turn_fingerprint: str | None = None
    runtime_state_checkpoint: RuntimeStateCheckpointPayload | None = None
    runtime_state_checkpoint_version: int | None = None
    status: StepStatusType = Field(default=StepStatusType.PROCESSING)
    error: ActionPayload | None = None
    execution_time_ms: int | None = None
    parent_step_id: str | None = None
    goal_handle: str  # 必須（Supervisor="S", Goal Worker="G{n}"）
    retry_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: str | None = None
    short_step_id: str  # 必須: {scope_handle}-{local_step_number}-{USER|THINK|TOOL}
    local_step_number: int  # 必須: scope 内での論理ステップ番号（1始まり）

    @model_validator(mode="after")
    def validate_user_request_shape(self) -> Self:
        if self.step_type is StepType.USER_REQUEST:
            if self.user_request_text is None or not self.user_request_text.strip():
                raise ValueError(
                    "user_request step requires non-empty user_request_text"
                )
            if any(
                value is not None
                for value in (
                    self.thinking,
                    self.llm_prompt_text,
                    self.llm_response_text,
                    self.tool_args,
                    self.tool_output,
                )
            ):
                raise ValueError(
                    "user_request step cannot contain LLM or tool payload fields"
                )
        elif self.user_request_text is not None:
            raise ValueError(
                "user_request_text is available only for user_request steps"
            )

        suffix_by_step_type = {
            StepType.USER_REQUEST: ShortStepSuffix.USER,
            StepType.ASSISTANT_MESSAGE: ShortStepSuffix.ASSISTANT,
            StepType.LLM_OUTPUT: ShortStepSuffix.THINK,
            StepType.TOOL_EXECUTION: ShortStepSuffix.TOOL,
        }
        expected_short_step_id = (
            f"{self.goal_handle}-{self.local_step_number}-"
            f"{suffix_by_step_type[self.step_type]}"
        )
        if self.short_step_id != expected_short_step_id:
            raise ValueError(
                "short_step_id must match goal_handle, local_step_number, and "
                f"step_type: expected {expected_short_step_id!r}"
            )
        return self

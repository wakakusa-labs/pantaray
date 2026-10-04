"""ActionAgent の LangGraph 状態定義モジュール。

内部の graph state 自体は TypedDict で保持するが、checkpoint / approval /
tool decision のような外部境界は Pydantic model を唯一の正本として使う。"""

from __future__ import annotations

from typing import Literal, NotRequired, Required, TypedDict

from pantaray_agents.agents.action_agent.runtime.models.approval import (
    ApprovalPendingOwnerModel,
    PendingApprovalRequestModel,
)
from pantaray_agents.agents.action_agent.runtime.models.conversation import (
    GoalConversationStateModel,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    ExecutionContextModel,
)
from pantaray_agents.agents.action_agent.runtime.models.memory_reference import (
    MemoryArtifactReferenceModel,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    NextActionModel,
    PendingToolBatchModel,
    ToolCallModel,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import ToolAttachment
from pantaray_agents.schema.agent.action import (
    MemoryContextEpochCheckpoint,
    MemoryDraftCheckpointModel,
    StepType,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_WORKSPACE,
    ReadAccessScope,
)
from pantaray_agents.utils.memory_source_policy import (
    MemorySourceCoverageSnapshot,
    build_unknown_memory_source_coverage_snapshot,
)

# -- タイプエイリアス -----------------------------------------------------------------

ActionStatus = Literal["processing", "success", "error", "canceled"]
ActionPhase = Literal[
    "init",
    "planning",
    "executing",
    "finalizing",
]
RunAuthority = Literal["authoritative", "superseded"]
ApprovalPendingOwner = ApprovalPendingOwnerModel


class AgentErrorState(TypedDict, total=False):
    """エージェント処理中に発生したエラー構造。"""

    error_type: str
    error_code: str
    error_message: str | None
    error_details: dict[str, JSONValue] | None
    severity: str
    metadata: dict[str, JSONValue] | None


ToolCall = ToolCallModel
PendingToolBatch = PendingToolBatchModel
PendingApprovalRequest = PendingApprovalRequestModel


def build_pending_approval_request(
    *,
    owner: ApprovalPendingOwner,
    tool_id: str,
    args: dict[str, JSONValue],
    approval_session_id: str,
    tool_request_id: str,
    requested_at: str,
    intent_class: str,
    command_summary: dict[str, JSONValue],
    goal_id: str | None = None,
    thinking: str | None = None,
    thinking_summary: str | None = None,
) -> PendingApprovalRequest:
    return PendingApprovalRequestModel(
        owner=owner,
        approval_session_id=approval_session_id,
        tool_request_id=tool_request_id,
        requested_at=requested_at,
        intent_class=intent_class,
        command_summary=dict(command_summary),
        goal_id=goal_id,
        tool_call=ToolCallModel(tool_id=tool_id, args=dict(args)),
        thinking=thinking,
        thinking_summary=thinking_summary,
    )


def build_tool_call(
    *,
    tool_id: str,
    args: dict[str, JSONValue],
) -> ToolCall:
    return ToolCallModel(tool_id=tool_id, args=dict(args))


def build_next_action(
    *,
    tool: ToolCall | None,
    decided_at: str,
    batch: PendingToolBatch | None = None,
) -> NextActionModel:
    return NextActionModel(tool=tool, batch=batch, decided_at=decided_at)


class HistoryEntry(TypedDict):
    """履歴（history）に保持するエントリ構造。

    各ツール行は note、引数、結果本文、結果行、参照を時系列で描画する。
    容量調整は保存済み境界より古い結果本文だけを省略し、原本は変更しない。
    """

    step_id: str
    step_number: int
    phase: ActionPhase
    step_type: StepType
    # Supervisor がツール呼び出しに添えた step_note（状況→判断→行動）。
    summary: str
    user_request_text: NotRequired[str]
    assistant_message_text: NotRequired[str]
    assistant_phase: NotRequired[Literal["commentary"]]
    tool_id: str | None
    started_at: str
    completed_at: str
    thinking: NotRequired[str | None]
    # 実行系が決定論的に書く結果行（build_outcome_line）。ツール実行は全件が埋める。
    result_line: NotRequired[str]
    args: NotRequired[dict[str, JSONValue]]  # ツール呼び出し時の引数
    output: NotRequired[JSONValue]  # 保存境界で確定したツール結果本文
    attachments: NotRequired[list[ToolAttachment]]
    short_step_id: NotRequired[str]  # 短縮ID（LLM向けI/FのSSOT）
    # ツール行が実行した呼び出しの identity（ActionToolCallOrigin と同じ 2 つ）。
    # llm_step_id は宣言した THINK 行の step_id で、同じ値を持つツール行が 1 回の
    # THINK が決めたバッチ。採番前の checkpoint から再開した区間には無い。
    call_id: NotRequired[str]
    llm_step_id: NotRequired[str]
    # The turn-context message this THINK was sent with, kept verbatim because
    # the conversation replays it as an item and a rebuilt one would not match
    # byte for byte. Absent on a THINK recorded before the field existed and on
    # every turn sent as one string.
    turn_context: NotRequired[str]
    # The head sections that turn context brought up to date, by field, as
    # sent; a later turn compares against them instead of re-sending.
    world_state: NotRequired[dict[str, str]]
    # AGENTS.md blocks first reached by this tool call. Kept when the output is
    # omitted, since each file is attached only once per Action.
    agents_md: NotRequired[str]


class TargetContextState(TypedDict):
    organization_name: str | None
    project_name: str | None


class ContextInputBaseline(TypedDict):
    prompt_tokens: int
    rendered_bytes: int


class ActionAgentContext(TypedDict, total=False):
    """ActionAgent が維持する文脈情報。"""

    request_summary: str | None
    target_context: TargetContextState
    insight_data: str
    structured_fact_data: str
    # 完了した Goal の成果物リスト（Supervisor への引き継ぎ用）
    # memory_search 対象 source の存在統計（初期化時スナップショット）
    memory_source_coverage: MemorySourceCoverageSnapshot
    analysis_summary: dict[str, JSONValue]
    prompt_name: str
    prompt_version: str
    last_supervisor_prompt: str
    workspace_root_catalog: str
    read_access_scope: ReadAccessScope
    workspace_context_prompt: str
    # ~/.pantaray/AGENTS.md as this run read it ("" when absent).
    agents_md_instructions: str
    # The Executing head's field values from the Action's first conversation
    # turn. Every later turn renders the head from them, byte for byte.
    executing_head_fields: dict[str, str]
    # Real paths of the repository AGENTS.md files already attached to a result.
    agents_md_attached_paths: list[str]
    additional_notes: list[str]
    # --- Supervisor self-repair bookkeeping ---
    # jsonschema（ToolValidationError）により tool 実行が拒否された回数（連続回数）。
    # - 連続で増えるケースは「LLM がツール入力を自己修復できていない」状態を示す。
    # - 成功した tool 実行（スキーマ検証を通過）または別種の失敗では 0 にリセットする。
    tool_validation_error_streak: int
    # Provider input tokens paired with complete prompt/system/tool-schema bytes.
    context_input_baseline: ContextInputBaseline
    # 85% に達したので次の provider 呼び出し前に窓を組み直す。
    context_reset_pending: bool
    # この step_number 未満の結果本文を省略する。境界は後退しない。
    context_body_omitted_before_step: int
    # キャッシュミス計測（pi cache-stats 方式）の 1 つ前の prompt_tokens。
    context_cache_prev_prompt_tokens: int
    # Goal Worker が同じ tool/policy error を連続で踏んだ回数（Goal単位）。
    # --- 短縮ID採番用カウンター ---
    # scope_handle（goal_handle）ごとの local_step_number の最後の値
    # 例: {"S": 3, "G1": 5, "G2": 2}
    local_step_counters: dict[str, int]
    # --- Plan ID 採番用カウンター ---
    # 物理削除でも ID を再利用しないために、次に採番する番号を保持する。
    # --- Approval request identity ---
    # LangGraph step ごとの approval-required logical request ordinal。
    approval_request_counters: dict[str, int]
    # 承認後の re-dispatch で再利用する tool_request_id。
    approval_resume_tool_request_id: str


NextAction = NextActionModel


class ActionAgentState(TypedDict, total=False):
    """LangGraph 実行時に共有される ActionAgent の状態。"""

    phase: ActionPhase
    step: Required[int]
    steps_taken: int
    # LLM/Tool の内訳（steps_taken は両者の合計として維持する）
    llm_steps_taken: int
    tool_steps_taken: int
    max_steps: Required[int]
    # Tool ステップ予算（env を SSOT として実行開始時に必ず確定させる）
    max_tool_steps: Required[int]
    status: ActionStatus
    started_at: str
    updated_at: str
    token_budget: int | None
    tokens_used: int
    total_prompt_tokens: int
    total_completion_tokens: int
    user_id: str
    suggestion_id: str | None
    action_id: str
    manifest_id: str
    execution_session_id: str
    execution_network_policy: str
    action_temp_dir: str
    app_runtime_python: str
    read_access_scope: ReadAccessScope
    # context は ActionAgent の実行における不変条件として「常に存在する」。
    # create_initial_state / initialize_context が必ず投入するため、型でも必須に寄せる。
    context: Required[ActionAgentContext]
    # 履歴はスコープ単位で独立管理する（Supervisor="S", Goal Worker="G{n}"）
    history_by_scope: dict[str, list[HistoryEntry]]
    goal_conversations: Required[dict[str, GoalConversationStateModel]]
    memory_artifact_references: Required[tuple[MemoryArtifactReferenceModel, ...]]
    next_action: NextAction | None
    pending_approval_request: NotRequired[PendingApprovalRequest]
    current_approval_blockers: NotRequired[list[PendingApprovalRequest]]
    final_output: str | None
    supervisor_pending_final_answer: NotRequired[str]
    supervisor_memory_draft: NotRequired[MemoryDraftCheckpointModel]
    memory_context_epoch: NotRequired[MemoryContextEpochCheckpoint]
    errors: list[AgentErrorState]
    chunks_sent: int
    cancel_check_max_consecutive_failures: int
    cancel_check_failure_grace_seconds: int
    cancel_check_consecutive_failures: int
    cancel_check_first_failure_at: str
    cancel_check_last_failure_at: str
    # --- superseded（権威なし）制御 ---
    # この実行が権威を失ったと判断した場合に利用する。
    run_authority: RunAuthority
    # True の場合、最終永続化（save_action）を実行しない（上書き事故防止）。
    skip_persist: bool
    superseded_reason: str | None
    superseded_at: str | None


class ActionAgentStateConfig(TypedDict, total=False):
    """グラフ構築時に利用するコンフィグレーション。

    並列実行に関するパラメータ:
    - max_parallel_memory_queries: memory_search 内部のクエリ並列数上限
    """

    max_steps: int
    max_tool_steps: int
    token_budget: int | None
    prompt_name: str
    prompt_version: str
    # cancellation status 確認が読めない場合の runaway 防止閾値（env/config から解決）
    cancel_check_max_consecutive_failures: int
    cancel_check_failure_grace_seconds: int
    # memory_search など検索系ツール内部でのクエリ並列数の上限（env から解決する。コード上の既定値は持たない）
    max_parallel_memory_queries: int | None


def create_initial_state(
    *,
    user_id: str,
    suggestion_id: str | None,
    action_id: str,
    started_at: str,
    max_steps: int,
    max_tool_steps: int,
    token_budget: int | None,
    execution_context: ExecutionContextModel | None = None,
    manifest_id: str | None = None,
    execution_session_id: str | None = None,
    execution_network_policy: str | None = None,
    action_temp_dir: str | None = None,
    app_runtime_python: str | None = None,
    read_access_scope: ReadAccessScope | None = None,
) -> ActionAgentState:
    """初期状態を生成する。"""

    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise RuntimeError(
            f"Invalid ActionAgent initial state: max_steps must be int, got {max_steps!r}"
        )
    if max_steps <= 0:
        raise RuntimeError(
            f"Invalid ActionAgent initial state: max_steps must be positive, got {max_steps!r}"
        )
    if isinstance(max_tool_steps, bool) or not isinstance(max_tool_steps, int):
        raise RuntimeError(
            "Invalid ActionAgent initial state: "
            f"max_tool_steps must be int, got {max_tool_steps!r}"
        )
    if max_tool_steps <= 0:
        raise RuntimeError(
            "Invalid ActionAgent initial state: "
            f"max_tool_steps must be positive, got {max_tool_steps!r}"
        )

    initial_state = ActionAgentState(
        phase="init",
        step=1,
        steps_taken=0,
        llm_steps_taken=0,
        tool_steps_taken=0,
        max_steps=max_steps,
        max_tool_steps=max_tool_steps,
        status="processing",
        started_at=started_at,
        updated_at=started_at,
        token_budget=token_budget,
        tokens_used=0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        user_id=user_id,
        suggestion_id=suggestion_id,
        action_id=action_id,
        context=ActionAgentContext(
            request_summary=None,
            target_context={"organization_name": None, "project_name": None},
            memory_source_coverage=build_unknown_memory_source_coverage_snapshot(
                evaluated_at=started_at
            ),
            additional_notes=[],
            tool_validation_error_streak=0,
            local_step_counters={},
            read_access_scope=read_access_scope or READ_ACCESS_SCOPE_WORKSPACE,
        ),
        history_by_scope={"S": []},
        goal_conversations={},
        memory_artifact_references=(),
        next_action=None,
        final_output=None,
        errors=[],
        chunks_sent=0,
        run_authority="authoritative",
        skip_persist=False,
        superseded_reason=None,
        superseded_at=None,
    )
    if execution_context is not None:
        manifest_id = execution_context.manifest_id
        execution_session_id = execution_context.execution_session_id
        execution_network_policy = execution_context.execution_network_policy
        action_temp_dir = execution_context.action_temp_dir
        app_runtime_python = execution_context.app_runtime_python
        read_access_scope = execution_context.read_access_scope
    if manifest_id is not None:
        initial_state["manifest_id"] = manifest_id
    if execution_session_id is not None:
        initial_state["execution_session_id"] = execution_session_id
    if execution_network_policy is not None:
        initial_state["execution_network_policy"] = execution_network_policy
    if action_temp_dir is not None:
        initial_state["action_temp_dir"] = action_temp_dir
    if app_runtime_python is not None:
        initial_state["app_runtime_python"] = app_runtime_python
    if read_access_scope is not None:
        initial_state["read_access_scope"] = read_access_scope
    return initial_state

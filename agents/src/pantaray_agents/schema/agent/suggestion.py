"""提案エージェント関連のスキーマ定義"""

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.schema.action_conversation import ActionStatus

from .action_message import ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS
from .base import AgentRequest, AgentResponse, JSONValue

type SuggestionPayload = dict[str, JSONValue]
type SuggestionInteractionContract = Literal["action_offer", "message_only"]


class SuggestionTargetContext(BaseModel):
    """Suggestion が Action へ渡す対象組織・プロジェクト。"""

    model_config = ConfigDict(extra="forbid")

    organization_name: str | None = Field(
        description=(
            "対象 organization 名。Workspace Context の候補を優先し、"
            "個人対象を明示する場合のみ individual を許容する"
        ),
    )
    project_name: str | None = Field(
        description="対象 project 名。判断できない場合は None",
    )


class SuggestionAgentRequest(AgentRequest):
    """提案エージェントのリクエスト。

    入力は短期 Insight の全文と、その Insight が再考を求めた理由。
    Suggestion はこの2つと記憶検索だけを起点に判断し、画像は受け取らない。
    """

    user_id: str = Field(description="対象ユーザーのID")
    suggestion_id: str = Field(description="提案のID（オーケストレーターが生成）")
    short_term_insight: str = Field(description="現在の短期 Insight の全文")
    reconsideration_reason: str = Field(description="短期 Insight が再考を求めた理由")


class SuggestionAgentResponse(AgentResponse):
    """提案エージェントのレスポンス。

    API仕様書4.3章とDBスキーマ（agent_suggestions）に準拠する。

    Attributes:
        suggestion_id (str): 提案のID。
        answer (str): 提案内容（LLMが最終的に出力した1文のゴール表現）。
        thinking (str | None): 提案に至った思考プロセス（best-effort）。
            取得できない場合は None。
        has_suggestion (bool): 提案が存在するかどうか。
        interaction_contract (SuggestionInteractionContract | None):
            提案の操作契約。`message_only` はリアクション不要、`action_offer` は
            承認後に ActionAgent を起動できる。
        user_id (str): 提案対象のユーザーのID。
        created_at (str): レスポンス生成時刻（基底クラスから継承）。
        status (str): 処理ステータス（基底クラスから継承）。
        error (AgentError | None): エラー情報（基底クラスから継承）。
    """

    suggestion_id: str = Field(description="提案のID")
    answer: str = Field(
        default="",
        description=(
            "提案内容（LLMが最終的に出力した1文のゴール表現。提案がない場合は空文字列）"
        ),
    )
    thinking: str | None = Field(
        default=None,
        description=(
            "提案に至った思考プロセス（best-effort）。取得できない場合は None"
        ),
    )
    has_suggestion: bool = Field(default=True, description="提案が存在するかどうか")
    interaction_contract: SuggestionInteractionContract | None = Field(
        default=None,
        description="提案の操作契約。action_offer または message_only",
    )
    suggestion_summary: str | None = Field(
        default=None,
        description="ActionAgent に渡す提案背景・前提・補足情報",
    )
    target_context: SuggestionTargetContext | None = Field(
        default=None,
        description="ActionAgent に渡す対象 organization / project",
    )
    user_id: str = Field(description="提案対象のユーザーのID")


class SuggestionPromptInsightItem(TypedDict, total=False):
    """提案プロンプトに埋め込むインサイト要約項目の型

    - summary: 要約テキスト
    - categories: カテゴリ名の配列
    - confidence: 信頼度（0.0-1.0想定）
    """

    summary: str
    categories: list[str]
    confidence: float


class SuggestionDecidedContent(TypedDict):
    """What the decision run decided to say: the writer's only input."""

    interaction_contract: SuggestionInteractionContract
    key_point: str


class SuggestionExtraction(TypedDict):
    """LLM抽出結果（内部処理用）。

    - thinking: 提案に至った思考プロセス（best-effort、取得できない場合はNone）。
    - answer: 利用者に見せる提案文。判断の時点では空で、文面の LLM 呼び出しが埋める。
    - decided: 判断が決めた伝える中身（提案なしの場合は None）。
    - has_suggestion: 提案有無（True: 提案あり, False: 提案なし）。
    - interaction_contract: 提案の操作契約（提案なしの場合は None）。
    """

    thinking: str | None
    answer: str
    decided: SuggestionDecidedContent | None
    suggestion_summary: str | None
    target_context: SuggestionTargetContext | None
    prompt_text: str
    response_text: str
    has_suggestion: bool
    interaction_contract: SuggestionInteractionContract | None


class SuggestionCandidate(BaseModel):
    """判断中に検討した候補と、その扱い。診断用で利用者には見せない。"""

    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(description="候補の短い説明")
    decision: Literal["suggested", "skipped"] = Field(
        description="提案したか見送ったか"
    )
    reason: str = Field(description="提案・見送りの短い理由")


class SuggestionStructuredOutput(BaseModel):
    """Suggestion LLM の JSON-only 出力。"""

    model_config = ConfigDict(extra="forbid")

    has_suggestion: bool = Field(description="提案の有無")
    interaction_contract: SuggestionInteractionContract | None = Field(
        default=None, description="提案の操作契約"
    )
    key_point: str = Field(
        max_length=ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
        description="利用者に伝える提案の中身すべて（何か・なぜ今か・決め手の事実・未確認の点・承認で行うこと）。提案なしの場合は空文字列",
    )
    details: str | None = Field(
        default=None,
        description="中身を支える事実。書き手には渡さない。提案なしの場合は null",
    )
    suggestion_summary: str | None = Field(
        description=(
            "ActionAgent に引き継ぐ提案背景・前提・補足。提案なしの場合は null"
        ),
    )
    target_context: SuggestionTargetContext | None = Field(
        description="対象 organization / project。判断できない場合は null",
    )
    # Stored with the raw response for diagnosing why a run stayed quiet.
    candidates: list[SuggestionCandidate] = Field(
        default_factory=list, description="検討した候補と扱い（診断用）"
    )


class SuggestionHistoryEntry(BaseModel):
    """提案本文と、取得できたユーザー反応・返信・対応する Action の状態と結果。"""

    answer: str = Field(description="提案内容（LLMが返した最終提案テキスト）")
    created_at: str = Field(description="提案作成時刻（ISO 8601形式）")
    thinking: str | None = Field(
        default=None,
        description="保存済みの思考プロセス",
    )
    user_reaction: Literal["accepted", "rejected"] | None = Field(
        default=None, description="承認・却下。未反応やmessage_onlyの場合はnull"
    )
    user_reply: str | None = Field(
        default=None,
        description=(
            "提案に対するユーザー自身の言葉。承認時は補足だけで、承認のみなら null"
        ),
    )
    action_status: ActionStatus | None = Field(
        default=None, description="対応する Action の現在の状態。Action がなければ null"
    )
    action_result: str | None = Field(
        default=None, description="対応する Action の最後に成功したターンの最終出力"
    )
    action_followups: list[str] = Field(
        default_factory=list,
        description="Action の途中でユーザーが送った指示（古い順、最初の依頼を除く）",
    )


# 提案履歴のリスト型（ユースケースの明確化のための別名）
type SuggestionHistory = list[SuggestionHistoryEntry]


class SuggestionPendingChunk(BaseModel):
    """未受信 suggestion_chunk を再送する際のペイロード。"""

    chunk_index: int = Field(description="チャンクのインデックス（0始まり）")
    content: str = Field(description="チャンク本文")


class SuggestionResumeHint(BaseModel):
    """再開要求に必要なヒント情報。"""

    next_chunk_index: int = Field(description="次に送出予定のチャンク番号")
    last_cursor: str | None = Field(
        default=None, description="最後に送出した位置を示す opaque cursor（任意）"
    )
    kind: Literal["suggestion"] = Field(
        description="resume_session に渡す process kind"
    )


class SuggestionStreamingState(BaseModel):
    """Suggestion ストリームの進行状況。"""

    status: Literal["streaming", "completed", "not_started"] = Field(
        description="ストリーム進行状態"
    )
    process_id: str | None = Field(
        default=None, description="関連するプロセスID（UUIDv4）"
    )
    session_id: str | None = Field(
        default=None, description="WebSocket セッションID（UUIDv4）"
    )
    resume_hint: SuggestionResumeHint | None = Field(
        default=None, description="resume_session に利用できるヒント"
    )
    pending_chunks: list[SuggestionPendingChunk] = Field(
        default_factory=list, description="未配信のsuggestion_chunkリスト"
    )


class SuggestionFinalState(BaseModel):
    """保存済み提案レコードを表す最終状態。"""

    status: str = Field(description="提案処理ステータス")
    has_suggestion: bool | None = Field(
        default=None, description="提案が存在するかどうか"
    )
    answer: str | None = Field(default=None, description="提案本文")
    thinking: str | None = Field(default=None, description="Thinkingの全文")
    suggestion_summary: str | None = Field(
        default=None,
        description="ActionAgent に渡す提案背景・前提・補足情報",
    )
    target_context: SuggestionTargetContext | None = Field(
        default=None,
        description="ActionAgent に渡す対象 organization / project",
    )
    interaction_contract: SuggestionInteractionContract | None = Field(
        default=None, description="提案の操作契約"
    )
    user_reaction: Literal["accepted", "rejected"] | None = Field(
        default=None, description="ユーザー反応。message_only では常に null"
    )
    prompt_name: str | None = Field(default=None, description="使用したプロンプト名")
    prompt_version: str | None = Field(
        default=None, description="使用したプロンプトバージョン"
    )
    created_at: str | None = Field(
        default=None, description="レコード作成時刻（ISO 8601）"
    )
    updated_at: str | None = Field(
        default=None, description="レコード更新時刻（ISO 8601）"
    )
    error: SuggestionPayload | None = Field(
        default=None, description="保存されたエラー情報"
    )
    request_images_count: int | None = Field(
        default=None, description="リクエスト時に送信された画像枚数"
    )
    used_images_count: int | None = Field(
        default=None, description="LLM 推論で実際に使用した画像枚数"
    )


class SuggestionStateResponse(BaseModel):
    """Suggestion 状態参照APIのレスポンス。"""

    suggestion_id: str = Field(description="提案ID")
    user_id: str = Field(description="ユーザーID")
    streaming_state: SuggestionStreamingState = Field(
        description="ストリーミングの進行状況"
    )
    final_state: SuggestionFinalState | None = Field(
        default=None, description="保存済みの最終状態。存在しない場合は None"
    )
    metadata: SuggestionPayload | None = Field(
        default=None, description="追加メタデータ（任意）"
    )

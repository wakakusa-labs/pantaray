"""サーバー → クライアント WebSocketメッセージスキーマ"""

from typing import Literal

from pydantic import BaseModel, Field

from ..agent.base import AgentError, JSONValue
from ..agent.suggestion import (
    SuggestionInteractionContract,
)

type ErrorPayload = dict[str, JSONValue]


class ApprovalBlockerMessage(BaseModel):
    """Action runtime を停止している承認 blocker。"""

    process_id: str = Field(
        description="この blocker で停止している物理 process ID（root または subagent）"
    )
    action_id: str = Field(description="Action ID")
    approval_session_id: str = Field(description="Approval session ID")
    tool_request_id: str = Field(description="Logical tool request ID")
    tool_id: str = Field(description="Tool ID")
    intent_class: str = Field(description="Tool intent class")
    command_summary: dict[str, JSONValue] = Field(description="Command summary")


class SuggestionChunkMessage(BaseModel):
    """Suggestion 本文の live 表示用チャンク。"""

    content: str = Field(description="Suggestion 本文の断片")


class SuggestionReactionCommittedMessage(BaseModel):
    """提案リアクションの DB 確定通知。"""

    suggestion_id: str = Field(description="提案ID（UUIDv4）")
    reaction: Literal["accepted", "rejected"] = Field(
        description="DB に確定したリアクション"
    )
    committed_at: str = Field(description="確定時刻（ISO 8601形式）")


class ActionRequestedMessage(BaseModel):
    """Action request の durable commit 通知。"""

    suggestion_id: str = Field(description="提案ID（UUIDv4）")
    command_id: str = Field(description="Action command ID（UUIDv4）")
    accepted_at: str = Field(description="受理時刻（ISO 8601形式）")
    committed_at: str = Field(description="durable commit 時刻（ISO 8601形式）")


class ProcessStartedMessage(BaseModel):
    """処理開始の通知メッセージ

    WebSocket event: "process_started"

    Attributes:
        kind (Literal["suggestion", "action"]): 処理種別
        process_id (str): 処理ID（UUIDv4）
        suggestion_id (str | None): 提案ID（UUIDv4、任意）
        action_id (str | None): アクションID（UUIDv4、任意）
        command_id (str | None): action command ID（UUIDv4、任意）
        accepted_at (str | None): 受け入れ時刻（任意、アクション実行時のみ、ISO 8601形式）
        started_at (str | None): 実行開始時刻（任意、アクション実行時のみ、ISO 8601形式）
    """

    kind: Literal["suggestion", "action"] = Field(description="処理種別")
    process_id: str = Field(description="処理ID（UUIDv4）")
    suggestion_id: str | None = Field(
        default=None, description="提案ID（UUIDv4、任意）"
    )
    action_id: str | None = Field(
        default=None, description="アクションID（UUIDv4、任意）"
    )
    command_id: str | None = Field(
        default=None, description="action command ID（UUIDv4、任意）"
    )
    accepted_at: str | None = Field(
        default=None,
        description="受け入れ時刻（任意、アクション実行時のみ、ISO 8601形式）",
    )
    started_at: str | None = Field(
        default=None,
        description="実行開始時刻（任意、アクション実行時のみ、ISO 8601形式）",
    )


class SessionStartedMessage(BaseModel):
    """セッション開始の通知メッセージ

    WebSocket event: "session_started"

    初回接続時にサーバーがセッションIDを発行して通知する。
    """

    session_id: str = Field(description="WebSocket接続のセッションID（UUIDv4）")
    issued_at: str = Field(description="セッションID発行時刻（ISO 8601形式）")


class CompletionChunkMessage(BaseModel):
    """Action 最終出力の断片

    WebSocket event: "completion_chunk"

    Attributes:
        content (str): Action 最終出力の断片
    """

    content: str = Field(description="Action 最終出力の断片")


class ProcessPausedMessage(BaseModel):
    """Action process が再開可能な待機状態に入った通知。"""

    kind: Literal["action"] = Field(description="処理種別")
    process_id: str = Field(description="処理ID（UUIDv4）")
    suggestion_id: str | None = Field(
        default=None, description="提案ID（UUIDv4、standalone Actionではなし）"
    )
    action_id: str = Field(description="アクションID（UUIDv4）")
    command_id: str = Field(description="action command ID（UUIDv4）")
    status: Literal["processing"] = Field(description="Action は処理中のまま待機する")
    reason: Literal["approval_pending"] = Field(description="待機理由")
    completed_at: str = Field(description="pause 確定時刻（ISO 8601形式）")
    approval_blockers: list[ApprovalBlockerMessage] = Field(
        description="Action 全体で待機中の承認 blocker 全集合（差分ではなく完全な状態、空も可）"
    )


## 既存のCompletionChunkMessageは上に統合


class ActionProcessCompletedReplayMessage(BaseModel):
    """既存のdurable terminal eventをidentityだけで再送する。"""

    kind: Literal["action"] = Field(description="処理種別")
    process_id: str = Field(description="処理ID（UUIDv4）")
    suggestion_id: str | None = Field(
        default=None, description="提案ID（UUIDv4、standalone Actionではなし）"
    )
    action_id: str = Field(description="アクションID（UUIDv4）")
    command_id: str = Field(description="action command ID（UUIDv4）")
    status: Literal["success", "error", "canceled"] = Field(
        description="durable terminal status"
    )


class PhysicalActionRunCompletedMessage(BaseModel):
    """共有 Action を終端しない物理 run の完了通知。"""

    kind: Literal["action"] = Field(description="処理種別")
    physical_run_only: Literal[True] = Field(description="物理 run 限定の終端")
    process_id: str = Field(description="終端した物理 process ID")
    status: Literal["canceled"] = Field(description="物理 run の終端状態")


class ScreenCaptureRequestedMessage(BaseModel):
    """capture_screen が 1 枚の撮影を desktop client に依頼する。

    WebSocket event: "screen_capture_requested"

    desktop client 専用の制御メッセージであり、renderer には転送されない。
    撮影可否（Screen Recording 権限・録画フィルタ）は desktop client 側だけが
    判断できるため、ここには撮影ポリシーを一切載せない。
    """

    kind: Literal["action"] = Field(description="処理種別")
    process_id: str = Field(description="依頼元の物理 process ID")
    action_id: str = Field(description="依頼元 Action ID")
    tool_request_id: str = Field(description="承認済みツール要求の論理 ID")
    capture_request_id: str = Field(description="1 回だけ消費できる撮影要求 ID")
    app_name: str = Field(description="承認された撮影対象のアプリ名")


class ProcessCompletedMessage(BaseModel):
    """処理完了の通知メッセージ

    WebSocket event: "process_completed"

    Attributes:
        kind (Literal["suggestion"]): 処理種別
        process_id (str): 処理ID（UUIDv4）
        has_suggestion (bool | None): 提案が存在するかどうか（任意）
        suggestion_id (str | None): 提案ID（UUIDv4、任意）
        status (str): 処理ステータス
    """

    kind: Literal["suggestion"] = Field(description="処理種別")
    process_id: str = Field(description="処理ID（UUIDv4）")
    has_suggestion: bool | None = Field(
        default=None, description="提案が存在するかどうか（任意）"
    )
    suggestion_id: str | None = Field(
        default=None, description="提案ID（UUIDv4、任意）"
    )
    interaction_contract: SuggestionInteractionContract | None = Field(
        default=None, description="提案の操作契約（suggestion 完了時のみ）"
    )
    status: str = Field(description="Suggestion 処理ステータス")


class SessionResumedMessage(BaseModel):
    """セッション再開成功の通知メッセージ

    WebSocket event: "session_resumed"
    """

    resumed_from_chunk: int = Field(
        description="再開位置（次に送るチャンクのインデックス）"
    )
    missing_chunks: list[int] = Field(
        default_factory=list, description="欠落していたチャンクのインデックス群"
    )


class SessionExpiredMessage(BaseModel):
    """セッション再開失敗の通知メッセージ

    WebSocket event: "session_expired"
    """

    reason: str = Field(description="失敗理由（例: session_timeout）")
    max_session_age_seconds: int = Field(description="セッションの最大許容経過秒数")


class ErrorMessage(BaseModel):
    """エラー発生の通知メッセージ

    WebSocket event: "error"

    Attributes:
        error_type (str): エラーの種類
        error_code (str): エラーコード
        error_message (str | None): エラーメッセージ
        error_details (ErrorPayload | None): エラーの詳細情報（JSON互換）
        severity (str): 重要度
        metadata (ErrorPayload | None): 追加のメタデータ（JSON互換）
    """

    error_type: str = Field(description="Canonical error category defined by ErrorType")
    error_code: str = Field(description="エラーコード")
    error_message: str | None = Field(default=None, description="エラーメッセージ")
    error_details: ErrorPayload | None = Field(
        default=None, description="エラーの詳細情報（JSON互換）"
    )
    severity: str = Field(description="重要度: info, warning, error, critical")
    metadata: ErrorPayload | None = Field(
        default=None, description="追加のメタデータ（JSON互換）"
    )

    @classmethod
    def from_agent_error(cls, agent_error: AgentError) -> "ErrorMessage":
        """AgentErrorからErrorMessageを生成するファクトリメソッド"""
        return cls(
            error_type=agent_error.error_type,
            error_code=agent_error.error_code,
            error_message=agent_error.error_message,
            error_details=agent_error.error_details,
            severity=agent_error.severity,
            metadata=agent_error.metadata,
        )

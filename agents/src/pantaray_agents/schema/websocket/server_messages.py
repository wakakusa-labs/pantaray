"""サーバー → クライアント WebSocketメッセージスキーマ"""

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..agent.base import AgentError, JSONValue
from ..agent.suggestion import (
    SuggestionInteractionContract,
)
from ..overlay_bootstrap import SuggestionTodoView
from ..user_browser import UserBrowserId

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
    # Snapshots stored before the approval review have no reason.
    review_reason: str | None = Field(
        default=None, description="Why the approval review left this to the user"
    )


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
    reason: str | None = Field(
        default=None, description="見送りと一緒に記録したユーザーの理由。なければ null"
    )


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


class SecretPathRule(BaseModel):
    """A file holding secrets: a path matching ``withheld`` and not ``excepted``.

    POSIX EREs over an absolute path from ``tools.files.secret_paths``, which
    JavaScript reads as they are.
    """

    model_config = ConfigDict(extra="forbid")

    withheld: str = Field(description="ERE of the withheld paths")
    excepted: str | None = Field(description="ERE of the paths it leaves loadable")


# The longest `wait` a browser operation may ask for; Electron's budget for a
# `wait` is derived from it.
BROWSER_MAX_WAIT_SECONDS: Final = 10


class _BrowserModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


BrowserRef = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
_BrowserId = Annotated[str, Field(min_length=1, pattern=r"\S")]
# A host name or an IP literal, IPv6 without brackets, as the command network
# reports one: never a scheme, user, port, path, query or fragment.
_HOST = r"^(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?|[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*)$"
BrowserHost = Annotated[str, Field(pattern=_HOST)]


class BrowserBackOperation(_BrowserModel):
    action: Literal["back"]


class BrowserSnapshotOperation(_BrowserModel):
    action: Literal["snapshot"]
    # None: the whole page; a ref: only that element's subtree.
    ref: BrowserRef | None = None


class BrowserClickOperation(_BrowserModel):
    action: Literal["click"]
    ref: BrowserRef


class BrowserHoverOperation(_BrowserModel):
    action: Literal["hover"]
    ref: BrowserRef


class BrowserTypeOperation(_BrowserModel):
    action: Literal["type"]
    ref: BrowserRef
    text: str
    submit: bool = False


class BrowserPressOperation(_BrowserModel):
    action: Literal["press"]
    # One key or a chord such as "Control+A".
    key: str = Field(min_length=1, pattern=r"\S")


class BrowserSelectOperation(_BrowserModel):
    action: Literal["select"]
    ref: BrowserRef
    values: list[str] = Field(min_length=1)


class BrowserScrollOperation(_BrowserModel):
    action: Literal["scroll"]
    # None: the page itself.
    ref: BrowserRef | None = None
    direction: Literal["up", "down", "left", "right"]


class BrowserWaitOperation(_BrowserModel):
    # Wait until ``text`` shows, or for ``seconds``: exactly one of them.
    action: Literal["wait"]
    text: str | None = Field(default=None, min_length=1, pattern=r"\S")
    seconds: float | None = Field(default=None, gt=0, le=BROWSER_MAX_WAIT_SECONDS)

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> Self:
        if (self.text is None) == (self.seconds is None):
            raise ValueError("wait takes exactly one of text and seconds")
        return self


class BrowserConsoleOperation(_BrowserModel):
    action: Literal["console"]


class BrowserWebTarget(_BrowserModel):
    kind: Literal["web"]
    # No blank, backslash or control character, which Chromium's URL parser
    # drops or rewrites. Only Electron parses the URL: it reports the host it
    # opened in the answer's ``opened_host``.
    url: str = Field(pattern=r"^https?://[^\s\\\x00-\x1f\x7f]+$")


class BrowserFileTarget(_BrowserModel):
    # A local document ``read`` may open, by its real path.
    kind: Literal["file"]
    path: str = Field(pattern=r"^/")


class BrowserNavigateOperation(_BrowserModel):
    action: Literal["navigate"]
    target: Annotated[BrowserWebTarget | BrowserFileTarget, Field(discriminator="kind")]


class BrowserScreenshotOperation(_BrowserModel):
    # A PNG of the viewport, of one element (``ref``), or of the full page.
    action: Literal["screenshot"]
    ref: BrowserRef | None = None
    full_page: Literal[True] | None = None

    @model_validator(mode="after")
    def _one_region(self) -> Self:
        if self.ref is not None and self.full_page is not None:
            raise ValueError("screenshot takes at most one of ref and full_page")
        return self


class BrowserPrintOperation(_BrowserModel):
    # Print the page to a PDF, the artifact of the answer.
    action: Literal["print"]


BrowserOperation = Annotated[
    BrowserNavigateOperation
    | BrowserBackOperation
    | BrowserSnapshotOperation
    | BrowserClickOperation
    | BrowserHoverOperation
    | BrowserTypeOperation
    | BrowserPressOperation
    | BrowserSelectOperation
    | BrowserScrollOperation
    | BrowserWaitOperation
    | BrowserScreenshotOperation
    | BrowserPrintOperation
    | BrowserConsoleOperation,
    Field(discriminator="action"),
]


class BrowserPagePolicy(_BrowserModel):
    """What the session's page may load, sent with every operation; the session
    applies the latest. The roots and secret rules are those ``read`` applies."""

    allow_network: bool
    file_roots: list[str]
    private_roots: list[str]
    private_root_exceptions: list[str]
    secret_paths: list[SecretPathRule]
    # External hosts this run may send writes to.
    write_hosts: list[BrowserHost]


class BrowserUserTarget(_BrowserModel):
    """The operation runs in a tab of the session's own in the user's browser."""

    browser: UserBrowserId
    # The Action whose chat shows that the user is asked to allow the connection.
    action_id: _BrowserId
    # Hosts the user approved this run to open in that tab.
    read_hosts: list[BrowserHost]


class BrowserOperationRequestedMessage(_BrowserModel):
    """WebSocket event "browser_operation_requested", for the desktop client only
    and never forwarded to the renderer; answered by ``POST /local/action-browser``."""

    operation_request_id: _BrowserId = Field(description="Consumed once")
    session_id: _BrowserId = Field(description="Browser session the operation runs in")
    operation: BrowserOperation = Field(
        description="The validated action, local paths resolved to real paths"
    )
    policy: BrowserPagePolicy
    user_browser: BrowserUserTarget | None = Field(
        description="The user's browser to run in; null for the app's hidden page"
    )

    @model_validator(mode="after")
    def _user_browser_is_web_only(self) -> Self:
        # The user's browser opens no local file and prints nothing, and it is
        # never used with the command network off.
        if self.user_browser is None:
            return self
        if not self.policy.allow_network:
            raise ValueError("the user's browser needs the network on")
        if isinstance(self.operation, BrowserPrintOperation):
            raise ValueError("the user's browser does not print")
        if isinstance(self.operation, BrowserNavigateOperation) and isinstance(
            self.operation.target, BrowserFileTarget
        ):
            raise ValueError("the user's browser opens no local file")
        return self


class BrowserSessionCloseRequestedMessage(_BrowserModel):
    """WebSocket event "browser_session_close_requested": idempotent, and Electron
    refuses any later operation naming the id."""

    session_id: _BrowserId = Field(description="Browser session to close")


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
    artifacts: list[str] = Field(
        description=(
            "Files the Suggestion made, relative to its artifact folder; empty "
            "unless it succeeded with files"
        ),
    )
    todo: SuggestionTodoView | None = Field(
        description="The TODO a shown Suggestion became; null for any other run"
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

"""クライアント → サーバー WebSocketメッセージスキーマ"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic.types import UUID4

from ..agent.action_message import (
    ACTION_MESSAGE_MAX_FILES,
    ACTION_MESSAGE_MAX_IMAGES,
    ACTION_MESSAGE_MAX_PROJECT_REFS,
    ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS,
    ActionProjectRef,
    FileAttachmentInput,
)
from ..agent.image import ImageInput

LanguageCode = Literal["en", "ja"]


class ExecuteActionMessage(BaseModel):
    """提案に基づきアクションを実行するメッセージ

    WebSocket event: "execute_action"

    Attributes:
        suggestion_id (str): 実行対象の提案ID（UUIDv4、必須）
    """

    suggestion_id: str = Field(description="実行対象の提案ID（UUIDv4、必須）")
    command_id: UUID4 = Field(description="冪等な action command ID（UUIDv4、必須）")
    approval_mode: Literal["prompt_each_time", "always_allow"]
    images: tuple[ImageInput, ...] = Field(max_length=ACTION_MESSAGE_MAX_IMAGES)
    language: LanguageCode | None = Field(
        default=None,
        description="UI言語（'en' または 'ja'）。未指定時はサーバー側で解決する。",
    )
    supplement: str | None = Field(
        default=None,
        max_length=ACTION_MESSAGE_SUPPLEMENT_MAX_CODEPOINTS,
        description="提案承認時にUSERが追加する条件（任意）",
    )
    supplement_project_refs: tuple[ActionProjectRef, ...] = Field(
        default=(),
        max_length=ACTION_MESSAGE_MAX_PROJECT_REFS,
        description="Workspace projects named in the trimmed supplement",
    )
    files: tuple[FileAttachmentInput, ...] = Field(
        default=(),
        max_length=ACTION_MESSAGE_MAX_FILES,
        description="Documents attached beside the supplement",
    )


class DismissSuggestionMessage(BaseModel):
    """提案を「いまは見送る（dismiss）」メッセージ。

    WebSocket event: "dismiss_suggestion"

    NOTE:
        DB側の `user_reaction` は後方互換のため従来の `"rejected"` を継続利用する。

    Attributes:
        suggestion_id (str): 対象の提案ID（UUIDv4、必須）
        reason (str | None): 見送り理由（任意）
    """

    suggestion_id: str = Field(description="対象の提案ID（UUIDv4、必須）")
    reason: str | None = Field(default=None, description="見送り理由（任意）")


class RejectSuggestionMessage(DismissSuggestionMessage):
    """提案を拒否するメッセージ（後方互換）。

    WebSocket event: "reject_suggestion"（非推奨）
    """


class StopProcessMessage(BaseModel):
    """進行中の処理を中断するメッセージ

    WebSocket event: "stop_process"

    Attributes:
        process_id (str): 中断する処理のID（UUIDv4、必須）
    """

    process_id: str = Field(description="中断する処理のID（UUIDv4、必須）")


class ResumeSessionMessage(BaseModel):
    """セッション再開の要求メッセージ

    WebSocket event: "resume_session"

    Attributes:
        session_id (str): 再開対象のセッションID（UUIDv4）
        last_cursor (str | None): 最後に受信した位置を示す opaque cursor
        process_id (str): 対象のプロセスID（UUIDv4）
        last_chunk_index (int): 最後に受信したチャンクのインデックス
        kind ("suggestion" | "action"): 再開対象のプロセス種別
    """

    session_id: str = Field(description="再開対象のセッションID（UUIDv4）")
    last_cursor: str | None = Field(
        default=None,
        description="最後に受信した位置を示す opaque cursor。未指定時は先頭から再開する。",
    )
    process_id: str = Field(description="対象のプロセスID（UUIDv4）")
    last_chunk_index: int = Field(description="最後に受信したチャンクのインデックス")
    kind: Literal["suggestion", "action"] = Field(description="再開対象のプロセス種別")
    suggestion_id: str | None = Field(
        default=None,
        description="Action resume では参照しない。対象 suggestion ID は durable Action authority から解決する。",
    )
    action_id: str | None = Field(
        default=None,
        description="Action resume を選択する action ID。durable Action authority と完全一致する必要がある。",
    )
    command_id: str | None = Field(
        default=None,
        description="Action resume では参照しない。command ID は durable Action authority から解決する。",
    )


class AckEventMessage(BaseModel):
    """WSイベントの受信確認メッセージ。

    WebSocket event: "ack_event"
    """

    session_id: str = Field(description="ACK対象のセッションID")
    process_id: str = Field(description="ACK対象のプロセスID")
    event_id: str = Field(description="ACKするイベントID")

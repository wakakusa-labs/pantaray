"""WebSocket オーケストレーションの基盤クラス。

`WSOrchestrationHandler` から共通の低レベル処理（送信・セッションストア連携）を切り出す。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import WebSocket
from pydantic import BaseModel
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.error_meta import (
    normalize_error_meta_kind,
)
from pantaray_agents.repositories.suggestion_runtime_results import (
    AppendProcessEventResult,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    ActionRequestedMessage,
    ErrorMessage,
    SuggestionReactionCommittedMessage,
)
from pantaray_agents.utils.metrics import (
    record_action_event_sent,
    record_suggestion_event_sent,
)

logger = logging.getLogger(__name__)

type EventMeta = dict[str, JSONValue]
type OutboundEnvelope = dict[str, JSONValue]

_PERSISTED_PUBLIC_EVENTS = {
    OutboundEvent.PROCESS_STARTED.value,
    OutboundEvent.SUGGESTION_CHUNK.value,
    OutboundEvent.SUGGESTION_REACTION_COMMITTED.value,
    OutboundEvent.ACTION_REQUESTED.value,
    OutboundEvent.PROCESS_COMPLETED.value,
    OutboundEvent.PROCESS_PAUSED.value,
    OutboundEvent.ERROR.value,
}

_REQUIRED_ERROR_META_KEYS = ("kind", "stage", "error_code")
_PROCESS_ERROR_KINDS = frozenset({"action", "suggestion"})
_NON_PERSISTED_ERROR_KINDS = frozenset({"session", "protocol"})


class PublicProcessEventPersistenceError(RuntimeError):
    """公開 process event を durable に保存できない場合の明示的な失敗。"""


def _classify_error_persistence(
    *,
    event_name: str,
    meta: EventMeta | None,
) -> bool:
    if meta is None:
        raise PublicProcessEventPersistenceError(
            f"{event_name} requires error meta for durable persistence classification"
        )
    raw_kind = meta.get("kind")
    kind = normalize_error_meta_kind(raw_kind)
    if kind in _PROCESS_ERROR_KINDS:
        return True
    if kind in _NON_PERSISTED_ERROR_KINDS:
        return False
    raise PublicProcessEventPersistenceError(
        f"{event_name} received unsupported error kind={kind!r}"
    )


def _validate_public_error_meta(
    *,
    event_name: str,
    meta: EventMeta | None,
) -> None:
    if meta is None:
        raise PublicProcessEventPersistenceError(f"{event_name} requires error meta")
    for required_key in _REQUIRED_ERROR_META_KEYS:
        _extract_non_empty_string(
            value=meta.get(required_key),
            field_name=required_key,
            event_name=event_name,
        )
    kind = normalize_error_meta_kind(meta.get("kind"))
    if kind in _PROCESS_ERROR_KINDS:
        _extract_non_empty_string(
            value=meta.get("suggestion_id"),
            field_name="suggestion_id",
            event_name=event_name,
        )


def _normalize_json_meta(meta: EventMeta | None) -> EventMeta | None:
    if not meta:
        return None
    normalized: EventMeta = {}
    for key, value in meta.items():
        if value is None:
            normalized[key] = None
            continue
        if isinstance(value, (str, int, float, bool, dict, list)):
            normalized[key] = value
            continue
        normalized[key] = str(value)
    return normalized


def _normalize_json_data(model: BaseModel) -> dict[str, JSONValue]:
    raw = model.model_dump(mode="json")
    normalized: dict[str, JSONValue] = {}
    for key, value in raw.items():
        if value is None:
            normalized[key] = None
        elif isinstance(value, (str, int, float, bool, dict, list)):
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


def _normalize_json_mapping(data: dict[str, JSONValue]) -> dict[str, JSONValue]:
    normalized: dict[str, JSONValue] = {}
    for key, value in data.items():
        if value is None:
            normalized[key] = None
        elif isinstance(value, (str, int, float, bool, dict, list)):
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


def _extract_suggestion_id(
    *,
    data: dict[str, JSONValue],
    meta: EventMeta | None,
) -> str | None:
    meta_value = meta.get("suggestion_id") if meta else None
    if isinstance(meta_value, str) and meta_value.strip():
        return meta_value.strip()
    data_value = data.get("suggestion_id")
    if isinstance(data_value, str) and data_value.strip():
        return data_value.strip()
    return None


def _extract_non_empty_string(
    *,
    value: JSONValue | None,
    field_name: str,
    event_name: str,
) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise PublicProcessEventPersistenceError(
        f"{event_name} requires non-empty {field_name} for durable persistence"
    )


class BaseWSHandler:
    """WS 送信・セッションストア連携の共通基盤。

    上位のハンドラはこのクラスを継承して使用する。
    """

    def __init__(
        self,
        websocket: WebSocket,
        session_store: InMemorySessionStore,
        session_id: str,
        user_id: str,
    ) -> None:
        self.websocket = websocket
        self.session_store = session_store
        self.session_id = session_id
        self.user_id = user_id
        self._is_closed: bool = False

    async def _send(
        self,
        event: str,
        model: BaseModel,
        *,
        process_id: str | None = None,
        meta: EventMeta | None = None,
        event_id: str | None = None,
        store_in_session_store: bool = True,
        chunk_index: int | None = None,
        persist_public_event: bool = True,
        persisted_sequence_override: int | None = None,
        persisted_data_override: dict[str, JSONValue] | None = None,
    ) -> bool:
        """WS へイベントを送信する共通ユーティリティ。失敗時は安全に無視する。

        サブクラス側で以下の属性/フィールドを用意しておく前提:
        - `_action_last_event_at`, `_suggestion_last_event_at`
        - `record_action_event_sent`, `record_suggestion_event_sent`
        - `session_store.append_stream_chunk`, `session_store.record_event_meta`
        """
        # すでにクローズ済みなら送信しない
        if self._is_closed or self.websocket.client_state != WebSocketState.CONNECTED:
            try:
                logger.warning(
                    "WS send skipped: event=%s is_closed=%s client_state=%s",
                    event,
                    self._is_closed,
                    self.websocket.client_state,
                )
            except Exception:
                pass
            return False

        resolved_event_id = str(event_id) if event_id else str(uuid.uuid4())
        normalized_meta = _normalize_json_meta(meta)
        normalized_data = _normalize_json_data(model)
        normalized_persisted_data = (
            _normalize_json_mapping(persisted_data_override)
            if persisted_data_override is not None
            else normalized_data
        )
        meta_kind = (normalized_meta or {}).get("kind")
        if persisted_sequence_override is not None and persist_public_event:
            raise ValueError(
                "persisted_sequence_override cannot be combined with persistence"
            )
        if persisted_sequence_override is not None:
            persisted_sequence = persisted_sequence_override
        elif persist_public_event:
            persisted_sequence = await self._persist_public_process_event_if_needed(
                event=event,
                event_id=resolved_event_id,
                data=normalized_persisted_data,
                meta=normalized_meta,
            )
        else:
            persisted_sequence = None

        # サブクラスがこれらの属性を持っていれば、最終イベント時刻・メトリクスを更新する
        now = datetime.now(UTC)
        try:
            if process_id is not None:
                if meta_kind == "action" and hasattr(self, "_action_last_event_at"):
                    self._action_last_event_at[process_id] = now  # type: ignore[attr-defined]
                elif meta_kind == "suggestion" and hasattr(
                    self, "_suggestion_last_event_at"
                ):
                    self._suggestion_last_event_at[process_id] = now  # type: ignore[attr-defined]
            if meta_kind == "action":
                record_action_event_sent(event)
            elif meta_kind == "suggestion":
                record_suggestion_event_sent(event)
        except Exception:
            # メトリクス更新失敗は致命ではない
            pass

        if store_in_session_store:
            # completion_chunk の場合はチャンクとして保存し、それ以外はメタのみ
            if process_id is not None and event in {
                OutboundEvent.SUGGESTION_CHUNK.value,
            }:
                # サブクラスの session_store 実装に委譲
                self.session_store.append_stream_chunk(
                    self.session_id,
                    process_id,
                    resolved_event_id,
                    event_name=event,
                    msg=model,
                )  # type: ignore[arg-type]
            else:
                self.session_store.record_event_meta(
                    self.session_id,
                    resolved_event_id,
                    process_id,
                    chunk_index,
                    event_name=event,
                    meta=meta or None,
                )

        try:
            payload: OutboundEnvelope = {
                "event_id": resolved_event_id,
                "event": event,
                "data": normalized_data,
            }
            if normalized_meta:
                payload["meta"] = normalized_meta
            if persisted_sequence is not None:
                payload["sequence"] = persisted_sequence
            logger.info("WS sending: event=%s meta=%r", event, meta)
            await self.websocket.send_json(payload)
            logger.info("WS sent successfully: event=%s", event)
            return True
        except Exception as exc:  # noqa: BLE001
            try:
                logger.error("WS send failed: event=%s error=%s", event, exc)
            except Exception:
                pass
            self._is_closed = True
            return False

    async def _persist_public_process_event_if_needed(
        self,
        *,
        event: str,
        event_id: str,
        data: dict[str, JSONValue],
        meta: EventMeta | None,
    ) -> int | None:
        if deps.is_mock_mode():
            return None
        if event not in _PERSISTED_PUBLIC_EVENTS:
            return None
        if event == OutboundEvent.ERROR.value and not _classify_error_persistence(
            event_name=event,
            meta=meta,
        ):
            return None
        suggestion_id = _extract_suggestion_id(data=data, meta=meta)
        if not suggestion_id:
            raise PublicProcessEventPersistenceError(
                f"{event} requires suggestion_id for durable persistence"
            )
        if event == OutboundEvent.ERROR.value:
            normalized_meta = meta or {}
            for required_key in _REQUIRED_ERROR_META_KEYS:
                _extract_non_empty_string(
                    value=normalized_meta.get(required_key),
                    field_name=required_key,
                    event_name=event,
                )
        repo_getter_name = "_get_action_state_repository"
        repo_getter = getattr(self, repo_getter_name, None)
        if not callable(repo_getter):
            raise PublicProcessEventPersistenceError(
                "Suggestion repository getter is unavailable for process event persistence"
            )
        repo = await repo_getter()
        if repo is None:
            raise PublicProcessEventPersistenceError(
                "Suggestion repository is unavailable"
            )
        action_id_value = None
        if meta:
            raw_action_id = meta.get("action_id")
            if isinstance(raw_action_id, str) and raw_action_id.strip():
                action_id_value = raw_action_id.strip()
        payload: dict[str, JSONValue] = {"data": data}
        if meta:
            payload["meta"] = meta
        result = await repo.append_process_event_and_project_history(
            event_id=event_id,
            suggestion_id=suggestion_id,
            user_id=str(self.user_id),
            event_name=event,
            payload=payload,
            action_id=action_id_value,
        )
        if result.error or result.data is None:
            raise PublicProcessEventPersistenceError(
                result.error
                or "Failed to persist process event before websocket delivery"
            )
        append_result: AppendProcessEventResult = result.data
        return append_result.sequence

    async def send_event(
        self,
        event: str,
        model: BaseModel,
        *,
        process_id: str | None = None,
        meta: EventMeta | None = None,
        persist_public_event: bool = True,
        persisted_sequence_override: int | None = None,
    ) -> bool:
        return await self._send(
            event,
            model,
            process_id=process_id,
            meta=meta,
            persist_public_event=persist_public_event,
            persisted_sequence_override=persisted_sequence_override,
        )

    async def send_error(
        self,
        message: ErrorMessage,
        *,
        process_id: str | None = None,
        meta: EventMeta | None = None,
        persist_public_event: bool = True,
        persisted_sequence_override: int | None = None,
    ) -> bool:
        payload_meta: EventMeta = dict(meta) if meta else {}
        if process_id is not None and "process_id" not in payload_meta:
            payload_meta["process_id"] = process_id
        _validate_public_error_meta(
            event_name=OutboundEvent.ERROR.value,
            meta=payload_meta or None,
        )
        return await self._send(
            OutboundEvent.ERROR.value,
            message,
            process_id=process_id,
            meta=payload_meta or None,
            persist_public_event=persist_public_event,
            persisted_sequence_override=persisted_sequence_override,
        )

    async def emit_suggestion_reaction_committed(
        self,
        *,
        suggestion_id: str,
        reaction: str,
        committed_at: str,
        reason: str | None,
        meta: EventMeta | None = None,
        persist_public_event: bool = True,
        persisted_sequence_override: int | None = None,
    ) -> bool:
        payload_meta: EventMeta = dict(meta) if meta else {}
        if "suggestion_id" not in payload_meta:
            payload_meta["suggestion_id"] = suggestion_id
        return await self._send(
            OutboundEvent.SUGGESTION_REACTION_COMMITTED.value,
            SuggestionReactionCommittedMessage(
                suggestion_id=suggestion_id,
                reaction=reaction,  # type: ignore[arg-type]
                committed_at=committed_at,
                reason=reason,
            ),
            meta=payload_meta,
            persist_public_event=persist_public_event,
            persisted_sequence_override=persisted_sequence_override,
        )

    async def emit_action_requested(
        self,
        *,
        suggestion_id: str,
        command_id: str,
        accepted_at: str,
        committed_at: str,
        meta: EventMeta | None = None,
        persist_public_event: bool = True,
        persisted_sequence_override: int | None = None,
    ) -> bool:
        return await self.send_event(
            OutboundEvent.ACTION_REQUESTED.value,
            ActionRequestedMessage(
                suggestion_id=suggestion_id,
                command_id=command_id,
                accepted_at=accepted_at,
                committed_at=committed_at,
            ),
            meta=meta,
            persist_public_event=persist_public_event,
            persisted_sequence_override=persisted_sequence_override,
        )

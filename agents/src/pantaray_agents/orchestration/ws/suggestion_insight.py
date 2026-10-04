"""Suggestion の dismiss とセッション再開を担う Mixin。

NOTE:
    既存の後方互換のため、WSイベントとしての "reject" 系も受け入れるが、
    UI/ドキュメント上は "dismiss" を主語に統一する。
"""

from __future__ import annotations

import logging
from typing import Literal

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.orchestration.ws.error_meta import (
    build_session_error_meta,
)
from pantaray_agents.orchestration.ws.handler_shared import (
    SuggestionStatusPersistenceError,
)
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    SessionExpiredMessage,
    SessionResumedMessage,
)
from pantaray_agents.utils.metrics import (
    record_ws_dependency_fail,
    record_ws_resume_expired,
    record_ws_resume_invalid_cursor,
    record_ws_resume_requested,
    record_ws_resume_succeeded,
    record_ws_resume_too_many_missing_chunks,
)
from pantaray_agents.utils.public_error import public_ws_error

logger = logging.getLogger(__name__)


class SuggestionInsightMixin:
    """Suggestion dismiss とセッション再開を制御する Mixin。"""

    # 実体は WSOrchestrationHandler.__init__ が tunables から設定する。
    _session_max_age_seconds: int
    _session_max_resume_missing_chunks: int

    async def resume_session(
        self,
        session_id: str,
        process_id: str,
        last_cursor: str | None,
        last_chunk_index: int | None,
        kind: Literal["suggestion", "action"],
    ) -> None:
        """Suggestion 用のセッション再開処理。"""
        _ = kind
        record_ws_resume_requested("suggestion")
        sess = self.session_store.get(session_id)
        max_age = self._session_max_age_seconds
        # session_id の存在有無や所有関係は外部へ漏らさない（存在判定オラクル回避）。
        if not sess or not self.session_store.has_active_session(
            session_id, user_id=str(self.user_id)
        ):
            record_ws_resume_expired("suggestion", reason="resume_data_unavailable")
            await self._send(
                OutboundEvent.SESSION_EXPIRED.value,
                SessionExpiredMessage(
                    reason="resume_data_unavailable",
                    max_session_age_seconds=max_age,
                ),
            )
            return

        ev_meta = sess.events.get(last_cursor) if last_cursor else None
        ev_index = ev_meta.get("chunk_index") if ev_meta is not None else None
        # last_cursor が別プロセスに紐づく場合は不正として扱う（存在判定オラクル回避: エラー内容は固定）
        if (
            ev_meta is not None
            and ev_meta.get("process_id")
            and ev_meta.get("process_id") != process_id
        ):
            record_ws_resume_invalid_cursor()
            await self.send_error(
                public_ws_error(
                    error_code="WS_RESUME_INVALID_CURSOR",
                    request_id=getattr(self, "session_id", None),
                    error_type=ErrorType.VALIDATION_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="resume_session parameters are invalid",
                ),
                process_id=process_id,
                meta=build_session_error_meta(
                    stage="resume_invalid_cursor",
                    error_code="WS_RESUME_INVALID_CURSOR",
                    process_id=process_id,
                ),
            )
            return
        proc = sess.processes.get(process_id)
        if not proc:
            record_ws_resume_succeeded("suggestion", missing_chunks_count=0)
            await self._send(
                OutboundEvent.SESSION_RESUMED.value,
                SessionResumedMessage(
                    resumed_from_chunk=(last_chunk_index or 0) + 1,
                    missing_chunks=[],
                ),
            )
            return
        # セッションストア容量制限等により、再開用のチャンク保持ができなかった場合は fail-closed。
        if not bool(getattr(proc, "resume_available", True)):
            record_ws_resume_expired("suggestion", reason="resume_data_unavailable")
            await self._send(
                OutboundEvent.SESSION_EXPIRED.value,
                SessionExpiredMessage(
                    reason="resume_data_unavailable",
                    max_session_age_seconds=max_age,
                ),
            )
            return
        next_idx = proc.next_chunk_index
        if isinstance(ev_index, int) and 0 <= ev_index < next_idx:
            resumed_from = ev_index + 1
        else:
            resumed_from = min(max((last_chunk_index or 0) + 1, 0), next_idx)
        missing = list(range(resumed_from, next_idx))
        # missing_chunks 上限（メモリDoS耐性）
        max_missing = self._session_max_resume_missing_chunks
        if max_missing > 0 and len(missing) > max_missing:
            record_ws_resume_too_many_missing_chunks()
            await self.send_error(
                public_ws_error(
                    error_code="WS_RESUME_TOO_MANY_MISSING_CHUNKS",
                    request_id=getattr(self, "session_id", None),
                    error_type=ErrorType.VALIDATION_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="resume_session missing chunk count exceeds server limit",
                ),
                process_id=process_id,
                meta=build_session_error_meta(
                    stage="resume_too_many_missing_chunks",
                    error_code="WS_RESUME_TOO_MANY_MISSING_CHUNKS",
                    process_id=process_id,
                ),
            )
            return
        # 再開したプロセスは relay の発見対象から外す（再接続時の二重配信防止）。
        # 先に relay tick が attach していれば、その結果 relay がこのセッションへ
        # 完了を届けるので、再開側では改めて追わない。
        already_relayed = process_id in self._relayed_suggestion_processes
        self.mark_suggestion_process_relayed(process_id)
        # 再開後の stop_process / ack_event 整合のため、現在セッションへプロセスメタを同期する
        try:
            self._sync_process_metadata_to_current_session(
                process_id,
                suggestion_id=getattr(proc, "suggestion_id", None),
                action_id=getattr(proc, "action_id", None),
                kind=getattr(proc, "kind", None),
            )
        except Exception as exc:  # noqa: BLE001
            record_ws_dependency_fail(
                dependency="session_store", operation="set_process_metadata"
            )
            logger.warning(
                "resume_session(suggestion): set_process_metadata failed. session_id=%s process_id=%s error=%s",
                getattr(self, "session_id", None),
                process_id,
                exc,
                exc_info=True,
            )
        record_ws_resume_succeeded("suggestion", missing_chunks_count=len(missing))
        await self._send(
            OutboundEvent.SESSION_RESUMED.value,
            SessionResumedMessage(
                resumed_from_chunk=resumed_from, missing_chunks=missing
            ),
        )
        # Replay
        for event_name, msg, meta in self.session_store.replay_missing_chunks(
            session_id, process_id, resumed_from
        ):
            await self._send(
                event_name,
                msg,
                process_id=process_id,
                meta=meta,
            )
        if not already_relayed and proc.completed_at is None and proc.suggestion_id:
            self.follow_resumed_suggestion_process(process_id, proc.suggestion_id)

    async def handle_dismiss(self, suggestion_id: str) -> None:
        rejected_at = now_utc_iso()
        try:
            row = await self._persist_suggestion_status(
                suggestion_id,
                user_reaction="rejected",
                action_status="idle",
                rejected_at=rejected_at,
                accepted_at=None,
            )
        except SuggestionStatusPersistenceError as exc:
            await self._send_suggestion_persistence_error(
                suggestion_id=suggestion_id,
                failure_kind="persist_dismiss_reaction",
                exc=exc,
            )
            return
        committed_at = (
            str(row.get("rejected_at") or rejected_at)
            if isinstance(row, dict)
            else rejected_at
        )
        await self.emit_suggestion_reaction_committed(
            suggestion_id=suggestion_id,
            reaction="rejected",
            committed_at=committed_at,
            meta={"suggestion_id": suggestion_id, "kind": "suggestion"},
        )

    async def handle_reject(self, suggestion_id: str) -> None:
        """後方互換: reject は dismiss と同義として扱う。"""
        await self.handle_dismiss(suggestion_id)

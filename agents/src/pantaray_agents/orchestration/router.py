"""オーケストレーションの WebSocket/HTTP ルーター。

このモジュールは WS/HTTP のI/Oを扱うエンドポイント群を提供する。
- セッション管理（in-memory `SESSION_STORE`）
- WebSocket のイベント入出力（仕様準拠の event 名）
- エージェントHTTPストリーミング呼び出し → NDJSONイベントをWSへ中継
- モックストリーム（開発・テスト用）

詳細な実装はハンドラ/ユーティリティに委譲し、接続・認証・
セッション作成・メッセージディスパッチに集中する。
"""

import json
import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError
from starlette.responses import Response
from starlette.websockets import WebSocketState

from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.local_runtime.runtime.admission import admission_is_open
from pantaray_agents.local_runtime.runtime.identity import current_owner_id
from pantaray_agents.local_runtime.runtime.local_api_auth import (
    local_api_token_matches,
)
from pantaray_agents.orchestration.common.errors import error_from_payload
from pantaray_agents.orchestration.session.constants import (
    SESSION_STORE_RETENTION_SECONDS,
)
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.capacity import (
    load_ws_capacity_limits,
    release_connection,
    try_acquire_connection,
)
from pantaray_agents.orchestration.ws.error_meta import (
    build_protocol_error_meta,
)
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.owner_bound_sockets import (
    bind_socket_to_owner,
    release_owner_bound_socket,
)
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.events import InboundEvent, OutboundEvent
from pantaray_agents.schema.websocket import (
    AckEventMessage,
    DismissSuggestionMessage,
    ErrorMessage,
    ExecuteActionMessage,
    RejectSuggestionMessage,
    ResumeSessionMessage,
    SessionStartedMessage,
    StopProcessMessage,
)
from pantaray_agents.utils.public_error import (
    PUBLIC_INTERNAL_ERROR_MESSAGE,
    public_ws_error,
)

logger = logging.getLogger(__name__)
ws_router = APIRouter()
# Service Unavailable. Electron main reconnects with backoff after any 5xx
# handshake denial, whereas the auth close codes (4401 / 4403) end its retries
# for the rest of the process (`frontend/electron/ws_reconnect_policy.js`).
WS_RETRYABLE_DENIAL_HTTP_STATUS = 503
WS_CLOSE_CODE_TRY_AGAIN_LATER = 1013

# --- Session Store ---
_ws_session_cfg = load_local_runtime_tunables().websocket_session
SESSION_MAX_AGE_SECONDS: int = _ws_session_cfg.max_age_seconds
SESSION_STORE = InMemorySessionStore(
    SESSION_MAX_AGE_SECONDS,
    max_sessions=_ws_session_cfg.store_max_sessions,
    max_processes_per_session=_ws_session_cfg.store_max_processes_per_session,
    max_events_per_session=_ws_session_cfg.store_max_events_per_session,
    max_chunks_per_process=_ws_session_cfg.store_max_chunks_per_process,
)


def _prune_sessions() -> None:
    """期限切れのセッションを `SESSION_STORE` から削除する。"""
    SESSION_STORE.prune()
    SESSION_STORE.prune_completed(SESSION_STORE_RETENTION_SECONDS)


def _to_error_message_payload(
    payload: dict[str, object] | None, default_code: str
) -> ErrorMessage:
    """任意のエラーペイロードを `ErrorMessage` に正規化する。"""
    return error_from_payload(payload, default_code)


async def _safe_send_json(ws: WebSocket, payload: dict) -> bool:
    """WSへ安全にJSONを送信する（切断済みなら無視）。"""
    try:
        if (
            getattr(ws, "client_state", None) is not None
            and ws.client_state != WebSocketState.CONNECTED
        ):
            return False
    except Exception:
        # If client_state attr missing or raises, continue to attempt send
        pass
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _safe_close(
    ws: WebSocket, code: int, *, reason: str | None = None, context: dict | None = None
) -> None:
    """WSを安全にクローズする（例外は握りつぶす）。"""
    try:
        logger.warning(
            "WS safe_close invoked: code=%s reason=%s context=%s", code, reason, context
        )
    except Exception:
        pass
    try:
        await ws.close(code=code)
    except Exception:
        pass


async def _send_retryable_denial_response(
    ws: WebSocket,
    *,
    status_code: int,
    reason: str,
) -> None:
    """Accept前のretryable failureをHTTP denialとして返す。"""
    await ws.send_denial_response(
        Response(status_code=status_code, content=reason.encode("utf-8"))
    )


@ws_router.websocket("/v1/agents/users/{user_id}/orchestrations")
async def orchestrations_ws(websocket: WebSocket, user_id: str):
    """オーケストレーション用 WebSocket エンドポイント。"""
    # --- Enforce Authorization: Bearer <local api token> on connection ---
    auth_header = websocket.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        await websocket.close(code=4401, reason="auth_header_missing")
        return

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        await websocket.close(code=4401, reason="bearer_token_missing")
        return

    if not local_api_token_matches(token):
        await websocket.close(code=4401, reason="local_api_token_invalid")
        return

    # Admission is checked before the owner is, because a barrier in flight is
    # exactly when the owner reads as someone else: 4403 would end main's
    # reconnects for good, while a denial response is retried (design 6.2).
    if not admission_is_open():
        await _send_retryable_denial_response(
            websocket,
            status_code=WS_RETRYABLE_DENIAL_HTTP_STATUS,
            reason="ws_admission_closed",
        )
        return

    # The handshake binds this socket to the owner current at accept time.
    if current_owner_id() != user_id:
        await websocket.close(code=4403, reason="forbidden_user_mismatch")
        return

    # --- Capacity guard (best-effort, in-process) ---
    ws_capacity_limits = load_ws_capacity_limits()
    acquired_connection = try_acquire_connection(
        user_id=str(user_id), limits=ws_capacity_limits
    )
    if not acquired_connection:
        await _send_retryable_denial_response(
            websocket,
            status_code=WS_RETRYABLE_DENIAL_HTTP_STATUS,
            reason="ws_capacity_exceeded",
        )
        return

    handler: WSOrchestrationHandler | None = None
    session_id: str | None = None
    try:
        await websocket.accept()
        # The socket is bound to the owner it authenticated as; an owner change
        # closes it from here instead of trusting the client to notice.
        bind_socket_to_owner(owner_id=str(user_id), websocket=websocket)
        # Bind first, then recheck, never the other way round. The barrier closes
        # admission before it reads the ledger, so a socket that is bound and
        # then sees admission open is either already in the list the barrier is
        # about to close or was bound after the barrier finished -- it cannot
        # outlive the owner it bound to. Checking before binding would leave the
        # handshake's own awaits as the window the barrier slips through.
        if not admission_is_open() or current_owner_id() != user_id:
            # `finally` takes the socket back out of the ledger.
            await _safe_close(
                websocket,
                WS_CLOSE_CODE_TRY_AGAIN_LATER,
                reason="identity_changed_during_handshake",
            )
            return
        # セッション開始通知
        _prune_sessions()
        session_id = str(uuid.uuid4())
        SESSION_STORE.create_session(session_id, user_id=str(user_id))
        await _safe_send_json(
            websocket,
            {
                "event": OutboundEvent.SESSION_STARTED.value,
                "data": SessionStartedMessage(
                    session_id=session_id,
                    issued_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ).model_dump(),
            },
        )
        try:
            logger.info(
                "WS session started: session_id=%s user_id=%s", session_id, user_id
            )
        except Exception:
            pass

        handler = WSOrchestrationHandler(
            websocket=websocket,
            session_store=SESSION_STORE,
            session_id=session_id,
            user_id=user_id,
        )
        # Suggestion は runtime（worker job）が起動するため、WS は生存中に
        # 進行中の suggestion process を検出して中継するだけの責務を持つ。
        handler.start_suggestion_relay()
        handler.start_chat_relay()

        # WS DoS safety（接続単位の簡易制限。単一プロセスでも最低限は守る）
        ws_cfg = load_local_runtime_tunables().websocket
        max_message_bytes = ws_cfg.max_message_bytes
        rate_window_seconds = ws_cfg.rate_limit_window_seconds
        rate_limit_max_messages = ws_cfg.rate_limit_max_messages
        recent_rx_times: deque[float] = deque()

        async def _handle_resume_session(data: dict) -> None:
            payload = ResumeSessionMessage(**data)
            await handler.resume_session(
                payload.session_id,
                payload.process_id,
                payload.last_cursor,
                payload.last_chunk_index,
                payload.kind,
                suggestion_id=payload.suggestion_id,
                action_id=payload.action_id,
                command_id=payload.command_id,
            )

        async def _parse_message(raw: str) -> tuple[str, dict] | None:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_INVALID_JSON",
                        request_id=handler.session_id,
                        error_type=ErrorType.VALIDATION_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="Payload must be valid JSON",
                    ),
                    meta=build_protocol_error_meta(
                        stage="invalid_json",
                        error_code="WS_INVALID_JSON",
                    ),
                )
                return None
            event = msg.get("event")
            data = msg.get("data") or {}
            if not isinstance(event, str):
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_INVALID_EVENT",
                        request_id=handler.session_id,
                        error_type=ErrorType.VALIDATION_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="Field 'event' must be a string",
                    ),
                    meta=build_protocol_error_meta(
                        stage="invalid_event",
                        error_code="WS_INVALID_EVENT",
                    ),
                )
                return None
            return event, data

        async def _handle_reject(data: dict) -> None:
            payload = RejectSuggestionMessage(**data)
            # suggestion_id の所有検証（clientは改ざん可能）
            if not await handler._is_suggestion_id_accessible(payload.suggestion_id):  # noqa: SLF001
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_SUGGESTION_NOT_FOUND",
                        request_id=handler.session_id,
                        error_type=ErrorType.VALIDATION_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="suggestion_id is not accessible for current user",
                    ),
                    meta=build_protocol_error_meta(
                        stage="suggestion_access_denied",
                        error_code="WS_SUGGESTION_NOT_FOUND",
                    ),
                )
                return
            await handler.handle_reject(payload.suggestion_id)

        async def _handle_dismiss(data: dict) -> None:
            payload = DismissSuggestionMessage(**data)
            # suggestion_id の所有検証（clientは改ざん可能）
            if not await handler._is_suggestion_id_accessible(payload.suggestion_id):  # noqa: SLF001
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_SUGGESTION_NOT_FOUND",
                        request_id=handler.session_id,
                        error_type=ErrorType.VALIDATION_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="suggestion_id is not accessible for current user",
                    ),
                    meta=build_protocol_error_meta(
                        stage="suggestion_access_denied",
                        error_code="WS_SUGGESTION_NOT_FOUND",
                    ),
                )
                return
            await handler.handle_dismiss(payload.suggestion_id)

        async def _handle_stop(data: dict) -> None:
            payload = StopProcessMessage(**data)
            # STOP はプロセス種別に応じて handler 側で完了イベント送信有無を制御する。
            await handler.handle_stop_process(payload.process_id)

        async def _dispatch(event: str, data: dict) -> None:
            if event == InboundEvent.EXECUTE_ACTION.value:
                await handler.execute_action(ExecuteActionMessage(**data))
                return
            if event == InboundEvent.DISMISS_SUGGESTION.value:
                await _handle_dismiss(data)
                return
            if event == InboundEvent.REJECT_SUGGESTION.value:
                await _handle_reject(data)
                return
            if event == InboundEvent.RESUME_SESSION.value:
                await _handle_resume_session(data)
                return
            if event == InboundEvent.STOP_PROCESS.value:
                await _handle_stop(data)
                return
            if event == InboundEvent.ACK_EVENT.value:
                await handler.handle_ack(AckEventMessage(**data))
                return
            await handler.send_error(
                public_ws_error(
                    error_code="WS_UNKNOWN_EVENT",
                    request_id=handler.session_id,
                    error_type=ErrorType.VALIDATION_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="Unsupported event",
                ),
                meta=build_protocol_error_meta(
                    stage="unknown_event",
                    error_code="WS_UNKNOWN_EVENT",
                ),
            )

        # ---- main loop ----
        while True:
            # break early if not connected anymore
            try:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
            except Exception:
                # if state is unavailable, proceed to receive which will raise
                pass

            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect as e:
                try:
                    logger.warning(
                        "WS disconnected: code=%s reason=%s session_id=%s user_id=%s",
                        getattr(e, "code", None),
                        getattr(e, "reason", None),
                        session_id,
                        user_id,
                    )
                except Exception:
                    pass
                break
            except RuntimeError:
                # "WebSocket is not connected. Need to call accept first." → treat as disconnect
                break

            # 1) サイズ制限（1009: Message Too Big）
            try:
                raw_bytes_len = len(raw.encode("utf-8", errors="ignore"))
            except Exception:
                raw_bytes_len = len(raw) if isinstance(raw, str) else 0
            if max_message_bytes > 0 and raw_bytes_len > max_message_bytes:
                await _safe_close(
                    websocket,
                    1009,
                    reason="message_too_big",
                    context={"bytes": raw_bytes_len, "max_bytes": max_message_bytes},
                )
                break

            # 2) 簡易レート制限（1013: Try Again Later）
            if rate_window_seconds > 0 and rate_limit_max_messages > 0:
                now_mono = time.monotonic()
                recent_rx_times.append(now_mono)
                cutoff = now_mono - rate_window_seconds
                while recent_rx_times and recent_rx_times[0] < cutoff:
                    recent_rx_times.popleft()
                if len(recent_rx_times) > rate_limit_max_messages:
                    await _safe_close(
                        websocket,
                        WS_CLOSE_CODE_TRY_AGAIN_LATER,
                        reason="rate_limited",
                        context={
                            "window_seconds": rate_window_seconds,
                            "max_messages": rate_limit_max_messages,
                        },
                    )
                    break

            parsed = await _parse_message(raw)
            if not parsed:
                continue
            event, data = parsed

            try:
                await _dispatch(event, data)
            except ValidationError:
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_INVALID_PAYLOAD",
                        request_id=handler.session_id,
                        error_type=ErrorType.VALIDATION_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="Payload validation failed",
                    ),
                    meta=build_protocol_error_meta(
                        stage="invalid_payload",
                        error_code="WS_INVALID_PAYLOAD",
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("WS dispatch failed: %s", e)
                await handler.send_error(
                    public_ws_error(
                        error_code="WS_HANDLER_EXCEPTION",
                        request_id=handler.session_id,
                        error_type=ErrorType.INTERNAL_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message=PUBLIC_INTERNAL_ERROR_MESSAGE,
                    ),
                    meta=build_protocol_error_meta(
                        stage="handler_exception",
                        error_code="WS_HANDLER_EXCEPTION",
                    ),
                )
    finally:
        # Released before any await: cancellation must not leave the socket in
        # the ledger. A socket that never bound is not there to release.
        release_owner_bound_socket(owner_id=str(user_id), websocket=websocket)
        if handler is not None:
            try:
                await handler.close()
            except Exception:
                # close時の例外でWebSocketハンドラの例外を握りつぶさない（B012回避）
                pass
        # release capacity slot even if accept() fails
        if acquired_connection:
            release_connection(user_id=str(user_id))

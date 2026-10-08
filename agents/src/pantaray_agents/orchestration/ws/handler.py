"""WebSocket オーケストレーションハンドラ。

runtime が起動した Suggestion/Action の進行をクライアントへ中継し、
セッション再開のための live chunk 履歴を保持し、
accept/dismiss などのユーザー反応を永続化する。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Coroutine

from fastapi import WebSocket

import pantaray_agents.dependencies as deps
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.activity_local_repository import (
    SQLiteActivityRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import (
    ActionFlowMixin,
    SessionResumeMixin,
    SuggestionFlowMixin,
)
from pantaray_agents.orchestration.ws.base import BaseWSHandler
from pantaray_agents.repositories.runtime_ports import (
    ActionRepositoryPort,
    SuggestionRepositoryPort,
)
from pantaray_agents.utils.ws_observability import RateLimiter

from .chat_relay import ChatRelayMixin
from .handler_process_control import WSHandlerProcessControlMixin
from .handler_shared import ProcessMetadata, SuggestionStatusPersistenceError
from .task_supervisor import WsTaskSupervisor

logger = logging.getLogger(__name__)


class WSOrchestrationHandler(
    # NOTE:
    # 本ハンドラは Mixin 群の実装に `super()` で委譲する（cooperative multiple inheritance）。
    # そのため、Mixin を先、共通基盤（BaseWSHandler）を最後に置く。
    SessionResumeMixin,
    SuggestionFlowMixin,
    WSHandlerProcessControlMixin,
    ActionFlowMixin,
    ChatRelayMixin,
    BaseWSHandler,
):
    def __init__(
        self,
        websocket: WebSocket,
        session_store: InMemorySessionStore,
        session_id: str,
        user_id: str,
    ) -> None:
        super().__init__(
            websocket=websocket,
            session_store=session_store,
            session_id=session_id,
            user_id=user_id,
        )
        self._task_supervisor = WsTaskSupervisor(session_id=session_id)
        self.process_tasks = self._task_supervisor.tasks
        self._action_processes: set[str] = set()
        self._action_completed: set[str] = set()
        self._process_metadata: dict[str, ProcessMetadata] = {}
        self._is_closed: bool = False
        # クライアント申告 suggestion_id を信じないため、WSセッション内で発行/観測したIDを保持する。
        # - mock mode では DB 検証ができないため、ここを所有検証の代替として用いる。
        self._issued_suggestion_ids: set[str] = set()
        # --- Observability / loop suppression ---
        self._rate_limiter = RateLimiter()
        # process_id -> attempt timestamps (monotonic seconds)
        self._resume_attempts: dict[str, deque[float]] = {}
        self._suggestion_repository: SuggestionRepositoryPort | None = None
        self._action_repository: ActionRepositoryPort | None = None
        ws_session_cfg = load_local_runtime_tunables().websocket_session
        self._session_max_age_seconds: int = ws_session_cfg.max_age_seconds
        self._session_max_resume_missing_chunks = (
            ws_session_cfg.max_resume_missing_chunks
        )
        self._init_suggestion_relay()

    def _sync_process_metadata_to_current_session(
        self,
        process_id: str,
        *,
        suggestion_id: str | None = None,
        action_id: str | None = None,
        command_id: str | None = None,
        kind: str | None = None,
    ) -> None:
        """現在のWSセッションに紐づく process metadata を更新する。"""
        current_session = self.session_store.get(self.session_id)
        if current_session is None:
            self.session_store.create_session(
                self.session_id, user_id=str(self.user_id)
            )
        elif current_session.user_id is None:
            current_session.user_id = str(self.user_id)
        self.session_store.set_process_metadata(
            self.session_id,
            process_id,
            suggestion_id=suggestion_id,
            action_id=action_id,
            command_id=command_id,
            kind=kind,
        )

    async def _is_suggestion_id_accessible(self, suggestion_id: str) -> bool:
        """suggestion_id が当該WS接続ユーザーに属するかを検証する。

        - mock mode: DB検証ができないため、WSセッション内で発行/観測したIDのみ許可する。
        - real mode: `agent_suggestions` を参照して user_id を照合する（service_role前提でも必須）。
        """
        if not suggestion_id or not isinstance(suggestion_id, str):
            return False

        if deps.is_mock_mode():
            return suggestion_id in self._issued_suggestion_ids

        repo = await self._get_suggestion_repository()
        if repo is None:
            return False
        try:
            result = await repo.get_suggestion(
                user_id=str(self.user_id),
                suggestion_id=suggestion_id,
            )
        except Exception:  # noqa: BLE001
            return False
        row = result.data if isinstance(result.data, dict) else None
        if not row:
            return False
        return str(row.get("user_id") or "") == str(self.user_id)

    def _spawn_background_task(
        self, *, task_key: str, coro: Coroutine[object, object, None]
    ) -> asyncio.Task[None] | None:
        """WSハンドラのライフサイクルに紐づくバックグラウンドTaskを起動/管理する。

        目的:
            - Suggestion relay などの周期処理を安全にスケジュールする。
            - close() 時に確実にキャンセルされるよう、taskを `self.process_tasks` にぶら下げる。

        Args:
            task_key: `self.process_tasks` のキー（衝突しない命名にする）。
            coro: 実行するコルーチン。
        """
        if getattr(self, "_is_closed", False):
            coro.close()
            return
        return self._task_supervisor.spawn(task_key=task_key, coro=coro)

    # ---- Low-level utilities ----

    async def _get_suggestion_repository(self) -> SuggestionRepositoryPort | None:
        if deps.is_mock_mode():
            return None
        if self._suggestion_repository is not None:
            return self._suggestion_repository
        try:
            db_path, busy_timeout_ms = read_local_runtime_db_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local suggestion repository unavailable: %s", exc)
            return None
        self._suggestion_repository = LocalSuggestionRepository(
            db_path=str(db_path),
            busy_timeout_ms=busy_timeout_ms,
            activity_repository=SQLiteActivityRuntimeRepository(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
            ),
        )
        return self._suggestion_repository

    async def _get_action_state_repository(
        self,
    ) -> SuggestionRepositoryPort | LocalSuggestionStateRepository | None:
        if deps.is_mock_mode():
            return await self._get_suggestion_repository()
        try:
            return deps.get_local_suggestion_state_repository()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local suggestion state repository unavailable: %s", exc)
            return None

    async def _get_action_repository(
        self,
    ) -> ActionRepositoryPort | None:
        try:
            return deps.get_local_action_repository()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Local action repository unavailable: %s", exc)
            return None

    async def _persist_suggestion_status(
        self,
        suggestion_id: str | None,
        *,
        user_reaction: str | None = None,
        action_status: str | None = None,
        action_failure_code: str | None = None,
        update_action_failure_code: bool = False,
        accepted_at: str | None = None,
        rejected_at: str | None = None,
    ) -> dict | None:
        """Suggestion/action 状態を永続化し、失敗時は明示例外を返す。"""

        if not suggestion_id:
            raise SuggestionStatusPersistenceError("suggestion_id is required")
        repo = await self._get_suggestion_repository()
        if repo is None:
            raise SuggestionStatusPersistenceError(
                "suggestion repository is unavailable"
            )
        try:
            if user_reaction is not None:
                result = await repo.mark_user_reaction(
                    user_id=str(self.user_id),
                    suggestion_id=suggestion_id,
                    user_reaction=user_reaction,
                    action_status=action_status,
                    action_failure_code=action_failure_code,
                    update_action_failure_code=update_action_failure_code,
                    accepted_at=accepted_at,
                    rejected_at=rejected_at,
                )
            elif action_status is not None:
                result = await repo.update_action_status(
                    user_id=str(self.user_id),
                    suggestion_id=suggestion_id,
                    action_status=action_status,
                    action_failure_code=action_failure_code,
                    update_action_failure_code=update_action_failure_code,
                )
            else:
                raise SuggestionStatusPersistenceError(
                    "persist requires user_reaction or action_status"
                )
        except Exception as exc:
            raise SuggestionStatusPersistenceError(str(exc)) from exc
        if result.error:
            raise SuggestionStatusPersistenceError(result.error)
        return result.data if isinstance(result.data, dict) else None

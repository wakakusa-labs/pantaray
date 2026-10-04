# src/pantaray_agents/routers/suggestion.py
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.dependencies import (
    get_local_suggestion_state_repository,
    get_suggestion_agent,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.identity import (
    OwnerMismatchError,
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
    WELCOME_SUGGESTION_MAX_CHARS,
    record_welcome_suggestion,
)
from pantaray_agents.orchestration.ws.deliverable_sessions import (
    owner_has_deliverable_session,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionFinalState,
    SuggestionPendingChunk,
    SuggestionResumeHint,
    SuggestionStateResponse,
    SuggestionStreamingState,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents/users", tags=["Suggestion Agent"])


class WelcomeSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Electron main writes it, in the user's language and with their shortcut.
    answer: str = Field(min_length=1, max_length=WELCOME_SUGGESTION_MAX_CHARS)


class WelcomeSuggestionResponse(BaseModel):
    created: bool


async def _get_suggestion_state_repository(
    suggestion_agent: SuggestionAgent,
):
    return get_local_suggestion_state_repository()


@router.get(
    "/{user_id}/suggestions/{suggestion_id}/state",
    response_model=SuggestionStateResponse,
)
async def suggestion_state(
    user_id: str,
    suggestion_id: str,
    session_id: str | None = Query(default=None, description="WebSocket セッションID"),
    process_id: str | None = Query(default=None, description="対象プロセスID"),
    last_chunk_index: int | None = Query(
        default=None, description="クライアントが受信済みの最後のチャンク番号"
    ),
    suggestion_agent: SuggestionAgent = Depends(get_suggestion_agent),
    resolved_user_id: str = Depends(get_current_user_id_from_token),
):
    """提案ストリームの状態を取得する。"""

    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )

    repository = await _get_suggestion_state_repository(suggestion_agent)
    repo_result = await repository.get_suggestion_state(
        user_id=user_id,
        suggestion_id=suggestion_id,
    )
    suggestion_row = repo_result.data if repo_result.data else None

    # --- WSセッション由来の状態（DB未保存でも返せる） ---
    sess_record = None
    proc_record = None
    if session_id and process_id:
        # 遅延インポートして循環依存を回避
        from pantaray_agents.orchestration.router import SESSION_STORE

        sess_record = SESSION_STORE.get(session_id)
        # セッション所有者を束縛（session_idの推測/漏えいによる越境参照を防ぐ）
        if sess_record and getattr(sess_record, "user_id", None) not in (None, user_id):
            sess_record = None
        proc_record = sess_record.processes.get(process_id) if sess_record else None
        # suggestion_id との整合を取り、異なるプロセスを誤って紐付けない
        if proc_record and getattr(proc_record, "suggestion_id", None) not in (
            None,
            suggestion_id,
        ):
            proc_record = None

    # DBもセッションも無ければ 404（=本当に未知）
    if suggestion_row is None and proc_record is None:
        if repo_result.error and repo_result.error != "No data found":
            raise HTTPException(status_code=500, detail=repo_result.error)
        raise HTTPException(status_code=404, detail="Suggestion not found")

    # streaming_state.status を決定（セッション優先）
    stream_status = "not_started"
    if proc_record is not None:
        stream_status = "streaming"
    elif suggestion_row:
        row_status = str(suggestion_row.get("status") or "").lower()
        stream_status = "streaming" if row_status == "processing" else "completed"

    pending_chunks = []
    resume_hint = None
    if sess_record and proc_record:
        # 遅延インポートして循環依存を回避（上で import 済みの可能性はあるが安全側で統一）
        from pantaray_agents.orchestration.router import SESSION_STORE

        start_index = (last_chunk_index or -1) + 1
        chunk_pairs = SESSION_STORE.iter_missing_chunks(
            session_id, process_id, start_index
        )
        pending_chunks = [
            SuggestionPendingChunk(chunk_index=idx, content=chunk.content)
            for idx, _event_name, chunk in chunk_pairs
        ]

        next_index = proc_record.next_chunk_index
        last_cursor = None
        if next_index > 0:
            target_index = next_index - 1
            for event_id, meta in sess_record.events.items():
                if (
                    meta.get("process_id") == process_id
                    and meta.get("event") == "suggestion_chunk"
                    and meta.get("chunk_index") == target_index
                ):
                    last_cursor = event_id
                    break

        resume_hint = SuggestionResumeHint(
            next_chunk_index=next_index,
            last_cursor=last_cursor,
            kind="suggestion",
        )

    streaming_state = SuggestionStreamingState(
        status=stream_status,
        process_id=process_id,
        session_id=session_id,
        resume_hint=resume_hint,
        pending_chunks=pending_chunks,
    )

    final_state = None
    metadata: dict[str, object] | None = None
    if suggestion_row:
        error_value = suggestion_row.get("error")
        if isinstance(error_value, str):
            try:
                error_value = json.loads(error_value)
            except ValueError:
                pass
        final_state = SuggestionFinalState(
            status=str(suggestion_row.get("status") or ""),
            has_suggestion=suggestion_row.get("has_suggestion"),
            answer=suggestion_row.get("answer"),
            # クライアントへ thought/thinking を返さない（内部保存のみ）
            thinking=None,
            suggestion_summary=suggestion_row.get("suggestion_summary"),
            target_context=suggestion_row.get("target_context_json"),
            interaction_contract=suggestion_row.get("interaction_contract"),
            user_reaction=suggestion_row.get("user_reaction"),
            prompt_name=suggestion_row.get("prompt_name"),
            prompt_version=suggestion_row.get("prompt_version"),
            created_at=suggestion_row.get("created_at"),
            updated_at=suggestion_row.get("updated_at"),
            error=error_value if isinstance(error_value, dict) else None,
            request_images_count=suggestion_row.get("request_images_count"),
            used_images_count=suggestion_row.get("used_images_count"),
        )

        metadata = {
            key: suggestion_row.get(key)
            for key in (
                "user_reaction",
                "interaction_contract",
                "request_images_count",
                "used_images_count",
                "prompt_name",
                "prompt_version",
            )
            if suggestion_row.get(key) is not None
        }
        if not metadata:
            metadata = None

    return SuggestionStateResponse(
        suggestion_id=suggestion_id,
        user_id=user_id,
        streaming_state=streaming_state,
        final_state=final_state,
        metadata=metadata,
    )


@router.post(
    "/{user_id}/suggestions/welcome",
    response_model=WelcomeSuggestionResponse,
)
async def create_welcome_suggestion(
    user_id: str,
    body: WelcomeSuggestionRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WelcomeSuggestionResponse:
    """Greet an owner who has no data yet; for anyone else this is a no-op.

    A greeting no session can show would be dropped like any Suggestion, so it
    is not stored and the caller is told to try again (503).
    """

    if not resolved_user_id or user_id != resolved_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )
    answer = body.answer.strip()
    if not answer:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="answer must not be blank",
        )
    try:
        verify_current_owner(user_id)
    except OwnerMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="owner mismatch"
        ) from exc
    if not owner_has_deliverable_session(user_id):
        raise PublicAgentHTTPError(
            "No session can show the welcome",
            error_code="WELCOME_NO_SESSION",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error_type=ErrorType.DEPENDENCY_ERROR,
            public_message="The desktop app is not connected yet. Try again.",
        )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        created = record_welcome_suggestion(
            connection=connection,
            user_id=user_id,
            answer=answer,
            # History accepts canonical UTC milliseconds only.
            now=now_utc_iso(),
        )
    return WelcomeSuggestionResponse(created=created)

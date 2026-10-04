# src/pantaray_agents/routers/activity.py
"""ActivitySummaryAgent のAPIルーター"""

import logging

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    status,
)

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.config_local_runtime import settings
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.orchestration.runtime.activity_job_queue import (
    enqueue_activity_summary_job,
)
from pantaray_agents.schema.agent.activity import (
    ActivitySummaryAgentRequest,
    ActivitySummaryAgentResponse,
)
from pantaray_agents.utils.trace_context import TraceContextManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents/users")


@router.post(
    "/{user_id}/activities/summaries/{summary_id}",
    response_model=ActivitySummaryAgentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Activity Summary Agent"],
)
async def generate_activity_summary(
    user_id: str,
    summary_id: str,
    payload: dict | None = Body(None),
    resolved_user_id: str = Depends(get_current_user_id_from_token),
    request_id: str = Header(
        ...,
        alias="X-Request-ID",
        description="必須。リクエスト追跡用の一意ID（UUID推奨）。",
    ),
    client_version: str | None = Header(
        None,
        alias="X-Client-Version",
        description="任意。クライアントアプリケーションのバージョン。",
    ),
) -> ActivitySummaryAgentResponse:
    """ActivitySummaryAgent (HTTP)

    階層的なサマリ（1h/24h/1w/1m/3m）を生成する。

    Spec: POST /v1/agents/users/{user_id}/activities/summaries/{summary_id}
    """
    try:
        # パスと認証ユーザーの整合性
        if user_id and resolved_user_id and resolved_user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
            )

        logger.debug("X-Request-ID=%s, X-Client-Version=%s", request_id, client_version)

        payload = payload or {}

        # 競合検出
        forced_values = {
            "user_id": user_id,
            "summary_id": summary_id,
        }
        conflicts = [
            key
            for key, forced in forced_values.items()
            if key in payload and payload.get(key) not in (None, forced)
        ]
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "REQUEST_VALIDATION_ERROR",
                    "error_message": f"Payload contains conflicting fields: {', '.join(conflicts)}",
                },
            )

        # 衝突回避
        reserved_keys = {"user_id", "summary_id"}
        extra_fields = {k: v for k, v in payload.items() if k not in reserved_keys}

        summary_req = ActivitySummaryAgentRequest(
            user_id=user_id,
            summary_id=summary_id,
            **extra_fields,
        )

        # モック時はダミー応答（Supabase不要）
        if bool(settings.get("use_mocks", False)):
            return ActivitySummaryAgentResponse(
                summary_id=summary_id,
                user_id=user_id,
                summary_type=extra_fields.get("summary_type", "1h"),
                summary="# 1h Summary (Mock)\n\n## Overview\nUser was working on development tasks.",
                thinking="Mock thinking process",
                period_start=extra_fields.get("period_start", "2024-01-01T00:00:00Z"),
                period_end=extra_fields.get("period_end", "2024-01-01T01:00:00Z"),
                source_ids=[],
                created_at="2024-01-01T01:00:30Z",
                status="success",
                error=None,
            )
        with TraceContextManager(
            user_id=user_id,
            request_id=request_id,
            extra={"activity_summary_id": summary_id},
        ):
            enqueue_result = enqueue_activity_summary_job(
                user_id=user_id,
                summary_id=summary_id,
                summary_type=summary_req.summary_type,
                period_start=summary_req.period_start,
                period_end=summary_req.period_end,
            )
            return ActivitySummaryAgentResponse(
                summary_id=enqueue_result["summary_id"],
                user_id=user_id,
                summary_type=str(extra_fields.get("summary_type") or ""),
                summary="",
                thinking=None,
                period_start=str(extra_fields.get("period_start") or ""),
                period_end=str(extra_fields.get("period_end") or ""),
                source_ids=[],
                created_at=now_utc_iso(),
                status="processing",
                error=None,
            )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "Activity summary failed: user_id=%s summary_id=%s request_id=%s",
            user_id,
            summary_id,
            request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "ACTIVITY_SUMMARY_ERROR",
                "error_message": "Activity summary encountered an error.",
            },
        ) from e

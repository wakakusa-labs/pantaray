"""Overlay bootstrap router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

import pantaray_agents.dependencies as deps
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.orchestration.overlay.bootstrap import (
    OverlayBootstrapError,
    OverlayBootstrapInvariantError,
    OverlayBootstrapMissingEventLogError,
    OverlayBootstrapNotFoundError,
    OverlayBootstrapService,
)
from pantaray_agents.schema.overlay_bootstrap import OverlayBootstrapResponse

router = APIRouter(prefix="/api/agent/history", tags=["Overlay Bootstrap"])


async def _get_overlay_repository() -> LocalSuggestionStateRepository:
    return deps.get_local_suggestion_state_repository()


@router.get(
    "/{suggestion_id}/overlay-bootstrap",
    response_model=OverlayBootstrapResponse,
)
async def overlay_bootstrap(
    suggestion_id: str,
    user_id: str = Depends(get_current_user_id_from_token),
    repository: LocalSuggestionStateRepository = Depends(_get_overlay_repository),
) -> OverlayBootstrapResponse:
    db_path, _ = read_local_runtime_db_config()
    service = OverlayBootstrapService(repository, db_path=db_path)
    try:
        return await service.build_for_suggestion(
            user_id=str(user_id),
            suggestion_id=str(suggestion_id),
        )
    except OverlayBootstrapNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (
        OverlayBootstrapMissingEventLogError,
        OverlayBootstrapInvariantError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except OverlayBootstrapError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="overlay bootstrap failed",
        ) from exc

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.utils.error_handling import (
    general_exception_handler,
    http_exception_handler,
    public_agent_http_error_handler,
)
from pantaray_agents.utils.log_redaction import RedactingFormatter
from pantaray_agents.utils.metrics import install_metrics_endpoint

APP_VERSION = "1.0.0"
_LOGGER_CONFIGURED_ATTR = "_pantaray_logging_configured"
# 5 MiB per file, mirrors the Electron main-process file sink for consistent retention.
PY_LOG_FILE_MAX_BYTES: Final[int] = 5 * 1024 * 1024
# Keep the active file plus 4 rotated files (~25 MiB cap), matching the Electron sink.
PY_LOG_FILE_BACKUP_COUNT: Final[int] = 4
_LOG_FORMAT: Final[str] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
type AppLifespan = Callable[[FastAPI], AsyncIterator[None]]


def configure_logging(
    *,
    log_level: str,
    log_file_path: str | None = None,
    enable_console: bool = True,
) -> logging.Logger:
    """Configure root logging exactly once for the current process.

    `log_file_path` selects where logs are written:
    - The local backend injects an explicit, writable path (the packaged bundle
      directory is read-only) and passes enable_console=False, since its stdout
      is captured by the Electron parent and not user-facing.
    - The cloud backend (server-side, not user-facing) omits it to use the
      server-standard location and keeps the console handler for container log
      scraping. Cloud is out of scope for the release-time local-logging policy.
    """

    root_logger = logging.getLogger()
    if getattr(root_logger, _LOGGER_CONFIGURED_ATTR, False):
        root_logger.setLevel(log_level)
        return logging.getLogger(__name__)

    if log_file_path is None:
        log_file = Path(__file__).resolve().parents[3] / "logs" / "agent.log"
    else:
        log_file = Path(log_file_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=PY_LOG_FILE_MAX_BYTES,
        backupCount=PY_LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
    root_logger.addHandler(file_handler)

    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
        root_logger.addHandler(console_handler)

    root_logger.setLevel(log_level)
    setattr(root_logger, _LOGGER_CONFIGURED_ATTR, True)
    return logging.getLogger(__name__)


def create_base_app(
    *,
    title: str,
    description: str,
    lifespan: AppLifespan | None = None,
) -> FastAPI:
    return FastAPI(
        title=title,
        description=description,
        version=APP_VERSION,
        contact={"name": "Pantaray", "email": "kento.shimizu.25@gmail.com"},
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        lifespan=lifespan,
    )


def install_common_middleware(app: FastAPI, *, allowed_origins: list[str]) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        # ルータが実際に提供する method と一致させる（workspace_settings などが
        # PUT・DELETE を持つ）。
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Client-Version",
        ],
        max_age=3600,
    )


def install_common_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(PublicAgentHTTPError, public_agent_http_error_handler)
    app.add_exception_handler(Exception, general_exception_handler)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = APP_VERSION


def install_health_route(app: FastAPI) -> None:
    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health_check() -> HealthResponse:
        return HealthResponse()


def finalize_app(app: FastAPI) -> None:
    install_common_exception_handlers(app)
    install_metrics_endpoint(app)
    install_health_route(app)

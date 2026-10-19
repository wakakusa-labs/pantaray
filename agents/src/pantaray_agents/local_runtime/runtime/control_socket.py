from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.storage.migrations import MigrationError

from . import integration_credentials as integration
from .connection_store import (
    LlmConnection,
    WebSearchCredential,
    apply_connection_configuration,
    clear_llm_connection,
    clear_web_search_credential,
    read_llm_connection,
    read_llm_route,
    read_optional_llm_connection,
    read_optional_web_search_credential,
    read_web_search_credential,
    read_web_search_route,
    set_llm_connection,
    set_web_search_credential,
)
from .control_payload import (
    ClearCloudSessionSuccessResponse,
    CloudSessionImport,
    CloudSessionSuccessResponse,
    ConfigureSuccessResponse,
    ControlResponse,
    ControlSuccessResponse,
    LlmRouteSuccessResponse,
    SetCloudSessionPayload,
    StatusSuccessResponse,
    WebSearchRouteSuccessResponse,
    build_error_response,
    normalize_session_version,
    read_clear_cloud_session_payload,
    read_set_cloud_session_payload,
    require_payload_object,
    require_request_object,
    require_string_field,
)
from .identity import current_owner_id
from .local_api_auth import read_local_api_token
from .route_identity import (
    RouteInputs,
    connection_identity,
    effective_route_identity,
    read_route_inputs,
    web_search_identity,
)
from .runtime_env import (
    read_helper_instance_id,
    read_local_backend_bound_host,
    read_local_backend_bound_port,
    read_local_runtime_control_socket_path,
    read_local_runtime_db_config,
)
from .session_store import (
    AUTH_CONTEXT_ABSENT_STATE,
    AUTH_CONTEXT_EXPIRED_STATE,
    AUTH_CONTEXT_PRESENT_STATE,
    ClearDesktopSessionResult,
    CloudSessionIdentity,
    apply_cloud_session_clear,
    apply_cloud_session_import,
    classify_cloud_session_clear,
    forget_cloud_session,
    mark_configured,
    read_auth_context_state,
    read_configured,
    read_credential_generation,
    restore_expired_cloud_identity,
    validate_cloud_session_import,
)
from .stop_barrier import (
    apply_with_stop_barrier,
    arm_cloud_session_expiry_timer,
    await_cancelled_cloud_session_expiry_work,
    identity_changes_serialized,
)

logger = logging.getLogger(__name__)

CONTROL_SOCKET_MAX_REQUEST_BYTES: Final[int] = 16 * 1024
CONTROL_SOCKET_MAX_PATH_BYTES: Final[int] = 103
CONTROL_SOCKET_READY_TIMEOUT_SECONDS: Final[float] = 5.0
CONTROL_SOCKET_STOP_WAIT_SECONDS: Final[float] = 1.0
CONFIGURE_OPERATION: Final[str] = "configure"
SET_CLOUD_SESSION_OPERATION: Final[str] = "set_cloud_session"
CLEAR_CLOUD_SESSION_OPERATION: Final[str] = "clear_cloud_session"
SET_LLM_CONNECTION_OPERATION: Final[str] = "set_llm_connection"
CLEAR_LLM_CONNECTION_OPERATION: Final[str] = "clear_llm_connection"
SET_WEB_SEARCH_CREDENTIAL_OPERATION: Final[str] = "set_web_search_credential"
CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION: Final[str] = "clear_web_search_credential"
STATUS_OPERATION: Final[str] = "status"
INVALID_REQUEST_ERROR_CODE: Final[str] = "invalid_request"
INTERNAL_ERROR_CODE: Final[str] = "internal_error"
_THREAD_LOCK = threading.Lock()
_READY_EVENT = threading.Event()
_SERVER_THREAD: threading.Thread | None = None
_SERVER_LOOP: asyncio.AbstractEventLoop | None = None
_SERVER: asyncio.AbstractServer | None = None
_SERVER_SOCKET_PATH: Path | None = None
_START_ERROR: Exception | None = None


def _cloud_session_identity(
    *, state: str, account_user_id: str, session_version: str
) -> CloudSessionIdentity:
    """The identity a cloud session will have once a payload is applied.

    ``session_version`` goes through the store's own normalization, so the
    identity built here and the one the store publishes cannot drift apart.
    """
    return CloudSessionIdentity(
        state="present" if state == AUTH_CONTEXT_PRESENT_STATE else "expired",
        user_id=account_user_id,
        session_version=normalize_session_version(session_version),
    )


def _validated_cloud_session(payload: SetCloudSessionPayload) -> CloudSessionImport:
    """Run the store's own checks before the barrier stops anything.

    A session the store would refuse -- a lifetime already over because the
    clock moved, a callback for an older sign-in -- must not cost the user a
    canceled Action on its way to being rejected.
    """
    return validate_cloud_session_import(
        user_id=payload["account_user_id"],
        desktop_access_token=payload["access_token"],
        expires_at=payload["expires_at"],
        session_version=payload["session_version"],
    )


def _read_cloud_session_change(
    cloud_session: dict[str, object], *, db_path: Path, busy_timeout_ms: int
) -> tuple[Callable[[], str], CloudSessionIdentity | None]:
    """Parse ``configure``'s cloud session into how to apply it and what it becomes."""
    state = require_string_field(cloud_session, "state")
    if state == AUTH_CONTEXT_PRESENT_STATE:
        session = _validated_cloud_session(
            read_set_cloud_session_payload(cloud_session)
        )
        return (
            partial(
                apply_cloud_session_import,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                session=session,
            ),
            _cloud_session_identity(
                state=state,
                account_user_id=session.user_id,
                session_version=session.session_version,
            ),
        )
    if state == AUTH_CONTEXT_EXPIRED_STATE:
        account_user_id = require_string_field(cloud_session, "account_user_id")
        session_version = require_string_field(cloud_session, "session_version")
        return (
            lambda: restore_expired_cloud_identity(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=account_user_id,
                session_version=session_version,
            ),
            _cloud_session_identity(
                state=state,
                account_user_id=account_user_id,
                session_version=session_version,
            ),
        )
    if state == AUTH_CONTEXT_ABSENT_STATE:
        return forget_cloud_session, None
    raise MigrationError("cloud_session.state must be present, expired, or absent")


def _status_response() -> StatusSuccessResponse:
    return {
        "ok": True,
        "helper_instance_id": read_helper_instance_id(),
        "local_api_token": read_local_api_token(),
        "active_owner_id": current_owner_id(),
        "configured": read_configured(),
        "cloud_session_state": read_auth_context_state(),
        "credential_generation": read_credential_generation(),
        "llm_route": read_llm_route(),
        "web_search_route": read_web_search_route(),
        "backend_host": read_local_backend_bound_host(),
        "backend_port": read_local_backend_bound_port(),
    }


async def _dispatch_llm_connection(
    connection: LlmConnection | None,
) -> LlmRouteSuccessResponse:
    """Store or drop the direct LLM connection, ``None`` meaning ``clear``.

    While a cloud session decides the route this changes nothing that is in
    flight: ``effective_route_identity`` leaves the stored setting out of the
    identity, so both sides of the comparison come out equal and the barrier
    stops nothing (design 6.2).
    """
    inputs = read_route_inputs()
    apply: Callable[[], None] = (
        clear_llm_connection
        if connection is None
        else partial(set_llm_connection, connection)
    )
    await apply_with_stop_barrier(
        before=inputs,
        after=effective_route_identity(
            replace(
                inputs,
                llm=None if connection is None else connection_identity(connection),
            )
        ),
        apply=apply,
    )
    return {"ok": True, "llm_route": read_llm_route()}


async def _dispatch_web_search_credential(
    credential: WebSearchCredential | None,
) -> WebSearchRouteSuccessResponse:
    """Store or drop the direct web-search credential, ``None`` meaning ``clear``."""
    inputs = read_route_inputs()
    apply: Callable[[], None] = (
        clear_web_search_credential
        if credential is None
        else partial(set_web_search_credential, credential)
    )
    await apply_with_stop_barrier(
        before=inputs,
        after=effective_route_identity(
            replace(
                inputs,
                web_search=(
                    None if credential is None else web_search_identity(credential)
                ),
            )
        ),
        apply=apply,
    )
    return {"ok": True, "web_search_route": read_web_search_route()}


async def _dispatch_configure(payload: dict[str, object]) -> ConfigureSuccessResponse:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    cloud_session = require_payload_object(payload, field_name="cloud_session")
    apply_cloud_session, cloud_identity = _read_cloud_session_change(
        cloud_session, db_path=db_path, busy_timeout_ms=busy_timeout_ms
    )
    # Every part of the payload is parsed before anything is applied, so a
    # malformed connection leaves the helper unconfigured instead of
    # half-configured.
    llm_connection = read_optional_llm_connection(payload)
    web_search_credential = read_optional_web_search_credential(payload)
    integration_credential = integration.read_optional_integration_credential(payload)
    inputs = read_route_inputs()

    def apply() -> str:
        state = apply_cloud_session()
        apply_connection_configuration(
            llm_connection=llm_connection,
            web_search_credential=web_search_credential,
        )
        integration.apply_integration_credential(integration_credential)
        mark_configured()
        return state

    state = await apply_with_stop_barrier(
        before=inputs,
        after=effective_route_identity(
            RouteInputs(
                configured=True,
                logged_out_owner_id=inputs.logged_out_owner_id,
                cloud=cloud_identity,
                llm=(
                    None
                    if llm_connection is None
                    else connection_identity(llm_connection)
                ),
                web_search=(
                    None
                    if web_search_credential is None
                    else web_search_identity(web_search_credential)
                ),
            )
        ),
        apply=apply,
    )
    arm_cloud_session_expiry_timer()
    return {
        "ok": True,
        "configured": True,
        "cloud_session_state": state,
        "credential_generation": read_credential_generation(),
        "helper_instance_id": read_helper_instance_id(),
        "llm_route": read_llm_route(),
        "web_search_route": read_web_search_route(),
    }


async def _dispatch_set_cloud_session(
    payload: dict[str, object],
) -> CloudSessionSuccessResponse:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    session = _validated_cloud_session(read_set_cloud_session_payload(payload))
    inputs = read_route_inputs()
    state = await apply_with_stop_barrier(
        before=inputs,
        after=effective_route_identity(
            replace(
                inputs,
                cloud=_cloud_session_identity(
                    state=AUTH_CONTEXT_PRESENT_STATE,
                    account_user_id=session.user_id,
                    session_version=session.session_version,
                ),
            )
        ),
        apply=partial(
            apply_cloud_session_import,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            session=session,
        ),
    )
    arm_cloud_session_expiry_timer()
    return {
        "ok": True,
        "cloud_session_state": state,
        "credential_generation": read_credential_generation(),
        "helper_instance_id": read_helper_instance_id(),
    }


async def _dispatch_clear_cloud_session(
    payload: dict[str, object],
) -> ClearCloudSessionSuccessResponse:
    clear_payload = read_clear_cloud_session_payload(payload)
    if clear_payload["helper_instance_id"] != read_helper_instance_id():
        # A callback from a previous helper lifetime never matches this one.
        return {
            "ok": True,
            "cloud_session_state": read_auth_context_state(),
            "stale": True,
        }
    plan = classify_cloud_session_clear(
        user_id=clear_payload["account_user_id"],
        reason=clear_payload["reason"],
        expected_session_version=clear_payload["session_version"],
        expected_credential_generation=clear_payload["credential_generation"],
    )
    if plan.stale:
        # Nothing to swap, so nothing to stop; the state is reported as it is.
        return {
            "ok": True,
            "cloud_session_state": read_auth_context_state(),
            "stale": True,
        }
    inputs = read_route_inputs()
    result: ClearDesktopSessionResult = await apply_with_stop_barrier(
        before=inputs,
        after=effective_route_identity(replace(inputs, cloud=plan.identity_after)),
        apply=lambda: apply_cloud_session_clear(plan),
    )
    arm_cloud_session_expiry_timer()
    return {
        "ok": True,
        "cloud_session_state": result.state,
        "stale": result.stale,
    }


async def dispatch_control_request(*, request: object) -> ControlSuccessResponse:
    parsed_request = require_request_object(request)
    operation = require_string_field(parsed_request, "operation")
    payload = require_payload_object(parsed_request, field_name="payload")

    if operation == STATUS_OPERATION:
        # The only operation that changes nothing, and the only one that does
        # not queue behind a barrier.
        return _status_response()

    async with identity_changes_serialized():
        if operation == SET_LLM_CONNECTION_OPERATION:
            return await _dispatch_llm_connection(read_llm_connection(payload))
        if operation == CLEAR_LLM_CONNECTION_OPERATION:
            return await _dispatch_llm_connection(None)
        if operation == SET_WEB_SEARCH_CREDENTIAL_OPERATION:
            return await _dispatch_web_search_credential(
                read_web_search_credential(payload)
            )
        if operation == CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION:
            return await _dispatch_web_search_credential(None)
        if operation == integration.SET_OPERATION:
            credential = integration.read_integration_credential(payload)
            return integration.apply_integration_credential(credential)
        if operation == integration.CLEAR_OPERATION:
            return integration.apply_integration_credential(None)
        if operation == CONFIGURE_OPERATION:
            return await _dispatch_configure(payload)
        if operation == SET_CLOUD_SESSION_OPERATION:
            return await _dispatch_set_cloud_session(payload)
        if operation == CLEAR_CLOUD_SESSION_OPERATION:
            return await _dispatch_clear_cloud_session(payload)

        raise MigrationError(f"Unsupported control operation: {operation}")


def _validate_control_socket_path(socket_path: Path) -> None:
    encoded = str(socket_path).encode("utf-8")
    if len(encoded) > CONTROL_SOCKET_MAX_PATH_BYTES:
        raise MigrationError(
            "Local runtime control socket path exceeds macOS Unix socket limits"
        )


def _prepare_control_socket_path(socket_path: Path) -> None:
    _validate_control_socket_path(socket_path)
    socket_dir = socket_path.parent
    socket_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_dir, 0o700)
    if not socket_path.exists():
        return
    socket_stat = os.lstat(socket_path)
    if not stat.S_ISSOCK(socket_stat.st_mode):
        raise MigrationError(
            f"Local runtime control path is not a Unix socket: {socket_path}"
        )
    socket_path.unlink()


def _cleanup_control_socket_path(socket_path: Path) -> None:
    try:
        if socket_path.exists():
            socket_path.unlink()
    except FileNotFoundError:
        return


async def _handle_control_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    response: ControlResponse
    try:
        raw_request = await reader.readline()
        if not raw_request:
            raise MigrationError("Control request must not be empty")
        if len(raw_request) > CONTROL_SOCKET_MAX_REQUEST_BYTES:
            raise MigrationError("Control request exceeds maximum size")
        try:
            decoded_request = raw_request.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError("Control request must be valid UTF-8") from exc
        try:
            parsed_request = json.loads(decoded_request)
        except json.JSONDecodeError as exc:
            raise MigrationError("Control request must be valid JSON") from exc
        response = await dispatch_control_request(request=parsed_request)
    except MigrationError as exc:
        response = build_error_response(
            error_code=INVALID_REQUEST_ERROR_CODE,
            message=str(exc),
        )
    except Exception as exc:
        logger.exception("Local runtime control socket failed: error=%s", exc)
        response = build_error_response(
            error_code=INTERNAL_ERROR_CODE,
            message="Local runtime control socket failed",
        )

    writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def _run_control_socket_loop(socket_path: Path) -> None:
    global _SERVER, _SERVER_LOOP, _SERVER_SOCKET_PATH, _START_ERROR
    loop = asyncio.new_event_loop()
    server: asyncio.AbstractServer | None = None
    asyncio.set_event_loop(loop)
    try:
        _prepare_control_socket_path(socket_path)
        server = loop.run_until_complete(
            asyncio.start_unix_server(_handle_control_client, path=str(socket_path))
        )
        os.chmod(socket_path, 0o600)
        _SERVER_LOOP = loop
        _SERVER = server
        _SERVER_SOCKET_PATH = socket_path
        _START_ERROR = None
        _READY_EVENT.set()
        loop.run_forever()
    except Exception as exc:
        _START_ERROR = exc
        _READY_EVENT.set()
    finally:
        if server is not None:
            server.close()
            loop.run_until_complete(server.wait_closed())
        _cleanup_control_socket_path(socket_path)
        loop.close()
        with _THREAD_LOCK:
            _SERVER = None
            _SERVER_LOOP = None
            _SERVER_SOCKET_PATH = None


def start_local_control_socket_server() -> None:
    global _SERVER_THREAD, _START_ERROR
    socket_path = read_local_runtime_control_socket_path()
    with _THREAD_LOCK:
        if _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
            return
        if _SERVER_THREAD is not None and not _SERVER_THREAD.is_alive():
            _SERVER_THREAD = None
        _START_ERROR = None
        _READY_EVENT.clear()
        thread = threading.Thread(
            target=_run_control_socket_loop,
            args=(socket_path,),
            name="local-runtime-control-socket",
            daemon=True,
        )
        thread.start()
        _SERVER_THREAD = thread
    if not _READY_EVENT.wait(timeout=CONTROL_SOCKET_READY_TIMEOUT_SECONDS):
        raise RuntimeError("Timed out starting local runtime control socket server")
    if _START_ERROR is not None:
        raise _START_ERROR


def _request_server_shutdown() -> None:
    loop = _SERVER_LOOP
    server = _SERVER
    if loop is None:
        return

    async def shutdown() -> None:
        # Before the loop stops, or a settlement caught mid-barrier never runs
        # the ``finally`` that hands admission back.
        await await_cancelled_cloud_session_expiry_work()
        if server is not None:
            server.close()
            await server.wait_closed()
        loop.stop()

    loop.create_task(shutdown())


def stop_local_control_socket_server() -> None:
    global _SERVER_THREAD
    with _THREAD_LOCK:
        thread = _SERVER_THREAD
        loop = _SERVER_LOOP
    if loop is not None:
        loop.call_soon_threadsafe(_request_server_shutdown)
    if thread is not None and thread.is_alive():
        thread.join(timeout=CONTROL_SOCKET_STOP_WAIT_SECONDS)
    with _THREAD_LOCK:
        if _SERVER_THREAD is not None and not _SERVER_THREAD.is_alive():
            _SERVER_THREAD = None


__all__ = [
    "CLEAR_CLOUD_SESSION_OPERATION",
    "CLEAR_LLM_CONNECTION_OPERATION",
    "CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION",
    "CONFIGURE_OPERATION",
    "CONTROL_SOCKET_MAX_REQUEST_BYTES",
    "SET_CLOUD_SESSION_OPERATION",
    "SET_LLM_CONNECTION_OPERATION",
    "SET_WEB_SEARCH_CREDENTIAL_OPERATION",
    "STATUS_OPERATION",
    "dispatch_control_request",
    "start_local_control_socket_server",
    "stop_local_control_socket_server",
]

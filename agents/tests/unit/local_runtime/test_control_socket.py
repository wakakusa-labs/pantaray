from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from starlette.websockets import WebSocket

from pantaray_agents.local_runtime.chat.turn_runs import (
    chat_turn_running,
    run_chat_turn_in_thread,
)
from pantaray_agents.local_runtime.runtime import (
    control_socket,
    session_store,
    stop_barrier,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    NewActionTarget,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.admission import admission_is_open
from pantaray_agents.local_runtime.runtime.connection_store import (
    ApiKeyConnection,
    WebSearchCredential,
    peek_llm_connection,
    peek_web_search_credential,
)
from pantaray_agents.local_runtime.runtime.control_socket import (
    CLEAR_CLOUD_SESSION_OPERATION,
    CLEAR_LLM_CONNECTION_OPERATION,
    CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION,
    CONFIGURE_OPERATION,
    SET_CLOUD_SESSION_OPERATION,
    SET_LLM_CONNECTION_OPERATION,
    SET_WEB_SEARCH_CREDENTIAL_OPERATION,
    STATUS_OPERATION,
    dispatch_control_request,
    start_local_control_socket_server,
    stop_local_control_socket_server,
)
from pantaray_agents.local_runtime.runtime.identity import (
    current_owner_id,
    logged_out_owner_id,
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
)
from pantaray_agents.local_runtime.runtime.local_api_auth import (
    clear_local_api_token,
    issue_local_api_token,
    read_local_api_token,
)
from pantaray_agents.local_runtime.runtime.local_owner import (
    ensure_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    HELPER_INSTANCE_ID_ENV,
    LOCAL_BACKEND_BOUND_HOST_ENV,
    LOCAL_BACKEND_BOUND_PORT_ENV,
    MAIN_PROCESS_PID_ENV,
    build_local_runtime_control_socket_path,
    read_local_runtime_control_socket_path,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    CloudSessionIdentity,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.runtime.stop_barrier import (
    cancel_cloud_session_expiry_work,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
)
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)
from pantaray_agents.orchestration.ws.owner_bound_sockets import (
    OWNER_CHANGED_CLOSE_CODE,
    bind_socket_to_owner,
    release_owner_bound_socket,
)
from pantaray_agents.schema.agent.action import ActionUserMessageInput

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
EXPIRY_SETTLEMENT_WAIT_SECONDS = 5.0


@pytest.fixture()
def local_runtime_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime_dir = Path(tempfile.mkdtemp(prefix="pruds-", dir="/tmp"))
    db_path = runtime_dir / "runtime.db"
    reset_desktop_session_store()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    register_logged_out_owner(
        ensure_logged_out_owner(db_path=db_path, busy_timeout_ms=1_000)
    )
    issue_local_api_token()
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv(HELPER_INSTANCE_ID_ENV, "helper-test-instance")
    monkeypatch.setenv(LOCAL_BACKEND_BOUND_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(LOCAL_BACKEND_BOUND_PORT_ENV, "49152")
    monkeypatch.setenv(MAIN_PROCESS_PID_ENV, "99999")
    yield db_path
    stop_local_control_socket_server()
    cancel_cloud_session_expiry_work()
    reset_desktop_session_store()
    reset_logged_out_owner()
    clear_local_api_token()
    shutil.rmtree(runtime_dir, ignore_errors=True)


def _future_expires_at() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _clear_payload(
    *,
    credential_generation: int,
    reason: str = "signed_out",
    user_id: str = "user-1",
    session_version: str = "1",
    helper_instance_id: str = "helper-test-instance",
) -> dict[str, object]:
    return {
        "reason": reason,
        "account_user_id": user_id,
        "session_version": session_version,
        "credential_generation": credential_generation,
        "helper_instance_id": helper_instance_id,
    }


@pytest.mark.asyncio
async def test_dispatch_control_request_sets_cloud_session(
    local_runtime_env: Path,
) -> None:
    response = await dispatch_control_request(
        request={
            "operation": SET_CLOUD_SESSION_OPERATION,
            "payload": {
                "account_user_id": "user-1",
                "access_token": "header.payload.signature",
                "expires_at": _future_expires_at(),
                "session_version": "1",
            },
        }
    )

    assert response == {
        "ok": True,
        "cloud_session_state": "present",
        "credential_generation": 1,
        "helper_instance_id": "helper-test-instance",
    }


@pytest.mark.asyncio
async def test_dispatch_control_request_rejects_unknown_operation(
    local_runtime_env: Path,
) -> None:
    with pytest.raises(MigrationError, match="Unsupported control operation"):
        await dispatch_control_request(
            request={
                "operation": "rotate_session",
                "payload": {
                    "account_user_id": "user-1",
                },
            }
        )


@pytest.mark.asyncio
async def test_dispatch_control_request_returns_helper_status(
    local_runtime_env: Path,
) -> None:
    response = await dispatch_control_request(
        request={
            "operation": STATUS_OPERATION,
            "payload": {},
        }
    )

    with sqlite3.connect(local_runtime_env) as connection:
        owner_id = connection.execute("SELECT user_id FROM local_owner").fetchone()[0]
    assert response == {
        "ok": True,
        "helper_instance_id": "helper-test-instance",
        "local_api_token": read_local_api_token(),
        "active_owner_id": owner_id,
        "configured": False,
        "cloud_session_state": "absent",
        "credential_generation": 0,
        "llm_route": "unconfigured",
        "web_search_route": "unconfigured",
        "backend_host": "127.0.0.1",
        "backend_port": 49152,
    }


@pytest.mark.asyncio
async def test_local_control_socket_server_round_trips_set_and_clear(
    local_runtime_env: Path,
) -> None:
    start_local_control_socket_server()
    socket_path = read_local_runtime_control_socket_path()
    assert socket_path == build_local_runtime_control_socket_path(
        local_runtime_env.parent
    )

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(
        (
            json.dumps(
                {
                    "operation": SET_CLOUD_SESSION_OPERATION,
                    "payload": {
                        "account_user_id": "user-1",
                        "access_token": "header.payload.signature",
                        "expires_at": _future_expires_at(),
                        "session_version": "1",
                    },
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    await writer.drain()
    import_response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()

    assert import_response == {
        "ok": True,
        "cloud_session_state": "present",
        "credential_generation": 1,
        "helper_instance_id": "helper-test-instance",
    }

    clear_reader, clear_writer = await asyncio.open_unix_connection(str(socket_path))
    clear_writer.write(
        (
            json.dumps(
                {
                    "operation": CLEAR_CLOUD_SESSION_OPERATION,
                    "payload": _clear_payload(credential_generation=1),
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    await clear_writer.drain()
    clear_response = json.loads((await clear_reader.readline()).decode("utf-8"))
    clear_writer.close()
    await clear_writer.wait_closed()

    assert clear_response == {
        "ok": True,
        "cloud_session_state": "absent",
        "stale": False,
    }

    status_reader, status_writer = await asyncio.open_unix_connection(str(socket_path))
    status_writer.write(
        (
            json.dumps(
                {
                    "operation": STATUS_OPERATION,
                    "payload": {},
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    await status_writer.drain()
    status_response = json.loads((await status_reader.readline()).decode("utf-8"))
    status_writer.close()
    await status_writer.wait_closed()

    with sqlite3.connect(local_runtime_env) as connection:
        owner_id = connection.execute("SELECT user_id FROM local_owner").fetchone()[0]
    assert status_response == {
        "ok": True,
        "helper_instance_id": "helper-test-instance",
        "local_api_token": read_local_api_token(),
        "active_owner_id": owner_id,
        "configured": False,
        "cloud_session_state": "absent",
        "credential_generation": 1,
        "llm_route": "unconfigured",
        "web_search_route": "unconfigured",
        "backend_host": "127.0.0.1",
        "backend_port": 49152,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("activate_before_clear", [False, True])
async def test_clear_session_revokes_source_and_pending_activation(
    local_runtime_env: Path,
    activate_before_clear: bool,
) -> None:
    import sqlite3
    from uuid import UUID, uuid4

    from pantaray_agents.local_runtime.context.source_control import (
        context_source_control,
    )
    from pantaray_agents.schema.context_source import (
        ActivateSource,
        RecorderBinding,
        SuspendSource,
    )

    session = {
        "operation": SET_CLOUD_SESSION_OPERATION,
        "payload": {
            "account_user_id": "user-1",
            "access_token": "header.payload.signature",
            "expires_at": _future_expires_at(),
            "session_version": "1",
        },
    }
    await dispatch_control_request(request=session)
    with sqlite3.connect(local_runtime_env) as connection:
        stopped = await context_source_control.transition(
            connection,
            "user-1",
            SuspendSource(
                kind="suspend",
                request_id=uuid4(),
                expected_epoch=UUID(int=0),
                reason="policy_change",
                policy_revision="test-policy",
            ),
        )
        assert stopped.kind == "applied" and stopped.state.kind == "stopped"
        epoch = stopped.state.epoch

        def activation() -> ActivateSource:
            return ActivateSource(
                kind="activate",
                request_id=uuid4(),
                issued_epoch=epoch,
                recorder_binding=RecorderBinding(
                    store_id="test-store", protocol_version=1
                ),
            )

        if activate_before_clear:
            ready = await context_source_control.transition(
                connection, "user-1", activation()
            )
            assert ready.kind == "applied" and ready.state.kind == "ready"
        await dispatch_control_request(
            request={
                "operation": CLEAR_CLOUD_SESSION_OPERATION,
                "payload": _clear_payload(credential_generation=1),
            }
        )
        async with context_source_control.gate.turn():
            assert context_source_control.gate.current("user-1") is None
        await dispatch_control_request(request=session)
        state = await context_source_control.read(connection, "user-1")
        assert state.kind == "stopped"
        blocked = await context_source_control.transition(
            connection, "user-1", activation()
        )
        assert blocked.kind == "blocked"


@pytest.mark.asyncio
async def test_switching_accounts_revokes_the_previous_owner_permit(
    local_runtime_env: Path,
) -> None:
    import sqlite3
    from uuid import UUID, uuid4

    from pantaray_agents.local_runtime.context.source_control import (
        context_source_control,
    )
    from pantaray_agents.local_runtime.runtime.identity import current_owner_id
    from pantaray_agents.schema.context_source import (
        ActivateSource,
        RecorderBinding,
        SuspendSource,
    )

    def session(user_id: str, session_version: str) -> dict[str, object]:
        return {
            "operation": SET_CLOUD_SESSION_OPERATION,
            "payload": {
                "account_user_id": user_id,
                "access_token": "header.payload.signature",
                "expires_at": _future_expires_at(),
                "session_version": session_version,
            },
        }

    await dispatch_control_request(request=session("user-1", "1"))
    with sqlite3.connect(local_runtime_env) as connection:
        stopped = await context_source_control.transition(
            connection,
            "user-1",
            SuspendSource(
                kind="suspend",
                request_id=uuid4(),
                expected_epoch=UUID(int=0),
                reason="policy_change",
                policy_revision="test-policy",
            ),
        )
        assert stopped.kind == "applied" and stopped.state.kind == "stopped"
        ready = await context_source_control.transition(
            connection,
            "user-1",
            ActivateSource(
                kind="activate",
                request_id=uuid4(),
                issued_epoch=stopped.state.epoch,
                recorder_binding=RecorderBinding(
                    store_id="test-store", protocol_version=1
                ),
            ),
        )
        assert ready.kind == "applied" and ready.state.kind == "ready"
        async with context_source_control.gate.turn():
            assert context_source_control.gate.current("user-1") is not None

    await dispatch_control_request(request=session("user-2", "2"))
    async with context_source_control.gate.turn():
        assert context_source_control.gate.current("user-1") is None

    # A delayed sign-out for user-1 is stale and leaves user-2 untouched.
    stale = await dispatch_control_request(
        request={
            "operation": CLEAR_CLOUD_SESSION_OPERATION,
            "payload": _clear_payload(credential_generation=1),
        }
    )
    assert stale["stale"] is True
    assert current_owner_id() == "user-2"


@pytest.mark.asyncio
async def test_expired_clear_keeps_owner_and_stale_clear_is_ignored(
    local_runtime_env: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime.identity import current_owner_id

    await dispatch_control_request(
        request={
            "operation": SET_CLOUD_SESSION_OPERATION,
            "payload": {
                "account_user_id": "user-1",
                "access_token": "header.payload.signature",
                "expires_at": _future_expires_at(),
                "session_version": "1",
            },
        }
    )
    stale = await dispatch_control_request(
        request={
            "operation": CLEAR_CLOUD_SESSION_OPERATION,
            "payload": _clear_payload(reason="expired", credential_generation=0),
        }
    )
    assert stale == {
        "ok": True,
        "cloud_session_state": "present",
        "stale": True,
    }

    expired = await dispatch_control_request(
        request={
            "operation": CLEAR_CLOUD_SESSION_OPERATION,
            "payload": _clear_payload(reason="expired", credential_generation=1),
        }
    )
    assert expired == {
        "ok": True,
        "cloud_session_state": "expired",
        "stale": False,
    }
    assert current_owner_id() == "user-1"


@pytest.mark.asyncio
async def test_configure_restores_an_expired_account_as_owner_then_forgets_it(
    local_runtime_env: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime.identity import (
        current_owner_id,
        logged_out_owner_id,
    )

    restored = await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": {
                    "state": "expired",
                    "account_user_id": "user-1",
                    "session_version": "3",
                }
            },
        }
    )
    assert restored == {
        "ok": True,
        "configured": True,
        "cloud_session_state": "expired",
        "credential_generation": 1,
        "helper_instance_id": "helper-test-instance",
        "llm_route": "cloud",
        "web_search_route": "cloud",
    }
    assert current_owner_id() == "user-1"
    with sqlite3.connect(local_runtime_env) as connection:
        user_rows = connection.execute(
            "SELECT COUNT(*) FROM users WHERE user_id = 'user-1'"
        ).fetchone()[0]
    assert user_rows == 1
    status = await dispatch_control_request(
        request={"operation": STATUS_OPERATION, "payload": {}}
    )
    assert status["configured"] is True

    # A sign-out issued for the old helper lifetime never matches this one.
    foreign = await dispatch_control_request(
        request={
            "operation": CLEAR_CLOUD_SESSION_OPERATION,
            "payload": _clear_payload(
                credential_generation=1,
                session_version="3",
                helper_instance_id="helper-previous-instance",
            ),
        }
    )
    assert foreign == {"ok": True, "cloud_session_state": "expired", "stale": True}
    assert current_owner_id() == "user-1"

    signed_out = await dispatch_control_request(
        request={
            "operation": CLEAR_CLOUD_SESSION_OPERATION,
            "payload": _clear_payload(credential_generation=1, session_version="3"),
        }
    )
    assert signed_out == {"ok": True, "cloud_session_state": "absent", "stale": False}
    assert current_owner_id() == logged_out_owner_id()


@pytest.mark.asyncio
async def test_configure_with_an_absent_session_drops_a_present_identity(
    local_runtime_env: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime.identity import (
        current_owner_id,
        logged_out_owner_id,
    )

    await dispatch_control_request(
        request={
            "operation": SET_CLOUD_SESSION_OPERATION,
            "payload": {
                "account_user_id": "user-1",
                "access_token": "header.payload.signature",
                "expires_at": _future_expires_at(),
                "session_version": "1",
            },
        }
    )
    response = await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {"cloud_session": {"state": "absent"}},
        }
    )
    assert response == {
        "ok": True,
        "configured": True,
        "cloud_session_state": "absent",
        "credential_generation": 1,
        "helper_instance_id": "helper-test-instance",
        "llm_route": "unconfigured",
        "web_search_route": "unconfigured",
    }
    assert current_owner_id() == logged_out_owner_id()


def _api_key_connection_payload(
    *, api_key: str = "openai-secret-key"
) -> dict[str, object]:
    return {
        "kind": "api_key",
        "provider": "openai",
        "model": "gpt-5",
        "api_key": api_key,
    }


def _web_search_credential_payload(
    *, api_key: str = "tavily-secret-key"
) -> dict[str, object]:
    return {"provider": "tavily", "api_key": api_key}


@pytest.mark.asyncio
async def test_configure_applies_the_cloud_session_and_both_connections(
    local_runtime_env: Path,
) -> None:
    response = await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": {"state": "absent"},
                "llm_connection": _api_key_connection_payload(),
                "web_search_credential": _web_search_credential_payload(),
            },
        }
    )

    assert response == {
        "ok": True,
        "configured": True,
        "cloud_session_state": "absent",
        "credential_generation": 0,
        "helper_instance_id": "helper-test-instance",
        "llm_route": "direct",
        "web_search_route": "direct",
    }
    assert peek_llm_connection() == ApiKeyConnection(
        provider="openai", model="gpt-5", api_key="openai-secret-key"
    )
    assert peek_web_search_credential() == WebSearchCredential(
        api_key="tavily-secret-key"
    )


@pytest.mark.asyncio
async def test_configure_keeps_a_signed_in_helper_on_the_cloud_route(
    local_runtime_env: Path,
) -> None:
    response = await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": {
                    "state": "present",
                    "account_user_id": "user-1",
                    "access_token": "header.payload.signature",
                    "expires_at": _future_expires_at(),
                    "session_version": "1",
                },
                "llm_connection": _api_key_connection_payload(),
                "web_search_credential": _web_search_credential_payload(),
            },
        }
    )

    assert response["cloud_session_state"] == "present"
    assert response["llm_route"] == "cloud"
    assert response["web_search_route"] == "cloud"


@pytest.mark.asyncio
async def test_configure_rejects_a_malformed_connection_before_applying_anything(
    local_runtime_env: Path,
) -> None:
    with pytest.raises(MigrationError, match="llm_connection.provider"):
        await dispatch_control_request(
            request={
                "operation": CONFIGURE_OPERATION,
                "payload": {
                    "cloud_session": {
                        "state": "present",
                        "account_user_id": "user-1",
                        "access_token": "header.payload.signature",
                        "expires_at": _future_expires_at(),
                        "session_version": "1",
                    },
                    "llm_connection": {
                        "kind": "api_key",
                        "provider": "gemini",
                        "model": "gemini-3",
                        "api_key": "gemini-key",
                    },
                    "web_search_credential": _web_search_credential_payload(),
                },
            }
        )

    status = await dispatch_control_request(
        request={"operation": STATUS_OPERATION, "payload": {}}
    )
    assert status["configured"] is False
    assert status["cloud_session_state"] == "absent"
    assert peek_llm_connection() is None
    assert peek_web_search_credential() is None


@pytest.mark.asyncio
async def test_connection_operations_switch_the_published_routes(
    local_runtime_env: Path,
) -> None:
    await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {"cloud_session": {"state": "absent"}},
        }
    )

    assert await dispatch_control_request(
        request={
            "operation": SET_LLM_CONNECTION_OPERATION,
            "payload": _api_key_connection_payload(),
        }
    ) == {"ok": True, "llm_route": "direct"}
    assert await dispatch_control_request(
        request={
            "operation": SET_WEB_SEARCH_CREDENTIAL_OPERATION,
            "payload": _web_search_credential_payload(),
        }
    ) == {"ok": True, "web_search_route": "direct"}

    assert await dispatch_control_request(
        request={"operation": CLEAR_LLM_CONNECTION_OPERATION, "payload": {}}
    ) == {"ok": True, "llm_route": "unconfigured"}
    assert await dispatch_control_request(
        request={"operation": CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION, "payload": {}}
    ) == {"ok": True, "web_search_route": "unconfigured"}
    assert peek_llm_connection() is None
    assert peek_web_search_credential() is None


@pytest.mark.asyncio
async def test_an_expired_cloud_session_never_routes_a_stored_connection_directly(
    local_runtime_env: Path,
) -> None:
    await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": {
                    "state": "expired",
                    "account_user_id": "user-1",
                    "session_version": "3",
                },
                "llm_connection": _api_key_connection_payload(),
                "web_search_credential": _web_search_credential_payload(),
            },
        }
    )

    status = await dispatch_control_request(
        request={"operation": STATUS_OPERATION, "payload": {}}
    )
    assert status["cloud_session_state"] == "expired"
    assert status["llm_route"] == "cloud"
    assert status["web_search_route"] == "cloud"


@pytest.mark.asyncio
async def test_status_publishes_routes_without_the_connection_credentials(
    local_runtime_env: Path,
) -> None:
    await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": {"state": "absent"},
                "llm_connection": _api_key_connection_payload(),
                "web_search_credential": _web_search_credential_payload(),
            },
        }
    )

    status = await dispatch_control_request(
        request={"operation": STATUS_OPERATION, "payload": {}}
    )

    assert status["llm_route"] == "direct"
    assert status["web_search_route"] == "direct"
    serialized = json.dumps(status)
    assert "openai-secret-key" not in serialized
    assert "tavily-secret-key" not in serialized


def _sign_in_request(
    *,
    user_id: str = "user-1",
    session_version: str = "1",
    access_token: str = "header.payload.signature",
    expires_at: str | None = None,
) -> dict[str, object]:
    return {
        "operation": SET_CLOUD_SESSION_OPERATION,
        "payload": {
            "account_user_id": user_id,
            "access_token": access_token,
            "expires_at": _future_expires_at() if expires_at is None else expires_at,
            "session_version": session_version,
        },
    }


async def _configure(*, signed_in: bool) -> None:
    """Get the helper past ``configure`` with a known starting identity.

    Both direct settings are stored either way, so a signed-in helper is one
    that has somewhere to fall back to and still routes to the cloud.
    """
    cloud_session: dict[str, object] = (
        {
            "state": "present",
            "account_user_id": "user-1",
            "access_token": "header.payload.signature",
            "expires_at": _future_expires_at(),
            "session_version": "1",
        }
        if signed_in
        else {"state": "absent"}
    )
    await dispatch_control_request(
        request={
            "operation": CONFIGURE_OPERATION,
            "payload": {
                "cloud_session": cloud_session,
                "llm_connection": _api_key_connection_payload(),
                "web_search_credential": _web_search_credential_payload(),
            },
        }
    )


def _start_action_run(
    *, db_path: Path, user_id: str, message_id: str = "message-1"
) -> SubmitActionMessageResult:
    """Put one Action run in flight for ``user_id``, the way the worker does."""
    result = submit_action_message(
        SubmitActionMessageCommand(
            user_id=user_id,
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id=message_id, content="keep running"
            ),
        )
    )
    payload = claim_next_pending_action_job(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id=user_id,
        claimed_by=f"worker:{message_id}",
    )
    assert payload is not None
    preparation = ActionJobRuntimeRepository(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ).prepare_execution(payload=payload, started_at="2026-09-18T00:00:00Z")
    assert preparation.skip_outcome is None
    return result


def _action_status(db_path: Path, action_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        return str(
            connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()[0]
        )


def _cancel_is_requested(db_path: Path, job_id: str) -> bool:
    with sqlite3.connect(db_path) as connection:
        return (
            connection.execute(
                "SELECT cancel_requested_at FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            is not None
        )


class _LedgeredSocket:
    """Stands in for an Orchestration WebSocket in the owner ledger.

    Closing real sockets is covered by ``tests/unit/test_ws_owner_bound_sockets``;
    what these tests ask is whether the barrier reaches for that close at all.
    """

    def __init__(self) -> None:
        self.close_codes: list[int] = []

    async def close(self, code: int, reason: str) -> None:
        self.close_codes.append(code)


@contextlib.contextmanager
def _socket_bound_to(owner_id: str) -> Iterator[_LedgeredSocket]:
    socket = _LedgeredSocket()
    websocket = cast(WebSocket, socket)
    bind_socket_to_owner(owner_id=owner_id, websocket=websocket)
    try:
        yield socket
    finally:
        release_owner_bound_socket(owner_id=owner_id, websocket=websocket)


def _observe_the_swap(
    monkeypatch: pytest.MonkeyPatch, observe: Callable[[], object]
) -> list[object]:
    """Record what is already true at the moment the new identity is written.

    The swap itself is the only externally visible instant between the barrier's
    steps, so ordering is asserted from inside the real
    ``apply_cloud_session_import`` rather than from a replaced route or identity
    function.
    """
    observed: list[object] = []
    apply_the_swap = control_socket.apply_cloud_session_import

    def spy(**fields: Any) -> str:
        observed.append(observe())
        return apply_the_swap(**fields)

    monkeypatch.setattr(control_socket, "apply_cloud_session_import", spy)
    return observed


@pytest.mark.asyncio
async def test_signing_in_stops_the_logged_out_run_before_the_owner_changes(
    local_runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(signed_in=False)
    owner_id = logged_out_owner_id()
    run = _start_action_run(db_path=local_runtime_env, user_id=owner_id)

    with _socket_bound_to(owner_id) as socket:
        observed = _observe_the_swap(
            monkeypatch,
            lambda: (
                _cancel_is_requested(local_runtime_env, run.job_id),
                admission_is_open(),
                socket.close_codes,
            ),
        )
        await dispatch_control_request(request=_sign_in_request())

    # The cancel is durable and the departing owner's socket is gone before
    # anything can read the account as the new owner.
    assert observed == [(True, False, [OWNER_CHANGED_CLOSE_CODE])]
    assert _action_status(local_runtime_env, run.action_id) == "canceled"
    assert current_owner_id() == "user-1"
    assert admission_is_open()


@pytest.mark.asyncio
async def test_signing_in_stops_the_logged_out_owners_chat_turn_first(
    local_runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _configure(signed_in=False)
    owner_id = logged_out_owner_id()
    started = threading.Event()

    async def turn() -> str:
        started.set()
        await asyncio.sleep(60)
        return "answered"

    outcome = run_chat_turn_in_thread(owner_id, turn)
    await asyncio.to_thread(started.wait, 5)
    observed = _observe_the_swap(monkeypatch, lambda: chat_turn_running(owner_id))

    await dispatch_control_request(request=_sign_in_request())

    # Stopped where it awaited, before anything read the account as the owner.
    assert observed == [False]
    assert outcome.result(timeout=5) == "stopped"


@pytest.mark.asyncio
async def test_signing_out_stops_the_accounts_run_and_closes_its_sockets(
    local_runtime_env: Path,
) -> None:
    await _configure(signed_in=True)
    run = _start_action_run(db_path=local_runtime_env, user_id="user-1")

    with _socket_bound_to("user-1") as socket:
        response = await dispatch_control_request(
            request={
                "operation": CLEAR_CLOUD_SESSION_OPERATION,
                "payload": _clear_payload(credential_generation=1),
            }
        )
        assert socket.close_codes == [OWNER_CHANGED_CLOSE_CODE]

    assert response == {"ok": True, "cloud_session_state": "absent", "stale": False}
    assert _action_status(local_runtime_env, run.action_id) == "canceled"
    assert current_owner_id() == logged_out_owner_id()


@pytest.mark.asyncio
async def test_switching_accounts_stops_the_previous_accounts_run(
    local_runtime_env: Path,
) -> None:
    await _configure(signed_in=True)
    run = _start_action_run(db_path=local_runtime_env, user_id="user-1")

    with _socket_bound_to("user-1") as socket:
        await dispatch_control_request(
            request=_sign_in_request(user_id="user-2", session_version="2")
        )
        assert socket.close_codes == [OWNER_CHANGED_CLOSE_CODE]

    assert _action_status(local_runtime_env, run.action_id) == "canceled"
    assert current_owner_id() == "user-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        pytest.param(
            SET_LLM_CONNECTION_OPERATION,
            _api_key_connection_payload(api_key="openai-rotated-key"),
            id="another_llm_key",
        ),
        pytest.param(CLEAR_LLM_CONNECTION_OPERATION, {}, id="no_llm_connection"),
        pytest.param(
            SET_WEB_SEARCH_CREDENTIAL_OPERATION,
            _web_search_credential_payload(api_key="tavily-rotated-key"),
            id="another_search_key",
        ),
        pytest.param(
            CLEAR_WEB_SEARCH_CREDENTIAL_OPERATION, {}, id="no_search_credential"
        ),
    ],
)
async def test_a_new_direct_destination_stops_the_run_but_leaves_the_sockets(
    local_runtime_env: Path, operation: str, payload: dict[str, object]
) -> None:
    await _configure(signed_in=False)
    owner_id = logged_out_owner_id()
    run = _start_action_run(db_path=local_runtime_env, user_id=owner_id)

    with _socket_bound_to(owner_id) as socket:
        await dispatch_control_request(
            request={"operation": operation, "payload": payload}
        )
        # The owner has not changed, so its sockets still answer to the right one.
        assert socket.close_codes == []

    assert _action_status(local_runtime_env, run.action_id) == "canceled"
    assert current_owner_id() == owner_id
    assert admission_is_open()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signed_in", "build_request"),
    [
        pytest.param(
            True,
            lambda: _sign_in_request(access_token="header.rotated.signature"),
            id="token_rotation_for_the_same_sign_in",
        ),
        pytest.param(
            True,
            lambda: {
                "operation": SET_LLM_CONNECTION_OPERATION,
                "payload": _api_key_connection_payload(api_key="openai-rotated-key"),
            },
            id="direct_connection_stored_while_signed_in",
        ),
        pytest.param(
            True,
            lambda: {
                "operation": CLEAR_CLOUD_SESSION_OPERATION,
                "payload": _clear_payload(credential_generation=0),
            },
            id="stale_clear",
        ),
        pytest.param(
            False,
            lambda: {
                "operation": SET_LLM_CONNECTION_OPERATION,
                "payload": _api_key_connection_payload(),
            },
            id="the_same_connection_resent",
        ),
        pytest.param(
            False,
            lambda: {
                "operation": SET_LLM_CONNECTION_OPERATION,
                "payload": {**_api_key_connection_payload(), "model": "gpt-6-luna"},
            },
            id="model_switch_during_action",
        ),
    ],
)
async def test_an_operation_that_keeps_the_identity_stops_nothing(
    local_runtime_env: Path,
    signed_in: bool,
    build_request: Callable[[], dict[str, object]],
) -> None:
    await _configure(signed_in=signed_in)
    owner_id = "user-1" if signed_in else logged_out_owner_id()
    run = _start_action_run(db_path=local_runtime_env, user_id=owner_id)

    with _socket_bound_to(owner_id) as socket:
        await dispatch_control_request(request=build_request())
        assert socket.close_codes == []

    assert _action_status(local_runtime_env, run.action_id) == "processing"
    assert not _cancel_is_requested(local_runtime_env, run.job_id)
    assert current_owner_id() == owner_id
    assert admission_is_open()


@pytest.mark.asyncio
async def test_a_rejected_sign_in_stops_nothing_and_reopens_admission(
    local_runtime_env: Path,
) -> None:
    await _configure(signed_in=False)
    owner_id = logged_out_owner_id()
    run = _start_action_run(db_path=local_runtime_env, user_id=owner_id)

    with _socket_bound_to(owner_id) as socket:
        with pytest.raises(MigrationError, match="expires_at must be in the future"):
            await dispatch_control_request(
                request=_sign_in_request(expires_at="2000-01-01T00:00:00Z")
            )
        assert socket.close_codes == []

    # The store refuses this payload, so the identity never changes and the run
    # must not pay for it.
    assert _action_status(local_runtime_env, run.action_id) == "processing"
    assert not _cancel_is_requested(local_runtime_env, run.job_id)
    assert admission_is_open()
    assert current_owner_id() == owner_id


def _requeue_like_startup_recovery(
    db_path: Path, run: SubmitActionMessageResult
) -> None:
    """Leave the run the shape ``action_startup_recovery`` leaves it in.

    The job goes back to ``queued`` and the process to ``enqueued``, while
    ``agent_actions.status`` stays ``processing``: the run is waiting to resume,
    not running.
    """
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                "UPDATE processes SET status = 'enqueued', current_job_id = NULL "
                "WHERE process_id = ?",
                (run.process_id,),
            )
            connection.execute(
                "UPDATE jobs SET status = 'queued', claimed_by = NULL, "
                "claimed_at = NULL, heartbeat_at = NULL WHERE job_id = ?",
                (run.job_id,),
            )


@pytest.mark.asyncio
async def test_the_first_configure_leaves_a_run_waiting_to_resume_alone(
    local_runtime_env: Path,
) -> None:
    run = _start_action_run(db_path=local_runtime_env, user_id=logged_out_owner_id())
    _requeue_like_startup_recovery(local_runtime_env, run)

    await _configure(signed_in=False)

    # Nothing could have been claimed before ``configure``, so there was nothing
    # for the barrier to stop.
    assert _action_status(local_runtime_env, run.action_id) == "processing"
    assert not _cancel_is_requested(local_runtime_env, run.job_id)
    assert admission_is_open()
    claimed = claim_next_pending_action_job(
        db_path=local_runtime_env,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id=logged_out_owner_id(),
        claimed_by="worker:after-configure",
    )
    assert claimed is not None and claimed["action_id"] == run.action_id


class _MovableClock:
    """The session store's clock, so a lifetime can run out without waiting."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def _wait_for(condition: Callable[[], bool]) -> None:
    """Let the helper's own expiry timer run, without pinning how long it takes."""
    deadline = time.monotonic() + EXPIRY_SETTLEMENT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the cloud session expiry never reached the barrier")


@pytest.mark.asyncio
async def test_a_lapsing_cloud_session_publishes_expired_through_the_barrier(
    local_runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _MovableClock()
    monkeypatch.setattr(session_store, "_now_utc", clock)
    await _configure(signed_in=True)
    run = _start_action_run(db_path=local_runtime_env, user_id="user-1")

    with _socket_bound_to("user-1") as socket:
        # A token rotation inside the same sign-in keeps the identity, so it
        # stops nothing; it only re-arms the timer on the shorter lifetime.
        await dispatch_control_request(
            request=_sign_in_request(
                access_token="header.rotated.signature",
                expires_at=(clock.now + timedelta(milliseconds=1)).isoformat(),
            )
        )
        assert _action_status(local_runtime_env, run.action_id) == "processing"
        clock.now += timedelta(hours=1)

        # The barrier is through when the cancel is durable and it has handed
        # admission back.
        await _wait_for(
            lambda: (
                _action_status(local_runtime_env, run.action_id) == "canceled"
                and admission_is_open()
            )
        )
        # The account keeps owning its data while the session is expired, so its
        # sockets are not the ones that go.
        assert socket.close_codes == []

    # Read without settling, or the read would publish the expiry it is checking.
    assert session_store.peek_cloud_session_identity_without_settling() == (
        CloudSessionIdentity(state="expired", user_id="user-1", session_version="1")
    )
    assert current_owner_id() == "user-1"


def _record_admission_windows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Log each barrier's admission window, in the order they open and close."""
    windows: list[str] = []
    close_admission = stop_barrier.admission_closed

    @contextlib.contextmanager
    def recording() -> Iterator[None]:
        with close_admission():
            windows.append("closed")
            try:
                yield
            finally:
                windows.append("open")

    monkeypatch.setattr(stop_barrier, "admission_closed", recording)
    return windows


def _send_from_inside_the_barrier(
    monkeypatch: pytest.MonkeyPatch, request: dict[str, object]
) -> list[asyncio.Task[object]]:
    """Send one control request from inside the first barrier's cancel step.

    That is the window an overlap would open in: admission is closed and the
    barrier is awaiting the cancel, which is exactly where another task on the
    same loop gets to run.
    """
    started: list[asyncio.Task[object]] = []
    cancel_the_action = stop_barrier.execute_action_cancel

    async def spy(**fields: Any) -> bool:
        if not started:
            started.append(
                asyncio.create_task(dispatch_control_request(request=request))
            )
            for _ in range(5):
                await asyncio.sleep(0)
        return await cancel_the_action(**fields)

    monkeypatch.setattr(stop_barrier, "execute_action_cancel", spy)
    return started


async def _lapse_the_session(clock: _MovableClock) -> None:
    """Rotate the token onto a lifetime that is over a moment later.

    The rotation keeps the sign-in, so it stops nothing; it only re-arms the
    expiry timer, which is how a session is made to lapse without waiting.
    """
    await dispatch_control_request(
        request=_sign_in_request(
            access_token="header.rotated.signature",
            expires_at=(clock.now + timedelta(milliseconds=1)).isoformat(),
        )
    )
    clock.now += timedelta(hours=1)


@pytest.mark.asyncio
async def test_an_operation_arriving_mid_barrier_waits_and_then_sees_the_new_state(
    local_runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _MovableClock()
    monkeypatch.setattr(session_store, "_now_utc", clock)
    await _configure(signed_in=True)
    run = _start_action_run(db_path=local_runtime_env, user_id="user-1")
    windows = _record_admission_windows(monkeypatch)
    # Electron main's refresh lands while the old lifetime is being settled;
    # the two are correlated, because the refresh failing is what lets it lapse.
    sign_in = _send_from_inside_the_barrier(
        monkeypatch, _sign_in_request(access_token="header.renewed.signature")
    )

    await _lapse_the_session(clock)
    await _wait_for(lambda: bool(sign_in) and sign_in[0].done())

    assert await sign_in[0] == {
        "ok": True,
        "cloud_session_state": "present",
        "credential_generation": 3,
        "helper_instance_id": "helper-test-instance",
    }
    # Two windows, one after the other. Overlapping would show as
    # closed/closed/open/open, and a sign-in that had snapshotted the session as
    # still present would have found nothing to change and opened no window.
    assert windows == ["closed", "open", "closed", "open"]
    assert _action_status(local_runtime_env, run.action_id) == "canceled"
    assert current_owner_id() == "user-1"
    assert admission_is_open()


@pytest.mark.asyncio
async def test_stopping_the_runtime_lets_an_interrupted_settlement_reopen_admission(
    local_runtime_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _MovableClock()
    monkeypatch.setattr(session_store, "_now_utc", clock)
    await _configure(signed_in=True)
    _start_action_run(db_path=local_runtime_env, user_id="user-1")
    held = asyncio.Event()

    async def hold_the_barrier(**fields: Any) -> bool:
        await held.wait()
        return True

    monkeypatch.setattr(stop_barrier, "execute_action_cancel", hold_the_barrier)
    await _lapse_the_session(clock)
    await _wait_for(lambda: not admission_is_open())

    await stop_barrier.await_cancelled_cloud_session_expiry_work()

    # The control socket's loop is about to close; a settlement left pending
    # inside the barrier would never run the finally that admits work again.
    assert admission_is_open()

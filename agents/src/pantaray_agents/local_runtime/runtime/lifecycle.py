from __future__ import annotations

from .connection_store import reset_connection_store
from .control_socket import (
    start_local_control_socket_server,
    stop_local_control_socket_server,
)
from .identity import reset_logged_out_owner
from .initialization import (
    initialize_local_runtime,
    release_initialized_local_runtime,
)
from .integration_credentials import apply_integration_credential
from .local_api_auth import clear_local_api_token, issue_local_api_token
from .session_store import reset_desktop_session_store
from .worker_daemon import (
    start_local_action_worker_daemon,
    stop_local_action_worker_daemon,
)


def start_local_runtime_if_enabled() -> None:
    initialized = initialize_local_runtime()
    worker_started = False
    try:
        issue_local_api_token()
        start_local_action_worker_daemon(runtime_process_lock=initialized.lock_lease)
        worker_started = True
        start_local_control_socket_server()
    except Exception:
        stop_local_control_socket_server()
        stop_local_action_worker_daemon()
        reset_connection_store()
        apply_integration_credential(None)
        reset_desktop_session_store()
        reset_logged_out_owner()
        clear_local_api_token()
        if not worker_started:
            release_initialized_local_runtime(initialized)
        raise


def stop_local_runtime_if_enabled() -> None:
    stop_local_control_socket_server()
    stop_local_action_worker_daemon()
    reset_connection_store()
    apply_integration_credential(None)
    reset_desktop_session_store()
    reset_logged_out_owner()
    clear_local_api_token()


__all__ = [
    "start_local_runtime_if_enabled",
    "stop_local_runtime_if_enabled",
]

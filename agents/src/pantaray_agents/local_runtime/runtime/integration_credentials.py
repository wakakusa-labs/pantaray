"""The external-app integration credential, held in this process's memory only.

Electron main keeps the durable copy and sends it over the control socket, in
``configure`` after every runtime start and in the two operations below when
the user saves or removes it. Like the other connection settings it is never
persisted, logged, quoted in an error or returned in ``status``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Final, Literal

from pantaray_agents.local_runtime.integrations.composio.gateway import (
    install_composio_gateway,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError

from .control_payload import (
    IntegrationCredentialSuccessResponse,
    require_payload_object,
    require_string_field,
)

SET_OPERATION: Final = "set_integration_credential"
CLEAR_OPERATION: Final = "clear_integration_credential"
CONFIGURE_FIELD: Final = "integration_credential"


@dataclass(frozen=True, slots=True)
class ComposioCredential:
    api_key: str = field(repr=False)
    provider: Literal["composio"] = "composio"


# One element per connection method; Composio is the only one.
type IntegrationCredential = ComposioCredential


_LOCK = Lock()
_CREDENTIAL: IntegrationCredential | None = None


def read_integration_credential(payload: dict[str, object]) -> IntegrationCredential:
    # A rejection never quotes a value: a key in the wrong field must not come back.
    if require_string_field(payload, "provider") != "composio":
        raise MigrationError("integration_credential.provider must be composio")
    return ComposioCredential(api_key=require_string_field(payload, "api_key"))


def read_optional_integration_credential(
    payload: dict[str, object],
) -> IntegrationCredential | None:
    if payload.get(CONFIGURE_FIELD) is None:
        return None
    return read_integration_credential(
        require_payload_object(payload, field_name=CONFIGURE_FIELD)
    )


def apply_integration_credential(
    credential: IntegrationCredential | None,
) -> IntegrationCredentialSuccessResponse:
    """Hold the credential and register its gateway; ``None`` clears both.

    The same credential sent again keeps the running gateway, its caches and
    its tool index. No stop barrier: a run that started on another key resolves
    every call again and finds its old accounts gone.
    """

    global _CREDENTIAL
    with _LOCK:
        if credential != _CREDENTIAL:
            _CREDENTIAL = credential
            install_composio_gateway(None if credential is None else credential.api_key)
        return {"ok": True, "integration_configured": _CREDENTIAL is not None}


__all__ = [
    "CLEAR_OPERATION",
    "CONFIGURE_FIELD",
    "ComposioCredential",
    "IntegrationCredential",
    "SET_OPERATION",
    "apply_integration_credential",
    "read_integration_credential",
    "read_optional_integration_credential",
]

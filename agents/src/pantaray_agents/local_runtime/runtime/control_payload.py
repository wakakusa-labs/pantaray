"""The shapes the control socket reads and writes, and the rules its fields obey.

Requests arrive as untrusted JSON from Electron main, so every field is read
through one of the ``require_*`` helpers. Responses carry only what design 6.2
lists: never a credential, and never a field value quoted back in an error.

Everything here is pure: it knows what a payload has to look like and nothing
about what the runtime currently holds. ``session_store`` and
``connection_store`` build on it, which is why it depends on neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError

if TYPE_CHECKING:
    # ``connection_store`` reads its payloads through this module, so importing
    # its route type at runtime would close the cycle. Only the annotations
    # below need it, and ``from __future__ import annotations`` leaves those
    # unevaluated.
    from .connection_store import ConnectionRoute

CLEAR_REASON_SIGNED_OUT: Final[Literal["signed_out"]] = "signed_out"
CLEAR_REASON_EXPIRED: Final[Literal["expired"]] = "expired"

type ClearReason = Literal["signed_out", "expired"]


@dataclass(frozen=True, slots=True)
class CloudSessionImport:
    """An incoming session that has passed every check, in its stored form."""

    user_id: str
    desktop_access_token: str
    expires_at: str
    session_version: str


def require_positive_timeout(busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")


def require_non_empty(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise MigrationError(f"{field_name} must not be empty")
    return normalized


def parse_session_version(value: str) -> int:
    normalized = require_non_empty(value, field_name="session_version")
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise MigrationError("session_version must be a positive integer") from exc
    if parsed <= 0:
        raise MigrationError("session_version must be a positive integer")
    return parsed


def normalize_session_version(value: str) -> str:
    """Validate a ``session_version`` and return the form the store keeps it in.

    This is the single normalization point for the version. A caller that builds
    a ``CloudSessionIdentity`` from a control-socket payload to compare against
    the store's own passes the raw field through here rather than normalizing it
    itself, so the two sides cannot drift apart (design 6.2).
    """
    return str(parse_session_version(value))


def parse_iso8601(value: str, *, field_name: str) -> datetime:
    normalized = require_non_empty(value, field_name=field_name)
    candidate = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MigrationError(f"{field_name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MigrationError(f"{field_name} must include timezone information")
    return parsed.astimezone(UTC)


def validate_cloud_session_fields(
    *,
    user_id: str,
    desktop_access_token: str,
    expires_at: str,
    session_version: str,
    now: datetime,
) -> CloudSessionImport:
    """Check every field of an incoming session, and normalize it for storage.

    ``now`` is passed in because the clock belongs to the store that decides
    when a lifetime is over, not to the field rules.
    """
    expires_at_dt = parse_iso8601(expires_at, field_name="expires_at")
    if expires_at_dt <= now:
        raise MigrationError("expires_at must be in the future")
    return CloudSessionImport(
        user_id=require_non_empty(user_id, field_name="user_id"),
        desktop_access_token=require_non_empty(
            desktop_access_token, field_name="desktop_access_token"
        ),
        # Keeps the Cloud-issued expiry precision; readers parse it before comparing.
        expires_at=expires_at_dt.isoformat().replace("+00:00", "Z"),
        session_version=normalize_session_version(session_version),
    )


class SetCloudSessionPayload(TypedDict):
    account_user_id: str
    access_token: str
    expires_at: str
    session_version: str


class ClearCloudSessionPayload(TypedDict):
    reason: ClearReason
    account_user_id: str
    session_version: str
    credential_generation: int
    helper_instance_id: str


class CloudSessionSuccessResponse(TypedDict):
    ok: Literal[True]
    cloud_session_state: str
    credential_generation: int
    # The helper that issued this generation; main pairs the two for later clears.
    helper_instance_id: str


class ConfigureSuccessResponse(TypedDict):
    ok: Literal[True]
    configured: Literal[True]
    cloud_session_state: str
    credential_generation: int
    helper_instance_id: str
    llm_route: ConnectionRoute
    web_search_route: ConnectionRoute


class ClearCloudSessionSuccessResponse(TypedDict):
    ok: Literal[True]
    cloud_session_state: str
    stale: bool


class LlmRouteSuccessResponse(TypedDict):
    ok: Literal[True]
    llm_route: ConnectionRoute


class WebSearchRouteSuccessResponse(TypedDict):
    ok: Literal[True]
    web_search_route: ConnectionRoute


class StatusSuccessResponse(TypedDict):
    ok: Literal[True]
    helper_instance_id: str
    # The control socket is the only channel that discloses this token.
    local_api_token: str
    active_owner_id: str
    configured: bool
    cloud_session_state: str
    credential_generation: int
    # Only the route name: the connection settings themselves never leave the
    # runtime process.
    llm_route: ConnectionRoute
    web_search_route: ConnectionRoute
    backend_host: str
    backend_port: int


class ErrorResponse(TypedDict):
    ok: Literal[False]
    error_code: str
    message: str


ControlSuccessResponse = (
    CloudSessionSuccessResponse
    | ConfigureSuccessResponse
    | ClearCloudSessionSuccessResponse
    | LlmRouteSuccessResponse
    | WebSearchRouteSuccessResponse
    | StatusSuccessResponse
)
ControlResponse = ControlSuccessResponse | ErrorResponse


def require_request_object(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        raise MigrationError("Control request must be a JSON object")
    return cast(dict[str, object], request)


def require_payload_object(
    payload: dict[str, object], *, field_name: str
) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise MigrationError(f"{field_name} must be an object")
    return cast(dict[str, object], value)


def require_string_field(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise MigrationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise MigrationError(f"{field_name} must not be empty")
    return normalized


def require_int_field(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError(f"{field_name} must be a non-negative integer")
    return value


def read_clear_reason(payload: dict[str, object]) -> ClearReason:
    value = require_string_field(payload, "reason")
    if value == CLEAR_REASON_SIGNED_OUT:
        return CLEAR_REASON_SIGNED_OUT
    if value == CLEAR_REASON_EXPIRED:
        return CLEAR_REASON_EXPIRED
    raise MigrationError("reason must be signed_out or expired")


def read_set_cloud_session_payload(
    payload: dict[str, object],
) -> SetCloudSessionPayload:
    return {
        "account_user_id": require_string_field(payload, "account_user_id"),
        "access_token": require_string_field(payload, "access_token"),
        "expires_at": require_string_field(payload, "expires_at"),
        "session_version": require_string_field(payload, "session_version"),
    }


def read_clear_cloud_session_payload(
    payload: dict[str, object],
) -> ClearCloudSessionPayload:
    return {
        "reason": read_clear_reason(payload),
        "account_user_id": require_string_field(payload, "account_user_id"),
        "session_version": require_string_field(payload, "session_version"),
        "credential_generation": require_int_field(payload, "credential_generation"),
        "helper_instance_id": require_string_field(payload, "helper_instance_id"),
    }


def build_error_response(*, error_code: str, message: str) -> ErrorResponse:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
    }


__all__ = [
    "CLEAR_REASON_EXPIRED",
    "CLEAR_REASON_SIGNED_OUT",
    "ClearCloudSessionPayload",
    "ClearCloudSessionSuccessResponse",
    "ClearReason",
    "CloudSessionImport",
    "CloudSessionSuccessResponse",
    "ConfigureSuccessResponse",
    "ControlResponse",
    "ControlSuccessResponse",
    "ErrorResponse",
    "LlmRouteSuccessResponse",
    "SetCloudSessionPayload",
    "StatusSuccessResponse",
    "WebSearchRouteSuccessResponse",
    "build_error_response",
    "normalize_session_version",
    "parse_iso8601",
    "parse_session_version",
    "read_clear_cloud_session_payload",
    "read_clear_reason",
    "read_set_cloud_session_payload",
    "require_int_field",
    "require_non_empty",
    "require_payload_object",
    "require_positive_timeout",
    "require_request_object",
    "require_string_field",
    "validate_cloud_session_fields",
]

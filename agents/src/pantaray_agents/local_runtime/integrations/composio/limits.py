"""What the user's Composio key allows, probed without creating anything."""

from __future__ import annotations

import logging
from typing import Final, Literal

import httpx2

from ..contracts import ApiDenial, GatewayLimits
from .client import METADATA_TIMEOUT, ComposioClient, ComposioRequestError
from .mapping import CODE_KEY_PERMISSION_DENIED, CODE_VALIDATION_ERROR

logger = logging.getLogger(__name__)

# Composio checks a key's permission before the body, so an empty body to an
# area's write route answers 403/812 when denied and 400/10400 when allowed,
# creating nothing (verified on 2026-10-18; no route reports a key's permissions).
_PROBE_SESSION: Final = "trs_pantaray_probe"
_LIMIT_PROBES: Final[tuple[tuple[Literal["connect", "execute"], str, str], ...]] = (
    ("connect", "auth_configs", "/auth_configs"),
    ("connect", "connected_accounts", "/connected_accounts/link"),
    ("connect", "session_management", f"/tool_router/session/{_PROBE_SESSION}/link"),
    ("execute", "session_management", "/tool_router/session"),
    (
        "execute",
        "session_tool_execution",
        f"/tool_router/session/{_PROBE_SESSION}/execute",
    ),
)
# Until a probe settles nothing says the key falls short; a real shortfall still
# answers 403/812 at the first call, which probes again.
UNPROBED_LIMITS: Final = GatewayLimits(can_connect=True, can_execute=True, reasons=[])


async def probe_limits(client: ComposioClient) -> GatewayLimits | None:
    """The key's limits, or None when a probe was inconclusive."""

    denied: dict[Literal["connect", "execute"], list[str]] = {
        "connect": [],
        "execute": [],
    }
    for capability, area, path in _LIMIT_PROBES:
        try:
            response = await client.send(
                "POST", path, json={}, timeout=METADATA_TIMEOUT
            )
        except httpx2.TransportError as exc:
            warn_composio_failure("Composio key probe failed", exc)
            return None
        if response.status == 401:
            return GatewayLimits(
                can_connect=False,
                can_execute=False,
                reasons=[
                    ApiDenial(source="connection_method", capability="connect"),
                    ApiDenial(source="connection_method", capability="execute"),
                ],
            )
        if response.status == 403 and response.code == CODE_KEY_PERMISSION_DENIED:
            if area not in denied[capability]:
                denied[capability].append(area)
        elif not (response.status == 400 and response.code == CODE_VALIDATION_ERROR):
            warn_composio_failure(
                "Composio key probe failed", ComposioRequestError(response)
            )
            return None
    return GatewayLimits(
        can_connect=not denied["connect"],
        can_execute=not denied["execute"],
        reasons=[
            # The areas are Composio's identifiers, shown as they are.
            ApiDenial(
                source="connection_method",
                capability=capability,
                detail=", ".join(areas),
            )
            for capability, areas in denied.items()
            if areas
        ],
    )


def warn_composio_failure(
    message: str, exc: Exception, *, toolkit: str | None = None
) -> None:
    # Only the status and code: Composio's text can quote part of the key, and
    # its bodies can hold a connect link, which is as good as a credential.
    reason = (
        f"status={exc.response.status} code={exc.response.code}"
        if isinstance(exc, ComposioRequestError)
        else type(exc).__name__
    )
    logger.warning(message, extra={"reason": reason, "toolkit": toolkit})

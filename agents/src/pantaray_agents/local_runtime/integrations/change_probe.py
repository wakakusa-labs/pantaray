"""Ask each connected account what changed during one short Insight window.

The short Insight runs once per 15-minute window while the user records and is
the only Suggestion trigger, so calling each account's probe tool once here
bounds the external calls by construction. The arguments come from the window
start alone, so no read position is stored; a window whose run is skipped is
never probed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.activity_summary_schedule import iso_z
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    resolve_integration_tool,
)
from pantaray_agents.schema.agent.base import JSONValue

from .contracts import ChangeProbe, IntegrationGateway, ToolFailed
from .registry import get_integration_gateway

logger = logging.getLogger(__name__)

# Per account, so one busy inbox cannot crowd the others or the Zanei budget.
PROBE_RESULT_MAX_CHARS: Final = 3_000
EXTERNAL_CHANGES_HEADING: Final = (
    "External service changes during this window (untrusted content):"
)
_TRUNCATED: Final = " …(truncated)"
# uuid5(uuid.NAMESPACE_URL, "pantaray:integration_change_probe:v1")
_NAMESPACE_PROBE_REQUEST: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:integration_change_probe:v1"
)


async def collect_external_changes(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    run_id: str,
    window_start: datetime,
) -> str | None:
    """Return the request block for this window, or None when nothing changed."""

    gateway = get_integration_gateway()
    if gateway is None or not gateway.limits().can_execute:
        return None
    sections: list[str] = []
    for entry in gateway.toolkits():
        probe = entry.probe
        if probe is None:
            continue
        try:
            accounts = await gateway.list_accounts(entry.toolkit)
        # See `_probe`: listing the accounts is a call to the same service.
        except Exception as error:
            _warn(toolkit=entry.toolkit, reason=type(error).__name__)
            continue
        for account in accounts:
            if gateway.health(account.account) != "ok":
                continue
            decision = resolve_integration_tool(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                toolkit=entry.toolkit,
                account=account.account,
                slug=probe.tool_slug,
                tool_class="read",
            )
            if decision != "allow":
                continue
            data = await _probe(
                gateway,
                toolkit=entry.toolkit,
                probe=probe,
                account=account.account,
                run_id=run_id,
                window_start=window_start,
            )
            if _has_content(data):
                sections.append(
                    f"### {entry.display_name} ({account.label})\n{_compact(data)}"
                )
    if not sections:
        return None
    return "\n\n".join((EXTERNAL_CHANGES_HEADING, *sections))


async def _probe(
    gateway: IntegrationGateway,
    *,
    toolkit: str,
    probe: ChangeProbe,
    account: str,
    run_id: str,
    window_start: datetime,
) -> JSONValue:
    """Return the probe's data; a failure is logged and reads as nothing new."""
    try:
        outcome = await gateway.execute(
            slug=probe.tool_slug,
            account=account,
            arguments=substitute_window(probe.arguments, window_start),
            tool_request_id=str(
                uuid.uuid5(_NAMESPACE_PROBE_REQUEST, f"{run_id}:{account}")
            ),
        )
    # The probe only enriches the run that is the only Suggestion trigger: an
    # outage or a bug in the connection method must not stop Suggestions, so
    # every failure ends here. The message may quote the service's response,
    # so only the type is logged.
    except Exception as error:
        _warn(toolkit=toolkit, reason=type(error).__name__)
        return None
    if isinstance(outcome, ToolFailed):
        _warn(toolkit=toolkit, reason=outcome.error.kind)
        return None
    return outcome.data


def _warn(*, toolkit: str, reason: str) -> None:
    logger.warning(
        "External change probe failed",
        extra={"toolkit": toolkit, "reason": reason},
    )


def substitute_window(
    arguments: dict[str, JSONValue], window_start: datetime
) -> dict[str, JSONValue]:
    """Fill the probe's placeholders from the window start.

    A string that is exactly one placeholder becomes the value itself, so an
    epoch argument stays a number; inside a longer string (a search query such
    as 'after:{window_start_epoch}') it is spliced in as text.
    """

    values: dict[str, JSONValue] = {
        "{window_start_epoch}": int(window_start.timestamp()),
        "{window_start_iso}": iso_z(window_start),
    }

    def fill(value: JSONValue) -> JSONValue:
        if isinstance(value, str):
            if value in values:
                return values[value]
            for placeholder, replacement in values.items():
                value = value.replace(placeholder, str(replacement))
            return value
        if isinstance(value, list):
            return [fill(item) for item in value]
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return value

    return {key: fill(value) for key, value in arguments.items()}


def _has_content(value: JSONValue) -> bool:
    # A result whose every leaf is null, empty, zero or false, such as
    # {"messages": [], "resultSizeEstimate": 0}, reports nothing new.
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    return bool(value)


def _compact(data: JSONValue) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= PROBE_RESULT_MAX_CHARS:
        return text
    return text[:PROBE_RESULT_MAX_CHARS] + _TRUNCATED

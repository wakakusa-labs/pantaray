"""Live check of the Composio gateway against a real connected Gmail account.

Reads COMPOSIO_API_KEY from the environment and never prints it. Points the
gateway at COMPOSIO_SMOKE_USER_ID (the Composio user that owns the connection)
instead of the desktop's fixed user, and uses the account
COMPOSIO_SMOKE_ACCOUNT. Read tools only; to see an inactive connection it
disables the account for one call and enables it again at once.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta

import httpx2

from pantaray_agents.local_runtime.integrations.change_probe import substitute_window
from pantaray_agents.local_runtime.integrations.composio import client
from pantaray_agents.local_runtime.integrations.composio.gateway import (
    ComposioGateway,
)
from pantaray_agents.local_runtime.integrations.composio.toolkits import (
    TOOLKITS_BY_SLUG,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    ToolFailed,
    ToolSucceeded,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)

KEY = os.environ["COMPOSIO_API_KEY"]
ACCOUNT = os.environ["COMPOSIO_SMOKE_ACCOUNT"]
failures = 0


def check(name: str, ok: bool, note: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}{f' ({note})' if note else ''}")


async def set_enabled(enabled: bool) -> None:
    async with httpx2.AsyncClient(timeout=30) as http:
        response = await http.patch(
            f"{client.BASE_URL}/connected_accounts/{ACCOUNT}/status",
            headers={"x-api-key": KEY},
            json={"enabled": enabled},
        )
    response.raise_for_status()


async def main() -> None:
    client.USER_ID = os.environ["COMPOSIO_SMOKE_USER_ID"]
    gateway = ComposioGateway(client.ComposioClient(KEY))
    set_integration_gateway(gateway)
    await gateway.sync()

    limits = gateway.limits()
    check("limits from the key probe", limits.can_connect and limits.can_execute)
    accounts = await gateway.list_accounts("gmail")
    mine = next((a for a in accounts if a.account == ACCOUNT), None)
    check("list_accounts has the account", mine is not None)
    check("label is an email address", mine is not None and "@" in mine.label)
    check("health", gateway.health(ACCOUNT) == "ok")

    tools = {t.slug: t for t in get_integration_tool_index().tools("gmail")}
    reads = sum(t.tool_class == "read" for t in tools.values())
    check("gmail tools indexed", len(tools) > 50, f"{reads} read of {len(tools)}")
    check(
        "classification",
        tools["GMAIL_FETCH_EMAILS"].tool_class == "read"
        and tools["GMAIL_SEND_EMAIL"].tool_class == "write",
    )

    denied = gateway.api_denied_reason(slug="GMAIL_CREATE_FILTER", account=ACCOUNT)
    check(
        "a scope the grant lacks denies the tool",
        denied is not None and denied.detail is not None,
        "" if denied is None else str(denied.detail),
    )
    check(
        "the full-mail grant allows reads",
        gateway.api_denied_reason(slug="GMAIL_FETCH_EMAILS", account=ACCOUNT) is None,
    )

    probe = TOOLKITS_BY_SLUG["gmail"].probe
    assert probe is not None
    since = datetime.now(UTC) - timedelta(days=1)
    outcome = await gateway.execute(
        slug=probe.tool_slug,
        account=ACCOUNT,
        arguments=substitute_window(probe.arguments, since),
        tool_request_id="smoke-probe",
    )
    data = outcome.data if isinstance(outcome, ToolSucceeded) else None
    check(
        "probe read through the session",
        isinstance(data, dict) and "messages" in data,
        ""
        if not isinstance(data, dict)
        else f"{len(data.get('messages') or [])} items",
    )

    bad = await gateway.execute(
        slug="GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        account=ACCOUNT,
        arguments={"message_id": "not-a-message-id"},
        tool_request_id="smoke-bad",
    )
    check(
        "a provider 400 is invalid_arguments with detail",
        isinstance(bad, ToolFailed)
        and bad.error.kind == "invalid_arguments"
        and bad.untrusted_detail is not None,
    )

    await set_enabled(False)
    try:
        inactive = await gateway.execute(
            slug="GMAIL_FETCH_EMAILS",
            account=ACCOUNT,
            arguments={"max_results": 1, "ids_only": True},
            tool_request_id="smoke-inactive",
        )
        await gateway.list_accounts("gmail")
        health = gateway.health(ACCOUNT)
    finally:
        await set_enabled(True)
    check(
        "an inactive connection asks to reconnect",
        isinstance(inactive, ToolFailed)
        and inactive.error.kind == "reauthorization_required"
        and health == "reauthorization_required",
    )
    gateway.invalidate_accounts()
    await gateway.list_accounts("gmail")
    check("the account is active again", gateway.health(ACCOUNT) == "ok")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:  # noqa: BLE001
        print(traceback.format_exc().replace(KEY, "<redacted>"))
        failures += 1
    print(f"failures={failures}")
    sys.exit(1 if failures else 0)

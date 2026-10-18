"""Which integration tools can run now, and the search over them.

Every caller of the two fixed integration tools (the Action, Suggestion research)
goes through here. Usability is resolved on every call, since the user may change
permissions at any time: a tool is usable with an account only when the
connection method can execute, the account's grant covers the tool, and the
saved permission allows or asks for it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    resolve_integration_tool,
)
from pantaray_agents.schema.agent.base import JSONValue

from .contracts import IntegrationAccount, IntegrationGateway, IntegrationTool
from .registry import get_integration_gateway, get_integration_tool_index
from .tool_index import SEARCH_LIMIT_DEFAULT

logger = logging.getLogger(__name__)

UPSTREAM_FAILED_MESSAGE = "The service failed or timed out; trying later may work."

_ACCOUNT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "account": {"type": "string"},
        "label": {"type": "string"},
        "permission": {"type": "string", "enum": ["allow", "ask"]},
    },
    "required": ["account", "label", "permission"],
}

# What search_integration_tools returns.
SEARCH_RESULT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tools": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "slug": {"type": "string"},
                    "toolkit": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tool_class": {"type": "string", "enum": ["read", "write"]},
                    "input_schema": {"type": "object"},
                    "accounts": {
                        "type": "array",
                        "minItems": 1,
                        "items": _ACCOUNT_SCHEMA,
                    },
                },
                "required": [
                    "slug",
                    "toolkit",
                    "name",
                    "description",
                    "tool_class",
                    "input_schema",
                    "accounts",
                ],
            },
        },
    },
    "required": ["tools"],
}


class IntegrationGatewayError(RuntimeError):
    """A gateway call raised. Its text is the connection's own, so it is dropped."""


def gateway_call[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        raise _dropped(exc) from None


async def gateway_await[T](call: Awaitable[T]) -> T:
    try:
        return await call
    except Exception as exc:  # noqa: BLE001
        raise _dropped(exc) from None


def _dropped(exc: Exception) -> IntegrationGatewayError:
    # The exception text may carry credentials or external text: log the type only.
    logger.warning("integration gateway call raised: %s", type(exc).__name__)
    return IntegrationGatewayError(type(exc).__name__)


@dataclass(frozen=True, slots=True)
class UsableAccount:
    account: IntegrationAccount
    permission: Literal["allow", "ask"]


async def integration_tools_available() -> bool:
    """Whether a run starting now is offered the integration tools.

    A gateway that raises offers none: a run that never uses an integration must
    still start when the connection method is broken.
    """

    gateway = get_integration_gateway()
    if gateway is None:
        return False
    try:
        for entry in gateway_call(gateway.toolkits):
            if await gateway_await(gateway.list_accounts(entry.toolkit)):
                return True
    except IntegrationGatewayError:
        return False
    return False


async def search_integration_tools(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    query: str,
    toolkit: str | None,
    limit: int | None,
    read_only: bool,
) -> dict[str, JSONValue]:
    """The usable tools matching ``query``; ``read_only`` leaves out write tools
    for a caller that cannot ask the user. Raises IntegrationGatewayError when the
    gateway raises."""

    gateway = get_integration_gateway()
    if gateway is None:
        return {"tools": []}
    names = (
        [toolkit]
        if toolkit
        else [entry.toolkit for entry in gateway_call(gateway.toolkits)]
    )
    accounts = {
        name: await gateway_await(gateway.list_accounts(name)) for name in names
    }
    usable_by_slug: dict[str, list[UsableAccount]] = {}

    def usable(tool: IntegrationTool) -> bool:
        if read_only and tool.tool_class == "write":
            return False
        # Design limit: one permission read per tool and account; read each
        # account's rows once per search when a search's p95 exceeds 200 ms.
        usable_by_slug[tool.slug] = usable_accounts(
            gateway,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            tool=tool,
            accounts=accounts.get(tool.toolkit, []),
        )
        return bool(usable_by_slug[tool.slug])

    tools = get_integration_tool_index().search(
        query, usable=usable, toolkit=toolkit, limit=limit or SEARCH_LIMIT_DEFAULT
    )
    return {
        "tools": [
            {
                "slug": tool.slug,
                "toolkit": tool.toolkit,
                "name": tool.name,
                "description": tool.description,
                "tool_class": tool.tool_class,
                "input_schema": tool.input_schema,
                "accounts": [
                    {
                        "account": item.account.account,
                        "label": item.account.label,
                        "permission": item.permission,
                    }
                    for item in usable_by_slug[tool.slug]
                ],
            }
            for tool in tools
        ]
    }


def usable_accounts(
    gateway: IntegrationGateway,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool: IntegrationTool,
    accounts: list[IntegrationAccount],
) -> list[UsableAccount]:
    """Raises IntegrationGatewayError when the gateway raises."""

    if not gateway_call(gateway.limits).can_execute:
        return []
    usable: list[UsableAccount] = []
    for account in accounts:
        denial = gateway_call(
            partial(gateway.api_denied_reason, slug=tool.slug, account=account.account)
        )
        if denial is not None:
            continue
        decision = resolve_integration_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            toolkit=tool.toolkit,
            account=account.account,
            slug=tool.slug,
            tool_class=tool.tool_class,
        )
        if decision != "denied":
            usable.append(UsableAccount(account=account, permission=decision))
    return usable


__all__ = [
    "IntegrationGatewayError",
    "SEARCH_RESULT_SCHEMA",
    "UPSTREAM_FAILED_MESSAGE",
    "UsableAccount",
    "gateway_await",
    "gateway_call",
    "integration_tools_available",
    "search_integration_tools",
    "usable_accounts",
]

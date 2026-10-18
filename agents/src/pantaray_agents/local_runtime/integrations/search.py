"""Which integration tools can run now, and the search over them.

Every caller of the two fixed integration tools (the Action, Suggestion research)
goes through here. Usability is resolved on every call, since the user may change
permissions at any time: a tool is usable with an account only when the
connection method can execute, the account's grant covers the tool, and the
saved permission allows or asks for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, Literal

from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    load_integration_account_access,
    load_integration_blocked_tools,
    resolve_integration_tool,
)
from pantaray_agents.schema.agent.base import JSONValue

from .contracts import IntegrationAccount, IntegrationGateway, IntegrationTool
from .registry import get_integration_gateway, get_integration_tool_index
from .settings import access_options
from .tool_index import SEARCH_LIMIT_DEFAULT

logger = logging.getLogger(__name__)

type SettingFix = Literal["read_only", "read_write"]

UPSTREAM_FAILED_MESSAGE = "The service failed or timed out; trying later may work."
# The remedies told to the Action's model where the refusal happens: in the
# search result and the run's refusal. Suggestion research never gets them; the
# user did not ask for anything. The labels are the settings screen's own.
_OPTION_LABELS: Final[dict[SettingFix, str]] = {
    "read_only": '"Read only" (「読み取りのみ」)',
    "read_write": '"Read and write" (「読み取りと書き込み」)',
}
RECONNECT_REMEDY: Final = (
    "The user can reconnect this account in Settings → Integrations (設定 → 連携)."
)
# Every Action start waits for this, integrations used or not; past it the
# Action runs without the integration tools.
AVAILABILITY_TIMEOUT_SECONDS: Final = 3.0

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

_HIDDEN_ACCOUNT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "toolkit": {"type": "string"},
        "account": {"type": "string"},
        "label": {"type": "string"},
        "remedy": {"type": "string"},
    },
    "required": ["toolkit", "account", "label", "remedy"],
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
        # Present only when tools matching the query exist that only these
        # accounts' setting ("Off" or "Read only") keeps out of "tools".
        "hidden_by_account_setting": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tool_count": {"type": "integer", "minimum": 1},
                "accounts": {
                    "type": "array",
                    "minItems": 1,
                    "items": _HIDDEN_ACCOUNT_SCHEMA,
                },
            },
            "required": ["tool_count", "accounts"],
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

    A gateway that raises or is too slow offers none: a run that never uses an
    integration must still start when the connection method is broken.
    """

    gateway = get_integration_gateway()
    if gateway is None:
        return False
    try:
        async with asyncio.timeout(AVAILABILITY_TIMEOUT_SECONDS):
            for entry in gateway_call(gateway.toolkits):
                if await gateway_await(gateway.list_accounts(entry.toolkit)):
                    return True
    except (IntegrationGatewayError, TimeoutError):
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
    for a caller that cannot ask the user, and the count of tools the account
    setting hides. Raises IntegrationGatewayError when the gateway raises."""

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
    hidden_by_slug: dict[str, list[tuple[IntegrationAccount, SettingFix]]] = {}
    settings: dict[str, AccountSetting] = {}

    def fix_for(
        tool: IntegrationTool, account: IntegrationAccount
    ) -> SettingFix | None:
        if account.account not in settings:
            settings[account.account] = load_account_setting(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                account=account,
            )
        return setting_fix(
            gateway, tool=tool, account=account, setting=settings[account.account]
        )

    def usable(tool: IntegrationTool) -> bool:
        if read_only and tool.tool_class == "write":
            return False
        candidates = accounts.get(tool.toolkit, [])
        # Design limit: one permission read per tool and account; read each
        # account's rows once per search when a search's p95 exceeds 200 ms.
        usable_by_slug[tool.slug] = usable_accounts(
            gateway,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            tool=tool,
            accounts=candidates,
        )
        if not usable_by_slug[tool.slug] and not read_only:
            hidden = [
                (item, fix)
                for item in candidates
                if (fix := fix_for(tool, item)) is not None
            ]
            if hidden:
                hidden_by_slug[tool.slug] = hidden
        return bool(usable_by_slug[tool.slug])

    index = get_integration_tool_index()
    tools = index.search(
        query, usable=usable, toolkit=toolkit, limit=limit or SEARCH_LIMIT_DEFAULT
    )
    result: dict[str, JSONValue] = {
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
    if hidden_by_slug:
        matched = index.search(
            query,
            usable=lambda tool: tool.slug in hidden_by_slug,
            toolkit=toolkit,
            limit=len(hidden_by_slug),
        )
        # One option per account: "Read and write" also covers its hidden reads.
        owners: dict[str, tuple[IntegrationAccount, SettingFix]] = {}
        for tool in matched:
            for item, fix in hidden_by_slug[tool.slug]:
                current = owners.get(item.account)
                if current is None or current[1] == "read_only":
                    owners[item.account] = (item, fix)
        if matched:
            result["hidden_by_account_setting"] = {
                "tool_count": len(matched),
                "accounts": [
                    {
                        "toolkit": item.toolkit,
                        "account": item.account,
                        "label": item.label,
                        "remedy": setting_remedy(fix),
                    }
                    for item, fix in owners.values()
                ],
            }
    return result


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


@dataclass(frozen=True, slots=True)
class AccountSetting:
    """What the user set for one account: its access and the tools turned off."""

    access: IntegrationAccess
    blocked: frozenset[str]


def load_account_setting(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, account: IntegrationAccount
) -> AccountSetting:
    access = load_integration_account_access(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        toolkit=account.toolkit,
        account=account.account,
    )
    blocked = load_integration_blocked_tools(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        account=account.account,
    )
    return AccountSetting(access=access, blocked=blocked)


def setting_fix(
    gateway: IntegrationGateway,
    *,
    tool: IntegrationTool,
    account: IntegrationAccount,
    setting: AccountSetting,
) -> SettingFix | None:
    """The option that would let ``tool`` run with ``account``, when changing the
    account's setting alone would and the settings screen offers that option.

    Raises IntegrationGatewayError when the gateway raises.
    """

    target: SettingFix = "read_only" if tool.tool_class == "read" else "read_write"
    if (
        setting.access in (target, "read_write")
        or tool.slug in setting.blocked
        # A connection that needs reconnecting is not fixed by a setting.
        or gateway_call(partial(gateway.health, account.account)) != "ok"
    ):
        return None
    denial = gateway_call(
        partial(gateway.api_denied_reason, slug=tool.slug, account=account.account)
    )
    # This tool alone decides: an option the API allows for it is one the
    # screen allows for the account.
    options = access_options(
        limits=gateway_call(gateway.limits), tools=[tool], denials={tool.slug: denial}
    )
    allowed = next(option.allowed for option in options if option.value == target)
    return target if allowed else None


def setting_remedy(fix: SettingFix) -> str:
    return (
        "The user can change this in Settings → Integrations (設定 → 連携) by"
        f" setting the account to {_OPTION_LABELS[fix]}."
    )


__all__ = [
    "RECONNECT_REMEDY",
    "AccountSetting",
    "IntegrationGatewayError",
    "SEARCH_RESULT_SCHEMA",
    "UPSTREAM_FAILED_MESSAGE",
    "UsableAccount",
    "gateway_await",
    "gateway_call",
    "integration_tools_available",
    "load_account_setting",
    "setting_fix",
    "setting_remedy",
    "search_integration_tools",
    "usable_accounts",
]

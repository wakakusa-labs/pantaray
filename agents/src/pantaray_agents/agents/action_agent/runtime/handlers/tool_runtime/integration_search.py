"""Which integration tools an Action is offered and can use, and the search over them.

Only whether the tools are offered is decided when the Action starts. Usability is
resolved on every call, since the user may change permissions mid-Action: a tool is
usable with an account only when the connection method can execute, the account's
grant covers the tool, and the saved permission allows or asks for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_int_value,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.integrations.contracts import (
    IntegrationAccount,
    IntegrationGateway,
    IntegrationTool,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
)
from pantaray_agents.local_runtime.integrations.tool_index import SEARCH_LIMIT_DEFAULT
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    resolve_integration_tool,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import UnprojectedToolExecutionResult


@dataclass(frozen=True, slots=True)
class UsableAccount:
    account: IntegrationAccount
    permission: Literal["allow", "ask"]


async def integration_tools_available() -> bool:
    """Whether an Action starting now is offered the integration tools."""

    gateway = get_integration_gateway()
    if gateway is None:
        return False
    for entry in gateway.toolkits():
        if await gateway.list_accounts(entry.toolkit):
            return True
    return False


async def run_integration_search_tool(
    *, step_id: str, tool_def: ToolDefinition, args: ToolArgs, state
) -> UnprojectedToolExecutionResult:
    started_at = now_utc_iso()
    gateway = get_integration_gateway()
    found: list[tuple[IntegrationTool, list[UsableAccount]]] = []
    if gateway is not None:
        found = await _search(gateway, user_id=state["user_id"], args=args)
    output: dict[str, JSONValue] = {
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
                    for item in usable
                ],
            }
            for tool, usable in found
        ]
    }
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=started_at,
        completed_at=now_utc_iso(),
        output=output,
    )


async def _search(
    gateway: IntegrationGateway, *, user_id: str, args: ToolArgs
) -> list[tuple[IntegrationTool, list[UsableAccount]]]:
    toolkit = optional_string_arg(args, "toolkit")
    names = [toolkit] if toolkit else [entry.toolkit for entry in gateway.toolkits()]
    accounts = {name: await gateway.list_accounts(name) for name in names}
    usable_by_slug: dict[str, list[UsableAccount]] = {}

    def usable(tool: IntegrationTool) -> bool:
        # Design limit: one permission read per tool and account; read each
        # account's rows once per search when a search's p95 exceeds 200 ms.
        usable_by_slug[tool.slug] = usable_accounts(
            gateway, user_id, tool, accounts.get(tool.toolkit, [])
        )
        return bool(usable_by_slug[tool.slug])

    tools = get_integration_tool_index().search(
        require_string_arg(args, "query"),
        usable=usable,
        toolkit=toolkit,
        limit=optional_int_value(args.get("limit")) or SEARCH_LIMIT_DEFAULT,
    )
    return [(tool, usable_by_slug[tool.slug]) for tool in tools]


def usable_accounts(
    gateway: IntegrationGateway,
    user_id: str,
    tool: IntegrationTool,
    accounts: list[IntegrationAccount],
) -> list[UsableAccount]:
    if not gateway.limits().can_execute:
        return []
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    usable: list[UsableAccount] = []
    for account in accounts:
        if (
            gateway.api_denied_reason(slug=tool.slug, account=account.account)
            is not None
        ):
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

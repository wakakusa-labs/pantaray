"""The Executing head's integrations section: what each connected account allows now.

It is rendered on every turn from the permissions as they are, through the same
resolution ``integration_run_tool`` applies on each call, so the model learns of a
change made mid-Action in its next turn context. Nothing in it moves unless a
permission does (sorted, no timestamps), so an unchanged turn sends no update.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Final

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.integration_search import (
    IntegrationGatewayError,
    gateway_await,
    gateway_call,
    usable_accounts,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationGateway,
    IntegrationTool,
    ToolClass,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    load_integration_account_access,
    load_integration_blocked_tools,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState

_HEADING: Final = "\n\n## Integrations\n"
INTEGRATIONS_UNAVAILABLE: Final = _HEADING + "Integrations are unavailable right now."

_SAVED_CLASSES: Final[dict[IntegrationAccess, tuple[ToolClass, ...]]] = {
    "off": (),
    "read_only": ("read",),
    "read_write": ("read", "write"),
}
# Keyed by (reads usable, writes usable).
_EFFECTIVE: Final[dict[tuple[bool, bool], str]] = {
    (False, False): "off",
    (True, False): "read only",
    (True, True): "read and write; writes ask every time",
    (False, True): "write only; writes ask every time",
}
_CLASS_NOUNS: Final[dict[ToolClass, str]] = {"read": "reads", "write": "writes"}


async def render_integrations_state(state: ActionAgentState) -> str:
    """The section's text, or "" when the Action has no integration tools.

    "" also when no account is connected any more, which withdraws the section.
    A gateway that raises renders a fixed line rather than failing the turn.
    """

    if not state["context"].get("integration_tools_exposed", False):
        return ""
    gateway = get_integration_gateway()
    if gateway is None:
        return ""
    try:
        lines = await _lines(gateway, user_id=state["user_id"])
    except IntegrationGatewayError:
        return INTEGRATIONS_UNAVAILABLE
    if not lines:
        return ""
    return _HEADING + "The user's connected app accounts:\n" + "\n".join(lines)


async def _lines(gateway: IntegrationGateway, *, user_id: str) -> list[str]:
    limits = gateway_call(gateway.limits)
    index = get_integration_tool_index()
    lines: list[str] = []
    for entry in sorted(gateway_call(gateway.toolkits), key=lambda e: e.toolkit):
        accounts = await gateway_await(gateway.list_accounts(entry.toolkit))
        tools = index.tools(entry.toolkit)
        for account in sorted(accounts, key=lambda a: (a.label, a.account)):
            lines += _account_lines(
                gateway, limits, user_id, entry.display_name, account, tools
            )
    return lines


def _account_lines(
    gateway: IntegrationGateway,
    limits: GatewayLimits,
    user_id: str,
    display_name: str,
    account: IntegrationAccount,
    tools: Sequence[IntegrationTool],
) -> list[str]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    saved = load_integration_account_access(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        toolkit=account.toolkit,
        account=account.account,
    )
    classes = _SAVED_CLASSES[saved]
    of_class = {
        tool_class: [tool for tool in tools if tool.tool_class == tool_class]
        for tool_class in classes
    }
    # Usable exactly when integration_run_tool would run one of the class's tools.
    # Design limit: a class nothing can run costs one permission read per tool;
    # read the account's rows once per turn when this render exceeds 100 ms.
    usable = {
        tool_class
        for tool_class, members in of_class.items()
        if any(usable_accounts(gateway, user_id, tool, [account]) for tool in members)
    }
    effective = _EFFECTIVE["read" in usable, "write" in usable]
    lines = [
        f"- {display_name}, {account.label} (account `{account.account}`): {effective}."
    ]
    narrowed: dict[str, list[str]] = {}
    for tool_class, members in of_class.items():
        if tool_class in usable:
            continue
        denial = _api_denial(gateway, limits, account, members)
        if denial is not None:
            narrowed.setdefault(_denial_text(denial), []).append(
                _CLASS_NOUNS[tool_class]
            )
    for why, nouns in narrowed.items():
        lines.append(f"  {' and '.join(nouns).capitalize()} are unavailable: {why}.")
    blocked = load_integration_blocked_tools(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        account=account.account,
    )
    # A block on a tool the account setting already refuses changes nothing.
    differing = sorted(
        tool.slug
        for tool in tools
        if tool.slug in blocked and tool.tool_class in classes
    )
    if differing:
        lines.append("  Blocked tools: " + ", ".join(differing) + ".")
    return lines


def _api_denial(
    gateway: IntegrationGateway,
    limits: GatewayLimits,
    account: IntegrationAccount,
    tools: Sequence[IntegrationTool],
) -> ApiDenial | None:
    """Why the API leaves none of ``tools`` usable, or None when the user's blocks do."""

    if not limits.can_execute:
        # GatewayLimits carries exactly one reason per denied capability.
        return next(
            reason for reason in limits.reasons if reason.capability == "execute"
        )
    if not tools:
        return ApiDenial(source="connection_method", capability="tool")
    denials = [
        gateway_call(
            partial(gateway.api_denied_reason, slug=tool.slug, account=account.account)
        )
        for tool in tools
    ]
    return denials[0] if all(denial is not None for denial in denials) else None


def _denial_text(denial: ApiDenial) -> str:
    # Fixed text: the denial's detail is the external service's identifier.
    if denial.source == "account_grant":
        return "the account's connection does not grant them"
    if denial.capability == "execute":
        return "the connection method cannot run tools now"
    return "the connection method offers none"


__all__ = ["INTEGRATIONS_UNAVAILABLE", "render_integrations_state"]

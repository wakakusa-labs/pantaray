"""What the integration settings screen shows and what it may change.

Each connected account is set on its own. The user chooses only among the
options the API allows; an option it does not allow carries the reason, known
before the user chooses, and the backend refuses it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field

from pantaray_agents.local_runtime.tooling.models import InternalStrictModel
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    initialize_integration_account_access,
    load_integration_account_access,
    load_integration_blocked_tools,
    set_integration_account_access,
    set_integration_tool_blocked,
)

from .contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationGateway,
    IntegrationHealth,
    IntegrationTool,
    ToolClass,
    ToolkitSlug,
)
from .tool_index import ToolIndex

# The decided default: a new account reads without asking and never writes
# until the user turns writing on.
NEW_ACCOUNT_ACCESS: Final[IntegrationAccess] = "read_only"


class AccessOption(InternalStrictModel):
    value: IntegrationAccess
    allowed: bool
    # Set exactly when the option is not allowed.
    reason: ApiDenial | None


class AccountStatus(InternalStrictModel):
    account: str
    label: str
    health: IntegrationHealth
    access: IntegrationAccess
    options: list[AccessOption]


class ToolkitStatus(InternalStrictModel):
    toolkit: ToolkitSlug
    display_name: str
    logo_url: str | None
    accounts: list[AccountStatus]


class IntegrationsNotConfigured(InternalStrictModel):
    state: Literal["not_configured"] = "not_configured"


class IntegrationsConfigured(InternalStrictModel):
    state: Literal["configured"] = "configured"
    limits: GatewayLimits
    toolkits: list[ToolkitStatus]


type IntegrationsStatus = Annotated[
    IntegrationsNotConfigured | IntegrationsConfigured, Field(discriminator="state")
]


class IntegrationToolSetting(InternalStrictModel):
    slug: str
    name: str
    tool_class: ToolClass
    blocked: bool
    api_denial: ApiDenial | None


class UnknownIntegrationAccountError(LookupError):
    pass


class UnknownIntegrationToolError(LookupError):
    pass


class IntegrationAccessNotAllowedError(ValueError):
    def __init__(self, reason: ApiDenial) -> None:
        super().__init__(f"the API does not allow this access: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PermissionStore:
    """Where one user's integration permissions are saved."""

    db_path: Path
    busy_timeout_ms: int
    user_id: str


def access_options(
    *,
    limits: GatewayLimits,
    tools: Sequence[IntegrationTool],
    denials: Mapping[str, ApiDenial | None],
) -> list[AccessOption]:
    """Off is always allowed; each other option needs one usable tool of its class."""

    execute_denial = next(
        (reason for reason in limits.reasons if reason.capability == "execute"), None
    )

    def option(value: IntegrationAccess, tool_class: ToolClass) -> AccessOption:
        if execute_denial is not None:
            return AccessOption(value=value, allowed=False, reason=execute_denial)
        of_class = [tool for tool in tools if tool.tool_class == tool_class]
        if not of_class:
            # The connection method offers no tool of this class for the toolkit.
            reason = ApiDenial(source="connection_method", capability="tool")
            return AccessOption(value=value, allowed=False, reason=reason)
        tool_denials = [denials[tool.slug] for tool in of_class]
        if None in tool_denials:
            return AccessOption(value=value, allowed=True, reason=None)
        return AccessOption(value=value, allowed=False, reason=tool_denials[0])

    return [
        AccessOption(value="off", allowed=True, reason=None),
        option("read_only", "read"),
        option("read_write", "write"),
    ]


async def load_integrations_status(
    *,
    gateway: IntegrationGateway | None,
    tool_index: ToolIndex,
    store: PermissionStore,
) -> IntegrationsStatus:
    if gateway is None:
        return IntegrationsNotConfigured()
    limits = gateway.limits()
    toolkits: list[ToolkitStatus] = []
    # Design limit: one list_accounts call per toolkit in turn. Run them
    # concurrently when the settings poll's p95 exceeds its 2 s interval.
    for entry in gateway.toolkits():
        tools = tool_index.tools(entry.toolkit)
        accounts = await gateway.list_accounts(entry.toolkit)
        toolkits.append(
            ToolkitStatus(
                toolkit=entry.toolkit,
                display_name=entry.display_name,
                logo_url=entry.logo_url,
                accounts=[
                    _account_status(gateway, store, limits, tools, account)
                    for account in accounts
                ],
            )
        )
    return IntegrationsConfigured(limits=limits, toolkits=toolkits)


async def set_account_access(
    *,
    gateway: IntegrationGateway,
    tool_index: ToolIndex,
    store: PermissionStore,
    toolkit: ToolkitSlug,
    account: str,
    access: IntegrationAccess,
    updated_at: str,
) -> AccountStatus:
    found = await _find_account(gateway, toolkit=toolkit, account=account)
    limits = gateway.limits()
    tools = tool_index.tools(toolkit)
    options = access_options(
        limits=limits, tools=tools, denials=_denials(gateway, tools, account)
    )
    chosen = next(option for option in options if option.value == access)
    if chosen.reason is not None:
        raise IntegrationAccessNotAllowedError(chosen.reason)
    set_integration_account_access(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        toolkit=toolkit,
        account=account,
        access=access,
        updated_at=updated_at,
    )
    return _account_status(gateway, store, limits, tools, found)


async def list_account_tools(
    *,
    gateway: IntegrationGateway,
    tool_index: ToolIndex,
    store: PermissionStore,
    toolkit: ToolkitSlug,
    account: str,
) -> list[IntegrationToolSetting]:
    await _find_account(gateway, toolkit=toolkit, account=account)
    blocked = load_integration_blocked_tools(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        account=account,
    )
    return [
        IntegrationToolSetting(
            slug=tool.slug,
            name=tool.name,
            tool_class=tool.tool_class,
            blocked=tool.slug in blocked,
            api_denial=gateway.api_denied_reason(slug=tool.slug, account=account),
        )
        for tool in tool_index.tools(toolkit)
    ]


async def set_tool_blocked(
    *,
    gateway: IntegrationGateway,
    tool_index: ToolIndex,
    store: PermissionStore,
    toolkit: ToolkitSlug,
    account: str,
    slug: str,
    blocked: bool,
    updated_at: str,
) -> None:
    await _find_account(gateway, toolkit=toolkit, account=account)
    if all(tool.slug != slug for tool in tool_index.tools(toolkit)):
        raise UnknownIntegrationToolError(slug)
    set_integration_tool_blocked(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        account=account,
        slug=slug,
        blocked=blocked,
        updated_at=updated_at,
    )


def apply_new_account_default(
    *, store: PermissionStore, toolkit: ToolkitSlug, account: str, updated_at: str
) -> None:
    """Save the default access when the connect flow sees the account active.

    A setting already saved, such as a reconnected account's, is kept. Reads
    never write it: an account with no saved row stays unusable.
    """

    initialize_integration_account_access(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        toolkit=toolkit,
        account=account,
        access=NEW_ACCOUNT_ACCESS,
        updated_at=updated_at,
    )


def carry_over_account_setting(
    *,
    store: PermissionStore,
    toolkit: ToolkitSlug,
    replaced: str,
    account: str,
    updated_at: str,
) -> None:
    """A reconnect's new account takes the replaced account's access and blocks.

    An access the new account already has is kept.
    """

    initialize_integration_account_access(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        toolkit=toolkit,
        account=account,
        access=load_integration_account_access(
            db_path=store.db_path,
            busy_timeout_ms=store.busy_timeout_ms,
            user_id=store.user_id,
            toolkit=toolkit,
            account=replaced,
        ),
        updated_at=updated_at,
    )
    for slug in load_integration_blocked_tools(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        account=replaced,
    ):
        set_integration_tool_blocked(
            db_path=store.db_path,
            busy_timeout_ms=store.busy_timeout_ms,
            user_id=store.user_id,
            account=account,
            slug=slug,
            blocked=True,
            updated_at=updated_at,
        )


async def _find_account(
    gateway: IntegrationGateway, *, toolkit: ToolkitSlug, account: str
) -> IntegrationAccount:
    if any(entry.toolkit == toolkit for entry in gateway.toolkits()):
        for found in await gateway.list_accounts(toolkit):
            if found.account == account:
                return found
    raise UnknownIntegrationAccountError(f"{toolkit}/{account}")


def _denials(
    gateway: IntegrationGateway, tools: Sequence[IntegrationTool], account: str
) -> dict[str, ApiDenial | None]:
    return {
        tool.slug: gateway.api_denied_reason(slug=tool.slug, account=account)
        for tool in tools
    }


def _account_status(
    gateway: IntegrationGateway,
    store: PermissionStore,
    limits: GatewayLimits,
    tools: Sequence[IntegrationTool],
    account: IntegrationAccount,
) -> AccountStatus:
    return AccountStatus(
        account=account.account,
        label=account.label,
        health=gateway.health(account.account),
        access=load_integration_account_access(
            db_path=store.db_path,
            busy_timeout_ms=store.busy_timeout_ms,
            user_id=store.user_id,
            toolkit=account.toolkit,
            account=account.account,
        ),
        options=access_options(
            limits=limits,
            tools=tools,
            denials=_denials(gateway, tools, account.account),
        ),
    )

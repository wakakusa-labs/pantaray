"""Types shared by every external-app connection method.

The connection method (the gateway implementation) owns the tool list, the
descriptions and the input schemas; Pantaray owns only the toolkit table and
the classification rule. Everything past ``IntegrationGateway`` is the
connection method's business.
"""

from __future__ import annotations

from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from pantaray_agents.local_runtime.tooling.models import InternalStrictModel
from pantaray_agents.schema.agent.base import JSONValue

# 'gmail', 'github', ...; the connection method's toolkit table is canonical.
type ToolkitSlug = str
type ToolClass = Literal["read", "write"]
type ClassOverride = Literal["write", "blocked"]


class IntegrationTool(InternalStrictModel):
    slug: str
    toolkit: ToolkitSlug
    name: str
    description: str
    # JSON Schema owned by the external service: a genuinely unstructured boundary.
    input_schema: dict[str, JSONValue]
    tool_class: ToolClass


class IntegrationAccount(InternalStrictModel):
    """One account connected to a toolkit; a toolkit may have several."""

    # The connection method's own account ID, used as the handle everywhere.
    account: str
    toolkit: ToolkitSlug
    # Email address, workspace name, ...: shown on screen and in confirmations.
    label: str


class ChangeProbe(InternalStrictModel):
    tool_slug: str
    # May hold placeholders such as '{window_start_epoch}'.
    arguments: dict[str, JSONValue]


class ToolkitEntry(InternalStrictModel):
    """One row of the connection method's toolkit table."""

    toolkit: ToolkitSlug
    display_name: str
    class_overrides: dict[str, ClassOverride] = Field(default_factory=dict)
    probe: ChangeProbe | None = None


type IntegrationHealth = Literal[
    "ok", "not_connected", "reauthorization_required", "unavailable"
]

type IntegrationErrorKind = Literal[
    "connection_missing",
    "reauthorization_required",
    "scope_missing",
    "invalid_arguments",
    "rate_limited",
    "usage_limit_reached",
    "upstream_failed",
    "outcome_unknown",
    "permission_denied",
    "account_required",
]


class IntegrationError(InternalStrictModel):
    kind: IntegrationErrorKind
    retry_after_seconds: int | None = None
    # The accounts the model may choose from; only for 'account_required'.
    candidates: list[IntegrationAccount] = Field(default_factory=list)

    @model_validator(mode="after")
    def _candidates_only_when_choosing(self) -> Self:
        # account_required exists because two or more accounts could run the tool.
        expected_ok = (
            len(self.candidates) >= 2
            if self.kind == "account_required"
            else not self.candidates
        )
        if not expected_ok:
            raise ValueError(
                "account_required carries two or more candidates; other kinds carry none"
            )
        return self


class ToolSucceeded(InternalStrictModel):
    outcome: Literal["succeeded"] = "succeeded"
    data: JSONValue


class ToolFailed(InternalStrictModel):
    outcome: Literal["failed"] = "failed"
    error: IntegrationError


type ToolOutcome = ToolSucceeded | ToolFailed


class ApiDenial(InternalStrictModel):
    """Why the API does not allow something, as data the settings screen localizes."""

    # The whole connection method (GatewayLimits), or one account's grant such
    # as its OAuth scopes (IntegrationGateway.api_denied_reason).
    source: Literal["connection_method", "account_grant"]
    capability: Literal["connect", "execute", "tool"]
    # An identifier from the external service shown as is, such as a missing
    # OAuth scope; never prose, which could not be localized.
    detail: str | None = None


class GatewayLimits(InternalStrictModel):
    """What the connection method allows, known before the user chooses."""

    can_connect: bool
    can_execute: bool
    # Exactly one reason per unavailable capability, so a greyed-out option
    # always has one to show.
    reasons: list[ApiDenial]

    @model_validator(mode="after")
    def _one_reason_per_denied_capability(self) -> Self:
        denied = [
            capability
            for capability, allowed in (
                ("connect", self.can_connect),
                ("execute", self.can_execute),
            )
            if not allowed
        ]
        if sorted(reason.capability for reason in self.reasons) != denied or any(
            reason.source != "connection_method" for reason in self.reasons
        ):
            raise ValueError(
                "limits carry one connection_method reason per denied capability"
            )
        return self


class IntegrationGateway(Protocol):
    def toolkits(self) -> list[ToolkitEntry]: ...

    async def list_accounts(self, toolkit: ToolkitSlug) -> list[IntegrationAccount]: ...

    def health(self, account: str) -> IntegrationHealth: ...

    def limits(self) -> GatewayLimits: ...

    async def list_tools(self, toolkit: ToolkitSlug) -> list[IntegrationTool]:
        """Return the toolkit's tools already classified; blocked tools are absent."""
        ...

    def api_denied_reason(self, *, slug: str, account: str) -> ApiDenial | None:
        """Why this account's grant does not allow the tool, or None when it does."""
        ...

    async def execute(
        self,
        *,
        slug: str,
        account: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
    ) -> ToolOutcome: ...

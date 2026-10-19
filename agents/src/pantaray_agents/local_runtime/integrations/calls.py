"""integration_run_tool's shared core: resolve one call, then run it.

Nothing from the run's start is trusted: the slug, the account and its permission
are resolved again on every call, and the arguments are validated against the
tool's input_schema before anything runs. Resolving and running are separate
steps so a caller can settle consent (the Action) or spend a budget (Suggestion
research) between them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import cast

from jsonschema import Draft7Validator, ValidationError  # type: ignore[import-untyped]
from referencing import Registry

from pantaray_agents.schema.agent.base import JSONValue

from .contracts import (
    IntegrationAccount,
    IntegrationError,
    IntegrationErrorKind,
    IntegrationGateway,
    IntegrationTool,
    ToolFailed,
    ToolOutcome,
)
from .registry import get_integration_gateway, get_integration_tool_index
from .search import (
    RECONNECT_REMEDY,
    UPSTREAM_FAILED_MESSAGE,
    IntegrationGatewayError,
    gateway_await,
    gateway_call,
    load_account_setting,
    setting_fix,
    setting_remedy,
    usable_accounts,
)

logger = logging.getLogger(__name__)

# What execute_integration_call returns on success.
RUN_RESULT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source": {"type": "string"},
        "untrusted": {"type": "boolean", "enum": [True]},
        "data": {},
    },
    "required": ["source", "untrusted", "data"],
}

# Fixed text per kind: a connection method's own error text never reaches the model.
_MESSAGES: dict[IntegrationErrorKind, str] = {
    "connection_missing": "The account is not connected; the user can connect it in Settings.",
    "reauthorization_required": "The account must be signed in again in Settings.",
    "scope_missing": "The connection lacks a permission this tool needs.",
    "invalid_arguments": "The service rejected the arguments.",
    "rate_limited": "The service is limiting requests; wait before trying again.",
    "usage_limit_reached": "The connection's monthly usage limit is used up; do not retry.",
    "upstream_failed": UPSTREAM_FAILED_MESSAGE,
    "outcome_unknown": "The write may or may not have run. Do not run it again; ask the user to check.",
    "permission_denied": "The current permission does not allow this call.",
    "account_required": "Several accounts can run this tool; pass account as one of the candidates.",
}


@dataclass(frozen=True, slots=True)
class IntegrationRefusal:
    error: IntegrationError
    message: str
    # The validator's text quotes the external schema and the model's arguments.
    untrusted_detail: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedIntegrationCall:
    gateway: IntegrationGateway
    tool: IntegrationTool
    toolkit_name: str
    account: IntegrationAccount
    arguments: dict[str, JSONValue]


async def resolve_integration_call(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    slug: str,
    account: str | None,
    arguments: str,
    read_only: bool,
) -> ResolvedIntegrationCall | IntegrationRefusal:
    """Resolve the call without running it; ``read_only`` refuses a write for a
    caller that cannot ask the user (a read never asks in this permission model)."""

    try:
        return await _resolve_with_gateway(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            slug=slug,
            requested=account,
            arguments=arguments,
            read_only=read_only,
        )
    except IntegrationGatewayError:
        return _refuse("upstream_failed", _MESSAGES["upstream_failed"])


async def execute_integration_call(
    resolved: ResolvedIntegrationCall, *, tool_request_id: str
) -> dict[str, JSONValue] | IntegrationRefusal:
    is_write = resolved.tool.tool_class == "write"
    try:
        outcome: ToolOutcome = await resolved.gateway.execute(
            slug=resolved.tool.slug,
            account=resolved.account.account,
            arguments=resolved.arguments,
            tool_request_id=tool_request_id,
        )
    except Exception as exc:  # noqa: BLE001
        # The gateway reports failures as outcomes. An exception still must not
        # hand its text to the model, and a write it interrupted may have run.
        logger.warning(
            "integration tool raised: slug=%s exception_type=%s",
            resolved.tool.slug,
            type(exc).__name__,
        )
        kind: IntegrationErrorKind = (
            "outcome_unknown" if is_write else "upstream_failed"
        )
        outcome = ToolFailed(error=IntegrationError(kind=kind))
    if isinstance(outcome, ToolFailed):
        return IntegrationRefusal(
            error=outcome.error,
            message=_MESSAGES[outcome.error.kind],
            untrusted_detail=outcome.untrusted_detail,
        )
    return {"source": resolved.tool.toolkit, "untrusted": True, "data": outcome.data}


def refusal_details(refusal: IntegrationRefusal) -> dict[str, JSONValue] | None:
    error = refusal.error
    details: dict[str, JSONValue] = {}
    if error.candidates:
        details["candidates"] = [
            {"account": item.account, "label": item.label} for item in error.candidates
        ]
    if error.retry_after_seconds is not None:
        details["retry_after_seconds"] = error.retry_after_seconds
    if refusal.untrusted_detail is not None:
        details["detail"] = {"untrusted": True, "text": refusal.untrusted_detail}
    return details or None


async def _resolve_with_gateway(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    slug: str,
    requested: str | None,
    arguments: str,
    read_only: bool,
) -> ResolvedIntegrationCall | IntegrationRefusal:
    gateway = get_integration_gateway()
    tool = get_integration_tool_index().get(slug)
    if gateway is None or tool is None:
        return _refuse(
            "invalid_arguments",
            f"No tool has the slug {slug!r}; find one with integration_list_tools.",
        )
    if read_only and tool.tool_class == "write":
        return _refuse(
            "permission_denied", f"{slug} writes to the app; only read tools run here."
        )
    connected = await gateway_await(gateway.list_accounts(tool.toolkit))
    usable = [
        item.account
        for item in usable_accounts(
            gateway,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            tool=tool,
            accounts=connected,
        )
    ]
    if requested is None:
        if len(usable) >= 2:
            return IntegrationRefusal(
                error=IntegrationError(kind="account_required", candidates=usable),
                message=_MESSAGES["account_required"],
            )
        if not usable:
            message = f"No connected {tool.toolkit} account may run {slug} now."
            if connected and not read_only:
                message = " ".join(
                    _denial(
                        gateway,
                        db_path=db_path,
                        busy_timeout_ms=busy_timeout_ms,
                        user_id=user_id,
                        tool=tool,
                        account=item,
                        read_only=read_only,
                    )
                    for item in connected
                )
            return _refuse("permission_denied", message)
        account = usable[0]
    else:
        target = next((item for item in connected if item.account == requested), None)
        if target is None:
            return _refuse(
                "connection_missing",
                f"{requested!r} is not a connected {tool.toolkit} account.",
            )
        if target not in usable:
            why = _denial(
                gateway,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                tool=tool,
                account=target,
                read_only=read_only,
            )
            return _refuse("permission_denied", why)
        account = target
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return _refuse("invalid_arguments", "arguments is not valid JSON.", str(exc))
    if not isinstance(parsed, dict):
        return _refuse("invalid_arguments", "arguments must be one JSON object.")
    try:
        Draft7Validator.check_schema(tool.input_schema)
        # An empty registry resolves only refs inside the schema: the default one
        # would fetch a remote $ref, synchronously and without a timeout.
        Draft7Validator(tool.input_schema, registry=Registry()).validate(parsed)
    except ValidationError as exc:
        return _refuse(
            "invalid_arguments",
            "arguments do not match the tool's input_schema.",
            "/".join(str(part) for part in exc.absolute_path) + f": {exc.message}"
            if exc.absolute_path
            else exc.message,
        )
    except Exception as exc:  # noqa: BLE001
        # The schema is untrusted data from the connection, so anything its
        # evaluation raises (unknown types, unresolvable or recursive refs, an
        # invalid schema) is caught here; the text may quote the schema or a URL.
        logger.warning("integration input_schema failed: %s", type(exc).__name__)
        return _refuse("upstream_failed", "The tool's input schema could not be used.")
    names = {
        entry.toolkit: entry.display_name for entry in gateway_call(gateway.toolkits)
    }
    return ResolvedIntegrationCall(
        gateway=gateway,
        tool=tool,
        toolkit_name=names[tool.toolkit],
        account=account,
        # json.loads yields only JSON values.
        arguments=cast(dict[str, JSONValue], parsed),
    )


def _denial(
    gateway: IntegrationGateway,
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool: IntegrationTool,
    account: IntegrationAccount,
    read_only: bool,
) -> str:
    """Say why the current permission refuses, without the connection's own text."""

    retry = "The permission may have changed during this run; do not retry."
    if not gateway_call(gateway.limits).can_execute:
        why = "the connection method does not allow running tools now"
    elif gateway_call(partial(gateway.health, account.account)) != "ok":
        # Reconnecting comes first: no setting makes this connection run.
        why = "the account must be reconnected"
        if not read_only:
            retry = f"{RECONNECT_REMEDY} Do not retry."
    elif (
        gateway_call(
            partial(gateway.api_denied_reason, slug=tool.slug, account=account.account)
        )
        is not None
    ):
        why = "the account's connection does not grant this tool"
    else:
        setting = load_account_setting(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            account=account,
        )
        if setting.access == "off":
            why = "the account is turned off"
        elif setting.access == "read_only" and tool.tool_class == "write":
            why = "the account allows read tools only"
        else:
            why = "this tool is blocked for the account"
        fix = setting_fix(gateway, tool=tool, account=account, setting=setting)
        if not read_only and fix is not None:
            retry = f"{setting_remedy(fix)} Do not retry."
    return f"Cannot run {tool.slug} with {account.label}: {why}. {retry}"


def _refuse(
    kind: IntegrationErrorKind, message: str, detail: str | None = None
) -> IntegrationRefusal:
    return IntegrationRefusal(
        IntegrationError(kind=kind), message, untrusted_detail=detail
    )


__all__ = [
    "IntegrationRefusal",
    "RUN_RESULT_SCHEMA",
    "ResolvedIntegrationCall",
    "execute_integration_call",
    "refusal_details",
    "resolve_integration_call",
]

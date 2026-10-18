"""Composio's replies read as the shared integration types; pure functions only."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from pantaray_agents.schema.agent.base import JSONValue

from ..classification import ToolDisposition, classify_tool
from ..contracts import (
    ClassOverride,
    IntegrationError,
    IntegrationErrorKind,
    IntegrationHealth,
    IntegrationTool,
    ToolClass,
    ToolFailed,
    ToolOutcome,
    ToolSucceeded,
)
from .client import ComposioResponse

# A tag that says the tool changes something outweighs readOnlyHint.
_CHANGE_HINTS: Final = frozenset({"createHint", "updateHint", "destructiveHint"})
# A connection still waiting for the user's consent is not an account yet.
PENDING_STATUSES: Final = frozenset({"INITIALIZING", "INITIATED"})
# The provider's text quotes the arguments back; enough to fix them.
_DETAIL_MAX_CHARS: Final = 1_000
# Composio's error codes (status 400 unless noted).
CODE_NO_ACTIVE_CONNECTION: Final = 4302
CODE_TOOLKIT_NOT_IN_SESSION: Final = 4324
CODE_SESSION_NOT_FOUND: Final = 4331  # 404
CODE_KEY_PERMISSION_DENIED: Final = 812  # 403
CODE_VALIDATION_ERROR: Final = 10400
_CODE_ACCOUNT_NOT_FOUND: Final = 4300
_CODE_TOOL_NOT_FOUND: Final = 4301  # 404
_SCOPE_SEPARATOR: Final = re.compile(r"[\s,]+")
# Pantaray's own text: Composio's questions for the user are not passed on.
NEEDS_INPUT_DETAIL: Final = "The tool asked for more input before it would run."

type ScopeGroups = tuple[tuple[str, ...], ...]


class _External(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _ScopeGroup(_External):
    any_of: list[str] = []
    # Set when the scopes are needed only for some arguments.
    when: dict[str, JSONValue] | None = None


class _ScopeRequirements(_External):
    all_of: list[_ScopeGroup] = []


class _ToolkitRef(_External):
    slug: str


class RawTool(_External):
    slug: str
    name: str
    description: str = ""
    toolkit: _ToolkitRef
    input_parameters: dict[str, JSONValue]
    tags: list[str] | None = None
    scopes: list[str] | None = None
    scope_requirements: _ScopeRequirements | None = None
    is_deprecated: bool = False


class _ExecuteReply(_External):
    # 'completed' is the only value that means the tool ran and succeeded
    # (seen on 2026-10-18); input_required means it has not run.
    result_type: Literal["completed", "failed", "input_required"]
    data: JSONValue = None
    error: str | None = None


class _AccountState(_External):
    val: dict[str, JSONValue] | None = None


class RawAccount(_External):
    id: str
    status: str
    toolkit: _ToolkitRef
    data: dict[str, JSONValue] | None = None
    state: _AccountState | None = None


def tool_disposition(
    raw: RawTool, class_overrides: Mapping[str, ClassOverride]
) -> ToolDisposition:
    tags = set(raw.tags or ())
    return classify_tool(
        raw.slug,
        metadata_read_only="readOnlyHint" in tags and not tags & _CHANGE_HINTS,
        class_overrides=class_overrides,
    )


def integration_tool(raw: RawTool, tool_class: ToolClass) -> IntegrationTool:
    return IntegrationTool(
        slug=raw.slug,
        toolkit=raw.toolkit.slug,
        name=raw.name,
        description=raw.description,
        input_schema=raw.input_parameters,
        tool_class=tool_class,
    )


def required_scopes(raw: RawTool) -> ScopeGroups:
    """Groups that each need one granted scope; checked before the call.

    A group that applies only to some arguments, or that names a scope with a
    placeholder filled at connection time ('{{subdomain}}'), cannot be judged
    here and is left to the service.
    """

    if raw.scope_requirements is not None:
        groups = [
            group.any_of
            for group in raw.scope_requirements.all_of
            if group.when is None
        ]
    else:
        # Without requirements the scopes are alternatives (verified on 2026-10-18).
        groups = [raw.scopes or []]
    return tuple(
        tuple(group)
        for group in groups
        if group and not any("{{" in scope for scope in group)
    )


def granted_scopes(raw: RawAccount) -> frozenset[str] | None:
    """The scopes the connection granted, or None when Composio does not say."""

    for values in ((raw.state.val if raw.state else None), raw.data):
        scope = (values or {}).get("scope")
        if isinstance(scope, str) and scope.strip():
            # Google separates with spaces; Slack's OAuth uses commas.
            return frozenset(s for s in _SCOPE_SEPARATOR.split(scope) if s)
    return None


def missing_scope(groups: ScopeGroups, granted: frozenset[str] | None) -> str | None:
    if granted is None:
        return None
    for group in groups:
        if not granted.intersection(group):
            return group[0]
    return None


def account_label(raw: RawAccount) -> str:
    # displayName holds the email address for Google (verified on 2026-10-18).
    for values in ((raw.state.val if raw.state else None), raw.data):
        name = (values or {}).get("displayName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return raw.id


def account_health(status: str) -> IntegrationHealth:
    # EXPIRED, REVOKED, FAILED and INACTIVE (disabled) all recover only by
    # connecting again, the one recovery Pantaray offers.
    return "ok" if status == "ACTIVE" else "reauthorization_required"


def _never_ran_or_unknown(is_write: bool) -> IntegrationErrorKind:
    return "outcome_unknown" if is_write else "upstream_failed"


def api_failure(response: ComposioResponse, *, is_write: bool) -> IntegrationError:
    """Map a non-200 Composio reply; ``is_write`` is False when the tool never ran.

    The gateway refuses an account it does not list before calling, so 4302
    names a known account whose connection is not active.
    """

    status, code = response.status, response.code
    kind: IntegrationErrorKind
    if status == 401:
        kind = "connection_missing"
    elif status == 402:
        # Not verified: the monthly cap's reply has not been seen.
        kind = "usage_limit_reached"
    elif status == 403:
        kind = (
            "permission_denied"
            if code == CODE_KEY_PERMISSION_DENIED
            else "scope_missing"
        )
    elif code == CODE_NO_ACTIVE_CONNECTION:
        kind = "reauthorization_required"
    elif code == _CODE_ACCOUNT_NOT_FOUND:
        kind = "connection_missing"
    elif code == _CODE_TOOL_NOT_FOUND:
        kind = "invalid_arguments"
    elif status == 404:
        kind = "connection_missing"
    elif status == 422:
        kind = "reauthorization_required"
    elif status == 429:
        return IntegrationError(
            kind="rate_limited", retry_after_seconds=response.retry_after_seconds
        )
    elif 400 <= status < 500 and status != 408:
        kind = "invalid_arguments"
    else:
        kind = _never_ran_or_unknown(is_write)
    return IntegrationError(kind=kind)


def execute_outcome(body: JSONValue, *, is_write: bool) -> ToolOutcome:
    """Map the body of a 200 reply to a session execute."""

    try:
        reply = _ExecuteReply.model_validate(body)
    except ValidationError:
        # Missing or unknown result_type: whether the tool ran is not known.
        return ToolFailed(error=IntegrationError(kind=_never_ran_or_unknown(is_write)))
    if reply.result_type == "input_required":
        return ToolFailed(
            error=IntegrationError(kind="invalid_arguments"),
            untrusted_detail=NEEDS_INPUT_DETAIL,
        )
    if reply.result_type == "completed" and not reply.error:
        return ToolSucceeded(data=reply.data)
    return _tool_failure(reply.data, reply.error or "", is_write=is_write)


def _tool_failure(data: JSONValue, text: str, *, is_write: bool) -> ToolFailed:
    """The tool call failed at the provider or at Composio's argument check."""

    status = _provider_status(data, text)
    kind: IntegrationErrorKind
    if status in (400, 404, 409, 422, "INVALID_ARGUMENT", "NOT_FOUND", "OUT_OF_RANGE"):
        return ToolFailed(
            error=IntegrationError(kind="invalid_arguments"),
            untrusted_detail=text[:_DETAIL_MAX_CHARS] or None,
        )
    if status in (401, "UNAUTHENTICATED"):
        kind = "reauthorization_required"
    elif status in (403, "PERMISSION_DENIED"):
        kind = "scope_missing"
    elif status in (429, "RESOURCE_EXHAUSTED"):
        kind = "rate_limited"
    else:
        # A 5xx, or a status that cannot be read: a write may have run.
        kind = _never_ran_or_unknown(is_write)
    return ToolFailed(error=IntegrationError(kind=kind))


def _provider_status(data: JSONValue, text: str) -> int | str | None:
    # Composio puts the provider's HTTP status in data.status_code; the error
    # text is often the provider's JSON, such as Google's {"error": {...}}.
    if isinstance(data, dict):
        code = data.get("status_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    try:
        parsed: JSONValue = json.loads(text)
    except ValueError:
        return None
    inner = parsed.get("error") if isinstance(parsed, dict) else None
    for holder in (inner, parsed):
        if isinstance(holder, dict):
            for key in ("code", "status_code", "status"):
                value = holder.get(key)
                if isinstance(value, int | str) and not isinstance(value, bool):
                    return value
    return None

"""integration_search_tools / integration_run_tool against a fake gateway.

AC-C1: a write always asks; unusable tools never show and are refused by slug.
AC-C8: a permission changed after search or approval refuses the call.
AC-C10: two accounts of one toolkit with different permissions.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ALLOWED_TOOL_IDS,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    integration_run,
    integration_search,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalRequiredToolControl,
    CompletedToolControl,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import (
    INTEGRATION_RUN_TOOL_TOOL,
    INTEGRATION_SEARCH_TOOLS_TOOL,
    INTEGRATION_TOOL_IDS,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    select_supervisor_act_tool_ids,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationError,
    IntegrationHealth,
    IntegrationTool,
    ToolFailed,
    ToolkitEntry,
    ToolkitSlug,
    ToolOutcome,
    ToolSucceeded,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    apply_approval_decision,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationStartInput
from pantaray_agents.local_runtime.tooling.repository import (
    load_approval_session_by_request,
    record_tool_invocation_start,
    set_action_approval_mode,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    set_integration_account_access,
    set_integration_tool_blocked,
)
from pantaray_agents.schema.agent.base import JSONValue

BUSY_TIMEOUT_MS = 1_000
NOW = "2026-03-23T00:00:02Z"
WORK = IntegrationAccount(
    account="acct-work", toolkit="gmail", label="work@example.com"
)
HOME = IntegrationAccount(
    account="acct-home", toolkit="gmail", label="home@example.com"
)
FULL_LIMITS = GatewayLimits(can_connect=True, can_execute=True, reasons=[])
_REQUESTS = itertools.count(1)
SEND_ARGUMENTS = {"to": "someone@example.com", "body": "Hello"}


def _tool(slug: str, tool_class: str, schema: dict[str, JSONValue]) -> IntegrationTool:
    return IntegrationTool(
        slug=slug,
        toolkit="gmail",
        name=slug.replace("_", " ").title(),
        description=f"{slug.lower().replace('_', ' ')} in the mailbox",
        input_schema=schema,
        tool_class=tool_class,  # type: ignore[arg-type]
    )


TOOLS = [
    _tool("GMAIL_FETCH_EMAILS", "read", {"type": "object"}),
    _tool(
        "GMAIL_SEND_EMAIL",
        "write",
        {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to"],
        },
    ),
    _tool("GMAIL_LIST_LABELS", "read", {"type": "object"}),
]


class FakeGateway:
    def __init__(self, *, accounts: list[IntegrationAccount]) -> None:
        self.accounts = accounts
        self.limits_value = FULL_LIMITS
        self.denied: dict[tuple[str, str], ApiDenial] = {}
        self.outcome: ToolOutcome | Exception = ToolSucceeded(data={"id": "msg-1"})
        self.executed: list[tuple[str, str, dict[str, JSONValue]]] = []

    def toolkits(self) -> list[ToolkitEntry]:
        return [ToolkitEntry(toolkit="gmail", display_name="Gmail")]

    async def list_accounts(self, toolkit: ToolkitSlug) -> list[IntegrationAccount]:
        return self.accounts if toolkit == "gmail" else []

    def health(self, account: str) -> IntegrationHealth:
        return "ok"

    def limits(self) -> GatewayLimits:
        return self.limits_value

    async def list_tools(self, toolkit: ToolkitSlug) -> list[IntegrationTool]:
        raise AssertionError("the Action reads the tool index, not the gateway")

    def api_denied_reason(self, *, slug: str, account: str) -> ApiDenial | None:
        return self.denied.get((account, slug))

    async def execute(
        self,
        *,
        slug: str,
        account: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
    ) -> ToolOutcome:
        self.executed.append((slug, account, arguments))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        allowed_tool_ids=(*BROKER_ALLOWED_TOOL_IDS, INTEGRATION_RUN_TOOL_TOOL.tool_id),
    )
    for module in (integration_run, integration_search):
        monkeypatch.setattr(
            module, "read_local_runtime_db_config", lambda: (db_path, BUSY_TIMEOUT_MS)
        )
    monkeypatch.setattr(
        integration_run,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )
    gateway = FakeGateway(accounts=[WORK, HOME])
    set_integration_gateway(gateway)
    get_integration_tool_index().replace("gmail", TOOLS)
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at=NOW,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["manifest_id"] = context.manifest_id  # type: ignore[attr-defined]
    state["execution_session_id"] = context.execution_session_id  # type: ignore[attr-defined]
    namespace = SimpleNamespace(
        db_path=db_path, context=context, gateway=gateway, state=state
    )
    _set_access(namespace, WORK, "read_write")
    _set_access(namespace, HOME, "read_only")
    yield namespace
    set_integration_gateway(None)


def _set_access(
    env: SimpleNamespace, account: IntegrationAccount, access: IntegrationAccess
) -> None:
    set_integration_account_access(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        toolkit="gmail",
        account=account.account,
        access=access,
        updated_at=NOW,
    )


async def _search(env: SimpleNamespace, query: str) -> dict[str, list[str]]:
    result = await integration_search.run_integration_search_tool(
        step_id="step-0",
        tool_def=INTEGRATION_SEARCH_TOOLS_TOOL,
        args={"query": query},
        state=env.state,
    )
    assert isinstance(result.output, dict)
    tools = result.output["tools"]
    assert isinstance(tools, list)
    return {
        str(tool["slug"]): [
            f"{a['account']}:{a['permission']}" for a in tool["accounts"]
        ]  # type: ignore[index]
        for tool in tools
    }


def _args(slug: str, account: str | None, arguments: object) -> dict[str, JSONValue]:
    return {"slug": slug, "account": account, "arguments": json.dumps(arguments)}


async def _preflight(env: SimpleNamespace, args: dict[str, JSONValue], request: str):
    return await integration_run.run_integration_run_preflight(
        step_id="step-1",
        tool_def=INTEGRATION_RUN_TOOL_TOOL,
        state=env.state,
        args=args,
        tool_request_id=request,
        requested_at=NOW,
    )


async def _run(
    env: SimpleNamespace, args: dict[str, JSONValue], request: str | None = None
) -> dict[str, JSONValue]:
    """Run the call as the Action runtime does: preflight, then the call itself."""

    request = request or f"request-{next(_REQUESTS)}"
    preparation = await _preflight(env, args, request)
    assert isinstance(preparation.control, CompletedToolControl)
    record_tool_invocation_start(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=ToolInvocationStartInput(
            invocation_id=f"invocation-{request}",
            tool_request_id=request,
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id=INTEGRATION_RUN_TOOL_TOOL.tool_id,
            manifest_id=env.context.manifest_id,
            execution_session_id=env.context.execution_session_id,
            cwd=".",
            timeout_ms=None,
            intent_class="network_access",
            network_policy="deny",
            command_summary_json={},
            capability_snapshot_json={"required_capabilities": []},
            request_json={},
            status="running",
            started_at=NOW,
        ),
    )
    result = await integration_run.run_integration_run_tool(
        step_id="step-1",
        tool_def=INTEGRATION_RUN_TOOL_TOOL,
        state=env.state,
        args=args,
        tool_request_id=request,
        tool_invocation_id=f"invocation-{request}",
        requested_at=NOW,
    )
    assert isinstance(result.output, dict)
    return result.output


async def _approve_send(env: SimpleNamespace, request: str) -> dict[str, JSONValue]:
    args = _args("GMAIL_SEND_EMAIL", WORK.account, SEND_ARGUMENTS)
    asked = await _preflight(env, args, request)
    assert isinstance(asked.control, ApprovalRequiredToolControl)
    apply_approval_decision(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        tool_request_id=request,
        user_id="user-1",
        action_id="action-1",
        approval_session_id=None,
        decision="approved_once",
        decided_at=NOW,
    )
    return args


def _error(output: dict[str, JSONValue]) -> dict[str, JSONValue]:
    error = output["error"]
    assert output["status"] == "error" and isinstance(error, dict)
    return error


@pytest.mark.asyncio
async def test_tools_are_offered_only_with_a_connected_account() -> None:
    set_integration_gateway(None)
    assert await integration_search.integration_tools_available() is False
    gateway = FakeGateway(accounts=[])
    set_integration_gateway(gateway)
    assert await integration_search.integration_tools_available() is False
    gateway.accounts = [WORK]
    assert await integration_search.integration_tools_available() is True
    set_integration_gateway(None)

    # AC-C6: without integrations the Action's tool set is exactly today's.
    assert select_supervisor_act_tool_ids(integrations=False) == (
        SUPERVISOR_SINGLE_REACT_TOOL_IDS
    )
    assert set(select_supervisor_act_tool_ids(integrations=True)) == {
        *SUPERVISOR_SINGLE_REACT_TOOL_IDS,
        *INTEGRATION_TOOL_IDS,
    }


@pytest.mark.asyncio
async def test_search_lists_only_usable_tools_with_their_accounts(
    env: SimpleNamespace,
) -> None:
    for account in (WORK, HOME):
        set_integration_tool_blocked(
            db_path=env.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            account=account.account,
            slug="GMAIL_LIST_LABELS",
            blocked=True,
            updated_at=NOW,
        )

    assert await _search(env, "email labels") == {
        "GMAIL_FETCH_EMAILS": ["acct-work:allow", "acct-home:allow"],
        # The read-only account cannot send, so only the work account is offered.
        "GMAIL_SEND_EMAIL": ["acct-work:ask"],
    }

    env.gateway.denied[(WORK.account, "GMAIL_SEND_EMAIL")] = ApiDenial(
        source="account_grant", capability="tool", detail="gmail.send"
    )
    assert "GMAIL_SEND_EMAIL" not in await _search(env, "send email")
    env.gateway.limits_value = GatewayLimits(
        can_connect=True,
        can_execute=False,
        reasons=[ApiDenial(source="connection_method", capability="execute")],
    )
    assert await _search(env, "email") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action_mode", [None, "always_allow"])
async def test_a_write_asks_every_time_and_runs_once_approved(
    env: SimpleNamespace, action_mode: str | None
) -> None:
    _grant_workspace_full_access(db_path=env.db_path, capability="process_exec_local")
    if action_mode is not None:
        set_action_approval_mode(
            db_path=env.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            action_id="action-1",
            approval_mode=action_mode,  # type: ignore[arg-type]
            updated_at=NOW,
        )

    args = await _approve_send(env, "send")

    pending = load_approval_session_by_request(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        tool_request_id="send",
    )
    assert pending is not None
    summary = pending.command_summary_json
    assert summary["account"] == "work@example.com"
    assert summary["toolkit"] == "Gmail"
    assert json.loads(str(summary["arguments"])) == SEND_ARGUMENTS
    assert await _run(env, args, "send") == {
        "source": "gmail",
        "untrusted": True,
        "data": {"id": "msg-1"},
    }
    assert env.gateway.executed == [("GMAIL_SEND_EMAIL", WORK.account, SEND_ARGUMENTS)]
    # The approval covered that one request; the next send asks again.
    again = await _preflight(env, args, "send-again")
    assert isinstance(again.control, ApprovalRequiredToolControl)


@pytest.mark.asyncio
async def test_unusable_accounts_and_slugs_are_refused_without_running(
    env: SimpleNamespace,
) -> None:
    sent_from_home = _error(
        await _run(env, _args("GMAIL_SEND_EMAIL", HOME.account, SEND_ARGUMENTS))
    )
    assert sent_from_home["error_type"] == "permission_denied"
    assert "read tools only" in str(sent_from_home["message"])

    unknown = _error(await _run(env, _args("GMAIL_DELETE_ALL", WORK.account, {})))
    assert unknown["error_type"] == "invalid_arguments"
    other_account = _error(
        await _run(env, _args("GMAIL_FETCH_EMAILS", "acct-other", {}))
    )
    assert other_account["error_type"] == "connection_missing"
    assert env.gateway.executed == []


@pytest.mark.asyncio
async def test_a_read_without_account_asks_the_model_to_choose(
    env: SimpleNamespace,
) -> None:
    choose = _error(await _run(env, _args("GMAIL_FETCH_EMAILS", None, {})))

    assert choose["error_type"] == "account_required"
    assert choose["details"] == {
        "candidates": [
            {"account": "acct-work", "label": "work@example.com"},
            {"account": "acct-home", "label": "home@example.com"},
        ]
    }

    _set_access(env, HOME, "off")
    assert (await _run(env, _args("GMAIL_FETCH_EMAILS", None, {})))["untrusted"]
    assert env.gateway.executed == [("GMAIL_FETCH_EMAILS", WORK.account, {})]


@pytest.mark.asyncio
async def test_a_permission_changed_after_search_or_approval_is_denied(
    env: SimpleNamespace,
) -> None:
    assert "acct-home:allow" in (await _search(env, "fetch"))["GMAIL_FETCH_EMAILS"]
    _set_access(env, HOME, "off")
    fetch = _error(await _run(env, _args("GMAIL_FETCH_EMAILS", HOME.account, {})))
    assert fetch["error_type"] == "permission_denied"
    assert "turned off" in str(fetch["message"])

    args = await _approve_send(env, "send")
    _set_access(env, WORK, "read_only")
    send = _error(await _run(env, args, "send"))
    assert send["error_type"] == "permission_denied"
    assert env.gateway.executed == []


@pytest.mark.asyncio
async def test_arguments_are_checked_before_the_user_is_asked(
    env: SimpleNamespace,
) -> None:
    args = _args("GMAIL_SEND_EMAIL", WORK.account, {"body": "no recipient"})

    error = _error(await _run(env, args, "bad-arguments"))

    assert error["error_type"] == "invalid_arguments"
    assert error["details"] == {
        "detail": {"untrusted": True, "text": "'to' is a required property"}
    }
    assert (
        load_approval_session_by_request(
            db_path=env.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            tool_request_id="bad-arguments",
        )
        is None
    )


@pytest.mark.asyncio
async def test_failures_reach_the_model_only_as_kinds(env: SimpleNamespace) -> None:
    env.gateway.outcome = ToolFailed(
        error=IntegrationError(kind="rate_limited", retry_after_seconds=30)
    )
    limited = _error(await _run(env, _args("GMAIL_FETCH_EMAILS", WORK.account, {})))
    assert limited["error_type"] == "rate_limited"
    assert limited["details"] == {"retry_after_seconds": 30}

    # A write whose result never came back is not retried and is not reported
    # with the connection's own text.
    env.gateway.outcome = RuntimeError("socket closed: token=secret")
    args = await _approve_send(env, "send")
    unknown = _error(await _run(env, args, "send"))
    assert unknown["error_type"] == "outcome_unknown"
    assert "secret" not in json.dumps(unknown)
    assert len(env.gateway.executed) == 2

"""integration_list_tools / integration_run_tool against a fake gateway.

AC-C1: a write always asks; unusable tools never show and are refused by slug.
AC-C8: a permission changed after listing or approval refuses the call.
AC-C10: two accounts of one toolkit with different permissions.
"""

from __future__ import annotations

import itertools
import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft7Validator  # type: ignore[import-untyped]
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ALLOWED_TOOL_IDS,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    integration_list,
    integration_run,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalRequiredToolControl,
    CompletedToolControl,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import (
    INTEGRATION_LIST_TOOLS_TOOL,
    INTEGRATION_RUN_TOOL_TOOL,
    INTEGRATION_TOOL_IDS,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    select_supervisor_act_tool_ids,
)
from pantaray_agents.local_runtime.integrations.calls import RUN_RESULT_SCHEMA
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationError,
    IntegrationHealth,
    IntegrationTool,
    ToolClass,
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
from pantaray_agents.local_runtime.integrations.search import (
    INTEGRATION_LIST_RESULT_CHARACTER_BUDGET,
    RECONNECT_REMEDY,
    integration_tools_available,
    list_integration_tools,
    setting_remedy,
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
from pantaray_agents.schema.tool_result import serialize_json_tool_output

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
        self.accounts_error: Exception | None = None
        self.unhealthy: set[str] = set()
        # GMAIL_LIST_LABELS is outside the list, reached only by a query.
        self.core_tools = ("GMAIL_FETCH_EMAILS", "GMAIL_SEND_EMAIL")
        self.note: str | None = None

    def toolkits(self) -> list[ToolkitEntry]:
        return [
            ToolkitEntry(
                toolkit="gmail",
                display_name="Gmail",
                core_tools=self.core_tools,
                note=self.note,
            )
        ]

    async def list_accounts(self, toolkit: ToolkitSlug) -> list[IntegrationAccount]:
        if self.accounts_error is not None:
            raise self.accounts_error
        return self.accounts if toolkit == "gmail" else []

    def health(self, account: str) -> IntegrationHealth:
        return "reauthorization_required" if account in self.unhealthy else "ok"

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
    for module in (integration_run, integration_list):
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


async def _list_output(
    env: SimpleNamespace, access: ToolClass, **args: JSONValue
) -> dict[str, JSONValue]:
    result = await integration_list.run_integration_list_tool(
        step_id="step-0",
        tool_def=INTEGRATION_LIST_TOOLS_TOOL,
        args={"toolkit": "gmail", "access": access, **args},
        state=env.state,
    )
    assert isinstance(result.output, dict)
    return result.output


async def _list(env: SimpleNamespace, access: ToolClass) -> dict[str, list[str]]:
    tools = (await _list_output(env, access))["tools"]
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


async def _approve_send(
    env: SimpleNamespace, request: str, account: str | None = WORK.account
) -> dict[str, JSONValue]:
    args = _args("GMAIL_SEND_EMAIL", account, SEND_ARGUMENTS)
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
    assert await integration_tools_available() is False
    gateway = FakeGateway(accounts=[])
    set_integration_gateway(gateway)
    assert await integration_tools_available() is False
    gateway.accounts = [WORK]
    assert await integration_tools_available() is True
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
async def test_the_list_is_the_usable_core_tools_of_one_class_in_order(
    env: SimpleNamespace,
) -> None:
    drafts = _tool("GMAIL_LIST_DRAFTS", "read", {"type": "object"})
    get_integration_tool_index().replace("gmail", [*TOOLS, drafts])
    # A core slug Composio no longer has is skipped.
    env.gateway.core_tools = (
        "GMAIL_LIST_DRAFTS",
        "GMAIL_REMOVED_TOOL",
        "GMAIL_SEND_EMAIL",
        "GMAIL_FETCH_EMAILS",
    )

    read = await _list_output(env, "read")
    assert [tool["slug"] for tool in read["tools"]] == [  # type: ignore[index, union-attr]
        "GMAIL_LIST_DRAFTS",
        "GMAIL_FETCH_EMAILS",
    ]
    assert read["other_tool_count"] == 1  # GMAIL_LIST_LABELS
    # The read-only account cannot send, so only the work account is offered.
    assert await _list(env, "write") == {"GMAIL_SEND_EMAIL": ["acct-work:ask"]}

    for account in (WORK, HOME):
        set_integration_tool_blocked(
            db_path=env.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            account=account.account,
            slug="GMAIL_LIST_DRAFTS",
            blocked=True,
            updated_at=NOW,
        )
    assert await _list(env, "read") == {
        "GMAIL_FETCH_EMAILS": ["acct-work:allow", "acct-home:allow"]
    }
    env.gateway.denied[(WORK.account, "GMAIL_SEND_EMAIL")] = ApiDenial(
        source="account_grant", capability="tool", detail="gmail.send"
    )
    assert await _list(env, "write") == {}
    env.gateway.limits_value = GatewayLimits(
        can_connect=True,
        can_execute=False,
        reasons=[ApiDenial(source="connection_method", capability="execute")],
    )
    assert await _list_output(env, "read") == {"tools": [], "other_tool_count": 0}


@pytest.mark.asyncio
async def test_every_list_of_a_toolkit_carries_its_note(env: SimpleNamespace) -> None:
    env.gateway.note = "Link a message with its account's email."
    for access in ("read", "write"):
        output = await _list_output(env, access)
        assert output["note"] == env.gateway.note


@pytest.mark.asyncio
async def test_a_query_searches_only_outside_the_list(env: SimpleNamespace) -> None:
    found = await _list_output(env, "read", query="fetch gmail emails")

    assert [tool["slug"] for tool in found["tools"]] == ["GMAIL_LIST_LABELS"]  # type: ignore[index, union-attr]
    assert "other_tool_count" not in found


@pytest.mark.asyncio
async def test_slugs_return_their_schemas_and_wrong_requests_name_the_fix(
    env: SimpleNamespace,
) -> None:
    sent = await _list_output(env, "write", slugs=["GMAIL_SEND_EMAIL"])
    assert sent["tools"][0]["input_schema"] == TOOLS[1].input_schema  # type: ignore[index]

    for access, args, expected in (
        ("read", {"slugs": ["GMAIL_SEND_EMAIL"]}, "is not a read tool of gmail"),
        ("read", {"slugs": ["SLACK_SEND_MESSAGE"]}, "is not a read tool of gmail"),
        ("read", {"slugs": ["GMAIL_LIST_LABELS"], "query": "labels"}, "not both"),
    ):
        error = _error(await _list_output(env, access, **args))  # type: ignore[arg-type]
        assert error["error_type"] == "invalid_arguments"
        assert expected in str(error["message"])

    unknown = await integration_list.run_integration_list_tool(
        step_id="step-0",
        tool_def=INTEGRATION_LIST_TOOLS_TOOL,
        args={"toolkit": "gmial", "access": "read"},
        state=env.state,
    )
    assert isinstance(unknown.output, dict)
    assert _error(unknown.output)["message"] == (
        "There is no toolkit 'gmial'; the toolkits are gmail."
    )


@pytest.mark.asyncio
async def test_a_long_list_keeps_schemas_only_while_it_fits(
    env: SimpleNamespace,
) -> None:
    def schema(fields: int) -> dict[str, JSONValue]:
        properties: dict[str, JSONValue] = {
            f"field_{n}": {"description": "x" * 80} for n in range(fields)
        }
        return {"type": "object", "properties": properties}

    # Shaped like Outlook's read list: tools of about 4,000 characters, and one
    # past the budget on its own.
    tools = [_tool(f"GMAIL_READ_{n}", "read", schema(30)) for n in range(7)]
    tools.append(_tool("GMAIL_READ_7", "read", schema(150)))
    get_integration_tool_index().replace("gmail", tools)
    env.gateway.core_tools = tuple(tool.slug for tool in tools)

    output = await _list_output(env, "read")

    listed = output["tools"]
    assert isinstance(listed, list) and len(listed) == 8
    kept = [tool for tool in listed if "input_schema" in tool]  # type: ignore[operator]
    assert 1 < len(kept) < 8 and listed[: len(kept)] == kept
    for tool in listed[len(kept) :]:
        assert tool["input_schema_omitted"] is True  # type: ignore[index]
        assert tool["accounts"]  # type: ignore[index]
    assert (
        len(serialize_json_tool_output(output))
        <= INTEGRATION_LIST_RESULT_CHARACTER_BUDGET
    )
    last = await _list_output(env, "read", slugs=["GMAIL_READ_7"])
    assert last["tools"][0]["input_schema"] == tools[7].input_schema  # type: ignore[index]


READ_ONLY_REMEDY = setting_remedy("read_only")
READ_WRITE_REMEDY = setting_remedy("read_write")


def _hidden(
    output: dict[str, JSONValue],
) -> tuple[JSONValue, dict[str, JSONValue]] | None:
    hidden = output.get("hidden_by_account_setting")
    if hidden is None:
        return None
    assert isinstance(hidden, dict) and isinstance(hidden["accounts"], list)
    return hidden["tool_count"], {
        str(item["account"]): item["remedy"]  # type: ignore[index]
        for item in hidden["accounts"]
    }


@pytest.mark.asyncio
async def test_the_list_counts_tools_only_an_account_setting_hides(
    env: SimpleNamespace,
) -> None:
    assert '"Read and write" (「読み取りと書き込み」)' in READ_WRITE_REMEDY
    assert '"Read only" (「読み取りのみ」)' in READ_ONLY_REMEDY
    assert "Settings → Integrations (設定 → 連携)" in READ_ONLY_REMEDY
    # The work account can send: nothing is hidden.
    assert _hidden(await _list_output(env, "write")) is None

    _set_access(env, WORK, "read_only")
    _set_access(env, HOME, "off")
    output = await _list_output(env, "write")

    assert output["tools"] == []
    assert _hidden(output) == (
        1,
        {"acct-work": READ_WRITE_REMEDY, "acct-home": READ_WRITE_REMEDY},
    )
    assert output["hidden_by_account_setting"]["accounts"][0] == {  # type: ignore[index]
        "toolkit": "gmail",
        "account": "acct-work",
        "label": "work@example.com",
        "remedy": READ_WRITE_REMEDY,
    }

    # "Read and write" would not help where the user turned the tool off, the
    # account's grant lacks it, or the connection needs reconnecting.
    set_integration_tool_blocked(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        account=HOME.account,
        slug="GMAIL_SEND_EMAIL",
        blocked=True,
        updated_at=NOW,
    )
    env.gateway.denied[(WORK.account, "GMAIL_SEND_EMAIL")] = ApiDenial(
        source="account_grant", capability="tool", detail="gmail.send"
    )
    assert _hidden(await _list_output(env, "write")) is None


@pytest.mark.asyncio
async def test_a_hidden_read_names_read_only_and_skips_unhealthy_accounts(
    env: SimpleNamespace,
) -> None:
    _set_access(env, WORK, "off")
    _set_access(env, HOME, "off")
    # The grant lacks every write, so "Read and write" is not an option at all.
    for account in (WORK, HOME):
        env.gateway.denied[(account.account, "GMAIL_SEND_EMAIL")] = ApiDenial(
            source="account_grant", capability="tool", detail="gmail.send"
        )
    env.gateway.unhealthy.add(WORK.account)

    assert _hidden(await _list_output(env, "read")) == (
        2,
        {"acct-home": READ_ONLY_REMEDY},
    )


@pytest.mark.asyncio
async def test_suggestion_research_names_no_setting_remedy(
    env: SimpleNamespace,
) -> None:
    _set_access(env, WORK, "off")
    _set_access(env, HOME, "off")

    output = await list_integration_tools(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        toolkit="gmail",
        access="read",
        query=None,
        slugs=None,
        offer_remedies=False,
    )

    assert output == {"tools": [], "other_tool_count": 0}


@pytest.mark.asyncio
async def test_a_refusal_names_the_remedy_that_would_work(
    env: SimpleNamespace,
) -> None:
    from_home = str(
        _error(
            await _run(env, _args("GMAIL_SEND_EMAIL", HOME.account, SEND_ARGUMENTS))
        )["message"]
    )
    assert "home@example.com" in from_home and READ_WRITE_REMEDY in from_home

    _set_access(env, HOME, "off")
    read_home = str(
        _error(await _run(env, _args("GMAIL_FETCH_EMAILS", HOME.account, {})))[
            "message"
        ]
    )
    assert READ_ONLY_REMEDY in read_home

    _set_access(env, WORK, "read_only")
    unnamed = _error(await _run(env, _args("GMAIL_SEND_EMAIL", None, SEND_ARGUMENTS)))
    assert unnamed["error_type"] == "permission_denied"
    message = str(unnamed["message"])
    assert "work@example.com" in message and "home@example.com" in message
    assert message.count(READ_WRITE_REMEDY) == 2

    # A tool the user turned off stays off under any account setting.
    set_integration_tool_blocked(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        account=WORK.account,
        slug="GMAIL_FETCH_EMAILS",
        blocked=True,
        updated_at=NOW,
    )
    blocked = str(
        _error(await _run(env, _args("GMAIL_FETCH_EMAILS", WORK.account, {})))[
            "message"
        ]
    )
    assert "blocked" in blocked
    assert READ_ONLY_REMEDY not in blocked and READ_WRITE_REMEDY not in blocked
    assert env.gateway.executed == []


@pytest.mark.asyncio
async def test_a_connection_to_reconnect_is_told_before_any_setting(
    env: SimpleNamespace,
) -> None:
    env.gateway.unhealthy.add(HOME.account)
    home = (
        "Cannot run GMAIL_SEND_EMAIL with home@example.com: the account must be"
        f" reconnected. {RECONNECT_REMEDY} Do not retry."
    )

    from_home = _error(
        await _run(env, _args("GMAIL_SEND_EMAIL", HOME.account, SEND_ARGUMENTS))
    )
    assert from_home["message"] == home

    _set_access(env, WORK, "read_only")
    unnamed = _error(await _run(env, _args("GMAIL_SEND_EMAIL", None, SEND_ARGUMENTS)))
    message = str(unnamed["message"])
    assert home in message
    assert message.count(READ_WRITE_REMEDY) == 1  # the work account's alone
    assert env.gateway.executed == []


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
        "account": {"account": WORK.account, "label": WORK.label},
        "untrusted": True,
        "data": {"id": "msg-1"},
    }
    assert env.gateway.executed == [("GMAIL_SEND_EMAIL", WORK.account, SEND_ARGUMENTS)]
    # The approval covered that one request; the next send asks again.
    again = await _preflight(env, args, "send-again")
    assert isinstance(again.control, ApprovalRequiredToolControl)


@pytest.mark.asyncio
async def test_the_result_names_the_accounts_own_id_when_the_gateway_knows_it(
    env: SimpleNamespace,
) -> None:
    env.gateway.outcome = ToolSucceeded(data={"id": "msg-1"}, account_user_id="U0SELF")

    output = await _run(env, _args("GMAIL_FETCH_EMAILS", WORK.account, {}))

    assert output["account"] == {
        "account": WORK.account,
        "label": WORK.label,
        "user_id": "U0SELF",
    }
    Draft7Validator(RUN_RESULT_SCHEMA).validate(output)


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
async def test_a_permission_changed_after_listing_or_approval_is_denied(
    env: SimpleNamespace,
) -> None:
    assert "acct-home:allow" in (await _list(env, "read"))["GMAIL_FETCH_EMAILS"]
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


@pytest.mark.asyncio
async def test_an_approval_does_not_carry_over_to_another_account(
    env: SimpleNamespace,
) -> None:
    # Two accounts that look alike on screen; only one may send when approved.
    first = IntegrationAccount(
        account="acct-a", toolkit="gmail", label="me@example.com"
    )
    second = IntegrationAccount(
        account="acct-b", toolkit="gmail", label="me@example.com"
    )
    env.gateway.accounts = [first, second]
    _set_access(env, first, "read_write")
    _set_access(env, second, "off")
    args = await _approve_send(env, "send", account=None)

    _set_access(env, first, "off")
    _set_access(env, second, "read_write")
    preparation = await _preflight(env, args, "send")
    result = await integration_run.run_integration_run_tool(
        step_id="step-1",
        tool_def=INTEGRATION_RUN_TOOL_TOOL,
        state=env.state,
        args=args,
        tool_request_id="send",
        tool_invocation_id="invocation-send",
        requested_at=NOW,
    )

    assert not isinstance(preparation.control, CompletedToolControl)
    assert isinstance(result.output, dict)
    assert _error(result.output)["error_type"] == "approval_state_changed"
    assert env.gateway.executed == []


@pytest.mark.asyncio
async def test_gateway_exceptions_never_reach_the_model(
    env: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    env.gateway.accounts_error = RuntimeError("Bearer synthetic-secret-token")

    listed = await integration_list.run_integration_list_tool(
        step_id="step-0",
        tool_def=INTEGRATION_LIST_TOOLS_TOOL,
        args={"toolkit": "gmail", "access": "read"},
        state=env.state,
    )
    args = _args("GMAIL_SEND_EMAIL", WORK.account, SEND_ARGUMENTS)
    preparation = await _preflight(env, args, "send")
    run = await _run(env, args, "send")

    assert listed.status == "error"
    assert isinstance(listed.output, dict)
    assert _error(listed.output)["error_type"] == "upstream_failed"
    assert _error(run)["error_type"] == "upstream_failed"
    for payload in (listed.output, preparation.result.output, run):
        assert "synthetic-secret" not in json.dumps(payload)
    assert "synthetic-secret" not in caplog.text
    assert env.gateway.executed == []


@pytest.mark.asyncio
async def test_input_schemas_resolve_only_their_own_refs(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[object] = []

    def no_network(*args: object, **kwargs: object) -> None:
        # Recorded, not raised: the validator would wrap the error as unresolvable.
        fetched.append(args)
        raise OSError("network is off in this test")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    remote_url = "http://127.0.0.1:9/schema.json"
    get_integration_tool_index().replace(
        "gmail",
        [
            *TOOLS,
            _tool("GMAIL_REMOTE_REF", "read", {"$ref": remote_url}),
            _tool("GMAIL_BAD_SCHEMA", "read", {"type": 5}),
            _tool(
                "GMAIL_LOCAL_REF",
                "read",
                {
                    "definitions": {"id": {"type": "string"}},
                    "type": "object",
                    "properties": {"id": {"$ref": "#/definitions/id"}},
                },
            ),
        ],
    )

    remote = await _run(env, _args("GMAIL_REMOTE_REF", WORK.account, {}))
    broken = await _run(env, _args("GMAIL_BAD_SCHEMA", WORK.account, {}))
    local_bad = await _run(env, _args("GMAIL_LOCAL_REF", WORK.account, {"id": 1}))
    local_ok = await _run(env, _args("GMAIL_LOCAL_REF", WORK.account, {"id": "a"}))

    assert fetched == []
    assert _error(remote)["error_type"] == "upstream_failed"
    assert "127.0.0.1" not in json.dumps(remote)
    assert _error(broken)["error_type"] == "upstream_failed"
    assert _error(local_bad)["error_type"] == "invalid_arguments"
    assert local_ok["untrusted"] is True
    assert env.gateway.executed == [("GMAIL_LOCAL_REF", WORK.account, {"id": "a"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/input", "$defs": {"input": {"type": "EXTERNAL_TEXT"}}},
        {"$ref": "#", "description": "EXTERNAL_TEXT"},
    ],
    ids=["unknown_type", "self_reference"],
)
async def test_an_unusable_input_schema_is_refused_without_its_text(
    env: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    schema: dict[str, JSONValue],
) -> None:
    get_integration_tool_index().replace(
        "gmail", [_tool("GMAIL_ODD_SCHEMA", "write", schema)]
    )
    args = _args("GMAIL_ODD_SCHEMA", WORK.account, {"to": "someone@example.com"})

    preparation = await _preflight(env, args, "odd")
    run = await _run(env, args, "odd")

    # The write never reached the user: the preflight left the refusal to the call.
    assert isinstance(preparation.control, CompletedToolControl)
    assert (
        load_approval_session_by_request(
            db_path=env.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            tool_request_id="odd",
        )
        is None
    )
    assert _error(run)["error_type"] == "upstream_failed"
    for payload in (preparation.result.output, run):
        assert "EXTERNAL_TEXT" not in json.dumps(payload)
    assert "EXTERNAL_TEXT" not in caplog.text
    assert env.gateway.executed == []

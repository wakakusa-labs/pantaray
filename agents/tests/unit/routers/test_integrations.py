from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationHealth,
    IntegrationTool,
    ToolkitEntry,
    ToolkitSlug,
    ToolOutcome,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.integrations.settings import (
    PermissionStore,
    apply_new_account_default,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.routers import integrations
from pantaray_agents.schema.agent.base import JSONValue

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
STATUS_URL = f"/v1/agents/users/{USER_ID}/integrations"
WORK = IntegrationAccount(account="ca_work", toolkit="gmail", label="work@example.com")
HOME = IntegrationAccount(account="ca_home", toolkit="gmail", label="home@example.com")
EXECUTE_DENIED = ApiDenial(source="connection_method", capability="execute")
SEND_SCOPE_MISSING = ApiDenial(
    source="account_grant", capability="tool", detail="gmail.send"
)
FULL_LIMITS = GatewayLimits(can_connect=True, can_execute=True, reasons=[])


def _tool(slug: str, tool_class: str) -> IntegrationTool:
    return IntegrationTool.model_validate(
        {
            "slug": slug,
            "toolkit": "gmail",
            "name": slug.title(),
            "description": "",
            "input_schema": {},
            "tool_class": tool_class,
        }
    )


TOOLS = [
    _tool("GMAIL_FETCH_EMAILS", "read"),
    _tool("GMAIL_SEND_EMAIL", "write"),
    _tool("GMAIL_CREATE_DRAFT", "write"),
]


class FakeGateway:
    def __init__(
        self,
        *,
        limits: GatewayLimits = FULL_LIMITS,
        denied: dict[tuple[str, str], ApiDenial] | None = None,
    ) -> None:
        self._limits = limits
        self._denied = denied or {}

    def toolkits(self) -> list[ToolkitEntry]:
        return [ToolkitEntry(toolkit="gmail", display_name="Gmail")]

    async def list_accounts(self, toolkit: ToolkitSlug) -> list[IntegrationAccount]:
        return [WORK, HOME] if toolkit == "gmail" else []

    def health(self, account: str) -> IntegrationHealth:
        return "ok" if account == WORK.account else "reauthorization_required"

    def limits(self) -> GatewayLimits:
        return self._limits

    async def list_tools(self, toolkit: ToolkitSlug) -> list[IntegrationTool]:
        raise AssertionError("the settings API reads the tool index, not the gateway")

    def api_denied_reason(self, *, slug: str, account: str) -> ApiDenial | None:
        return self._denied.get((account, slug))

    async def execute(
        self,
        *,
        slug: str,
        account: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
    ) -> ToolOutcome:
        raise AssertionError("settings never execute a tool")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PermissionStore]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    monkeypatch.setattr(
        integrations,
        "read_local_runtime_db_config",
        lambda: (db_path, BUSY_TIMEOUT_MS),
    )
    yield PermissionStore(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, user_id=USER_ID
    )
    set_integration_gateway(None)


def _register(gateway: FakeGateway) -> None:
    set_integration_gateway(gateway)
    get_integration_tool_index().replace("gmail", TOOLS)


def _client(user_id: str = USER_ID) -> TestClient:
    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: user_id
    return TestClient(app)


def _account_url(account: IntegrationAccount, suffix: str) -> str:
    return f"{STATUS_URL}/gmail/accounts/{account.account}/{suffix}"


def _accounts(client: TestClient) -> dict[str, dict[str, object]]:
    body = client.get(STATUS_URL).json()
    assert body["state"] == "configured"
    [toolkit] = body["toolkits"]
    assert toolkit["display_name"] == "Gmail"
    return {account["account"]: account for account in toolkit["accounts"]}


def _options(account: dict[str, object]) -> dict[str, tuple[bool, object]]:
    options = account["options"]
    assert isinstance(options, list)
    return {
        option["value"]: (option["allowed"], option["reason"]) for option in options
    }


def test_without_a_gateway_the_section_is_not_configured(
    store: PermissionStore,
) -> None:
    with _client() as client:
        assert client.get(STATUS_URL).json() == {"state": "not_configured"}
        response = client.put(_account_url(WORK, "access"), json={"access": "off"})
    assert response.status_code == 404


def test_without_execution_every_option_but_off_names_the_connection_method(
    store: PermissionStore,
) -> None:
    _register(
        FakeGateway(
            limits=GatewayLimits(
                can_connect=True, can_execute=False, reasons=[EXECUTE_DENIED]
            )
        )
    )
    denied = (False, EXECUTE_DENIED.model_dump(mode="json"))

    with _client() as client:
        accounts = _accounts(client)
        for account in accounts.values():
            assert _options(account) == {
                "off": (True, None),
                "read_only": denied,
                "read_write": denied,
            }
        response = client.put(
            _account_url(WORK, "access"), json={"access": "read_only"}
        )
        assert response.status_code == 409
        assert response.json()["detail"] == {"reason": denied[1]}
        assert _accounts(client)[WORK.account]["access"] == "off"


def test_two_accounts_of_one_toolkit_are_reported_and_set_independently(
    store: PermissionStore,
) -> None:
    # The work account's grant lacks the send scope, so no write tool is usable.
    _register(
        FakeGateway(
            denied={
                (WORK.account, "GMAIL_SEND_EMAIL"): SEND_SCOPE_MISSING,
                (WORK.account, "GMAIL_CREATE_DRAFT"): SEND_SCOPE_MISSING,
                (HOME.account, "GMAIL_SEND_EMAIL"): SEND_SCOPE_MISSING,
            }
        )
    )

    with _client() as client:
        accounts = _accounts(client)
        assert {name: a["health"] for name, a in accounts.items()} == {
            WORK.account: "ok",
            HOME.account: "reauthorization_required",
        }
        assert _options(accounts[WORK.account])["read_write"] == (
            False,
            SEND_SCOPE_MISSING.model_dump(mode="json"),
        )
        # One write tool (drafts) is still granted to the home account.
        assert _options(accounts[HOME.account])["read_write"] == (True, None)

        rejected = client.put(
            _account_url(WORK, "access"), json={"access": "read_write"}
        )
        assert rejected.status_code == 409
        for account, access in ((WORK, "read_only"), (HOME, "read_write")):
            response = client.put(
                _account_url(account, "access"), json={"access": access}
            )
            assert response.status_code == 200
            assert response.json()["access"] == access

        accounts = _accounts(client)
        assert accounts[WORK.account]["access"] == "read_only"
        assert accounts[HOME.account]["access"] == "read_write"


@pytest.mark.parametrize(
    "url",
    [
        f"{STATUS_URL}/gmail/accounts/ca_unknown/access",
        f"{STATUS_URL}/slack/accounts/{WORK.account}/access",
    ],
)
def test_access_for_an_unknown_account_is_rejected(
    store: PermissionStore, url: str
) -> None:
    _register(FakeGateway())
    with _client() as client:
        assert client.put(url, json={"access": "read_only"}).status_code == 404


def test_tool_blocks_belong_to_one_account_and_show_the_api_denial(
    store: PermissionStore,
) -> None:
    _register(
        FakeGateway(denied={(WORK.account, "GMAIL_SEND_EMAIL"): SEND_SCOPE_MISSING})
    )
    block_url = _account_url(WORK, "tools/GMAIL_FETCH_EMAILS/block")

    with _client() as client:
        assert client.put(block_url).status_code == 204
        work_tools = {
            t["slug"]: t for t in client.get(_account_url(WORK, "tools")).json()
        }
        home_tools = {
            t["slug"]: t for t in client.get(_account_url(HOME, "tools")).json()
        }
        assert work_tools["GMAIL_FETCH_EMAILS"]["blocked"] is True
        assert work_tools["GMAIL_SEND_EMAIL"] == {
            "slug": "GMAIL_SEND_EMAIL",
            "name": "Gmail_Send_Email",
            "tool_class": "write",
            "blocked": False,
            "api_denial": SEND_SCOPE_MISSING.model_dump(mode="json"),
        }
        assert not any(tool["blocked"] for tool in home_tools.values())
        assert home_tools["GMAIL_SEND_EMAIL"]["api_denial"] is None

        assert client.delete(block_url).status_code == 204
        work_tools = {
            t["slug"]: t for t in client.get(_account_url(WORK, "tools")).json()
        }
        assert work_tools["GMAIL_FETCH_EMAILS"]["blocked"] is False

        unknown = client.put(_account_url(WORK, "tools/SLACK_SEND_MESSAGE/block"))
        assert unknown.status_code == 404


def test_a_new_account_is_read_only_only_once_the_connect_flow_saves_it(
    store: PermissionStore,
) -> None:
    _register(FakeGateway())
    with _client() as client:
        assert _accounts(client)[WORK.account]["access"] == "off"
        assert _accounts(client)[WORK.account]["access"] == "off"
        apply_new_account_default(
            store=store,
            toolkit="gmail",
            account=WORK.account,
            updated_at="2026-10-18T00:00:00Z",
        )
        accounts = _accounts(client)
    assert accounts[WORK.account]["access"] == "read_only"
    assert accounts[HOME.account]["access"] == "off"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("put", "/gmail/accounts/ca_work/access"),
        ("get", "/gmail/accounts/ca_work/tools"),
        ("put", "/gmail/accounts/ca_work/tools/GMAIL_SEND_EMAIL/block"),
        ("delete", "/gmail/accounts/ca_work/tools/GMAIL_SEND_EMAIL/block"),
    ],
)
def test_another_authenticated_user_is_rejected(
    store: PermissionStore, method: str, suffix: str
) -> None:
    _register(FakeGateway())
    with _client("user-2") as client:
        response = client.request(
            method, STATUS_URL + suffix, json={"access": "read_only"}
        )
    assert response.status_code == 403

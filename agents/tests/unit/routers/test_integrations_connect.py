"""The connect API over the real Composio gateway, with Composio's REST API stubbed."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.integrations.composio import client as client_module
from pantaray_agents.local_runtime.integrations.composio import (
    connect as connect_module,
)
from pantaray_agents.local_runtime.integrations.composio.client import ComposioClient
from pantaray_agents.local_runtime.integrations.composio.gateway import (
    ComposioGateway,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.integrations.settings import PermissionStore
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.repository import (
    integration_permissions as permissions_repository,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    load_integration_account_access,
    load_integration_blocked_tools,
    set_integration_account_access,
    set_integration_tool_blocked,
)
from pantaray_agents.routers import integrations
from pantaray_agents.schema.agent.base import JSONValue

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
BASE = f"/v1/agents/users/{USER_ID}/integrations"
API_KEY = "ak_test_secret_key_value"
WORK = "ca_work"
LINK_TOKEN = "lk_secret_link_token"
SAVED_AT = "2026-10-18T00:00:00.000Z"

type Json = dict[str, JSONValue]


def _error(status: int, code: int) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": code, "status": status, "message": "says"}}
    )


@dataclass
class FakeComposio:
    """Composio's connect routes as seen on 2026-10-18; unknown routes answer 500."""

    store: PermissionStore
    statuses: dict[str, str] = field(default_factory=lambda: {WORK: "ACTIVE"})
    owners: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    session_toolkits: dict[str, list[JSONValue]] = field(default_factory=dict)
    replies: dict[tuple[str, str], httpx2.Response] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    # Each revoke and delete records whether Pantaray's setting was still saved.
    access_seen: list[tuple[str, IntegrationAccess]] = field(default_factory=list)
    tools_delay: float = 0.0
    links: int = 0

    def _account(self, account: str) -> Json:
        status = self.statuses[account]
        val: Json = {"status": status, "displayName": f"{account}@example.com"}
        if status == "INITIATED":
            val["redirectUrl"] = f"https://backend.composio.dev/s/{LINK_TOKEN}"
        return {
            "id": account,
            "user_id": self.owners.get(account, client_module.USER_ID),
            "status": status,
            "toolkit": {"slug": "gmail"},
            "state": {"val": val},
            "data": val,
            "status_reason": self.reasons.get(account),
        }

    def _link_url(self) -> str:
        return f"https://connect.composio.dev/link/{LINK_TOKEN}"

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        method, path = request.method, request.url.path.removeprefix("/api/v3.1")
        body = json.loads(request.content) if request.content else None
        probe = body == {}
        if not probe:
            self.requests.append((method, path))
        if (method, path) in self.replies:
            return self.replies[(method, path)]
        account = path.split("/")[2] if path.count("/") >= 2 else ""
        if method == "GET" and path == "/connected_accounts":
            items = [self._account(a) for a in self.statuses]
            return httpx2.Response(200, json={"items": items, "next_cursor": None})
        if method == "GET" and path == "/tools":
            await asyncio.sleep(self.tools_delay)
            tool: Json = {
                "slug": "GMAIL_FETCH_EMAILS",
                "name": "Fetch",
                "toolkit": {"slug": "gmail"},
                "input_parameters": {},
                "tags": ["readOnlyHint"],
            }
            return httpx2.Response(200, json={"items": [tool], "next_cursor": None})
        if path == "/tool_router/session" and body != {}:
            assert isinstance(body, dict)
            toolkits = body["toolkits"]
            assert isinstance(toolkits, dict)
            enabled = toolkits["enable"]
            assert isinstance(enabled, list)
            session_id = f"trs_{len(self.session_toolkits)}"
            self.session_toolkits[session_id] = enabled
            return httpx2.Response(201, json={"session_id": session_id})
        if probe:
            # A key probe: the area is allowed.
            return _error(400, 10400)
        if method == "POST" and path.endswith("/link"):
            assert isinstance(body, dict)
            if body["toolkit"] not in self.session_toolkits[path.split("/")[3]]:
                return _error(400, 4324)
            self.links += 1
            created = f"ca_new_{self.links}"
            self.statuses[created] = "INITIALIZING"
            return httpx2.Response(
                201,
                json={
                    "link_token": LINK_TOKEN,
                    "redirect_url": self._link_url(),
                    "connected_account_id": created,
                },
            )
        if account not in self.statuses:
            return _error(404, 606)
        if method == "GET":
            return httpx2.Response(200, json=self._account(account))
        if path.endswith("/revoke"):
            self.access_seen.append(("revoke", self._saved_access(account)))
            if self.statuses[account] != "ACTIVE":
                return _error(409, 623)
            self.statuses[account] = "REVOKED"
            return httpx2.Response(200, json={"revoked_tokens": ["access_token"]})
        if method == "DELETE":
            self.access_seen.append(("delete", self._saved_access(account)))
            del self.statuses[account]
            return httpx2.Response(200, json={"success": True})
        return _error(500, 0)

    def _saved_access(self, account: str) -> IntegrationAccess:
        return _access(self.store, account)

    def sent(self, method: str, suffix: str) -> list[str]:
        return [p for m, p in self.requests if m == method and p.endswith(suffix)]


def _access(store: PermissionStore, account: str) -> IntegrationAccess:
    return load_integration_account_access(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=USER_ID,
        toolkit="gmail",
        account=account,
    )


def _blocked(store: PermissionStore, account: str) -> frozenset[str]:
    return load_integration_blocked_tools(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=USER_ID,
        account=account,
    )


def _block(store: PermissionStore, account: str, slug: str) -> None:
    set_integration_tool_blocked(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=USER_ID,
        account=account,
        slug=slug,
        blocked=True,
        updated_at=SAVED_AT,
    )


def _save(store: PermissionStore, account: str, access: IntegrationAccess) -> None:
    set_integration_account_access(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=USER_ID,
        toolkit="gmail",
        account=account,
        access=access,
        updated_at=SAVED_AT,
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PermissionStore]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    monkeypatch.setattr(
        integrations, "read_local_runtime_db_config", lambda: (db_path, BUSY_TIMEOUT_MS)
    )
    yield PermissionStore(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, user_id=USER_ID
    )
    set_integration_gateway(None)


@pytest.fixture
def composio(store: PermissionStore) -> FakeComposio:
    return FakeComposio(store=store)


@pytest.fixture
def gateway(composio: FakeComposio) -> ComposioGateway:
    created = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    set_integration_gateway(created)
    # Probed and indexed up front, so no background sync runs during a test.
    asyncio.run(created.sync())
    return created


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER_ID
    return TestClient(app)


def _accounts(client: TestClient) -> dict[str, dict[str, JSONValue]]:
    body = client.get(BASE).json()
    [toolkit] = [t for t in body["toolkits"] if t["toolkit"] == "gmail"]
    return {account["account"]: account for account in toolkit["accounts"]}


def test_connect_returns_a_link_for_one_more_account(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    with _client() as client:
        before = datetime.now(UTC)
        first = client.post(f"{BASE}/gmail/connect", json={})
        after = datetime.now(UTC)
        second = client.post(f"{BASE}/gmail/connect", json={})

    assert first.status_code == 200
    body = first.json()
    assert body["redirect_url"] == composio._link_url()
    assert body["connection_id"] == "ca_new_1"
    assert second.json()["connection_id"] == "ca_new_2"
    expires_at = datetime.fromisoformat(body["expires_at"])
    # Composio's documented link lifetime; its reply carries no expiry.
    lifetime = timedelta(minutes=10)
    assert before + lifetime - timedelta(milliseconds=1) <= expires_at
    assert expires_at <= after + lifetime


def test_connecting_a_toolkit_with_no_account_yet_links_through_its_session(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    # Composio links only a toolkit the session enables (4324 otherwise).
    with _client() as client:
        response = client.post(f"{BASE}/slack/connect", json={})
    assert response.status_code == 200
    [link_path] = composio.sent("POST", "/link")
    assert "slack" in composio.session_toolkits[link_path.split("/")[3]]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/gmail/connect"),
        ("post", f"/gmail/accounts/{WORK}/reconnect"),
    ],
)
def test_a_key_that_cannot_connect_is_refused_with_the_reason(
    composio: FakeComposio, store: PermissionStore, method: str, path: str
) -> None:
    composio.replies[("POST", "/auth_configs")] = _error(403, 812)
    created = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    set_integration_gateway(created)
    asyncio.run(created.sync())

    with _client() as client:
        response = client.request(method, BASE + path, json={})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "reason": {
            "source": "connection_method",
            "capability": "connect",
            "detail": "auth_configs",
        }
    }
    assert not composio.sent("POST", "/link")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/notion_unknown/connect"),
        ("post", "/gmail/accounts/ca_unknown/reconnect"),
        ("post", f"/slack/accounts/{WORK}/reconnect"),
        ("delete", "/gmail/accounts/ca_unknown"),
        ("delete", f"/slack/accounts/{WORK}"),
        ("get", "/connections/ca_unknown"),
        ("get", "/connections/not-an-id"),
    ],
)
def test_unknown_toolkits_accounts_and_connections_are_404(
    composio: FakeComposio, gateway: ComposioGateway, method: str, path: str
) -> None:
    with _client() as client:
        response = client.request(method, BASE + path, json={})
    assert response.status_code == 404
    assert not composio.sent("POST", "/revoke")
    assert not composio.sent("DELETE", "")


def test_a_connection_of_another_composio_user_is_404(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    composio.statuses["ca_other"] = "ACTIVE"
    composio.owners["ca_other"] = "someone-else"
    with _client() as client:
        response = client.get(f"{BASE}/connections/ca_other")
    assert response.status_code == 404
    assert _access(store, "ca_other") == "off"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/gmail/connect"),
        ("post", f"/gmail/accounts/{WORK}/reconnect"),
        ("delete", f"/gmail/accounts/{WORK}"),
        ("get", f"/connections/{WORK}"),
    ],
)
def test_without_a_gateway_every_connect_route_is_404(
    store: PermissionStore, method: str, path: str
) -> None:
    with _client() as client:
        assert client.request(method, BASE + path, json={}).status_code == 404


def test_a_failed_link_is_502_without_composio_text(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.replies[("POST", "/tool_router/session/trs_0/link")] = _error(500, 0)
    with _client() as client:
        response = client.post(f"{BASE}/gmail/connect", json={})
    assert response.status_code == 502
    assert "says" not in response.text


def test_a_rejected_key_is_its_own_status_and_other_failures_stay_errors(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    # Composio's reply to a deleted key quotes part of it.
    rejected = httpx2.Response(
        401, json={"error": {"code": 801, "message": f"Invalid API key {API_KEY[:8]}"}}
    )
    composio.replies[("GET", "/connected_accounts")] = rejected
    gateway.invalidate_accounts()
    with _client() as client:
        response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {"state": "key_rejected"}

    composio.replies[("GET", "/connected_accounts")] = _error(503, 0)
    gateway.invalidate_accounts()
    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER_ID
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get(BASE).status_code == 500


@pytest.mark.parametrize(
    ("composio_status", "state"),
    [
        ("INITIALIZING", "pending"),
        ("INITIATED", "pending"),
        ("ACTIVE", "active"),
        ("FAILED", "failed"),
        ("EXPIRED", "expired"),
        ("REVOKED", "failed"),
    ],
)
def test_composio_statuses_map_to_the_connection_state(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    composio_status: str,
    state: str,
) -> None:
    composio.statuses["ca_new"] = composio_status
    with _client() as client:
        response = client.get(f"{BASE}/connections/ca_new")
    assert response.json() == {"state": state}
    # A connection this process did not link gets no setting, even active.
    assert _access(store, "ca_new") == "off"


def test_the_first_active_poll_saves_the_default_and_indexes_the_toolkit(
    composio: FakeComposio, store: PermissionStore
) -> None:
    composio.statuses = {}
    created = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    set_integration_gateway(created)
    asyncio.run(created.sync())

    with _client() as client:
        connection_id = client.post(f"{BASE}/gmail/connect", json={}).json()[
            "connection_id"
        ]
        assert client.get(f"{BASE}/connections/{connection_id}").json() == {
            "state": "pending"
        }
        assert _accounts(client) == {}
        composio.statuses[connection_id] = "ACTIVE"
        assert client.get(f"{BASE}/connections/{connection_id}").json() == {
            "state": "active"
        }
        accounts = _accounts(client)

    assert accounts[connection_id]["access"] == "read_only"
    assert accounts[connection_id]["health"] == "ok"
    assert [t.slug for t in get_integration_tool_index().tools("gmail")] == [
        "GMAIL_FETCH_EMAILS"
    ]


async def test_concurrent_first_active_polls_refresh_the_caches_once(
    composio: FakeComposio, store: PermissionStore
) -> None:
    composio.statuses = {}
    created = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    set_integration_gateway(created)
    await created.sync()
    link = await created.connections.connect("gmail", store=store)
    composio.statuses[link.connection_id] = "ACTIVE"
    # Holds the first poll inside the activation while the others arrive.
    composio.tools_delay = 0.05
    composio.requests.clear()

    states = await asyncio.gather(
        *(
            integrations.get_connection_state(
                user_id=USER_ID,
                connection_id=link.connection_id,
                resolved_user_id=USER_ID,
            )
            for _ in range(3)
        )
    )

    assert [s.state for s in states] == ["active"] * 3
    assert len(composio.sent("GET", "/tools")) == 1
    assert len(composio.sent("GET", "/connected_accounts")) == 1
    assert _access(store, link.connection_id) == "read_only"


def test_a_failed_activation_is_502_and_the_next_poll_completes_it(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    _save(store, WORK, "read_write")
    delete_old = ("DELETE", f"/connected_accounts/{WORK}")
    composio.replies[delete_old] = _error(503, 0)

    with _client() as client:
        connection_id = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        composio.statuses[connection_id] = "ACTIVE"
        poll = f"{BASE}/connections/{connection_id}"
        assert client.get(poll).status_code == 502
        # The setting moved before Composio was asked to delete the old one.
        assert WORK in composio.statuses
        assert _access(store, connection_id) == "read_write"
        del composio.replies[delete_old]
        assert client.get(poll).json() == {"state": "active"}

    assert WORK not in composio.statuses
    assert _access(store, WORK) == "off"
    assert _access(store, connection_id) == "read_write"


@pytest.mark.parametrize("saved", ["off", "read_write"])
def test_a_reconnected_account_replaces_the_old_one_with_its_setting(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    saved: IntegrationAccess,
) -> None:
    composio.statuses[WORK] = "EXPIRED"
    gateway.invalidate_accounts()
    _save(store, WORK, saved)
    _block(store, WORK, "GMAIL_FETCH_EMAILS")

    with _client() as client:
        assert _accounts(client)[WORK]["health"] == "reauthorization_required"
        response = client.post(f"{BASE}/gmail/accounts/{WORK}/reconnect", json={})
        assert response.status_code == 200
        connection_id = response.json()["connection_id"]
        poll = f"{BASE}/connections/{connection_id}"
        assert client.get(poll).json() == {"state": "pending"}
        composio.statuses[connection_id] = "ACTIVE"
        assert client.get(poll).json() == {"state": "active"}
        accounts = _accounts(client)
        tools = client.get(f"{BASE}/gmail/accounts/{connection_id}/tools").json()

    assert list(accounts) == [connection_id]
    assert accounts[connection_id]["access"] == saved
    assert [t["slug"] for t in tools if t["blocked"]] == ["GMAIL_FETCH_EMAILS"]
    # Deleted without a revoke, which could cut the new connection's grant.
    assert composio.sent("DELETE", "") == [f"/connected_accounts/{WORK}"]
    assert not composio.sent("POST", "/revoke")
    assert _access(store, WORK) == "off"


def test_an_abandoned_reconnect_leaves_the_old_account_and_hides_the_link(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    composio.statuses[WORK] = "EXPIRED"
    _save(store, WORK, "read_write")

    with _client() as client:
        connection_id = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        # Composio expires the link about 10 minutes later; nobody polls it.
        composio.statuses[connection_id] = "EXPIRED"
        composio.reasons[connection_id] = (
            "Authorization was started but not completed within 10 minutes"
        )
        gateway.invalidate_accounts()
        accounts = _accounts(client)

    assert list(accounts) == [WORK]
    assert accounts[WORK]["access"] == "read_write"


@pytest.mark.parametrize("composio_status", ["FAILED", "EXPIRED"])
def test_a_link_that_ends_without_consent_is_deleted(
    composio: FakeComposio, gateway: ComposioGateway, composio_status: str
) -> None:
    with _client() as client:
        connection_id = client.post(f"{BASE}/gmail/connect", json={}).json()[
            "connection_id"
        ]
        composio.statuses[connection_id] = composio_status
        state = client.get(f"{BASE}/connections/{connection_id}").json()

    assert state == {"state": composio_status.lower()}
    assert connection_id not in composio.statuses
    # An account this process did not link is only reported, never deleted.
    composio.statuses["ca_elsewhere"] = composio_status
    with _client() as client:
        client.get(f"{BASE}/connections/ca_elsewhere")
    assert "ca_elsewhere" in composio.statuses


def test_an_access_saved_before_the_first_poll_is_kept_with_the_old_blocks(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    _save(store, WORK, "off")
    _block(store, WORK, "GMAIL_FETCH_EMAILS")

    with _client() as client:
        connection_id = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        # The new account shows up active, and the user sets it before any poll.
        composio.statuses[connection_id] = "ACTIVE"
        gateway.invalidate_accounts()
        chosen = client.put(
            f"{BASE}/gmail/accounts/{connection_id}/access",
            json={"access": "read_only"},
        )
        assert chosen.status_code == 200
        assert client.get(f"{BASE}/connections/{connection_id}").json() == {
            "state": "active"
        }

    assert _access(store, connection_id) == "read_only"
    assert _blocked(store, connection_id) == {"GMAIL_FETCH_EMAILS"}
    assert WORK not in composio.statuses
    assert _access(store, WORK) == "off"
    assert not _blocked(store, WORK)


def test_a_failed_move_leaves_the_new_account_without_access(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save(store, WORK, "read_write")
    _block(store, WORK, "GMAIL_FETCH_EMAILS")
    save_scope = permissions_repository._save_scope

    def failing_on_blocks(
        connection: sqlite3.Connection, *, scope: str, **kwargs: str
    ) -> None:
        if scope.startswith(permissions_repository.INTEGRATION_TOOL_SCOPE_PREFIX):
            raise sqlite3.OperationalError("disk I/O error")
        save_scope(connection, scope=scope, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(permissions_repository, "_save_scope", failing_on_blocks)

    with _client() as client:
        connection_id = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        composio.statuses[connection_id] = "ACTIVE"
        with pytest.raises(sqlite3.OperationalError):
            client.get(f"{BASE}/connections/{connection_id}")

    # Rolled back whole: the new account has no access, the old keeps its own.
    assert _access(store, connection_id) == "off"
    assert _access(store, WORK) == "read_write"
    assert WORK in composio.statuses


@pytest.mark.parametrize("reconnect", [False, True])
def test_after_a_restart_an_active_link_gets_no_setting_and_replaces_nothing(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    reconnect: bool,
) -> None:
    _save(store, WORK, "off")
    _block(store, WORK, "GMAIL_FETCH_EMAILS")
    path = f"/gmail/accounts/{WORK}/reconnect" if reconnect else "/gmail/connect"
    with _client() as client:
        connection_id = client.post(BASE + path, json={}).json()["connection_id"]

    # A restart: a new gateway that never opened the link.
    restarted = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    set_integration_gateway(restarted)
    asyncio.run(restarted.sync())
    composio.statuses[connection_id] = "ACTIVE"
    with _client() as client:
        state = client.get(f"{BASE}/connections/{connection_id}").json()
        accounts = _accounts(client)

    assert state == {"state": "active"}
    assert accounts[connection_id]["access"] == "off"
    assert not _blocked(store, connection_id)
    # The old account is left for the user to disconnect.
    assert WORK in composio.statuses
    assert _blocked(store, WORK) == {"GMAIL_FETCH_EMAILS"}


def test_disconnect_revokes_then_deletes_then_clears_only_that_account(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    composio.statuses["ca_home"] = "ACTIVE"
    gateway.invalidate_accounts()
    _save(store, WORK, "read_write")
    _save(store, "ca_home", "read_only")

    with _client() as client:
        response = client.delete(f"{BASE}/gmail/accounts/{WORK}")
        accounts = _accounts(client)

    assert response.status_code == 204
    # Pantaray's setting stayed until Composio had revoked and deleted.
    assert composio.access_seen == [("revoke", "read_write"), ("delete", "read_write")]
    assert _access(store, WORK) == "off"
    assert _access(store, "ca_home") == "read_only"
    assert list(accounts) == ["ca_home"]


def test_a_failed_revoke_deletes_nothing_anywhere(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    _save(store, WORK, "read_write")
    composio.replies[("POST", f"/connected_accounts/{WORK}/revoke")] = _error(500, 0)

    with _client() as client:
        response = client.delete(f"{BASE}/gmail/accounts/{WORK}")
        accounts = _accounts(client)

    assert response.status_code == 502
    assert not composio.sent("DELETE", "")
    assert _access(store, WORK) == "read_write"
    assert list(accounts) == [WORK]


def test_a_failed_delete_keeps_the_local_setting_and_a_retry_completes(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    _save(store, WORK, "read_write")
    composio.replies[("DELETE", f"/connected_accounts/{WORK}")] = _error(500, 0)

    with _client() as client:
        assert client.delete(f"{BASE}/gmail/accounts/{WORK}").status_code == 502
        assert _access(store, WORK) == "read_write"
        del composio.replies[("DELETE", f"/connected_accounts/{WORK}")]
        # Revoked already, so Composio answers 409 and the delete goes ahead.
        assert client.delete(f"{BASE}/gmail/accounts/{WORK}").status_code == 204

    assert _access(store, WORK) == "off"
    assert WORK not in composio.statuses


def test_an_expired_account_is_deleted_without_a_revocable_grant(
    composio: FakeComposio, gateway: ComposioGateway, store: PermissionStore
) -> None:
    composio.statuses[WORK] = "EXPIRED"
    gateway.invalidate_accounts()
    _save(store, WORK, "read_only")

    with _client() as client:
        assert client.delete(f"{BASE}/gmail/accounts/{WORK}").status_code == 204

    assert WORK not in composio.statuses
    assert _access(store, WORK) == "off"


def test_links_and_link_tokens_never_reach_logs(
    composio: FakeComposio,
    gateway: ComposioGateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with _client() as client:
        connected = client.post(f"{BASE}/gmail/connect", json={})
        reconnected = client.post(f"{BASE}/gmail/accounts/{WORK}/reconnect", json={})
        # Composio's GET of a link in progress carries the link too.
        composio.statuses[reconnected.json()["connection_id"]] = "INITIATED"
        client.get(f"{BASE}/connections/{reconnected.json()['connection_id']}")
        # A reply that fails validation still holds the link.
        composio.replies[("POST", "/tool_router/session/trs_0/link")] = httpx2.Response(
            201, json={"redirect_url": composio._link_url()}
        )
        failed = client.post(f"{BASE}/gmail/connect", json={})

    assert connected.json()["redirect_url"] == composio._link_url()
    assert failed.status_code == 502
    assert LINK_TOKEN not in failed.text
    logged = caplog.text + " ".join(str(vars(record)) for record in caplog.records)
    assert caplog.records
    assert LINK_TOKEN not in logged
    assert API_KEY not in logged


def _finalized() -> None:
    for thread in threading.enumerate():
        if thread.name == "composio-finalize":
            thread.join(timeout=5)


def _refresh(client: TestClient, gateway: ComposioGateway) -> None:
    """An account read such as an Action turn's, after the cache went stale."""
    gateway.invalidate_accounts()
    assert client.get(BASE).status_code == 200
    _finalized()


def test_an_account_read_finalizes_links_nobody_polls_exactly_once(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    for name in ("apply_new_account_default", "carry_over_account_setting"):
        real = getattr(connect_module, name)

        def counted(*, _real=real, _name=name, **kwargs: object) -> None:
            calls.append(_name)
            _real(**kwargs)

        monkeypatch.setattr(connect_module, name, counted)
    _save(store, WORK, "read_write")
    _block(store, WORK, "GMAIL_FETCH_EMAILS")

    with _client() as client:
        added = client.post(f"{BASE}/gmail/connect", json={}).json()["connection_id"]
        reconnected = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        # The user finishes both in the browser; the screen polls neither.
        composio.statuses[added] = "ACTIVE"
        composio.statuses[reconnected] = "ACTIVE"
        for _ in range(3):
            _refresh(client, gateway)
        # A late poll finds the work done.
        assert client.get(f"{BASE}/connections/{added}").json() == {"state": "active"}
        accounts = _accounts(client)

    assert sorted(calls) == ["apply_new_account_default", "carry_over_account_setting"]
    assert accounts[added]["access"] == "read_only"
    assert accounts[reconnected]["access"] == "read_write"
    assert _blocked(store, reconnected) == {"GMAIL_FETCH_EMAILS"}
    assert WORK not in accounts
    assert composio.sent("DELETE", "") == [f"/connected_accounts/{WORK}"]
    assert _access(store, WORK) == "off"


def test_a_failed_finalize_keeps_the_account_list_and_is_tried_again(
    composio: FakeComposio,
    gateway: ComposioGateway,
    store: PermissionStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _save(store, WORK, "read_write")
    delete_old = ("DELETE", f"/connected_accounts/{WORK}")
    composio.replies[delete_old] = _error(503, 0)

    with _client() as client:
        connection_id = client.post(
            f"{BASE}/gmail/accounts/{WORK}/reconnect", json={}
        ).json()["connection_id"]
        composio.statuses[connection_id] = "ACTIVE"
        _refresh(client, gateway)
        assert set(_accounts(client)) == {WORK, connection_id}
        del composio.replies[delete_old]
        _refresh(client, gateway)
        accounts = _accounts(client)

    assert list(accounts) == [connection_id]
    [failure] = [r for r in caplog.records if "not finalized" in r.getMessage()]
    assert vars(failure)["account"] == connection_id
    assert LINK_TOKEN not in caplog.text

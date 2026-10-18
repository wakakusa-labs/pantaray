from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field

import httpx2
import pytest

from pantaray_agents.local_runtime.integrations import search
from pantaray_agents.local_runtime.integrations.composio import (
    gateway as gateway_module,
)
from pantaray_agents.local_runtime.integrations.composio.client import ComposioClient
from pantaray_agents.local_runtime.integrations.composio.gateway import (
    ACCOUNTS_TTL_SECONDS,
    ComposioGateway,
    install_composio_gateway,
)
from pantaray_agents.local_runtime.integrations.composio.mapping import (
    NEEDS_INPUT_DETAIL,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    ToolFailed,
    ToolSucceeded,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.schema.agent.base import JSONValue

API_KEY = "ak_test_secret_key_value"
GMAIL_ACCOUNT = "ca_gmail_1"
FULL_GMAIL = "https://mail.google.com/"

type Reply = httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


def _error(status: int, code: int, message: str = "Composio says") -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": code, "status": status, "message": message}}
    )


def _tool(
    slug: str,
    tags: list[str] | None,
    *,
    toolkit: str = "gmail",
    scope_requirements: JSONValue = None,
    scopes: list[str] | None = None,
    deprecated: bool = False,
) -> dict[str, JSONValue]:
    return {
        "slug": slug,
        "name": slug.title(),
        "description": f"{slug} description",
        "toolkit": {"slug": toolkit},
        "input_parameters": {"type": "object", "properties": {}},
        "tags": list(tags) if tags is not None else None,
        "scopes": list(scopes) if scopes is not None else None,
        "scope_requirements": scope_requirements,
        "is_deprecated": deprecated,
    }


def _any_of(*groups: list[str]) -> JSONValue:
    return {"all_of": [{"any_of": list(group)} for group in groups]}


def _account(
    account_id: str = GMAIL_ACCOUNT,
    *,
    toolkit: str = "gmail",
    status: str = "ACTIVE",
    scope: str | None = FULL_GMAIL,
    display_name: str | None = "someone@example.com",
) -> dict[str, JSONValue]:
    val: dict[str, JSONValue] = {"access_token": "REDACTED"}
    if scope is not None:
        val["scope"] = scope
    if display_name is not None:
        val["displayName"] = display_name
    return {
        "id": account_id,
        "status": status,
        "toolkit": {"slug": toolkit},
        "state": {"val": val},
        "data": val,
    }


def _page(items: list[dict[str, JSONValue]], cursor: str | None = None) -> JSONValue:
    return {"items": list(items), "next_cursor": cursor}


@dataclass
class FakeComposio:
    """Composio's REST API, routed by method and path; unknown routes answer 500."""

    accounts: list[dict[str, JSONValue]] = field(default_factory=lambda: [_account()])
    tools: dict[str, list[dict[str, JSONValue]]] = field(
        default_factory=lambda: {
            "gmail": [
                _tool(
                    "GMAIL_FETCH_EMAILS",
                    ["readOnlyHint"],
                    scope_requirements=_any_of([FULL_GMAIL, "gmail.readonly"]),
                ),
                _tool("GMAIL_SEND_EMAIL", ["createHint"]),
            ]
        }
    )
    probe_replies: dict[str, httpx2.Response] = field(default_factory=dict)
    execute_replies: list[Reply] = field(default_factory=list)
    requests: list[httpx2.Request] = field(default_factory=list)
    sessions_created: int = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v3.1")
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and path == "/connected_accounts":
            return httpx2.Response(200, json=_page(self.accounts))
        if request.method == "GET" and path == "/tools":
            toolkit = request.url.params["toolkit_slug"]
            return httpx2.Response(200, json=_page(self.tools.get(toolkit, [])))
        if body == {}:
            return self.probe_replies.get(path, _error(400, 10400))
        if path == "/tool_router/session":
            self.sessions_created += 1
            return httpx2.Response(
                201, json={"session_id": f"trs_{self.sessions_created}"}
            )
        if path.endswith("/execute"):
            reply = self.execute_replies.pop(0)
            return reply(request) if callable(reply) else reply
        return _error(500, 0)

    def paths(self, method: str, suffix: str) -> list[str]:
        return [
            r.url.path
            for r in self.requests
            # Key probes post an empty body; they are not calls.
            if r.method == method and r.url.path.endswith(suffix) and r.content != b"{}"
        ]


@pytest.fixture
def composio() -> FakeComposio:
    return FakeComposio()


@pytest.fixture
def gateway(composio: FakeComposio) -> Iterator[ComposioGateway]:
    created = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(composio.handle))
    )
    yield created
    set_integration_gateway(None)


async def _ready(gateway: ComposioGateway) -> None:
    await gateway.sync()
    await gateway.list_tools("gmail")


async def _run(gateway: ComposioGateway, slug: str = "GMAIL_FETCH_EMAILS") -> object:
    return await gateway.execute(
        slug=slug, account=GMAIL_ACCOUNT, arguments={}, tool_request_id="req-1"
    )


async def test_read_needs_read_only_hint_and_no_change_hint(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.tools["box"] = [
        _tool("BOX_LIST", ["readOnlyHint", "idempotentHint"], toolkit="box"),
        _tool("BOX_TAGGED_BOTH", ["readOnlyHint", "updateHint"], toolkit="box"),
        _tool("BOX_UNTAGGED", None, toolkit="box"),
        _tool("BOX_OLD", ["readOnlyHint"], toolkit="box", deprecated=True),
        # The toolkit table lowers these.
        _tool("BOX_ASK_QUESTION", ["readOnlyHint"], toolkit="box"),
        _tool("BOX_AUTHORIZE_USER", ["readOnlyHint"], toolkit="box"),
    ]

    tools = {tool.slug: tool.tool_class for tool in await gateway.list_tools("box")}

    assert tools == {
        "BOX_LIST": "read",
        "BOX_TAGGED_BOTH": "write",
        "BOX_UNTAGGED": "write",
        "BOX_ASK_QUESTION": "write",
    }


async def test_tool_list_follows_the_cursor() -> None:
    pages = {
        None: _page([_tool("GMAIL_A", ["readOnlyHint"])], cursor="next"),
        "next": _page([_tool("GMAIL_B", ["readOnlyHint"])]),
    }

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=pages[request.url.params.get("cursor")])

    paged = ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(handle))
    )

    assert [tool.slug for tool in await paged.list_tools("gmail")] == [
        "GMAIL_A",
        "GMAIL_B",
    ]


async def test_a_denied_area_greys_out_its_capability_only(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.probe_replies["/tool_router/session/trs_pantaray_probe/execute"] = _error(
        403, 812
    )

    await gateway.sync()

    limits = gateway.limits()
    assert (limits.can_connect, limits.can_execute) == (True, False)
    assert limits.reasons == [
        ApiDenial(
            source="connection_method",
            capability="execute",
            detail="session_tool_execution",
        )
    ]


async def test_an_inconclusive_probe_is_tried_again_at_the_next_sync(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.probe_replies["/auth_configs"] = _error(503, 0)
    await gateway.sync()
    assert gateway.limits().can_execute

    composio.probe_replies["/auth_configs"] = _error(403, 812)
    await gateway.sync()

    assert not gateway.limits().can_connect


async def test_an_invalid_key_denies_both_capabilities(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.probe_replies["/auth_configs"] = _error(401, 801)
    composio.accounts = []

    await gateway.sync()

    limits = gateway.limits()
    assert (limits.can_connect, limits.can_execute) == (False, False)


@pytest.mark.parametrize(
    ("reply", "slug", "kind"),
    [
        (_error(401, 801), "GMAIL_FETCH_EMAILS", "connection_missing"),
        (_error(402, 0), "GMAIL_FETCH_EMAILS", "usage_limit_reached"),
        (_error(403, 812), "GMAIL_FETCH_EMAILS", "permission_denied"),
        (_error(403, 0), "GMAIL_FETCH_EMAILS", "scope_missing"),
        (_error(400, 4302), "GMAIL_SEND_EMAIL", "reauthorization_required"),
        (_error(400, 4300), "GMAIL_FETCH_EMAILS", "connection_missing"),
        (_error(404, 4301), "GMAIL_FETCH_EMAILS", "invalid_arguments"),
        (_error(404, 0), "GMAIL_FETCH_EMAILS", "connection_missing"),
        (_error(422, 0), "GMAIL_FETCH_EMAILS", "reauthorization_required"),
        (_error(400, 10400), "GMAIL_SEND_EMAIL", "invalid_arguments"),
        (_error(500, 0), "GMAIL_FETCH_EMAILS", "upstream_failed"),
        (_error(502, 0), "GMAIL_SEND_EMAIL", "outcome_unknown"),
        (_error(408, 0), "GMAIL_SEND_EMAIL", "outcome_unknown"),
        # A slug the gateway never listed is treated as a write.
        (_error(500, 0), "GMAIL_UNLISTED", "outcome_unknown"),
    ],
)
async def test_composio_replies_map_to_one_kind(
    composio: FakeComposio,
    gateway: ComposioGateway,
    reply: httpx2.Response,
    slug: str,
    kind: str,
) -> None:
    await _ready(gateway)
    composio.execute_replies.append(reply)

    outcome = await _run(gateway, slug)

    assert isinstance(outcome, ToolFailed)
    assert outcome.error.kind == kind
    assert outcome.untrusted_detail is None


async def test_rate_limited_carries_retry_after(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await _ready(gateway)
    composio.execute_replies.append(
        httpx2.Response(429, headers={"retry-after": "30"}, json={})
    )

    outcome = await _run(gateway)

    assert isinstance(outcome, ToolFailed)
    assert (outcome.error.kind, outcome.error.retry_after_seconds) == (
        "rate_limited",
        30,
    )


def _tool_failed(error: str, data: JSONValue = None) -> httpx2.Response:
    return httpx2.Response(
        200, json={"data": data, "error": error, "result_type": "failed"}
    )


GOOGLE_INVALID = json.dumps(
    {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid id"}}
)


@pytest.mark.parametrize(
    ("reply", "slug", "kind", "detail"),
    [
        (
            _tool_failed("Following fields are missing", {"status_code": 400}),
            "GMAIL_SEND_EMAIL",
            "invalid_arguments",
            "Following fields are missing",
        ),
        (_tool_failed(GOOGLE_INVALID), "GMAIL_FETCH_EMAILS", "invalid_arguments", None),
        (
            _tool_failed(json.dumps({"error": {"status": "UNAUTHENTICATED"}})),
            "GMAIL_FETCH_EMAILS",
            "reauthorization_required",
            None,
        ),
        (
            _tool_failed("denied", {"status_code": 403}),
            "GMAIL_FETCH_EMAILS",
            "scope_missing",
            None,
        ),
        (
            _tool_failed("upstream exploded"),
            "GMAIL_FETCH_EMAILS",
            "upstream_failed",
            None,
        ),
        # A failure that cannot be read may have followed a write.
        (
            _tool_failed("upstream exploded"),
            "GMAIL_SEND_EMAIL",
            "outcome_unknown",
            None,
        ),
    ],
)
async def test_a_failed_tool_maps_by_the_provider_status(
    composio: FakeComposio,
    gateway: ComposioGateway,
    reply: httpx2.Response,
    slug: str,
    kind: str,
    detail: str | None,
) -> None:
    await _ready(gateway)
    composio.execute_replies.append(reply)

    outcome = await _run(gateway, slug)

    assert isinstance(outcome, ToolFailed)
    assert outcome.error.kind == kind
    if kind == "invalid_arguments":
        assert outcome.untrusted_detail == (detail or GOOGLE_INVALID)
    else:
        assert outcome.untrusted_detail is None


def _raise(exc: Exception) -> Callable[[httpx2.Request], httpx2.Response]:
    def reply(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return reply


@pytest.mark.parametrize(
    ("exc", "slug", "kind"),
    [
        (httpx2.ReadTimeout("timed out"), "GMAIL_SEND_EMAIL", "outcome_unknown"),
        (httpx2.ReadTimeout("timed out"), "GMAIL_FETCH_EMAILS", "upstream_failed"),
        (httpx2.RemoteProtocolError("gone"), "GMAIL_SEND_EMAIL", "outcome_unknown"),
        # The request never left, so a write cannot have run.
        (httpx2.ConnectError("refused"), "GMAIL_SEND_EMAIL", "upstream_failed"),
    ],
)
async def test_no_reply_maps_by_whether_a_write_may_have_run(
    composio: FakeComposio,
    gateway: ComposioGateway,
    exc: Exception,
    slug: str,
    kind: str,
) -> None:
    await _ready(gateway)
    composio.execute_replies.append(_raise(exc))

    outcome = await _run(gateway, slug)

    assert isinstance(outcome, ToolFailed)
    assert outcome.error.kind == kind


async def test_a_session_without_the_toolkit_is_renewed_once(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await _ready(gateway)
    composio.execute_replies += [
        _error(400, 4324),
        httpx2.Response(
            200,
            json={"data": {"messages": []}, "error": None, "result_type": "completed"},
        ),
    ]

    outcome = await _run(gateway, "GMAIL_SEND_EMAIL")

    assert outcome == ToolSucceeded(data={"messages": []})
    assert composio.paths("POST", "/execute") == [
        "/api/v3.1/tool_router/session/trs_1/execute",
        "/api/v3.1/tool_router/session/trs_2/execute",
    ]


async def test_a_second_refusal_after_renewal_is_not_retried(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await _ready(gateway)
    composio.execute_replies += [_error(404, 4331), _error(400, 4324)]

    outcome = await _run(gateway, "GMAIL_SEND_EMAIL")

    assert isinstance(outcome, ToolFailed)
    assert len(composio.paths("POST", "/execute")) == 2


async def test_the_session_follows_the_connected_toolkits(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await _ready(gateway)
    ok = httpx2.Response(
        200, json={"data": {}, "error": None, "result_type": "completed"}
    )
    composio.execute_replies += [ok, ok, ok]

    await _run(gateway)
    await _run(gateway)
    composio.accounts.append(_account("ca_github_1", toolkit="github", scope=None))
    gateway.invalidate_accounts()
    await _run(gateway)

    sessions = [
        json.loads(r.content)
        for r in composio.requests
        if r.url.path.endswith("/tool_router/session") and r.content != b"{}"
    ]
    assert [s["toolkits"] for s in sessions] == [
        {"enable": ["gmail"]},
        {"enable": ["github", "gmail"]},
    ]
    assert sessions[0]["multi_account"] == {
        "enable": True,
        "require_explicit_selection": True,
    }
    assert sessions[0]["workbench"] == {"enable": False}
    assert sessions[0]["search"] == {"enable": False}
    assert sessions[0]["manage_connections"] == {"enable": False}
    sent = json.loads(composio.requests[-1].content)
    assert sent["account"] == GMAIL_ACCOUNT


async def test_an_account_missing_a_scope_group_is_denied_the_tool(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.tools["gmail"] = [
        _tool(
            "GMAIL_CREATE_FILTER",
            ["createHint"],
            scope_requirements=_any_of(["gmail.settings.basic"]),
        ),
        _tool(
            "GMAIL_FETCH_EMAILS",
            ["readOnlyHint"],
            scope_requirements={
                "all_of": [
                    {"any_of": [FULL_GMAIL, "gmail.readonly"]},
                    # Needed only with some arguments: left to the service.
                    {"any_of": ["labels"], "when": {"param.x": ["True"]}},
                ]
            },
        ),
        _tool("GMAIL_LEGACY", ["readOnlyHint"], scopes=["gmail.readonly", FULL_GMAIL]),
    ]
    composio.accounts = [
        _account(),
        _account("ca_slack_1", toolkit="slack", scope="chat:write,channels:read"),
        _account("ca_no_scope", scope=None),
    ]
    composio.tools["slack"] = [
        _tool(
            "SLACK_SEND",
            ["createHint"],
            toolkit="slack",
            scope_requirements=_any_of(["chat:write"]),
        )
    ]
    await gateway.sync()
    await gateway.list_tools("gmail")
    await gateway.list_tools("slack")

    def denial(slug: str, account: str) -> str | None:
        reason = gateway.api_denied_reason(slug=slug, account=account)
        return None if reason is None else f"{reason.source}:{reason.detail}"

    assert denial("GMAIL_CREATE_FILTER", GMAIL_ACCOUNT) == (
        "account_grant:gmail.settings.basic"
    )
    assert denial("GMAIL_FETCH_EMAILS", GMAIL_ACCOUNT) is None
    assert denial("GMAIL_LEGACY", GMAIL_ACCOUNT) is None
    assert denial("SLACK_SEND", "ca_slack_1") is None
    # Composio does not say what was granted, so the service decides.
    assert denial("GMAIL_CREATE_FILTER", "ca_no_scope") is None


async def test_accounts_are_cached_until_invalidated_or_stale(
    composio: FakeComposio, gateway: ComposioGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: now[0])

    await gateway.list_accounts("gmail")
    await gateway.list_accounts("gmail")
    assert len(composio.paths("GET", "/connected_accounts")) == 1

    gateway.invalidate_accounts()
    await gateway.list_accounts("gmail")
    assert len(composio.paths("GET", "/connected_accounts")) == 2

    now[0] += ACCOUNTS_TTL_SECONDS
    await gateway.list_accounts("gmail")
    assert len(composio.paths("GET", "/connected_accounts")) == 3


async def test_an_inactive_connection_refreshes_the_accounts(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await _ready(gateway)
    composio.accounts = [_account(status="INACTIVE")]
    composio.execute_replies.append(_error(400, 4302))

    await _run(gateway)

    assert gateway.health(GMAIL_ACCOUNT) == "ok"
    await gateway.list_accounts("gmail")
    assert gateway.health(GMAIL_ACCOUNT) == "reauthorization_required"


async def test_accounts_carry_label_and_leave_out_pending_connections(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    composio.accounts = [
        _account(),
        _account("ca_pending", status="INITIALIZING"),
        _account("ca_nameless", display_name=None),
        _account("ca_other", toolkit="not_in_the_table"),
    ]

    accounts = await gateway.list_accounts("gmail")

    assert [(a.account, a.label) for a in accounts] == [
        (GMAIL_ACCOUNT, "someone@example.com"),
        ("ca_nameless", "ca_nameless"),
    ]
    assert gateway.health("ca_pending") == "not_connected"


async def test_sync_fills_only_the_registered_gateways_index(
    composio: FakeComposio, gateway: ComposioGateway
) -> None:
    await gateway.sync()
    assert get_integration_tool_index().tools("gmail") == []

    set_integration_gateway(gateway)
    gateway.invalidate_accounts()
    await gateway.sync()
    assert {t.slug for t in get_integration_tool_index().tools("gmail")} == {
        "GMAIL_FETCH_EMAILS",
        "GMAIL_SEND_EMAIL",
    }

    composio.accounts = []
    gateway.invalidate_accounts()
    await gateway.sync()
    assert get_integration_tool_index().tools("gmail") == []


async def test_the_key_never_reaches_logs_or_reprs(
    composio: FakeComposio,
    gateway: ComposioGateway,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    composio.probe_replies["/auth_configs"] = _error(
        401, 801, f"Invalid API key: {API_KEY}"
    )
    await gateway.sync()
    composio.execute_replies.append(_error(401, 801, f"Invalid API key: {API_KEY}"))

    outcome = await _run(gateway)

    records = caplog.text + " ".join(
        json.dumps(r.__dict__, default=str) for r in caplog.records
    )
    assert API_KEY not in records
    assert API_KEY not in repr(gateway) + repr(gateway._client) + repr(outcome)


def _held(
    composio: FakeComposio,
    holds: Callable[[httpx2.Request], Awaitable[object] | None],
) -> ComposioGateway:
    """A gateway whose transport waits on ``holds`` before Composio answers."""

    async def handle(request: httpx2.Request) -> httpx2.Response:
        waiting = holds(request)
        if waiting is not None:
            await waiting
        return composio.handle(request)

    return ComposioGateway(
        ComposioClient(API_KEY, transport=httpx2.MockTransport(handle))
    )


async def test_a_cleared_key_sends_nothing_a_waiting_call_had_not_sent(
    composio: FakeComposio,
) -> None:
    session_seen, release = asyncio.Event(), asyncio.Event()

    def hold_session(request: httpx2.Request) -> Awaitable[object] | None:
        creating = request.url.path.endswith("/tool_router/session")
        if not creating or request.content == b"{}":
            return None
        session_seen.set()
        return release.wait()

    held = _held(composio, hold_session)
    await held.sync()
    await held.list_tools("gmail")
    set_integration_gateway(held)
    try:
        call = asyncio.create_task(
            held.execute(
                slug="GMAIL_SEND_EMAIL",
                account=GMAIL_ACCOUNT,
                arguments={},
                tool_request_id="req-1",
            )
        )
        await session_seen.wait()
        install_composio_gateway(None)
        release.set()
        outcome = await call
    finally:
        set_integration_gateway(None)

    assert isinstance(outcome, ToolFailed)
    assert outcome.error.kind == "connection_missing"
    assert composio.paths("POST", "/execute") == []


@pytest.mark.parametrize(
    ("body", "slug", "kind"),
    [
        (
            {"result_type": "input_required", "input_requests": {"q": "Composio asks"}},
            "GMAIL_SEND_EMAIL",
            "invalid_arguments",
        ),
        (
            {"data": {"sent": True}, "error": None},
            "GMAIL_SEND_EMAIL",
            "outcome_unknown",
        ),
        ({"data": {}, "error": None}, "GMAIL_FETCH_EMAILS", "upstream_failed"),
        (
            {"data": {}, "error": None, "result_type": "queued"},
            "GMAIL_FETCH_EMAILS",
            "upstream_failed",
        ),
    ],
)
async def test_only_a_completed_result_is_a_success(
    composio: FakeComposio,
    gateway: ComposioGateway,
    body: JSONValue,
    slug: str,
    kind: str,
) -> None:
    await _ready(gateway)
    composio.execute_replies.append(httpx2.Response(200, json=body))

    outcome = await _run(gateway, slug)

    assert isinstance(outcome, ToolFailed)
    assert outcome.error.kind == kind
    assert outcome.untrusted_detail == (
        NEEDS_INPUT_DETAIL if kind == "invalid_arguments" else None
    )


async def test_concurrent_account_reads_share_one_request(
    composio: FakeComposio,
) -> None:
    held = _held(composio, lambda request: asyncio.sleep(0.05))

    await asyncio.gather(held.list_accounts("gmail"), held.list_accounts("gmail"))

    assert len(composio.paths("GET", "/connected_accounts")) == 1


async def test_a_held_tool_list_does_not_delay_action_start(
    composio: FakeComposio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search, "AVAILABILITY_TIMEOUT_SECONDS", 1.0)
    release = threading.Event()

    def hold_tools(request: httpx2.Request) -> None:
        if request.url.path.endswith("/tools"):
            # Blocks the background sync's own thread and loop.
            release.wait(timeout=10)

    held = _held(composio, hold_tools)
    set_integration_gateway(held)
    try:
        started = time.monotonic()
        available = await search.integration_tools_available()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        set_integration_gateway(None)

    assert available is True
    assert elapsed < 1.0


async def test_a_held_account_read_gives_up_at_the_action_start_deadline(
    composio: FakeComposio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search, "AVAILABILITY_TIMEOUT_SECONDS", 0.2)
    never = asyncio.Event()

    def hold_accounts(request: httpx2.Request) -> Awaitable[object] | None:
        if request.url.path.endswith("/connected_accounts"):
            return never.wait()
        return None

    set_integration_gateway(_held(composio, hold_accounts))
    try:
        started = time.monotonic()
        available = await search.integration_tools_available()
        elapsed = time.monotonic() - started
    finally:
        set_integration_gateway(None)

    assert available is False
    assert elapsed < 1.0

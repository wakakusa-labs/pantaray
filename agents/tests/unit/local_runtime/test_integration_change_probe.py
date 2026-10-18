from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.integrations import change_probe, registry
from pantaray_agents.local_runtime.integrations.change_probe import (
    EXTERNAL_CHANGES_HEADING,
    PROBE_RESULT_MAX_CHARS,
    collect_external_changes,
    substitute_window,
)
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    ChangeProbe,
    GatewayLimits,
    IntegrationAccount,
    IntegrationError,
    IntegrationHealth,
    IntegrationTool,
    ToolFailed,
    ToolkitEntry,
    ToolOutcome,
    ToolSucceeded,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    set_integration_account_access,
    set_integration_tool_blocked,
)
from pantaray_agents.schema.agent.base import JSONValue

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
RUN_ID = "run-1"
TIMESTAMP = "2026-10-18T00:00:00Z"
WINDOW_START = datetime(2026, 10, 18, 9, 15, tzinfo=UTC)
GMAIL_PROBE = "GMAIL_FETCH_EMAILS"
NEW_MAIL: JSONValue = {"messages": [{"subject": "Quote for the venue", "sender": "a"}]}


@dataclass
class _FakeGateway:
    entries: list[ToolkitEntry]
    accounts: dict[str, list[IntegrationAccount]]
    results: dict[str, ToolOutcome | Exception] = field(default_factory=dict)
    unhealthy: set[str] = field(default_factory=set)
    can_execute: bool = True
    failing_toolkits: set[str] = field(default_factory=set)
    hung: set[str] = field(default_factory=set)
    executed: Counter[str] = field(default_factory=Counter)
    arguments: list[dict[str, JSONValue]] = field(default_factory=list)

    def toolkits(self) -> list[ToolkitEntry]:
        return self.entries

    async def list_accounts(self, toolkit: str) -> list[IntegrationAccount]:
        if toolkit in self.failing_toolkits:
            raise ConnectionError("account listing failed")
        return self.accounts.get(toolkit, [])

    def health(self, account: str) -> IntegrationHealth:
        return "reauthorization_required" if account in self.unhealthy else "ok"

    def limits(self) -> GatewayLimits:
        denied = (
            []
            if self.can_execute
            else [ApiDenial(source="connection_method", capability="execute")]
        )
        return GatewayLimits(
            can_connect=True, can_execute=self.can_execute, reasons=denied
        )

    async def list_tools(self, toolkit: str) -> list[IntegrationTool]:
        raise AssertionError("the probe does not list tools")

    def api_denied_reason(self, *, slug: str, account: str) -> ApiDenial | None:
        return None

    async def execute(
        self,
        *,
        slug: str,
        account: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
    ) -> ToolOutcome:
        self.executed[account] += 1
        self.arguments.append(arguments)
        if account in self.hung:
            await asyncio.Event().wait()
        result = self.results.get(account, ToolSucceeded(data=NEW_MAIL))
        if isinstance(result, Exception):
            raise result
        return result


def _account(account: str, toolkit: str = "gmail") -> IntegrationAccount:
    return IntegrationAccount(account=account, toolkit=toolkit, label=f"{account}@x")


def _gmail(arguments: dict[str, JSONValue] | None = None) -> ToolkitEntry:
    return ToolkitEntry(
        toolkit="gmail",
        display_name="Gmail",
        probe=ChangeProbe(
            tool_slug=GMAIL_PROBE,
            arguments=arguments or {"query": "after:{window_start_epoch}"},
        ),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, BUSY_TIMEOUT_MS, load_default_migrations())
    return path


@pytest.fixture(autouse=True)
def _no_gateway_after_test() -> Iterator[None]:
    yield
    registry.set_integration_gateway(None)


def _allow(
    db_path: Path,
    account: str,
    *,
    toolkit: str = "gmail",
    access: IntegrationAccess = "read_only",
) -> None:
    set_integration_account_access(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        toolkit=toolkit,
        account=account,
        access=access,
        updated_at=TIMESTAMP,
    )


async def _collect(db_path: Path) -> str | None:
    return await collect_external_changes(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        run_id=RUN_ID,
        since=WINDOW_START,
    )


async def test_only_qualified_accounts_are_probed_once_each(db_path: Path) -> None:
    linear = ToolkitEntry(toolkit="linear", display_name="Linear")
    gateway = _FakeGateway(
        entries=[_gmail(), linear],
        accounts={
            "gmail": [
                _account("work"),
                _account("home"),
                _account("off"),
                _account("expired"),
                _account("blocked"),
            ],
            "linear": [_account("team", "linear")],
        },
        unhealthy={"expired"},
    )
    for account in ("work", "home", "expired", "blocked"):
        _allow(db_path, account)
    _allow(db_path, "off", access="off")
    _allow(db_path, "team", toolkit="linear")
    set_integration_tool_blocked(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        account="blocked",
        slug=GMAIL_PROBE,
        blocked=True,
        updated_at=TIMESTAMP,
    )
    registry.set_integration_gateway(gateway)

    block = await _collect(db_path)

    # One call per qualified account per window: the bound on the service's quota.
    assert gateway.executed == Counter({"work": 1, "home": 1})
    assert block is not None
    assert block.startswith(EXTERNAL_CHANGES_HEADING)
    assert "### Gmail (work@x)\n" in block
    assert "### Gmail (home@x)\n" in block
    assert '"subject":"Quote for the venue"' in block


async def test_no_gateway_or_no_execution_calls_nothing(db_path: Path) -> None:
    assert await _collect(db_path) is None

    gateway = _FakeGateway(
        entries=[_gmail()], accounts={"gmail": [_account("work")]}, can_execute=False
    )
    _allow(db_path, "work")
    registry.set_integration_gateway(gateway)

    assert await _collect(db_path) is None
    assert gateway.executed == Counter()


async def test_failures_and_empty_results_add_nothing_and_log_no_content(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    slack = ToolkitEntry(
        toolkit="slack",
        display_name="Slack",
        probe=ChangeProbe(tool_slug="SLACK_SEARCH", arguments={}),
    )
    gateway = _FakeGateway(
        entries=[_gmail(), slack],
        accounts={
            "gmail": [
                _account("failed"),
                _account("raised"),
                _account("empty"),
                _account("work"),
            ]
        },
        results={
            "failed": ToolFailed(error=IntegrationError(kind="rate_limited")),
            "raised": RuntimeError("upstream said: secret body text"),
            "empty": ToolSucceeded(
                data={"messages": [], "nextPageToken": None, "resultSizeEstimate": 0}
            ),
        },
        failing_toolkits={"slack"},
    )
    for account in ("failed", "raised", "empty", "work"):
        _allow(db_path, account)
    registry.set_integration_gateway(gateway)

    with caplog.at_level(logging.WARNING):
        block = await _collect(db_path)

    assert block is not None
    assert "work@x" in block
    for skipped in ("failed@x", "raised@x", "empty@x", "Slack"):
        assert skipped not in block
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 3
    assert "secret body text" not in caplog.text
    assert all("@x" not in str(record.__dict__) for record in warnings)


async def test_each_result_is_cut_to_the_limit(db_path: Path) -> None:
    gateway = _FakeGateway(
        entries=[_gmail()],
        accounts={"gmail": [_account("work")]},
        results={"work": ToolSucceeded(data={"messages": ["x" * 10_000]})},
    )
    _allow(db_path, "work")
    registry.set_integration_gateway(gateway)

    block = await _collect(db_path)

    assert block is not None
    body = block.split("### Gmail (work@x)\n", 1)[1]
    assert body.startswith('{"messages":["xxx')
    assert body.endswith("(truncated)")
    assert PROBE_RESULT_MAX_CHARS < len(body) < PROBE_RESULT_MAX_CHARS + 20


async def test_a_hung_service_ends_at_the_budget_keeping_finished_sections(
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(change_probe, "PROBE_BUDGET_SECONDS", 0.05)
    gateway = _FakeGateway(
        entries=[_gmail()],
        accounts={"gmail": [_account("work"), _account("hung"), _account("home")]},
        hung={"hung"},
    )
    for account in ("work", "hung", "home"):
        _allow(db_path, account)
    registry.set_integration_gateway(gateway)

    with caplog.at_level(logging.WARNING):
        block = await _collect(db_path)

    assert block is not None
    assert "work@x" in block
    assert "hung@x" not in block
    assert gateway.executed == Counter({"work": 1, "hung": 1})
    assert [record.getMessage() for record in caplog.records] == [
        "External change probes ran out of time"
    ]


def test_placeholders_are_filled_from_the_window_start() -> None:
    epoch = int(WINDOW_START.timestamp())

    filled = substitute_window(
        {
            "query": "in:inbox after:{window_start_epoch}",
            "since": "{window_start_epoch}",
            "filter": {"updated_after": ["{window_start_iso}"]},
            "include_payload": False,
            "max_results": 20,
        },
        WINDOW_START,
    )

    assert filled == {
        "query": f"in:inbox after:{epoch}",
        "since": epoch,
        "filter": {"updated_after": ["2026-10-18T09:15:00Z"]},
        "include_payload": False,
        "max_results": 20,
    }

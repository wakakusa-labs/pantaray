"""Suggestion research's read-only integration tools against a fake gateway.

AC-C4: one Suggestion runs integration_run_tool at most four times in all.
AC-C6: without a connected account the research tool set is today's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.agents.action_agent.runtime.test_integration_action_tools import (
    TOOLS,
    WORK,
    FakeGateway,
)
from tests.unit.local_runtime.broker_test_support import _bootstrap_runtime_db

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    integration_run,
    integration_search,
)
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.agents.suggestion_agent.react import (
    SUGGESTION_INTEGRATION_TOOL_IDS,
)
from pantaray_agents.local_runtime.integrations.contracts import ToolSucceeded
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    set_integration_account_access,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    LocalSuggestionResearchTools,
    SuggestionResearchSnapshot,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.integrations import (
    INTEGRATION_CALL_LIMIT_REACHED,
    SUGGESTION_INTEGRATION_RUN_LIMIT,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ReactToolResult,
    ToolCallEnvelope,
)

BUSY_TIMEOUT_MS = 1_000
NOW = "2026-03-23T00:00:02Z"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path, _context = _bootstrap_runtime_db(tmp_path)
    for module in (integration_run, integration_search):
        monkeypatch.setattr(
            module, "read_local_runtime_db_config", lambda: (db_path, BUSY_TIMEOUT_MS)
        )
    yield db_path
    set_integration_gateway(None)


def _connect(db_path: Path) -> FakeGateway:
    """WORK may read and, asking each time, write; the Action could send with it."""

    gateway = FakeGateway(accounts=[WORK])
    set_integration_gateway(gateway)
    # Registering a gateway starts an empty index.
    get_integration_tool_index().replace("gmail", TOOLS)
    set_integration_account_access(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        toolkit="gmail",
        account=WORK.account,
        access="read_write",
        updated_at=NOW,
    )
    return gateway


def _research_tools(db_path: Path) -> LocalSuggestionResearchTools:
    return LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=SuggestionResearchSnapshot(
            folders=(),
            memory_revisions={},
            memory_folders=(),
            stable_memory=SuggestionStableMemoryContext("", False, False),
            commands_allowed=False,
            read_access_scope="workspace",
        ),
        activity_start=None,
    )


async def _lens_run(tools: LocalSuggestionResearchTools) -> ReactToolRegistry:
    """The tools one lens run of the Suggestion builds when it starts."""

    return ReactToolRegistry(
        await tools.build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    )


async def _call(
    registry: ReactToolRegistry, name: str, args: dict[str, JSONValue]
) -> ReactToolResult:
    return await registry.execute(
        ReactToolCall(
            tool_name=name,
            tool_args=args,
            tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=args),
        ),
        1,
    )


async def _run(registry: ReactToolRegistry, slug: str) -> ReactToolResult:
    return await _call(
        registry,
        "integration_run_tool",
        {"slug": slug, "account": None, "arguments": json.dumps({})},
    )


async def _names(tools: LocalSuggestionResearchTools) -> set[str]:
    definitions = await tools.build_tool_definitions(
        user_id="user-1", run_id="suggestion-1"
    )
    return {definition.name for definition in definitions}


async def test_the_tool_set_changes_only_with_a_connected_account(
    db_path: Path,
) -> None:
    without = await _names(_research_tools(db_path))
    assert without.isdisjoint(SUGGESTION_INTEGRATION_TOOL_IDS)

    set_integration_gateway(FakeGateway(accounts=[]))
    assert await _names(_research_tools(db_path)) == without

    _connect(db_path)
    assert await _names(_research_tools(db_path)) == {
        *without,
        *SUGGESTION_INTEGRATION_TOOL_IDS,
    }


async def test_a_write_tool_is_never_offered_and_never_runs(db_path: Path) -> None:
    gateway = _connect(db_path)
    registry = await _lens_run(_research_tools(db_path))

    found = await _call(registry, "integration_search_tools", {"query": "email"})
    assert found.status == "success" and isinstance(found.output, dict)
    assert [tool["slug"] for tool in found.output["tools"]] == [  # type: ignore[index, union-attr]
        "GMAIL_FETCH_EMAILS"
    ]

    sent = await _run(registry, "GMAIL_SEND_EMAIL")
    assert sent.status == "error" and isinstance(sent.output, dict)
    assert sent.output["error_code"] == "permission_denied"
    assert gateway.executed == []

    read = await _run(registry, "GMAIL_FETCH_EMAILS")
    assert read.output == {
        "source": "gmail",
        "untrusted": True,
        "data": {"id": "msg-1"},
    }
    assert gateway.executed == [("GMAIL_FETCH_EMAILS", WORK.account, {})]


async def test_the_fifth_run_across_lens_runs_never_reaches_the_gateway(
    db_path: Path,
) -> None:
    gateway = _connect(db_path)
    suggestion = _research_tools(db_path)
    lenses = [await _lens_run(suggestion) for _ in range(3)]

    # Any call counts, a refused one too: each may cost the user's quota.
    for registry, slug in zip(
        (lenses[0], lenses[0], lenses[1], lenses[2]),
        (
            "GMAIL_FETCH_EMAILS",
            "GMAIL_SEND_EMAIL",
            "GMAIL_LIST_LABELS",
            "GMAIL_FETCH_EMAILS",
        ),
        strict=True,
    ):
        await _run(registry, slug)
    assert len(gateway.executed) == SUGGESTION_INTEGRATION_RUN_LIMIT - 1

    refused = await _run(lenses[1], "GMAIL_FETCH_EMAILS")
    assert refused.status == "error" and isinstance(refused.output, dict)
    assert refused.output["error_code"] == INTEGRATION_CALL_LIMIT_REACHED
    assert len(gateway.executed) == SUGGESTION_INTEGRATION_RUN_LIMIT - 1

    # The next Suggestion starts with its own budget.
    fresh = await _run(await _lens_run(_research_tools(db_path)), "GMAIL_LIST_LABELS")
    assert fresh.status == "success"


async def test_a_long_result_is_saved_where_the_runs_read_reads_it(
    db_path: Path,
) -> None:
    gateway = _connect(db_path)
    long_body = "quarterly numbers " * 2_000
    gateway.outcome = ToolSucceeded(data={"body": long_body})
    registry = await _lens_run(_research_tools(db_path))

    result = await _run(registry, "GMAIL_FETCH_EMAILS")

    assert result.status == "success"
    assert long_body not in json.dumps(result.output)
    path = _saved_path(result.output)
    read = await _call(registry, "read", {"path": path})
    assert read.status == "success" and "quarterly numbers" in json.dumps(read.output)


def _saved_path(output: JSONValue) -> str:
    """The path storage leaves where it moved text out."""

    if isinstance(output, dict):
        if isinstance(output.get("path"), str) and "storage" in output:
            return str(output["path"])
        for value in output.values():
            if found := _saved_path(value):
                return found
    return ""

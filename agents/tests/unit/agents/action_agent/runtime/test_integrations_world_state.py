"""The Executing head's integrations section (AC-C8).

A permission changed between turns reaches the next turn context exactly once;
unchanged permissions send nothing, and an Action without integration tools
sends the head it sent before the section existed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.agents.action_agent.runtime.test_integration_action_tools import (
    HOME,
    TOOLS,
    WORK,
    FakeGateway,
)
from tests.unit.agents.test_action_conversation_projection import (
    _RUN_1,
    _executing_agent,
    _state,
    _think_once,
    _user,
)
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm import (
    integrations_state,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    integration_search,
)
from pantaray_agents.conversation.prefix import TURN_CONTEXT_HEADING
from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
)
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_tool_index,
    set_integration_gateway,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    set_integration_account_access,
    set_integration_tool_blocked,
)

BUSY_TIMEOUT_MS = 1_000
NOW = "2026-03-23T00:00:00Z"
_WORK_LINE = (
    "- Gmail, work@example.com (account `acct-work`): "
    "read and write; writes ask every time."
)
_HOME_LINE = "- Gmail, home@example.com (account `acct-home`): read only."


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    for module in (integration_search, integrations_state):
        monkeypatch.setattr(
            module, "read_local_runtime_db_config", lambda: (db_path, BUSY_TIMEOUT_MS)
        )
    gateway = FakeGateway(accounts=[WORK, HOME])
    set_integration_gateway(gateway)
    get_integration_tool_index().replace("gmail", TOOLS)
    state = _state([_user(1)])
    state["context"].update(_RUN_1)
    state["context"]["integration_tools_exposed"] = True
    namespace = SimpleNamespace(db_path=db_path, gateway=gateway, state=state)
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


def _block(env: SimpleNamespace, account: IntegrationAccount, slug: str) -> None:
    set_integration_tool_blocked(
        db_path=env.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        account=account.account,
        slug=slug,
        blocked=True,
        updated_at=NOW,
    )


def _render(env: SimpleNamespace) -> str:
    return asyncio.run(integrations_state.render_integrations_state(env.state))


def _section(*lines: str) -> str:
    return "\n\n## Integrations\nThe user's connected app accounts:\n" + "\n".join(
        lines
    )


def test_each_account_shows_its_effective_access_in_a_stable_order(
    env: SimpleNamespace,
) -> None:
    # Listed by label, not in the order the gateway returned them.
    assert _render(env) == _section(_HOME_LINE, _WORK_LINE)


def test_access_the_api_narrows_says_why(env: SimpleNamespace) -> None:
    grant = ApiDenial(source="account_grant", capability="tool", detail="gmail.send")
    env.gateway.denied[("acct-work", "GMAIL_SEND_EMAIL")] = grant

    assert _render(env) == _section(
        _HOME_LINE,
        "- Gmail, work@example.com (account `acct-work`): read only.",
        "  Writes are unavailable: the account's connection does not grant them.",
    )
    # The external identifier stays out of the prompt.
    assert "gmail.send" not in _render(env)

    env.gateway.limits_value = GatewayLimits(
        can_connect=True,
        can_execute=False,
        reasons=[ApiDenial(source="connection_method", capability="execute")],
    )
    assert _render(env) == _section(
        "- Gmail, home@example.com (account `acct-home`): off.",
        "  Reads are unavailable: the connection method cannot run tools now.",
        "- Gmail, work@example.com (account `acct-work`): off.",
        "  Reads and writes are unavailable: the connection method cannot run tools now.",
    )


def test_only_tool_blocks_the_account_setting_does_not_already_imply_are_listed(
    env: SimpleNamespace,
) -> None:
    _block(env, HOME, "GMAIL_LIST_LABELS")
    # A read-only account already refuses every write.
    _block(env, HOME, "GMAIL_SEND_EMAIL")
    _block(env, WORK, "GMAIL_SEND_EMAIL")

    assert _render(env) == _section(
        _HOME_LINE,
        "  Blocked tools: GMAIL_LIST_LABELS.",
        "- Gmail, work@example.com (account `acct-work`): read only.",
        "  Blocked tools: GMAIL_SEND_EMAIL.",
    )


def test_a_gateway_failure_renders_the_fixed_line(env: SimpleNamespace) -> None:
    env.gateway.accounts_error = RuntimeError("token=secret")

    assert _render(env) == integrations_state.INTEGRATIONS_UNAVAILABLE
    assert "secret" not in _render(env)


def test_a_permission_change_sends_one_update_and_no_change_sends_none(
    env: SimpleNamespace,
) -> None:
    head, first = _think_once(env.state, think=2, call_id="c2")
    _, unchanged = _think_once(env.state, think=3, call_id="c3")
    _set_access(env, HOME, "off")
    later, changed = _think_once(env.state, think=4, call_id="c4")
    _, after = _think_once(env.state, think=5, call_id="c5")

    assert _section(_HOME_LINE, _WORK_LINE) in head
    assert later == head
    assert first.turn_context is None and unchanged.turn_context is None
    assert changed.turn_context == (
        TURN_CONTEXT_HEADING
        + "## Integrations Update\n"
        + "These connected accounts and permissions replace the earlier ones."
        + _section("- Gmail, home@example.com (account `acct-home`): off.", _WORK_LINE)
    )
    # Sent once: the next turn reads it off the row that carried it.
    assert after.turn_context is None


def test_disconnecting_every_account_withdraws_the_section_once(
    env: SimpleNamespace,
) -> None:
    _think_once(env.state, think=2, call_id="c2")
    env.gateway.accounts = []
    _, removed = _think_once(env.state, think=3, call_id="c3")
    _, after = _think_once(env.state, think=4, call_id="c4")

    assert removed.turn_context == (
        TURN_CONTEXT_HEADING
        + "## Integrations Update\nNo app account is connected any more."
    )
    assert after.turn_context is None


def test_an_action_without_integration_tools_keeps_the_head_unchanged(
    env: SimpleNamespace,
) -> None:
    env.state["context"]["integration_tools_exposed"] = False
    head, _ = _think_once(env.state, think=2, call_id="c2")
    _set_access(env, HOME, "off")
    _, prepared = _think_once(env.state, think=3, call_id="c3")

    fields: dict[str, Any] = env.state["context"]["executing_head_fields"]
    template = _executing_agent().executing_prompt.partition("{action_history}")[0]
    assert fields["integrations_state"] == ""
    # The head is byte for byte the one rendered without the section's placeholder.
    assert head == template.replace("{integrations_state}", "").format(**fields)
    assert prepared.turn_context is None

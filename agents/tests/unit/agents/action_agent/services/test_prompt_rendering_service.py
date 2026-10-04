from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.services.prompt_rendering_service import (
    PromptRenderingDeps,
    PromptRenderingService,
)
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)


def test_workspace_path_contract_uses_workspace_read_scope_by_default() -> None:
    service = _build_service()
    state = _state_with_workspace_contract()

    rendered = service.render_workspace_path_contract(state)

    assert rendered.startswith("# Workspace Path Rules")
    assert (
        "Read/search paths (`read`, `list`, `glob`, `grep`): use Workspace Roots below"
        in rendered
    )
    assert (
        "Edit/command paths (`apply_patch`, `bash.cwd`, `run_python.cwd`): use Workspace Roots below"
        in rendered
    )
    assert "Use these virtual paths for apply_patch" not in rendered
    assert "TOOL DEFINITIONS" not in rendered


def test_workspace_path_contract_uses_full_access_read_scope() -> None:
    service = _build_service()
    state = _state_with_workspace_contract(
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
    )

    rendered = service.render_workspace_path_contract(state)

    assert (
        "Read/search paths (`read`, `list`, `glob`, `grep`): any local filesystem path is allowed."
        in rendered
    )
    assert (
        "Edit/command paths (`apply_patch`, `bash.cwd`, `run_python.cwd`): use Workspace Roots below"
        in rendered
    )
    assert (
        "An `apply_patch` path, `bash.cwd`, or `run_python.cwd` outside Workspace Roots waits for the user "
        "to approve that one call" in rendered
    )


def test_workspace_path_contract_ignores_workspace_context_prompt_text() -> None:
    service = _build_service()
    state = _state_with_workspace_contract(
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        workspace_context_prompt="Read/search access: full local filesystem",
    )

    rendered = service.render_workspace_path_contract(state)

    assert (
        "Read/search paths (`read`, `list`, `glob`, `grep`): use Workspace Roots below"
        in rendered
    )
    assert "any local filesystem path is allowed" not in rendered


def test_workspace_context_prompt_is_separate_from_tool_path_contract() -> None:
    service = _build_service()
    state = _state_with_workspace_contract()

    rendered = service.render_workspace_context_prompt(state)

    assert "Organizations:" in rendered
    assert "Use these virtual paths for apply_patch" not in rendered


def test_workspace_context_rules_are_shared_prompt_fragment() -> None:
    service = _build_service()

    rendered = service.render_workspace_context_rules()

    assert "Use Workspace Context only as optional context" in rendered
    assert "Do not assume Workspace Context is complete" in rendered


def test_target_context_renders_scope_guard_when_present() -> None:
    service = _build_service()
    state = _state_with_workspace_contract()
    state["context"]["target_context"] = {
        "organization_name": "Wakakusa",
        "project_name": "Pantaray",
    }

    rendered = service.render_target_context(state)

    assert "Organization: Wakakusa" in rendered
    assert "Project: Pantaray" in rendered
    assert "Treat Target Context as the execution scope." in rendered
    assert "other registered projects" in rendered


def test_memory_context_model_describes_stock_flow_and_anchor_search() -> None:
    service = _build_service()

    rendered = service.render_memory_context_model()

    assert "Stock knowledge" in rendered
    assert "Flow knowledge" in rendered
    assert "Agent work records" in rendered
    assert "Search by shared anchors" in rendered
    assert "Reconcile stock and flow evidence" in rendered


def _build_service() -> PromptRenderingService:
    return PromptRenderingService(
        PromptRenderingDeps(formatter=cast(object, SimpleNamespace()))
    )


def _state_with_workspace_contract(
    *,
    read_access_scope: str = READ_ACCESS_SCOPE_WORKSPACE,
    workspace_context_prompt: str = (
        "Organizations:\n"
        "The user may have registered organizations, projects, and local folders."
    ),
) -> ActionAgentState:
    return cast(
        ActionAgentState,
        {
            "context": {
                "workspace_root_catalog": (
                    "# Workspace Roots\n"
                    "- `.`: current execution workspace\n"
                    "- `/Users/example/repo`: Repo"
                ),
                "read_access_scope": read_access_scope,
                "workspace_context_prompt": workspace_context_prompt,
            }
        },
    )

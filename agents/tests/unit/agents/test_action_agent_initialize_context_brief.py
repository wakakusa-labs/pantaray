from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import build_action_request
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.agents_md import (
    PANTARAY_DEFAULT_AGENTS_MD,
    load_pantaray_agents_md,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.initial import (
    initialize_context,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    create_workspace_folder,
    create_workspace_organization,
    create_workspace_project,
    update_read_access_scope,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.repositories.action_runtime_resume_contract import (
    ActionResumeUserStep,
)
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.utils.memory_source_policy import MEMORY_SOURCE_ORDER
from pantaray_agents.utils.prompt_loader import PromptConfig


@pytest.mark.asyncio
async def test_initialize_context_uses_profile_briefs_for_action_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ActionAgentの初期文脈は、長期insight/factsをbriefで渡すこと。

    - long_term_insight_data / structured_data の全文を初期プロンプトに入れない
    - 詳細は memory_search（Storage検索）で取得する前提
    """
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)

    # long-term insight（brief + full）
    await repo.save_insight(
        {
            "insight_id": "ins-1",
            "user_id": "user-1",
            "insight_profile_brief": "BRIEF-LT",
            "long_term_insight_data": "FULL-LT",
            "short_term_insight_data": "short-1",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )

    # structured facts（brief + full）
    await repo.save_structured_fact(
        {
            "fact_id": "fact-1",
            "user_id": "user-1",
            "facts_profile_brief": "BRIEF-FACTS",
            "structured_data": "FULL-FACTS",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    await _save_profile_artifact_projection(repo)

    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]
    ctx = updated["context"]

    assert "BRIEF-LT" in ctx["insight_data"]
    assert "FULL-LT" not in ctx["insight_data"]
    # Recent short-term insights are looked up with tools, not shown up front.
    assert "short-1" not in ctx["insight_data"]
    assert ctx["structured_fact_data"] == "BRIEF-FACTS"
    assert "FULL-FACTS" not in ctx["structured_fact_data"]
    assert ctx["request_summary"] == "SUM"
    assert ctx["target_context"] == {
        "organization_name": "Wakakusa",
        "project_name": "Pantaray",
    }
    coverage = ctx["memory_source_coverage"]
    assert isinstance(coverage["evaluated_at"], str)
    assert len(coverage["slots"]) == len(MEMORY_SOURCE_ORDER)
    assert {slot["source"] for slot in coverage["slots"]} == set(MEMORY_SOURCE_ORDER)


@pytest.mark.asyncio
async def test_initialize_context_enters_react_without_planning_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新規 Action は mandatory planning を経ず executing の ReAct から始まる。"""

    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]

    assert updated["phase"] == "executing"
    # executing THINK が必須とする実行モードは init で確定している。
    assert "use_goal_workers" not in updated["context"]


@pytest.mark.asyncio
async def test_initialize_context_projects_persisted_user_message_as_first_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]

    user_history = updated["history_by_scope"]["S"]
    assert len(user_history) == 1
    assert user_history[0]["step_id"] == "step:message:action-1"
    assert user_history[0]["step_number"] == 1
    assert user_history[0]["short_step_id"] == "S-1-USER"
    assert user_history[0]["step_type"] == "user_request"
    assert user_history[0]["phase"] == "init"
    assert user_history[0]["summary"] == ""
    assert user_history[0]["user_request_text"] == (
        "SUG\n\nSuggestion metadata:\n"
        "- Suggestion: sug-1\n"
        "- Suggestion summary: SUM\n"
        "- Organization: Wakakusa\n"
        "- Project: Pantaray\n"
        "- Approved at: 2026-03-22T00:00:01Z"
    )
    assert "thinking" not in user_history[0]
    assert updated["step"] == 2
    assert updated["context"]["local_step_counters"] == {"S": 1}
    assert updated["steps_taken"] == 0
    assert updated["llm_steps_taken"] == 0
    assert updated["tool_steps_taken"] == 0

    assert repo.data.get("action_steps", []) == []


@pytest.mark.asyncio
async def test_initialize_context_projects_followup_to_restored_history_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    state = _state_with_execution_context(execution_context)
    prior_message = ActionUserMessageInput(
        message_id="message-1",
        content="Prior suggestion",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id="sug-1",
            approved_at="2026-03-22T00:30:00Z",
            summary="Prior summary",
            organization_name="Prior org",
            project_name="Prior project",
        ),
    )
    prior_step = ActionResumeUserStep(
        step_id="step-1",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        message=prior_message,
        created_at="2026-03-22T00:30:00Z",
    )
    first = project_persisted_user_request_step(
        state,
        step_id=prior_step.step_id,
        step_number=prior_step.step_number,
        local_step_number=prior_step.local_step_number,
        short_step_id=prior_step.short_step_id,
        request_text=render_action_user_request_text(prior_message),
        occurred_at=prior_step.created_at,
        history_phase="init",
    )
    coverage_reader = AsyncMock(wraps=repo.get_memory_source_coverage_snapshot)
    repo.get_memory_source_coverage_snapshot = coverage_reader  # type: ignore[method-assign]
    memory_reader = AsyncMock(wraps=repo.get_initial_memory_context)
    repo.get_initial_memory_context = memory_reader  # type: ignore[method-assign]
    first["context"]["insight_data"] = "insight read when the Action started"
    request = build_action_request(
        action_id="action-1",
        suggestion_id="sug-1",
        user_id="user-1",
        content="Follow up",
        message_id="message-2",
        user_step_id="step-2",
        user_step_number=2,
        user_step_local_step_number=2,
        user_step_created_at="2026-03-22T01:00:00Z",
    )
    updated = await initialize_context(  # type: ignore[arg-type]
        agent,
        first,
        _runtime(agent, request=request, intervening_user_step=prior_step),
    )

    history = updated["history_by_scope"]["S"]
    assert [entry["step_id"] for entry in history] == [
        "step-1",
        "step-2",
    ]
    assert history[-1]["short_step_id"] == "S-2-USER"
    assert history[-1]["user_request_text"] == "Follow up"
    assert updated["step"] == 3
    assert updated["context"]["request_summary"] == "Prior summary"
    assert updated["context"]["target_context"] == {
        "organization_name": "Prior org",
        "project_name": "Prior project",
    }
    # Memory and its source coverage are read once per Action; a follow-up
    # keeps what the head shows.
    coverage_reader.assert_not_awaited()
    memory_reader.assert_not_awaited()
    assert updated["context"]["insight_data"] == "insight read when the Action started"
    assert repo.data.get("action_steps", []) == []

    replay = await initialize_context(  # type: ignore[arg-type]
        agent,
        updated,
        _runtime(agent, request=request, intervening_user_step=prior_step),
    )
    assert [entry["step_id"] for entry in replay["history_by_scope"]["S"]] == [
        "step-1",
        "step-2",
    ]


@pytest.mark.asyncio
async def test_initialize_context_supports_action_without_suggestion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    state = _state_with_execution_context(execution_context, suggestion_id=None)
    request = build_action_request(
        action_id="action-1",
        suggestion_id=None,
        user_id="user-1",
        content="Standalone request",
    )

    updated = await initialize_context(  # type: ignore[arg-type]
        agent,
        state,
        _runtime(agent, request=request),
    )

    assert updated["suggestion_id"] is None
    assert updated["context"]["request_summary"] is None
    assert updated["context"]["target_context"] == {
        "organization_name": None,
        "project_name": None,
    }
    assert updated["history_by_scope"]["S"][0]["user_request_text"] == (
        "Standalone request"
    )


@pytest.mark.asyncio
async def test_initialize_context_renders_workspace_prompt_from_session_read_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_context = _bootstrap_execution_context(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        read_access_scope=READ_ACCESS_SCOPE_FULL_ACCESS,
        next_read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]
    ctx = updated["context"]

    assert ctx["read_access_scope"] == READ_ACCESS_SCOPE_FULL_ACCESS
    assert (
        "Read/search access: full local filesystem" in ctx["workspace_context_prompt"]
    )
    assert (
        "Read/search access: workspace roots only"
        not in ctx["workspace_context_prompt"]
    )


@pytest.mark.asyncio
async def test_initialize_context_uses_manifest_roots_not_current_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    execution_context, db_path = _bootstrap_execution_context_with_db(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        workspace_folders=((repo_a, "Repo A"),),
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_b,
        display_name="Repo B",
        organization_ids=(),
        project_ids=(),
        now="2026-03-24T00:00:00Z",
    )
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]
    ctx = updated["context"]

    assert str(repo_a.resolve()) in ctx["workspace_root_catalog"]
    assert "Repo A" in ctx["workspace_context_prompt"]
    assert str(repo_b.resolve()) not in ctx["workspace_root_catalog"]
    assert "Repo B" not in ctx["workspace_context_prompt"]


@pytest.mark.asyncio
async def test_initialize_context_uses_manifest_root_context_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo-a"
    repo_path.mkdir()
    db_path = _bootstrap_runtime_db(tmp_path=tmp_path, monkeypatch=monkeypatch)
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org A",
        now="2026-03-23T00:00:00Z",
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project A",
        organization_ids=(organization.organization_id,),
        now="2026-03-23T00:00:00Z",
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_path,
        display_name="Repo A",
        organization_ids=(organization.organization_id,),
        project_ids=(project.project_id,),
        now="2026-03-23T00:00:00Z",
    )
    execution_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=datetime.now(UTC).isoformat(),
        allowed_tool_ids=("read",),
    )
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]
    ctx = updated["context"]

    assert (
        f"- Repo A: {repo_path.resolve()} (projects: Project A; organizations: Org A)"
    ) in ctx["workspace_context_prompt"]
    assert "Organizations:" in ctx["workspace_context_prompt"]
    assert "- Org A" in ctx["workspace_context_prompt"]
    assert "Projects:" in ctx["workspace_context_prompt"]
    assert "- Project A (organizations: Org A)" in ctx["workspace_context_prompt"]


@pytest.mark.asyncio
async def test_initialize_context_uses_workspace_context_snapshot_not_current_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo-a"
    repo_path.mkdir()
    db_path = _bootstrap_runtime_db(tmp_path=tmp_path, monkeypatch=monkeypatch)
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org A",
        now="2026-03-23T00:00:00Z",
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project A",
        organization_ids=(organization.organization_id,),
        now="2026-03-23T00:00:00Z",
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_path,
        display_name="Repo A",
        organization_ids=(organization.organization_id,),
        project_ids=(project.project_id,),
        now="2026-03-23T00:00:00Z",
    )
    execution_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=datetime.now(UTC).isoformat(),
        allowed_tool_ids=("read",),
    )
    create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org B",
        now="2026-03-24T00:00:00Z",
    )
    create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project B",
        organization_ids=(),
        now="2026-03-24T00:00:00Z",
    )
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(execution_context)

    updated = await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]
    ctx = updated["context"]

    assert "- Org A" in ctx["workspace_context_prompt"]
    assert "- Project A (organizations: Org A)" in ctx["workspace_context_prompt"]
    assert "Org B" not in ctx["workspace_context_prompt"]
    assert "Project B" not in ctx["workspace_context_prompt"]


@pytest.mark.asyncio
async def test_initialize_context_rejects_manifest_without_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_context, db_path = _bootstrap_execution_context_with_db(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM workspace_manifest_roots WHERE manifest_id = ?",
            (execution_context.manifest_id,),
        )
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(execution_context)

    with pytest.raises(RuntimeError, match="Action workspace manifest has no roots"):
        await initialize_context(agent, state, _runtime(agent))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_initialize_context_rejects_missing_execution_context() -> None:
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)

    with pytest.raises(RuntimeError, match="Action execution context is required"):
        await initialize_context(agent, {"context": {}}, _runtime(agent))  # type: ignore[arg-type]


def _build_agent(repo: MockActionAgentRepository) -> ActionAgent:
    def _fake_load_config(prompt_name: str) -> PromptConfig:
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        return ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=repo,
        )


async def _save_suggestion(repo: MockActionAgentRepository) -> None:
    await repo.save_suggestion(
        {
            "suggestion_id": "sug-1",
            "user_id": "user-1",
            "answer": "SUG",
            "thinking": "THINK",
            "prompt_text": "PROMPT",
            "response_text": "RAW_RESPONSE",
            "accepted_at": "2026-03-22T00:00:01Z",
            "created_at": "2026-03-22T00:00:00Z",
            "updated_at": "2026-03-22T00:00:01Z",
            "request_summary": "SUM",
            "target_context_json": {
                "organization_name": "Wakakusa",
                "project_name": "Pantaray",
            },
        }
    )
    await repo.save_data(
        "actions",
        {
            "action_id": "action-1",
            "user_id": "user-1",
            "suggestion_id": "sug-1",
            "status": "processing",
            "final_output": "",
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "created_at": "2026-03-22T00:00:01Z",
            "updated_at": "2026-03-22T00:00:01Z",
        },
    )


def _runtime(
    agent: ActionAgent,
    *,
    request: ActionAgentRequest | None = None,
    intervening_user_step: ActionResumeUserStep | None = None,
) -> object:
    return SimpleNamespace(
        request=request
        or build_action_request(
            action_id="action-1",
            suggestion_id="sug-1",
            user_id="user-1",
            content="SUG",
            user_step_created_at="2026-03-22T00:00:01Z",
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id="sug-1",
                approved_at="2026-03-22T00:00:01Z",
                summary="SUM",
                organization_name="Wakakusa",
                project_name="Pantaray",
            ),
        ),
        state_config={
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "max_parallel_memory_queries": 2,
        },
        intervening_user_step=intervening_user_step,
        services=agent._runtime_services,  # noqa: SLF001
    )


async def _save_profile_artifact_projection(
    repo: MockActionAgentRepository,
) -> None:
    now = datetime.now(UTC).isoformat()
    for source_type, record_id, artifact_id, relative_path, digest in (
        ("long_term_insight", "ins-1", "artifact-ins-1", "index.md", "a" * 64),
        ("facts", "fact-1", "artifact-fact-1", "facts.md", "b" * 64),
    ):
        await repo.save_data(
            "memory_artifacts",
            {
                "artifact_id": artifact_id,
                "user_id": "user-1",
                "source_type": source_type,
                "source_record_id": record_id,
                "root_path": f"artifacts/{source_type}/{artifact_id}",
                "logical_updated_at": now,
            },
        )
        await repo.save_data(
            "memory_artifact_files",
            {
                "file_id": f"{artifact_id}-file",
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "sha256": digest,
                "byte_size": 1,
                "mime_type": "text/markdown",
            },
        )


def _bootstrap_execution_context(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: str = READ_ACCESS_SCOPE_WORKSPACE,
    next_read_access_scope: str | None = None,
    workspace_folders: tuple[tuple[Path, str], ...] = (),
) -> ActionExecutionContext:
    execution_context, _db_path = _bootstrap_execution_context_with_db(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        read_access_scope=read_access_scope,
        next_read_access_scope=next_read_access_scope,
        workspace_folders=workspace_folders,
    )
    return execution_context


def _bootstrap_execution_context_with_db(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: str = READ_ACCESS_SCOPE_WORKSPACE,
    next_read_access_scope: str | None = None,
    workspace_folders: tuple[tuple[Path, str], ...] = (),
) -> tuple[ActionExecutionContext, Path]:
    db_path = _bootstrap_runtime_db(tmp_path=tmp_path, monkeypatch=monkeypatch)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        read_access_scope=read_access_scope,
        now=datetime.now(UTC).isoformat(),
    )
    for folder_path, display_name in workspace_folders:
        create_workspace_folder(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            real_path=folder_path,
            display_name=display_name,
            organization_ids=(),
            project_ids=(),
            now=datetime.now(UTC).isoformat(),
        )
    execution_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=datetime.now(UTC).isoformat(),
        allowed_tool_ids=("read",),
    )
    if next_read_access_scope is not None:
        update_read_access_scope(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            read_access_scope=next_read_access_scope,
            now=datetime.now(UTC).isoformat(),
        )
    return execution_context, db_path


def _bootstrap_runtime_db(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    return db_path


def _state_with_execution_context(
    execution_context: ActionExecutionContext,
    *,
    suggestion_id: str | None = "sug-1",
) -> ActionAgentState:
    return create_initial_state(
        user_id="user-1",
        suggestion_id=suggestion_id,
        action_id="action-1",
        started_at="2026-03-22T00:00:01Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
        manifest_id=execution_context.manifest_id,
        execution_session_id=execution_context.execution_session_id,
        execution_network_policy=execution_context.network_policy,
        action_temp_dir=str(execution_context.action_temp_dir),
        app_runtime_python=str(execution_context.app_runtime_python),
        read_access_scope=execution_context.read_access_scope,
    )


@pytest.mark.asyncio
async def test_assistant_utterance_precedes_reply_and_survives_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.checkpoint import (
        build_runtime_state_checkpoint,
        restore_runtime_state_checkpoint,
    )
    from pantaray_agents.agents.action_agent.support.formatter import (
        ActionAgentFormatter,
    )
    from pantaray_agents.schema.agent.action_assistant_message import (
        ActionAssistantMessageStep,
    )

    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    context = _bootstrap_execution_context(tmp_path=tmp_path, monkeypatch=monkeypatch)
    request = build_action_request(
        action_id="action-1",
        suggestion_id=None,
        user_id="user-1",
        content="その案の詳細を教えて",
        user_step_number=2,
        user_step_local_step_number=2,
    ).model_copy(
        update={
            "preceding_assistant_messages": (
                ActionAssistantMessageStep(
                    step_id="assistant-1",
                    step_number=1,
                    local_step_number=1,
                    short_step_id="S-1-ASSISTANT",
                    content="提案: Sakuraの事前チェックを整理しましょう。",
                    created_at="2025-01-01T00:00:00Z",
                    assistant_phase="commentary",
                ),
            )
        }
    )
    state = _state_with_execution_context(context)
    state["suggestion_id"] = None
    updated = await initialize_context(agent, state, _runtime(agent, request=request))  # type: ignore[arg-type]
    formatter = ActionAgentFormatter()
    first = formatter.format_history(updated, omit_before_step_number=10)
    assert first.index("- Assistant Message (phase: commentary):") < first.index(
        "- User Request:"
    )
    assert "提案: Sakuraの事前チェックを整理しましょう。" in first
    assert (
        updated["history_by_scope"]["S"][1]["user_request_text"]
        == "その案の詳細を教えて"
    )
    assert updated["step"] == 3
    restored = restore_runtime_state_checkpoint(
        build_runtime_state_checkpoint(updated),
        expected_action_id="action-1",
        expected_suggestion_id=None,
        expected_user_id="user-1",
    )
    # Restarting the same request must preserve, rather than duplicate, the utterance.
    assert restored["history_by_scope"]["S"][0]["assistant_phase"] == "commentary"
    restarted = await initialize_context(
        agent, restored, _runtime(agent, request=request)
    )  # type: ignore[arg-type]
    assert formatter.format_history(restarted) == first
    followup = build_action_request(
        action_id="action-1",
        suggestion_id=None,
        user_id="user-1",
        content="続けて",
        message_id="followup",
        user_step_number=4,
        user_step_local_step_number=4,
    )
    followup = followup.model_copy(
        update={
            "preceding_assistant_messages": (
                *request.preceding_assistant_messages,
                ActionAssistantMessageStep(
                    step_id="later-assistant",
                    step_number=3,
                    local_step_number=3,
                    short_step_id="S-3-ASSISTANT",
                    content="追加の発言",
                    created_at="2025-01-01T00:00:02Z",
                ),
            )
        }
    )
    continued = await initialize_context(
        agent, restarted, _runtime(agent, request=followup)
    )  # type: ignore[arg-type]
    history = formatter.format_history(continued)
    assert (
        history.index("提案: Sakura")
        < history.index("その案の詳細")
        < history.index("追加の発言")
        < history.index("続けて")
    )
    assert len(continued["history_by_scope"]["S"]) == 4
    assert continued["context"]["local_step_counters"]["S"] == 4
    assert history.count("- Assistant Message (phase: commentary):") == 1


def _executing_agent() -> SimpleNamespace:
    """The production executing template, as the agent reads it."""

    import yaml

    config = yaml.safe_load(
        (
            Path(__file__).parents[3]
            / "src/pantaray_agents/prompts/action/executing.yaml"
        ).read_text(encoding="utf-8")
    )
    return SimpleNamespace(
        executing_prompt=config["prompt"],
        executing_system_instruction="SYS",
        DEFAULT_SYSTEM_INSTRUCTION="SYS",
        executing_role_rule=lambda key: "",
        executing_world_state_update=lambda key: config["world_state_updates"][key],
    )


async def _initialized_with_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_agents_md: str | None
) -> tuple[ActionAgentState, SimpleNamespace]:
    home = tmp_path / "home"
    (home / ".pantaray").mkdir(parents=True)
    if user_agents_md is not None:
        (home / ".pantaray" / "AGENTS.md").write_text(user_agents_md, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo = MockActionAgentRepository()
    agent = _build_agent(repo)
    await _save_suggestion(repo)
    state = _state_with_execution_context(
        _bootstrap_execution_context(tmp_path=tmp_path, monkeypatch=monkeypatch)
    )
    runtime = _runtime(agent)
    updated = await initialize_context(agent, state, runtime)  # type: ignore[arg-type]
    return updated, runtime


@pytest.mark.asyncio
async def test_pantaray_default_agents_md_leads_the_head_without_a_user_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
        build_executing_turn,
    )

    updated, runtime = await _initialized_with_home(
        tmp_path, monkeypatch, user_agents_md=None
    )

    head = build_executing_turn(_executing_agent(), updated, runtime, tools=()).head  # type: ignore[arg-type]
    assert PANTARAY_DEFAULT_AGENTS_MD.startswith(
        "# AGENTS.md instructions (Pantaray default)\n\n"
        "<INSTRUCTIONS>\n# Working principles\n"
    )
    assert (
        head.index("### Workspace Context Rules")
        < head.index(PANTARAY_DEFAULT_AGENTS_MD)
        < head.index("## Suggestion Summary")
    )
    assert "AGENTS.md instructions for ~/.pantaray" not in head


@pytest.mark.asyncio
async def test_pantaray_agents_md_rides_in_a_head_that_resume_keeps_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.checkpoint import (
        build_runtime_state_checkpoint,
        restore_runtime_state_checkpoint,
    )
    from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
        build_executing_turn,
    )

    updated, runtime = await _initialized_with_home(
        tmp_path, monkeypatch, user_agents_md="Be brief.\n"
    )
    executing = _executing_agent()

    def head(of: ActionAgentState) -> bytes:
        turn = build_executing_turn(executing, of, runtime, tools=())  # type: ignore[arg-type]
        return turn.head.encode("utf-8")

    first = head(updated).decode("utf-8")
    block = (
        "# AGENTS.md instructions for ~/.pantaray\n\n"
        "<INSTRUCTIONS>\nBe brief.\n\n</INSTRUCTIONS>"
    )
    # Pantaray's default comes first, so the user's file is read as overriding it.
    assert (
        first.index("### Workspace Context Rules")
        < first.index(PANTARAY_DEFAULT_AGENTS_MD + "\n\n" + block)
        < first.index("## Suggestion Summary")
    )
    # Editing the file mid-run must not reach the cached head; resume restores it.
    (tmp_path / "home" / ".pantaray" / "AGENTS.md").write_text(
        "Changed.\n", encoding="utf-8"
    )
    restored = restore_runtime_state_checkpoint(
        build_runtime_state_checkpoint(updated),
        expected_action_id="action-1",
        expected_suggestion_id="sug-1",
        expected_user_id="user-1",
    )
    assert head(updated) == head(restored) == first.encode("utf-8")
    # A later run reads the edited file: the head stays, the turn appends it.
    restored["context"]["agents_md_instructions"] = load_pantaray_agents_md()
    turn = build_executing_turn(executing, restored, runtime, tools=())  # type: ignore[arg-type]
    assert turn.head == first
    prepared = turn.prepare(
        restored,
        rendering=runtime.services.rendering,
        repair_notice="",
        provider_turns={},
    )
    assert prepared.turn_context is not None
    assert "## AGENTS.md Update" in prepared.turn_context
    assert "Changed." in prepared.turn_context

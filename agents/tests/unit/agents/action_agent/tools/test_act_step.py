from __future__ import annotations

# pylint: disable=import-error,redefined-outer-name,unused-argument,protected-access
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    base_state,
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    seed_action_header,
)

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import action_step
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    FinalizedToolExecutionError,
    ToolCompletionAuditPersistenceError,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionPhase,
    build_next_action,
    build_tool_call,
)
from pantaray_agents.agents.action_agent.tools import (
    MEMORY_SEARCH_TOOL,
    THINKING_TOOL,
    WEB_CRAWL_TOOL,
    WEB_EXTRACT_TOOL,
    WEB_SEARCH_TOOL,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.agent.action_history import SUPERVISOR_SCOPE_HANDLE


def _runtime_services(action_agent: ActionAgent):
    return action_agent._runtime_services  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("planning", "executing"))
@pytest.mark.parametrize(
    ("run_authority", "skip_persist"),
    (("superseded", False), ("authoritative", True)),
)
async def test_action_step_does_not_execute_without_write_authority(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
    phase: ActionPhase,
    run_authority: str,
    skip_persist: bool,
) -> None:
    run_tool = AsyncMock()
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        run_tool,
    )
    action_agent._cancellation_service.check_cancellation = AsyncMock(  # noqa: SLF001
        return_value=False
    )
    state = base_state(action_agent)
    state["phase"] = phase
    state["run_authority"] = run_authority
    state["skip_persist"] = skip_persist
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=MEMORY_SEARCH_TOOL.tool_id,
            args={"query": "must not execute"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    steps_before = len(action_agent.repository.data.get("action_steps", []))

    updated = await action_step(
        action_agent,
        state,
        MagicMock(),
        sink=create_state_token_sink(state),
    )

    run_tool.assert_not_awaited()
    assert updated["step"] == state["step"]
    assert updated["tool_steps_taken"] == state["tool_steps_taken"]
    assert updated["next_action"] == state["next_action"]
    assert len(action_agent.repository.data.get("action_steps", [])) == steps_before


@pytest.mark.asyncio
async def test_action_step_handles_malformed_next_action_tool(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """malformed state（next_action.tool が dict 以外）でも、例外でジョブを即死させず自己修復へ寄せる。"""

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )

    # cancellation 判定は本テストの関心外のためスタブする
    action_agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = {"tool": "not-a-dict"}
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )
    assert updated.get("next_action") is None
    assert updated.get("errors"), (
        "validation error が state.errors に記録される想定です"
    )
    # 失敗はジョブ全体のクラッシュではなく、supervisor再試行のための非終端エラーとして扱う
    assert updated.get("status") in ("processing", None, "error")


@pytest.mark.asyncio
async def test_action_step_web_search_tool_end_to_end(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    """web_search が action_step 経由で実行され、state/DB に反映されること。"""
    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        AsyncMock(
            return_value={
                "tool_id": "web_search",
                "status": "ok",
                "request_id": "req-1",
                "result": {
                    "status": "success",
                    "query": "q",
                    "results": [
                        {
                            "url": "https://example.com/1",
                            "title": "T1",
                            "content": "C1",
                            "score": 0.9,
                        }
                    ],
                    "images": [
                        {
                            "url": "https://img.example.com/1.png",
                            "description": "Image 1",
                        }
                    ],
                },
            }
        ),
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(WEB_SEARCH_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=WEB_SEARCH_TOOL.tool_id,
            args={"query": "q"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    history_entry = updated_state["history_by_scope"][SUPERVISOR_SCOPE_HANDLE][-1]
    assert history_entry["tool_id"] == WEB_SEARCH_TOOL.tool_id
    payload = history_entry["output"]
    assert isinstance(payload, dict)
    assert payload["query"] == "q"
    assert payload["results"][0]["title"] == "T1"
    assert payload["images"][0]["description"] == "Image 1"

    # action_step が保存される
    repo_steps = action_agent.repository.data.get("action_steps", [])
    assert repo_steps
    assert repo_steps[-1]["step_name"] == f"tool::{WEB_SEARCH_TOOL.tool_id}"


@pytest.mark.asyncio
async def test_action_step_web_extract_tool_end_to_end(
    action_agent: ActionAgent,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        AsyncMock(
            return_value={
                "tool_id": "web_extract",
                "status": "ok",
                "request_id": "req-1",
                "result": {
                    "results": [
                        {
                            "url": "https://example.com/doc",
                            "raw_content": "abcdefg",
                        }
                    ],
                    "failed_results": [],
                },
            }
        ),
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(WEB_EXTRACT_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=WEB_EXTRACT_TOOL.tool_id,
            args={"urls": ["https://example.com/doc"]},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    history_entry = updated_state["history_by_scope"][SUPERVISOR_SCOPE_HANDLE][-1]
    assert history_entry["tool_id"] == WEB_EXTRACT_TOOL.tool_id
    payload = history_entry["output"]
    assert isinstance(payload, dict)
    assert payload["results"][0]["url"] == "https://example.com/doc"
    assert payload["results"][0]["raw_content"] == "abcdefg"
    assert payload["failed_results"] == []

    repo_steps = action_agent.repository.data.get("action_steps", [])
    assert repo_steps
    assert repo_steps[-1]["step_name"] == f"tool::{WEB_EXTRACT_TOOL.tool_id}"


@pytest.mark.asyncio
async def test_action_step_web_extract_non_public_url_is_validation_error(
    action_agent: ActionAgent,
    monkeypatch,
    tmp_path,
) -> None:
    from unittest.mock import patch

    monkeypatch.setenv("TAVILY_API_KEY", "dummy")

    tavily_client_factory = MagicMock()

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(WEB_EXTRACT_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=WEB_EXTRACT_TOOL.tool_id,
            args={"urls": ["http://localhost/private"]},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    with patch("tavily.TavilyClient", tavily_client_factory):
        updated_state = await action_step(
            action_agent, state, runtime, sink=create_state_token_sink(state)
        )

    assert updated_state.get("errors"), "validation error が記録されていません"
    assert updated_state.get("status") in ("processing", None)
    tavily_client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_action_step_web_crawl_tool_end_to_end(
    action_agent: ActionAgent,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        AsyncMock(
            return_value={
                "tool_id": "web_crawl",
                "status": "ok",
                "request_id": "req-1",
                "result": {
                    "base_url": "https://example.com/docs",
                    "results": [
                        {
                            "url": "https://example.com/a",
                            "raw_content": "abcdef",
                        },
                        {
                            "url": "https://example.com/b",
                            "raw_content": "uvwxyz",
                        },
                    ],
                },
            }
        ),
    )

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 8,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(WEB_CRAWL_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=WEB_CRAWL_TOOL.tool_id,
            args={
                "url": "https://example.com/docs",
                "instructions": "Only docs pages",
            },
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    history_entry = updated_state["history_by_scope"][SUPERVISOR_SCOPE_HANDLE][-1]
    assert history_entry["tool_id"] == WEB_CRAWL_TOOL.tool_id
    payload = history_entry["output"]
    assert isinstance(payload, dict)
    assert payload["base_url"] == "https://example.com/docs"
    assert payload["results"][0]["raw_content"] == "abcdef"
    assert payload["results"][1]["raw_content"] == "uvwxyz"

    repo_steps = action_agent.repository.data.get("action_steps", [])
    assert repo_steps
    assert repo_steps[-1]["step_name"] == f"tool::{WEB_CRAWL_TOOL.tool_id}"


@pytest.mark.asyncio
async def test_action_step_does_not_retry_or_commit_failed_tool_state(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    attempts = {"count": 0}
    stored_error = {
        "storage": "action_file",
        "path": "/runtime/actions/action-1/tool-results/invocation-1/output.json",
        "media_type": "application/json",
        "byte_size": 21_000,
        "character_count": 21_000,
        "line_count": 10,
    }

    async def failing_run_tool(_agent, _tool_def, _args, execution_state, **_kwargs):
        attempts["count"] += 1
        execution_state["final_output"] = "mutated-by-failed-tool"
        raise FinalizedToolExecutionError(
            cause=RuntimeError("temporary failure"),
            finalized_output=FinalizedToolOutput(
                output=stored_error,
                storage_kind="action_file",
                owner_kind="tool_invocation",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            ),
            tool_invocation_id="invocation-1",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        failing_run_tool,
    )

    captured_errors: list[dict] = []

    async def _emit_error(event: dict) -> None:
        captured_errors.append(event)

    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-err",
            suggestion_id="sug-err",
            user_id="user-err",
        ),
        state_config={
            "max_steps": 5,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=_emit_error,
        services=_runtime_services(action_agent),
    )

    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=THINKING_TOOL.tool_id,
            args={"query": "ツール失敗テスト"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert attempts["count"] == 1
    assert captured_errors == []
    assert updated_state.get("errors")
    assert updated_state["errors"][-1]["severity"] == "warning"
    assert updated_state["final_output"] != "mutated-by-failed-tool"
    assert updated_state["history_by_scope"]["S"][-1]["output"] == stored_error
    saved_output = action_agent.repository.data["action_steps"][-1]["tool_output"]
    assert saved_output == {
        "schema_version": 1,
        "status": "error",
        "output": stored_error,
        "output_storage_kind": "action_file",
        "output_owner_kind": "tool_invocation",
    }


@pytest.mark.asyncio
async def test_action_step_does_not_retry_completed_tool_when_audit_persistence_fails(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    attempts = 0

    async def audit_failure_after_tool_completion(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ToolCompletionAuditPersistenceError(
            tool_id=THINKING_TOOL.tool_id,
            tool_invocation_id="invocation-1",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        audit_failure_after_tool_completion,
    )
    runtime = ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-audit",
            suggestion_id="sug-audit",
            user_id="user-audit",
        ),
        state_config={
            "max_steps": 5,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=THINKING_TOOL.tool_id,
            args={"query": "audit failure"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert attempts == 1
    assert updated_state["status"] == "error"
    assert updated_state["errors"][-1]["error_code"] == (
        "ACTION_TOOL_AUDIT_PERSISTENCE_FAILED"
    )
    assert updated_state["errors"][-1]["error_details"]["attempts"] == 1

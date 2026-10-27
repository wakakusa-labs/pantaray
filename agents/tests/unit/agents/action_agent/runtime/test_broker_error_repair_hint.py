"""A broker tool's refusal reaches the model with its repair guidance.

The Supervisor cases raise a real ``BrokerPolicyError`` inside the broker and
follow the call as the Supervisor does: the wrapper, the step finalization, the
history row and the conversation item the next turn sends.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.unit.local_runtime.broker_test_support import BROKER_ACTOR_PROCESS_ID
from tests.unit.local_runtime.path_access_policy_support import (
    ACTION_ID,
    USER_ID,
    bootstrap_path_policy_runtime_db,
)
from tests.unit.local_runtime.test_pdf_page_render import write_pdf

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.builders import (
    _build_tool_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.broker_tools import (
    run_broker_tool_wrapper,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.finalization_boundary import (
    finalize_action_step_preparation,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.outcome_line import (
    build_outcome_line,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.support.conversation_projection import (
    project_action_conversation,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.tooling.documents import (
    PageOutOfRangeError,
    RenderedPages,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_WORKSPACE,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs import (
    action_subagent_broker as subagent_broker,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files import render_pages as drawing
from pantaray_agents.utils.trace_context import TraceContextManager


async def _model_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str, args: Any
) -> dict[str, Any]:
    """The tool-result item the next Action turn sends for one failed call."""

    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path,
        allowed_tool_ids=("read", "render_pdf_page"),
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    execution = cast(Any, context)
    state = create_initial_state(
        user_id=USER_ID,
        suggestion_id=None,
        action_id=ACTION_ID,
        started_at="2026-10-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
        manifest_id=execution.manifest_id,
        execution_session_id=execution.execution_session_id,
        execution_network_policy=execution.network_policy,
        action_temp_dir=str(execution.action_temp_dir),
        app_runtime_python=str(execution.app_runtime_python),
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )
    resolved_args = args(execution.workspace_path) if callable(args) else args
    tool_def = cast(ToolDefinition, type("Tool", (), {"tool_id": tool_id}))
    with TraceContextManager(extra={"process_id": BROKER_ACTOR_PROCESS_ID}):
        preparation = await run_broker_tool_wrapper(
            None,
            "tool-2",
            tool_def,
            resolved_args,
            state,
            invocation_id=None,
            tool_request_id="request-1",
            requested_at="2026-10-01T00:00:00Z",
            preflight_only=False,
        )
    result = finalize_action_step_preparation(state=state, preparation=preparation)
    assert result.status == "error"
    output = result.finalized_output.output
    entry = _build_tool_history_entry(
        step_id="tool-2",
        step_number=2,
        phase="executing",
        summary="note",
        tool_id=tool_id,
        started_at="2026-10-01T00:00:00Z",
        completed_at="2026-10-01T00:00:01Z",
        result_line=build_outcome_line(
            tool_id,
            output,
            status="failed",
            error_code=f"ACTION_TOOL_{tool_id.upper()}_FAILED",
        ),
        args=cast(dict[str, JSONValue], resolved_args),
        output=output,
        short_step_id="S-2-TOOL",
        origin=ActionToolCallOrigin(call_id="call_a", llm_step_id="think-1"),
    )
    think: dict[str, Any] = {
        "step_id": "think-1",
        "step_number": 1,
        "phase": "executing",
        "step_type": StepType.LLM_OUTPUT,
        "summary": "note",
        "tool_id": None,
        "started_at": "2026-10-01T00:00:00Z",
        "completed_at": "2026-10-01T00:00:00Z",
        "short_step_id": "S-1-THINK",
    }
    projection = project_action_conversation(
        cast(Any, [think, entry]),
        request_fingerprint="",
        omit_before_step_number=0,
        turn_context=None,
        repair_notice="",
        provider_turns={},
    )
    assert projection is not None
    return cast(Any, projection.conversation[1]).output


@pytest.mark.asyncio
async def test_a_missing_read_path_reaches_the_model_with_its_fix_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await _model_sees(
        tmp_path,
        monkeypatch,
        "read",
        lambda workspace: {"path": str(workspace / "missing.txt")},
    )

    assert seen["result"] == "read: failed READ_PATH_NOT_FOUND"
    error = seen["output"]["error"]
    assert error["code"] == "READ_PATH_NOT_FOUND"
    assert error["fix_hint"].startswith("Retry with an existing")
    assert "examples" not in error


@pytest.mark.asyncio
async def test_a_read_outside_the_workspace_reaches_the_model_with_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    seen = await _model_sees(tmp_path, monkeypatch, "read", {"path": str(outside)})

    error = seen["output"]["error"]
    assert error["code"] == "READ_SCOPE_DENIED"
    assert "full_access" in error["fix_hint"]
    assert error["examples"] == [".", "/Users/example/project", "src"]


@pytest.mark.asyncio
async def test_a_page_past_the_end_reaches_the_model_with_the_valid_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def past_the_end(**_kwargs: object) -> RenderedPages:
        raise PageOutOfRangeError(number=5, total_units=2)

    monkeypatch.setattr(drawing, "render_pdf_pages", past_the_end)

    def report(workspace: Path) -> dict[str, JSONValue]:
        write_pdf(workspace / "report.pdf", marks=[(0, 0, 0), (0, 0, 0)])
        return {"path": str(workspace / "report.pdf"), "pages": [5]}

    seen = await _model_sees(tmp_path, monkeypatch, "render_pdf_page", report)

    assert seen["result"] == "render_pdf_page: failed RENDER_PAGE_OUT_OF_RANGE"
    error = seen["output"]["error"]
    assert error["code"] == "RENDER_PAGE_OUT_OF_RANGE"
    assert "between 1 and 2" in error["fix_hint"]


@pytest.mark.asyncio
async def test_a_subagent_refusal_reaches_the_child_with_its_repair_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(**_kwargs: object) -> None:
        raise BrokerPolicyError(
            "GREP_PATTERN_INVALID: regex parse error",
            code="GREP_PATTERN_INVALID",
            fix_hint="Escape regex metacharacters.",
            examples=("foo\\(",),
        )

    monkeypatch.setattr(subagent_broker, "execute_broker_tool", refuse)

    result = await subagent_broker.execute_action_subagent_broker_tool(
        db_path=tmp_path / "unused.sqlite3",
        busy_timeout_ms=1_000,
        payload=cast(Any, {"user_id": "u", "process_id": "p"}),
        authority=cast(Any, SimpleNamespace(manifest_id="m", execution_session_id="s")),
        tool_id="grep",
        args={"pattern": "foo("},
        tool_request_id="request-1",
        call_id="call_a",
        agents_md=cast(Any, None),
    )

    assert result.output == {
        "status": "error",
        "error_code": "GREP_PATTERN_INVALID",
        "message": "GREP_PATTERN_INVALID: regex parse error",
        "details": {
            "fix_hint": "Escape regex metacharacters.",
            "examples": ["foo\\("],
        },
    }

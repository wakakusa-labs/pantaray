from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import validate

from pantaray_agents.agents.artifact_react import ReactToolCall, ToolCallEnvelope
from pantaray_agents.local_runtime.tooling.agent_experience.action_history import (
    ActionTurnWindow,
    AgentExperienceActionHistoryTools,
)
from pantaray_agents.schema.agent.base import JSONValue

from .local_action_repository_support import (
    ACTION_ID,
    BUSY_TIMEOUT_MS,
    USER_ID,
    bootstrap_action_repository_db,
    insert_action_step,
)

_PRIVATE_PROMPT = "private-prompt-marker:" + "embedded history " * 50_000
_PRIVATE_THOUGHT = "private-thought-marker"
_RESPONSE = "Verify the repository directory before invoking Git."
_TOOL_OUTPUT = {"exit_code": 128, "stderr": "not a git repository"}
_IN_PROGRESS = "in-progress-turn-marker"


@pytest.fixture
def history_tools(tmp_path: Path) -> AgentExperienceActionHistoryTools:
    db_path = bootstrap_action_repository_db(tmp_path)
    for number, suffix in ((1, "THINK"), (2, "TOOL"), (3, "THINK")):
        insert_action_step(
            db_path=db_path,
            step_id=f"step-{number}",
            short_step_id=f"G1-{number}-{suffix}",
            step_number=number,
            created_at="2026-09-26T21:50:00.123Z",
            completed_at="2026-09-26T21:51:00.456Z",
        )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """UPDATE agent_action_steps
               SET llm_prompt_text=?,thinking=?,llm_response_text=?
               WHERE step_id='step-1'""",
            (_PRIVATE_PROMPT, _PRIVATE_THOUGHT, _RESPONSE),
        )
        connection.execute(
            """UPDATE agent_action_steps
               SET step_type='tool_execution',step_name='tool::bash',
                   local_step_number=2,status='error',tool_args=?,tool_output=?,
                   started_at='2026-09-26T21:50:30.000Z'
               WHERE step_id='step-2'""",
            (json.dumps({"cmd": "git status"}), json.dumps(_TOOL_OUTPUT)),
        )
        connection.execute(
            "UPDATE agent_action_steps SET llm_response_text=? WHERE step_id='step-3'",
            (_IN_PROGRESS,),
        )
    return AgentExperienceActionHistoryTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        turns=(ActionTurnWindow(ACTION_ID, 1, 2),),
    )


def _call(name: str, **arguments: JSONValue) -> ReactToolCall:
    args: dict[str, JSONValue] = {"action_id": ACTION_ID, **arguments}
    return ReactToolCall(
        tool_name=name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=name, reason=None, args=args),
    )


async def test_memory_fetch_preserves_evidence_without_embedding_execution_context(
    history_tools: AgentExperienceActionHistoryTools,
) -> None:
    result = await history_tools.fetch_history(
        _call("history_fetch", refs=["G1-1-THINK", "G1-2-TOOL"]), 1
    )

    assert result.status == "success"
    rendered = json.dumps(result.output)
    assert len(rendered) < 2_000
    assert "llm_prompt_text" not in rendered
    assert "thinking" not in rendered
    assert _RESPONSE in rendered
    assert "not a git repository" in rendered
    validate(result.output, history_tools.definitions()[2].response_schema)
    with sqlite3.connect(history_tools.db_path) as connection:
        assert connection.execute(
            "SELECT llm_prompt_text,thinking FROM agent_action_steps WHERE step_id='step-1'"
        ).fetchone() == (_PRIVATE_PROMPT, _PRIVATE_THOUGHT)


@pytest.mark.parametrize("query", ["private-prompt-marker", _PRIVATE_THOUGHT])
async def test_memory_search_cannot_discover_execution_context(
    history_tools: AgentExperienceActionHistoryTools, query: str
) -> None:
    result = await history_tools.search_steps(
        _call("search_action_steps", query=query), 1
    )

    assert result.status == "success"
    assert result.output == {"status": "success", "matches": [], "next_offset": None}


async def test_memory_search_keeps_response_and_tool_evidence(
    history_tools: AgentExperienceActionHistoryTools,
) -> None:
    result = await history_tools.search_steps(
        _call("search_action_steps", query="repository"), 1
    )

    assert result.status == "success"
    rendered = json.dumps(result.output)
    assert "G1-1-THINK" in rendered
    assert "G1-2-TOOL" in rendered
    assert _RESPONSE in rendered
    assert "not a git repository" in rendered
    assert "private-prompt-marker" not in rendered
    assert _PRIVATE_THOUGHT not in rendered


@pytest.mark.usefixtures("tokyo_local_zone")
async def test_memory_history_shows_step_times_in_the_local_zone(
    history_tools: AgentExperienceActionHistoryTools,
) -> None:
    # 21:50Z is already the next morning in Tokyo; Memory must see the 27th.
    listed = await history_tools.list_steps(_call("list_action_steps"), 1)
    fetched = await history_tools.fetch_history(
        _call("history_fetch", refs=["G1-2-TOOL"]), 1
    )

    assert isinstance(listed.output, dict) and isinstance(fetched.output, dict)
    steps = listed.output["steps"]
    assert isinstance(steps, list)
    assert [(step["created_at"], step["completed_at"]) for step in steps] == [
        ("2026-09-27T06:50+09:00", "2026-09-27T06:51+09:00")
    ] * 2
    (step,) = fetched.output["steps"]
    assert (step["started_at"], step["completed_at"]) == (
        "2026-09-27T06:50+09:00",
        "2026-09-27T06:51+09:00",
    )


async def test_memory_history_stays_within_the_bound_turn(
    history_tools: AgentExperienceActionHistoryTools,
) -> None:
    # Step 3 belongs to a turn still in progress when the run was bound.
    fetched = await history_tools.fetch_history(
        _call("history_fetch", refs=["G1-3-THINK"]), 1
    )
    listed = await history_tools.list_steps(_call("list_action_steps"), 1)
    searched = await history_tools.search_steps(
        _call("search_action_steps", query=_IN_PROGRESS), 1
    )

    assert fetched.status == "error"
    assert isinstance(fetched.output, dict)
    assert fetched.output["error_code"] == "ACTION_HISTORY_NOT_FOUND"
    assert isinstance(listed.output, dict)
    steps = listed.output["steps"]
    assert isinstance(steps, list)
    assert [step["short_step_id"] for step in steps] == [
        "G1-1-THINK",
        "G1-2-TOOL",
    ]
    assert searched.output == {"status": "success", "matches": [], "next_offset": None}

from __future__ import annotations

from pathlib import Path
from string import Formatter

import yaml

from pantaray_agents.agents.action_agent.support.world_state import (
    WORLD_STATE_SECTIONS,
)
from pantaray_agents.agents.action_agent.tools import (
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_CONCURRENCY,
    WRITE_SESSION_MEMORY_TOOL_ID,
)
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _executing_config() -> dict[str, object]:
    config = yaml.safe_load(
        _read_text(
            Path(__file__).parents[3]
            / "src"
            / "pantaray_agents"
            / "prompts"
            / "action"
            / "executing.yaml"
        )
    )
    assert isinstance(config, dict)
    return config


def _executing_prompt_template() -> str:
    template = _executing_config().get("prompt")
    assert isinstance(template, str)
    return template


def _supervisor_rules() -> str:
    config = _executing_config()
    rules = config.get("role_rules")
    assert isinstance(rules, dict)
    rule = rules.get("supervisor")
    assert isinstance(rule, str)
    return rule


def _shared_system_instruction() -> str:
    instruction = _executing_config().get("system_instruction")
    assert isinstance(instruction, str)
    return instruction


def test_executing_prompt_defines_supervisor_final_answer_flow() -> None:
    text = _read_text(
        Path(__file__).parents[3]
        / "src"
        / "pantaray_agents"
        / "prompts"
        / "action"
        / "executing.yaml"
    )
    assert "<final_answer>" not in text
    assert "## Final Answer Flow" in text
    assert "Pending Final Answer Draft" not in text
    assert "latest `draft_final_answer` call" in text
    assert "call `draft_final_answer` first" in text
    assert "call `submit_final_answer`" in text
    assert "Do not restate its internal details in the final answer" in text


def test_executing_prompt_defines_one_parent_tool_use_rule() -> None:
    text = _read_text(
        Path(__file__).parents[3]
        / "src"
        / "pantaray_agents"
        / "prompts"
        / "action"
        / "executing.yaml"
    )
    assert "## Supervisor Mode Rules" not in text
    assert "## Tool Use Rules" in text
    assert "{role_rules}" in text
    assert "role_rules:" in text
    assert "supervisor:" in text
    # 親プロンプトは Goal Worker 協調セクションを持たない。
    assert "supervisor_goal_worker:" not in text
    assert "{goal_conversations}" not in text
    assert "{pending_goal_completion_evidence}" not in text


def test_executing_prompt_names_the_session_memory_tool_the_supervisor_has() -> None:
    assert f"`{WRITE_SESSION_MEMORY_TOOL_ID}`" in _supervisor_rules()
    assert WRITE_SESSION_MEMORY_TOOL_ID in SUPERVISOR_SINGLE_REACT_TOOL_IDS


def test_executing_prompt_reconciles_plan_and_reports_against_current_evidence() -> (
    None
):
    section = _supervisor_rules()

    evidence_inputs = (
        "latest user instructions",
        "your session memory",
        "subagent reports",
        "actual tool results and current DB/file state",
    )
    assert all(input_name in section for input_name in evidence_inputs)
    assert "latest user instructions and verified evidence take precedence" in section
    assert "Update stale session memory" in section
    assert "evidence candidates, not truth" in section
    assert "instead of accepting them blindly" in section
    assert "call `history_fetch`" in section
    # Checking a change is not tied to the role, so subagents get it too.
    shared = _shared_system_instruction()
    assert "inspect the resulting current state" in shared
    assert "mutation tool's success" in shared
    assert "purely inline answer" in shared


def test_executing_prompt_delegates_model_guidance_to_spawn_tool_metadata() -> None:
    section = _supervisor_rules()
    prompt_text = _read_text(
        Path(__file__).parents[3]
        / "src"
        / "pantaray_agents"
        / "prompts"
        / "action"
        / "executing.yaml"
    )

    assert "delegate them to subagents and run them in parallel" in section
    assert "Request all their `spawn_subagent` calls in the same turn" in section
    assert "reasonably substantial, self-contained piece of work" in section
    assert "Brief each subagent in detail so it does not redo your work" in section
    assert "quote content you have already read" in section
    assert "it knows nothing of this conversation" in section
    assert "a colleague who just walked in" in section
    assert "exactly what to return" in section
    assert "Parallel subagents must not write the same files" in section
    assert "Do not spawn a subagent just to run one command or one check" in section
    assert "choose an explicit model from the tool definition" in section
    for setting in SUBAGENT_MODEL_SETTINGS:
        assert setting.selector not in prompt_text
        assert setting.recommendation not in prompt_text


def test_executing_prompt_states_the_tool_batch_rules() -> None:
    """1 ターン複数呼び出しの可否を、実行時のポリシーと同じ語で宣言している。"""

    text = _read_text(
        Path(__file__).parents[3]
        / "src"
        / "pantaray_agents"
        / "prompts"
        / "action"
        / "executing.yaml"
    )

    assert "exactly one provided tool" not in text
    assert "One turn may request several read-only calls at once" in text
    for tool_id, declared in TOOL_CONCURRENCY.items():
        if declared.placement != "sequential":
            assert f"`{tool_id}`" in text
    assert "must be the only call of their turn" in text
    assert "Do not request two changing tools" in text
    assert "The runtime may defer or drop requested calls" in text
    assert "Every call needs its own internal `step_note`" in text


def test_action_prompts_treat_request_summary_as_handoff_note() -> None:
    base = Path(__file__).parents[3] / "src" / "pantaray_agents" / "prompts" / "action"
    text = _read_text(base / "executing.yaml")

    assert "Suggestion Summary as the" in text
    assert "handoff note" in text
    assert "work surface" in text


# The head is rendered once per Action. These change while it lasts, so they
# reach the model through world_state_updates instead of a rewritten head:
# - current_time: wall clock.
# - the workspace and ~/.pantaray AGENTS.md: re-read by every run.
_CHANGING_PROMPT_FIELDS = frozenset(
    field for _, fields in WORLD_STATE_SECTIONS for field in fields
)
# Fixed for the whole Action. The profile briefs are read once, when the Action
# starts; Pantaray's default AGENTS.md ships with the app.
_FIXED_PROMPT_FIELDS = frozenset(
    {
        "pantaray_default_agents_md",
        "workspace_context_rules",
        "request_summary",
        "target_context",
        "memory_context_model",
        "insight_data",
        "structured_fact_data",
    }
)


def _prompt_fields(template: str) -> set[str]:
    return {
        field for _, field, _, _ in Formatter().parse(template) if field is not None
    }


def test_every_executing_prompt_field_is_fixed_or_a_world_state_section() -> None:
    """新しい差し込み値は、固定か、変わったら追記する側かを宣言してから足す。"""

    assert _prompt_fields(_executing_prompt_template()) == (
        _FIXED_PROMPT_FIELDS | _CHANGING_PROMPT_FIELDS | {"action_history"}
    )


def test_the_executing_prompt_ends_with_the_history() -> None:
    """履歴の後ろに置いたものは変わらなくても毎回送られるので、何も置かない。"""

    template = _executing_prompt_template()
    assert template.rstrip().endswith("{action_history}")
    assert "rebuilt every turn" not in template


def test_every_world_state_section_has_an_update_naming_only_its_fields() -> None:
    updates = _executing_config().get("world_state_updates")
    assert isinstance(updates, dict)
    for section, fields in WORLD_STATE_SECTIONS:
        assert _prompt_fields(updates[section]) == set(fields)
    assert _prompt_fields(updates["agents_md_removed"]) == set()

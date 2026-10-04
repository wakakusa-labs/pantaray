from __future__ import annotations

from pathlib import Path
from string import Formatter

import yaml

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    PARALLEL_SAFE_TOOL_IDS,
    SOLO_TURN_TOOL_IDS,
)
from pantaray_agents.agents.action_agent.support.world_state import (
    WORLD_STATE_SECTIONS,
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


def _soft_plan_section() -> str:
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


def test_executing_prompt_keeps_one_optional_complex_task_plan() -> None:
    section = _soft_plan_section()

    assert "For a complex task, you may use one `plan.md`" in section
    assert "Do not create it for a small task" in section
    assert "same `plan.md`" in section
    assert all(
        required_part in section
        for required_part in (
            "Explicit user requests",
            "inferred true purpose, clearly labeled as an inference",
            "Success criteria",
            "Constraints and non-goals",
        )
    )
    assert "does not select an Action mode" in section
    assert "not a mandatory checklist" in section
    assert "open items neither block finalization nor prove success" in section


def test_executing_prompt_reconciles_plan_and_reports_against_current_evidence() -> (
    None
):
    section = _soft_plan_section()

    evidence_inputs = (
        "latest user instructions",
        "existing `plan.md`",
        "subagent reports",
        "actual tool results and current DB/file state",
    )
    assert all(input_name in section for input_name in evidence_inputs)
    assert "latest user instructions and verified evidence take precedence" in section
    assert "Update a stale `plan.md`" in section
    assert "evidence candidates, not truth" in section
    assert "instead of accepting them blindly" in section
    assert "call `history_fetch`" in section
    # Checking a change is not tied to the role, so subagents get it too.
    shared = _shared_system_instruction()
    assert "inspect the resulting current state" in shared
    assert "mutation tool's success" in shared
    assert "purely inline answer" in shared


def test_executing_prompt_delegates_model_guidance_to_spawn_tool_metadata() -> None:
    section = _soft_plan_section()
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
    for tool_id in sorted(PARALLEL_SAFE_TOOL_IDS):
        assert f"`{tool_id}`" in text
    for tool_id in sorted(SOLO_TURN_TOOL_IDS):
        assert f"`{tool_id}`" in text
    assert "must be the only call of their turn" in text
    assert "Do not request two changing tools" in text
    assert "The runtime may defer or drop requested calls" in text
    assert "Every call needs its own internal `step_note`" in text


def test_action_prompts_include_memory_source_coverage_placeholder() -> None:
    base = Path(__file__).parents[3] / "src" / "pantaray_agents" / "prompts" / "action"
    text = _read_text(base / "executing.yaml")

    assert "{memory_source_coverage}" in text


def test_action_prompts_treat_request_summary_as_handoff_note() -> None:
    base = Path(__file__).parents[3] / "src" / "pantaray_agents" / "prompts" / "action"
    text = _read_text(base / "executing.yaml")

    assert "Suggestion Summary as the" in text
    assert "handoff note" in text
    assert "work surface" in text


# The head is rendered once per Action. These change while it lasts, so they
# reach the model through world_state_updates instead of a rewritten head:
# - linkable_persisted_memory: memory_context_epoch, extended mid-run.
# - current_time: wall clock.
# - the workspace and ~/.pantaray AGENTS.md: re-read by every run.
_CHANGING_PROMPT_FIELDS = frozenset(
    field for _, fields in WORLD_STATE_SECTIONS for field in fields
)
# Fixed for the whole Action. Memory and its source coverage are read once,
# when the Action starts; Pantaray's default AGENTS.md ships with the app.
_FIXED_PROMPT_FIELDS = frozenset(
    {
        "pantaray_default_agents_md",
        "workspace_context_rules",
        "request_summary",
        "target_context",
        "memory_context_model",
        "insight_data",
        "structured_fact_data",
        "memory_artifact_references",
        "memory_source_coverage",
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

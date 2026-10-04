from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from jsonschema import SchemaError

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.validation import (
    validate_tool_args,
)
from pantaray_agents.agents.action_agent.tools import (
    TOOL_REGISTRY,
    build_native_action_tools,
    select_supervisor_act_tool_registry,
)
from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    ActionSubagentBrokerAuthority,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    build_action_subagent_broker_tools,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload


def _model_facing_parameters() -> dict[str, dict[str, JSONValue]]:
    """What the Action and a subagent send to the model, keyed by channel/tool."""

    action = {
        f"action/{tool.name}": tool.parameters
        for tool in build_native_action_tools(select_supervisor_act_tool_registry())
    }
    # Building the child tool set binds these into closures only; nothing runs.
    subagent = {
        f"subagent/{tool.name}": dict(tool.request_schema)
        for tool in build_action_subagent_broker_tools(
            db_path=Path("unused.sqlite3"),
            busy_timeout_ms=0,
            payload=cast(ActionSubagentJobPayload, {}),
            authority=cast(ActionSubagentBrokerAuthority, None),
        )
    }
    return {**action, **subagent}


def _property(schema: JSONValue, *path: str) -> dict[str, JSONValue]:
    node = schema
    for name in path:
        assert isinstance(node, dict)
        properties = node["properties"]
        assert isinstance(properties, dict)
        node = properties[name]
    assert isinstance(node, dict)
    return node


def test_tool_definition_rejects_invalid_output_schema_at_construction() -> None:
    with pytest.raises(SchemaError):
        replace(TOOL_REGISTRY["bash"], output_schema={"type": "invalid-type"})


def test_tool_prompt_contracts_default_to_multiline_guide_sections() -> None:
    for tool in TOOL_REGISTRY.values():
        description = tool.prompt_contract.description

        assert description.startswith("Purpose:\n"), tool.tool_id
        assert "\n\nWhen:\n" in description, tool.tool_id
        assert "\n\nAvoid:\n" in description, tool.tool_id


def test_every_tool_argument_the_model_sees_carries_its_description() -> None:
    parameters_by_tool = _model_facing_parameters()

    assert "subagent/bash" in parameters_by_tool
    for tool, parameters in parameters_by_tool.items():
        properties = parameters["properties"]
        assert isinstance(properties, dict)
        for name, schema in properties.items():
            assert isinstance(schema, dict)
            description = schema.get("description")
            assert isinstance(description, str) and description.strip(), (
                f"{tool}.{name} reaches the model without a description"
            )


def test_nested_and_access_field_descriptions_reach_the_model() -> None:
    parameters_by_tool = _model_facing_parameters()
    memory_search = parameters_by_tool["action/memory_search"]
    for channel in ("action", "subagent"):
        bash = parameters_by_tool[f"{channel}/bash"]
        assert "login environment" in str(
            _property(bash, "use_login_environment")["description"]
        )
        assert "most specific existing folder" in str(
            _property(bash, "additional_write_folders")["description"]
        )
        assert "language of the user's request" in str(
            _property(bash, "justification")["description"]
        )
        run_python = parameters_by_tool[f"{channel}/run_python"]
        assert _property(run_python, "justification") == _property(
            bash, "justification"
        )

    assert "hours" in str(
        _property(memory_search, "time_hint", "radius_hours")["description"]
    )
    assert "center" in str(_property(memory_search, "time_hint")["description"])


def test_only_the_action_itself_is_offered_a_run_outside_the_sandbox() -> None:
    parameters_by_tool = _model_facing_parameters()

    assert "Never set it pre-emptively" in str(
        _property(parameters_by_tool["action/bash"], "run_outside_sandbox")
    )
    for tool in ("subagent/bash", "action/run_python", "subagent/run_python"):
        properties = parameters_by_tool[tool]["properties"]
        assert isinstance(properties, dict)
        assert "run_outside_sandbox" not in properties, tool


@pytest.mark.parametrize(
    ("tool_id", "args", "accepted"),
    [
        ("read", {"path": "notes.md", "offset": 1000, "limit": 11}, True),
        ("read", {"path": "notes.md", "offset": "1000"}, False),
        ("read", {"path": "notes.md", "description": "x"}, False),
        (
            "bash",
            {
                "command": "gh pr list",
                "use_login_environment": True,
                "justification": "GitHub の PR の状態を確認します。",
            },
            True,
        ),
        ("bash", {"command": "gh pr list", "use_login_environment": "yes"}, False),
        (
            "memory_search",
            {
                "query": "release",
                "time_hint": {"center": "2026-09-30T00:00:00Z", "radius_hours": 24},
            },
            True,
        ),
        (
            "memory_search",
            {"query": "release", "time_hint": {"center": "2026-09-30T00:00:00Z"}},
            False,
        ),
    ],
)
def test_field_descriptions_leave_argument_validation_unchanged(
    tool_id: str, args: dict[str, JSONValue], accepted: bool
) -> None:
    if accepted:
        validate_tool_args(TOOL_REGISTRY[tool_id], args)
        return
    with pytest.raises(ToolValidationError):
        validate_tool_args(TOOL_REGISTRY[tool_id], args)

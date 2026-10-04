from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

import pantaray_agents.agents.action_agent.tools.apply_patch_tool as apply_patch_tool_module
import pantaray_agents.agents.action_agent.tools.bash_tool as bash_tool_module
import pantaray_agents.agents.action_agent.tools.discovery_tools as discovery_tools_module
import pantaray_agents.agents.action_agent.tools.read_tool as read_tool_module
import pantaray_agents.agents.action_agent.tools.run_python_tool as run_python_tool_module
from pantaray_agents.agents.action_agent.tools.apply_patch_tool import (
    APPLY_PATCH_TOOL,
    APPLY_PATCH_TOOL_FIELD_PRESENTATION,
)
from pantaray_agents.agents.action_agent.tools.base import (
    ToolDefinition,
    build_validation_input_schema,
    schema_to_plain_json,
)
from pantaray_agents.agents.action_agent.tools.bash_tool import (
    BASH_TOOL,
    BASH_TOOL_FIELD_PRESENTATION,
)
from pantaray_agents.agents.action_agent.tools.broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)
from pantaray_agents.agents.action_agent.tools.discovery_tools import (
    GLOB_TOOL,
    GLOB_TOOL_FIELD_PRESENTATION,
    GREP_TOOL,
    GREP_TOOL_FIELD_PRESENTATION,
    LIST_TOOL,
    LIST_TOOL_FIELD_PRESENTATION,
)
from pantaray_agents.agents.action_agent.tools.read_tool import (
    READ_TOOL,
    READ_TOOL_FIELD_PRESENTATION,
)
from pantaray_agents.agents.action_agent.tools.run_python_tool import (
    RUN_PYTHON_TOOL,
    RUN_PYTHON_TOOL_FIELD_PRESENTATION,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchToolArgs,
    BashToolArgs,
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
    ReadToolArgs,
    RunPythonToolArgs,
)
from pantaray_agents.schema.agent.base import JSONValue


@dataclass(frozen=True, slots=True)
class BrokeredToolSchemaContract:
    tool: ToolDefinition
    model: type[BaseModel]
    presentation: tuple[BrokerToolFieldPresentation, ...]
    expected_schema: dict[str, JSONValue]


WRITE_FOLDER_REQUEST_SCHEMA: dict[str, JSONValue] = {
    "additional_write_folders": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "pattern": r"\S"},
    },
    "justification": {"type": "string", "minLength": 1, "pattern": r"\S"},
}

BROKERED_TOOL_SCHEMA_CONTRACTS = (
    BrokeredToolSchemaContract(
        tool=READ_TOOL,
        model=ReadToolArgs,
        presentation=READ_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "offset": {"type": "integer", "minimum": 1},
                "column": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
                "start_unit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
            "description": "Read a local file or directory.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=LIST_TOOL,
        model=ListToolArgs,
        presentation=LIST_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
            "additionalProperties": False,
            "description": "List a readable local directory.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=GLOB_TOOL,
        model=GlobToolArgs,
        presentation=GLOB_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "base_path": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "pattern": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["base_path", "pattern"],
            "additionalProperties": False,
            "description": "Find readable local paths by glob.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=GREP_TOOL,
        model=GrepToolArgs,
        presentation=GREP_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "base_path": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "pattern": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "include_glob": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["base_path", "pattern"],
            "additionalProperties": False,
            "description": "Search workspace text.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=BASH_TOOL,
        model=BashToolArgs,
        presentation=BASH_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "cwd": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "use_login_environment": {"type": "boolean"},
                **WRITE_FOLDER_REQUEST_SCHEMA,
            },
            "required": ["command"],
            "additionalProperties": False,
            "description": "Run a non-interactive workspace shell command or script.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=RUN_PYTHON_TOOL,
        model=RunPythonToolArgs,
        presentation=RUN_PYTHON_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "minLength": 1, "pattern": r"\S"},
                **WRITE_FOLDER_REQUEST_SCHEMA,
            },
            "required": ["code"],
            "additionalProperties": False,
            "description": "Run generated Python in the broker sandbox.",
        },
    ),
    BrokeredToolSchemaContract(
        tool=APPLY_PATCH_TOOL,
        model=ApplyPatchToolArgs,
        presentation=APPLY_PATCH_TOOL_FIELD_PRESENTATION,
        expected_schema={
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "op",
                                    "path",
                                    "new_lines",
                                    "trailing_newline",
                                ],
                                "properties": {
                                    "op": {"const": "add"},
                                    "path": {"type": "string", "minLength": 1},
                                    "new_lines": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "trailing_newline": {"type": "boolean"},
                                },
                            },
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["op", "path", "edits"],
                                "properties": {
                                    "op": {"const": "update"},
                                    "path": {"type": "string", "minLength": 1},
                                    "edits": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 1,
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["old_lines", "new_lines"],
                                            "properties": {
                                                "old_lines": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "description": (
                                                        "Existing contiguous lines to "
                                                        "replace or delete."
                                                    ),
                                                    "items": {"type": "string"},
                                                },
                                                "new_lines": {
                                                    "type": "array",
                                                    "description": "Replacement lines.",
                                                    "items": {"type": "string"},
                                                },
                                                "before_lines": {
                                                    "type": "array",
                                                    "description": (
                                                        "Existing nearby lines before "
                                                        "old_lines, used only to locate "
                                                        "the edit."
                                                    ),
                                                    "items": {"type": "string"},
                                                },
                                                "after_lines": {
                                                    "type": "array",
                                                    "description": (
                                                        "Existing nearby lines after "
                                                        "old_lines, used only to locate "
                                                        "the edit."
                                                    ),
                                                    "items": {"type": "string"},
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["op", "path"],
                                "properties": {
                                    "op": {"const": "delete"},
                                    "path": {"type": "string", "minLength": 1},
                                },
                            },
                        ]
                    },
                }
            },
            "required": ["changes"],
            "additionalProperties": False,
            "description": "Apply a workspace patch.",
        },
    ),
)


def test_brokered_tool_public_input_schemas_keep_current_llm_contract() -> None:
    for contract in BROKERED_TOOL_SCHEMA_CONTRACTS:
        public = schema_to_plain_json(contract.tool.input_schema)
        assert isinstance(public, dict)
        assert _field_descriptions(public) == {
            field.name: field.description for field in contract.presentation
        }
        assert _without_field_descriptions(public) == contract.expected_schema


def test_brokered_tool_input_schemas_are_generated_from_runtime_models() -> None:
    for contract in BROKERED_TOOL_SCHEMA_CONTRACTS:
        generated = build_validation_input_schema(
            broker_tool_input_spec_from_model(
                model=contract.model,
                fields=contract.presentation,
                description=str(contract.expected_schema["description"]),
            )
        )
        assert _without_field_descriptions(generated) == contract.expected_schema
        _assert_no_unresolved_pydantic_refs(generated)


def test_brokered_tool_definitions_do_not_hand_write_field_schemas() -> None:
    brokered_modules = (
        read_tool_module,
        discovery_tools_module,
        apply_patch_tool_module,
        bash_tool_module,
        run_python_tool_module,
    )
    for module in brokered_modules:
        assert "field_spec(" not in inspect.getsource(module)


def _field_descriptions(schema: dict[str, JSONValue]) -> dict[str, JSONValue]:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    return {
        name: field["description"]
        for name, field in properties.items()
        if isinstance(field, dict)
    }


def _without_field_descriptions(schema: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """The data shape alone; the descriptions come from the field presentation."""

    properties = schema["properties"]
    assert isinstance(properties, dict)
    return {
        **schema,
        "properties": {
            name: {key: value for key, value in field.items() if key != "description"}
            for name, field in properties.items()
            if isinstance(field, dict)
        },
    }


def _assert_no_unresolved_pydantic_refs(value: JSONValue) -> None:
    for item in _walk_json(value):
        if isinstance(item, dict):
            assert "$defs" not in item
            assert "$ref" not in item
            assert "discriminator" not in item
        if isinstance(item, str):
            assert "#/$defs" not in item


def _walk_json(value: JSONValue) -> Iterable[JSONValue]:
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk_json(nested)
    if isinstance(value, list):
        for nested in value:
            yield from _walk_json(nested)

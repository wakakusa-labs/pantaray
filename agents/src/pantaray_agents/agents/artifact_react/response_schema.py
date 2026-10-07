from __future__ import annotations

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ReactToolDefinition

from .artifact_patch_contract import ARTIFACT_PATCH_TOOL_NAME, COMPLETED_TOOL_NAME

RESERVED_ARTIFACT_TOOL_NAMES = frozenset(
    (ARTIFACT_PATCH_TOOL_NAME, COMPLETED_TOOL_NAME)
)


class ReactToolEnvelopeResponseFormat:
    """Provider-facing structured output format for artifact ReAct tool calls."""

    def __init__(self, *, tool_names: tuple[str, ...], extensible_args: bool) -> None:
        if not tool_names:
            raise ValueError("tool_names must not be empty")
        if len(set(tool_names)) != len(tool_names):
            raise ValueError("tool_names must be unique")
        self._tool_names = tool_names
        self._extensible_args = extensible_args

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tool_names

    @property
    def extensible_args(self) -> bool:
        return self._extensible_args

    def model_validate(self, payload: object) -> object:
        return payload

    def model_json_schema(self) -> dict[str, JSONValue]:
        if self._extensible_args:
            return _build_extensible_tool_envelope_schema(self._tool_names)
        return _build_artifact_only_response_schema()


def build_artifact_react_response_format(
    *,
    tool_definitions: tuple[ReactToolDefinition, ...] = (),
) -> ReactToolEnvelopeResponseFormat:
    validate_extra_react_tool_names(tool_definitions)
    extra_tool_names = tuple(tool.name for tool in tool_definitions)
    return ReactToolEnvelopeResponseFormat(
        tool_names=(
            ARTIFACT_PATCH_TOOL_NAME,
            COMPLETED_TOOL_NAME,
            *extra_tool_names,
        ),
        extensible_args=bool(extra_tool_names),
    )


def validate_extra_react_tool_names(
    tool_definitions: tuple[ReactToolDefinition, ...],
) -> None:
    extra_tool_names = tuple(tool.name for tool in tool_definitions)
    normalized_tool_names = tuple(name.strip() for name in extra_tool_names)
    reserved_names = RESERVED_ARTIFACT_TOOL_NAMES.intersection(normalized_tool_names)
    if reserved_names:
        reserved = ", ".join(sorted(reserved_names))
        raise ValueError(f"extra tool names must not use reserved names: {reserved}")
    if len(set(normalized_tool_names)) != len(normalized_tool_names):
        raise ValueError("extra tool names must be unique")


def _build_artifact_only_response_schema() -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool_id": {
                "type": "string",
                "enum": [ARTIFACT_PATCH_TOOL_NAME, COMPLETED_TOOL_NAME],
            },
            "reason": {"type": "string"},
            "args": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "chunks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["lines"],
                            "properties": {
                                "lines": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["op", "text"],
                                        "properties": {
                                            "op": {
                                                "type": "string",
                                                "enum": ["context", "remove", "add"],
                                            },
                                            "text": {"type": "string"},
                                        },
                                    },
                                }
                            },
                        },
                    },
                },
            },
        },
        "required": ["tool_id", "args"],
    }


def _build_extensible_tool_envelope_schema(
    tool_names: tuple[str, ...],
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool_id": {
                "type": "string",
                "enum": list(tool_names),
            },
            "reason": {"type": "string"},
            "args": {
                "type": "object",
            },
        },
        "required": ["tool_id", "args"],
    }


__all__ = [
    "RESERVED_ARTIFACT_TOOL_NAMES",
    "ReactToolEnvelopeResponseFormat",
    "build_artifact_react_response_format",
    "validate_extra_react_tool_names",
]

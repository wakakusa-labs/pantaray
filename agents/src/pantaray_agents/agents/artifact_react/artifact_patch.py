from __future__ import annotations

import json

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    JsonSchema,
    ReactToolCall,
    ReactToolDefinition,
    ToolCallEnvelope,
)
from pantaray_agents.utils.artifact_patch.parser import (
    ArtifactCompletion,
    ArtifactGenericToolCall,
    parse_artifact_document_tool_output,
)

from .artifact_patch_contract import (
    ARTIFACT_PATCH_TOOL_NAME,
    COMPLETED_TOOL_NAME,
    artifact_patch_request_schema,
)
from .types import ReactFinish, ReactParseError


def parse_artifact_document_react_output(
    output: str | object,
) -> ReactFinish | ReactToolCall:
    try:
        parsed = parse_artifact_document_tool_output(output)
    except RuntimeError as exc:
        raise ReactParseError(str(exc)) from exc
    if isinstance(parsed, ArtifactCompletion):
        return ReactFinish(final_text="", reason=parsed.reason)
    if isinstance(parsed, ArtifactGenericToolCall):
        return ReactToolCall(
            tool_name=parsed.tool_id,
            tool_args=parsed.args,
            tool_call_envelope=_tool_call_envelope(
                tool_id=parsed.tool_id,
                reason=parsed.reason,
                args=parsed.args,
            ),
        )
    return ReactToolCall(
        tool_name=ARTIFACT_PATCH_TOOL_NAME,
        tool_args=parsed.args,
        tool_call_envelope=_tool_call_envelope(
            tool_id=parsed.tool_id,
            reason=parsed.reason,
            args=parsed.args,
        ),
    )


def _tool_call_envelope(
    *,
    tool_id: str,
    reason: str | None,
    args: dict[str, JSONValue],
) -> ToolCallEnvelope:
    return ToolCallEnvelope(tool_id=tool_id, reason=reason, args=args)


def build_artifact_react_tools_definition_block(
    *,
    logical_path: str,
    tool_definitions: tuple[ReactToolDefinition, ...] = (),
) -> str:
    definitions = (
        _format_tool_definition(
            name=ARTIFACT_PATCH_TOOL_NAME,
            description=(
                f"Edit `{logical_path}` by applying an artifact patch to the current "
                "document."
            ),
            args_schema=artifact_patch_request_schema(logical_path=logical_path),
        ),
        _format_tool_definition(
            name=COMPLETED_TOOL_NAME,
            description=(
                "Finish only when the actual current document satisfies the task and no "
                "further edits are needed. Put the finish explanation in the top-level "
                "reason field."
            ),
            args_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        ),
        *(_format_react_tool_definition(tool) for tool in tool_definitions),
    )
    return "\n\n".join(definitions)


def _format_react_tool_definition(tool: ReactToolDefinition) -> str:
    return _format_tool_definition(
        name=tool.name,
        description=tool.description,
        args_schema=tool.request_schema,
    )


def _format_tool_definition(
    *,
    name: str,
    description: str,
    args_schema: JsonSchema,
) -> str:
    return "\n".join(
        (
            f"Tool: {name}",
            f"ID: {name}",
            f"Description: {description.strip()}",
            "Args schema:",
            json.dumps(dict(args_schema), ensure_ascii=False, indent=2),
        )
    )

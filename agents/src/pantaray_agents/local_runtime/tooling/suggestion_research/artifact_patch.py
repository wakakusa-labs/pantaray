"""apply_patch for a Suggestion's make stage, confined to its artifact folder.

The make stage has nobody to ask for approval, so it may write nowhere but the
one folder that holds what the Suggestion made. A path resolves inside that
folder or is refused; every file is opened below the folder's descriptor
without following a symlink, so a link planted in the folder cannot lead a
write out of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from pantaray_agents.schema.agent.suggestion import SUGGESTION_ARTIFACTS_ADAPTER
from pantaray_agents.tools.contract import (
    JsonSchema,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
    tool_error_response,
)

from ..brokering.broker_protocol import ApplyPatchToolArgs
from ..brokering.broker_structured_patch import (
    StructuredPatchError,
    apply_structured_workspace_patch,
    structured_patch_llm_feedback,
)

ARTIFACT_PATCH_TOOL_ID = "apply_patch"
PATH_OUTSIDE_FOLDER_ERROR_CODE = "PATH_OUTSIDE_ARTIFACT_FOLDER"


_LINES: JsonSchema = {"type": "array", "items": {"type": "string"}}
# ApplyPatchToolArgs, which validates each call again, as the model reads it.
_REQUEST_SCHEMA: JsonSchema = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
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
                        "required": ["op", "path", "new_lines", "trailing_newline"],
                        "properties": {
                            "op": {"const": "add"},
                            "path": {"type": "string", "minLength": 1},
                            "new_lines": _LINES,
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
                                        "before_lines": _LINES,
                                        "old_lines": {**_LINES, "minItems": 1},
                                        "new_lines": _LINES,
                                        "after_lines": _LINES,
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
}


class _PathOutsideFolder(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SuggestionArtifactPatch:
    # Canonical: a path the model names is compared with it as text.
    folder: Path

    def definition(self) -> ReactToolDefinition:
        return ReactToolDefinition(
            name=ARTIFACT_PATCH_TOOL_ID,
            description=(
                "Create, update or delete one file in this Suggestion's artifact "
                f"folder, `{self.folder}`, and nowhere else. Give the path "
                "relative to that folder. add: {op, path, new_lines, "
                "trailing_newline}; update: {op, path, edits} with exactly one "
                "edit whose old_lines are copied from the current file; delete: "
                "{op, path}. Returns the diff."
            ),
            request_schema=_REQUEST_SCHEMA,
            response_schema=react_tool_response_schema(
                success_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status", "path", "diff"],
                    "properties": {
                        "status": {"const": "success"},
                        "path": {"type": "string"},
                        "diff": {"type": "string"},
                    },
                }
            ),
            execute=self.run,
            concurrency=ToolConcurrency("sequential"),
        )

    def folder_relative(self, path: str) -> str:
        """``path`` relative to the folder; anything outside it is refused."""

        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            if not candidate.is_relative_to(self.folder):
                raise _PathOutsideFolder(f"{path} is outside the artifact folder")
            candidate = candidate.relative_to(self.folder)
        relative = str(candidate)
        try:
            SUGGESTION_ARTIFACTS_ADAPTER.validate_python([relative])
        except ValidationError as exc:
            raise _PathOutsideFolder(
                f"{path} is not a plain path inside the artifact folder"
            ) from exc
        return relative

    async def run(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        try:
            args = ApplyPatchToolArgs.model_validate(call.tool_args)
        except ValidationError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="INVALID_ARGUMENTS",
                message=str(exc)[:1200],
            )
        (change,) = args.changes
        try:
            relative = self.folder_relative(change.path)
        except _PathOutsideFolder as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=PATH_OUTSIDE_FOLDER_ERROR_CODE,
                message=str(exc),
            )
        try:
            diff = apply_structured_workspace_patch(
                patch_root=self.folder,
                path_rewrites={change.path: relative},
                changes=args.changes,
            )
        except StructuredPatchError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=exc.code,
                message=f"{exc}\n{structured_patch_llm_feedback(exc.code)}",
            )
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={"status": "success", "path": relative, "diff": diff},
        )


__all__ = ["ARTIFACT_PATCH_TOOL_ID", "SuggestionArtifactPatch"]

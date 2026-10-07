from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.utils.artifact_text_patch import ArtifactTextPatchError
from pantaray_agents.utils.structured_artifact_patch import (
    StructuredArtifactPatch,
    StructuredArtifactPatchError,
    parse_structured_artifact_patch_request,
)

from .artifact_patch_contract import (
    ARTIFACT_PATCH_TOOL_NAME,
    artifact_patch_request_schema,
    artifact_patch_response_schema,
)
from .commit import PatchCommitResult

type ArtifactPatchApplier = Callable[[str, StructuredArtifactPatch], str]
type ArtifactCommitter = Callable[
    [str, str, str, ReactToolCall, int], Awaitable[PatchCommitResult]
]

PATCH_APPLY_MESSAGE = (
    "Patch could not be applied. Retry with structured chunks that match the "
    "exact current document context."
)


@dataclass
class ArtifactPatchState:
    current_text: str


@dataclass(frozen=True)
class ArtifactPatchTool:
    state: ArtifactPatchState
    apply_patch: ArtifactPatchApplier
    commit_patch: ArtifactCommitter
    logical_path: str

    async def execute(self, call: ReactToolCall, step_number: int) -> ReactToolResult:
        try:
            patch = parse_structured_artifact_patch_request(call.tool_args)
            updated_text = self.apply_patch(self.state.current_text, patch)
        except ArtifactTextPatchError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="PATCH_APPLY_FAILED",
                message=PATCH_APPLY_MESSAGE,
                details=patch_error_details(exc),
            )

        commit_result = await self.commit_patch(
            self.state.current_text,
            updated_text,
            sha256_text(self.state.current_text),
            call,
            step_number,
        )
        self.state.current_text = commit_result.committed_text
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output=commit_result.to_tool_output(),
            final_step_recorded=True,
        )

    def definition(self) -> ReactToolDefinition:
        return ReactToolDefinition(
            name=ARTIFACT_PATCH_TOOL_NAME,
            description="Apply an artifact patch to the current document.",
            request_schema=artifact_patch_request_schema(
                logical_path=self.logical_path
            ),
            response_schema=artifact_patch_response_schema(),
            execute=self.execute,
        )


def patch_error_details(exc: ArtifactTextPatchError) -> dict[str, JSONValue]:
    details: dict[str, JSONValue] = {"reason": str(exc)}
    if isinstance(exc, StructuredArtifactPatchError):
        details.update(_sanitize_patch_error_details(exc.details))
    return details


def _sanitize_patch_error_details(
    raw_details: dict[str, JSONValue],
) -> dict[str, JSONValue]:
    sanitized: dict[str, JSONValue] = {}
    for key, value in raw_details.items():
        if key == "text":
            sanitized["text_omitted"] = True
            continue
        if key == "old_lines":
            sanitized["old_line_count"] = len(value) if isinstance(value, list) else 0
            sanitized["old_lines_omitted"] = True
            continue
        sanitized[key] = value
    return sanitized


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

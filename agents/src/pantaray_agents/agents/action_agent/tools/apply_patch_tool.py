"""Brokered workspace patch tool definition."""

from __future__ import annotations

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchToolArgs,
    apply_patch_tool_output_json_schema,
)

from .base import (
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

APPLY_PATCH_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="changes",
        description=(
            "Exactly one structured workspace file operation.\n"
            "- add: {op, path, new_lines, trailing_newline}.\n"
            "- update: {op, path, edits}; edits must contain exactly one edit "
            "that replaces old_lines with new_lines, using optional "
            "before_lines/after_lines only as nearby location hints.\n"
            "- delete: {op, path}.\n"
            "- path may be absolute or relative to the current workspace cwd; the resolved target must be writable.\n"
            "- Do not use patch DSL or unified diff text."
        ),
    ),
)

APPLY_PATCH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="apply_patch",
        name="Apply Patch",
        description=(
            "Apply a patch to one local file. "
            "Use this for direct workspace file creation, updates, and deletes. "
            "Provide structured file operations with exact old_lines/new_lines "
            "instead of patch DSL or unified diff text."
        ),
        guide=ToolGuideSpec(
            what=(
                "Use this to directly create, update, or delete workspace files with "
                "structured file operations.\n\n"
                "Use changes[] with exactly one add, update, or delete operation "
                "for one file. Update operations must contain exactly one edit "
                "location. Path values may be absolute or relative to the current workspace cwd. "
                "For update and delete, the first call may return status=needs_read "
                "without changing the file. Retry only after rebuilding the request "
                "from the returned text or windows[].text.\n\n"
                "For update changes, copy old_lines exactly from the current file "
                "for the lines that should be replaced or deleted. Put the "
                "replacement block in new_lines. Use new_lines=[] to delete "
                "old_lines. When old_lines is not unique, use before_lines and "
                "after_lines for nearby current-file context; those hint lines are "
                "not replaced and must not be repeated in new_lines.\n\n"
                "For long existing lines in old_lines, before_lines, or after_lines, "
                "you may abbreviate the middle as <OMITTED>. The prefix and suffix "
                "around <OMITTED> must still be copied from the same current-file "
                "line. In new_lines, <OMITTED> is literal output text.\n\n"
                "For add changes, put the new file content in new_lines and set "
                "trailing_newline explicitly."
            ),
            when=(
                "Use for one prepared file edit at a time. For known file content "
                "changes, fix and retry apply_patch instead of switching to bash "
                "or run_python."
            ),
            pitfalls=(
                "A path outside registered workspaces waits for the user to approve that one write; "
                "use one only when the user asked for that location. Use absolute paths or paths relative "
                "to the current workspace cwd. Success returns actual absolute applied_paths. For Update File, "
                "copy removed lines exactly from the current file into old_lines, "
                "including whitespace and punctuation. Use before_lines/after_lines "
                "for unchanged location context instead of putting unrelated "
                "context into old_lines. Include only edits that are relevant to the "
                "requested change; do not include unrelated edits or unrelated "
                "context. If status=needs_read, the patch was not applied; rebuild "
                "old_lines, before_lines, and after_lines only from the returned "
                "text or windows[].text. The read, grep, and glob tools are useful "
                "for exploration, but they do not satisfy this apply_patch snapshot "
                "gate. "
                "Do not provide patch DSL, unified diff text, line prefixes, or "
                "hunk headers. "
                "Do not use shell commands or run_python for direct known-content edits "
                "that can be represented as a patch."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="surgical_edit",
            required_capabilities=("scoped_write",),
            default_timeout_ms=None,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=ApplyPatchToolArgs,
            fields=APPLY_PATCH_TOOL_FIELD_PRESENTATION,
            description="Apply a workspace patch.",
        ),
        output_schema=apply_patch_tool_output_json_schema(),
    )
)

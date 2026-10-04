"""Brokered generated Python tool definition."""

from __future__ import annotations

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RunPythonToolArgs,
)

from .base import (
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .bash_tool import WRITE_FOLDER_REQUEST_FIELD_PRESENTATION
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

RUN_PYTHON_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="code",
        description=(
            "Generated Python source code to run for temporary computation "
            "or structured processing.\n"
            "- Keep it short and deterministic.\n"
            "- Network access follows the user's command network setting.\n"
            "- Do not use it for background processes.\n"
            "- Do not use it for direct known-content edits that should be "
            "represented as apply_patch."
        ),
    ),
    BrokerToolFieldPresentation(
        name="args",
        description=(
            "Optional argv values passed to the generated script.\n"
            "- Each value must be a string.\n"
            "- Keep data small enough to fit in the tool request."
        ),
    ),
    BrokerToolFieldPresentation(
        name="cwd",
        description=(
            "Optional workspace cwd.\n"
            "- Use the current workspace marker, an absolute local workspace "
            "path under Workspace Roots in Workspace Path Rules, or a "
            "subdirectory under a registered workspace.\n"
            "- A cwd outside registered workspaces waits for the user to "
            "approve that one call; use one only when the user asked for "
            "that location.\n"
            "- File paths used by the script should be inside registered "
            "workspaces or approved folders."
        ),
    ),
    *WRITE_FOLDER_REQUEST_FIELD_PRESENTATION,
)

RUN_PYTHON_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="run_python",
        name="Run Python",
        description=(
            "Run short, generated Python code through the local broker sandbox. "
            "Use this for temporary computation or structured workspace-scoped "
            "processing when read/apply_patch are not sufficient."
        ),
        guide=ToolGuideSpec(
            what=(
                "Executes generated Python with the app runtime Python inside the "
                "broker sandbox. Use tempfile for temporary files; this call's "
                "TMPDIR is removed when execution finishes.\n\n"
                "Input shape:\n"
                "- code contains one complete, short Python program.\n"
                "- args is optional argv data for that program.\n"
                "- cwd is optional; outside registered workspaces it waits for "
                "the user's approval."
            ),
            when=(
                "Use for temporary calculations or workspace-scoped file transforms. "
                "Use read/apply_patch for normal source inspection and direct file "
                "creation or edits. To write a folder outside the workspace, rerun "
                "with additional_write_folders and justification exactly as the "
                "bash tool describes."
            ),
            pitfalls=(
                "The code is approval-gated and sandboxed. Keep code minimal and "
                "deterministic. Network access follows the user's command network "
                "setting. Avoid long-running processes, background processes, and paths outside "
                "registered workspaces unless the user asked for that location. "
                "Do not use run_python as a fallback to "
                "bypass a rejected apply_patch; for direct known-content edits, "
                "fix the apply_patch input."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="process_exec_local",
            required_capabilities=("process_exec_local",),
            default_timeout_ms=30_000,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=RunPythonToolArgs,
            fields=RUN_PYTHON_TOOL_FIELD_PRESENTATION,
            description="Run generated Python in the broker sandbox.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
            "required": ["status", "exit_code", "stdout", "stderr"],
            "additionalProperties": False,
        },
    )
)

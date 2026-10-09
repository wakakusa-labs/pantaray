from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from pantaray_agents.tools.files.read_contract import (
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
    ReadToolArgs,
)
from pantaray_agents.tools.files.read_output import ReadToolOutput
from pantaray_agents.tools.files.render_pages import RenderPdfPageToolArgs

from .broker_protocol import (
    ApplyPatchToolArgs,
    ApplyPatchToolOutput,
    BashToolArgs,
    BashToolOutput,
    BrokerManagedToolDefinition,
    BrokerToolRegistry,
    GlobToolOutput,
    GrepToolOutput,
    ListToolOutput,
    RenderPdfPageOutput,
    RunPythonToolArgs,
    RunPythonToolOutput,
    ToolError,
)


class BrokerModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    input_models: dict[str, type[BaseModel]]
    output_models: dict[str, type[BaseModel]]
    error_models: dict[str, type[BaseModel]]


BROKER_MODEL_REGISTRY = BrokerModelRegistry(
    input_models={
        "ReadToolArgs": ReadToolArgs,
        "RenderPdfPageToolArgs": RenderPdfPageToolArgs,
        "ListToolArgs": ListToolArgs,
        "GlobToolArgs": GlobToolArgs,
        "GrepToolArgs": GrepToolArgs,
        "ApplyPatchToolArgs": ApplyPatchToolArgs,
        "BashToolArgs": BashToolArgs,
        "RunPythonToolArgs": RunPythonToolArgs,
    },
    output_models={
        "ReadToolOutput": ReadToolOutput,
        "RenderPdfPageOutput": RenderPdfPageOutput,
        "ListToolOutput": ListToolOutput,
        "GlobToolOutput": GlobToolOutput,
        "GrepToolOutput": GrepToolOutput,
        "ApplyPatchToolOutput": ApplyPatchToolOutput,
        "BashToolOutput": BashToolOutput,
        "RunPythonToolOutput": RunPythonToolOutput,
    },
    error_models={
        "ToolError": ToolError,
    },
)

BROKER_TOOL_REGISTRY = BrokerToolRegistry(
    definitions={
        "read": BrokerManagedToolDefinition(
            tool_id="read",
            execution_path="broker_direct_read",
            path_access_kind="read",
            input_model_ref="ReadToolArgs",
            output_model_ref="ReadToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_read"],
            approval_policy="none",
            resource_kinds=["none"],
        ),
        "render_pdf_page": BrokerManagedToolDefinition(
            tool_id="render_pdf_page",
            execution_path="broker_direct_render_pdf",
            path_access_kind="read",
            input_model_ref="RenderPdfPageToolArgs",
            output_model_ref="RenderPdfPageOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_read"],
            approval_policy="none",
            resource_kinds=["none"],
        ),
        "list": BrokerManagedToolDefinition(
            tool_id="list",
            execution_path="broker_direct_list",
            path_access_kind="read",
            input_model_ref="ListToolArgs",
            output_model_ref="ListToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_read"],
            approval_policy="none",
            resource_kinds=["none"],
        ),
        "glob": BrokerManagedToolDefinition(
            tool_id="glob",
            execution_path="broker_direct_glob",
            path_access_kind="read",
            input_model_ref="GlobToolArgs",
            output_model_ref="GlobToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_read"],
            approval_policy="none",
            resource_kinds=["none"],
        ),
        "grep": BrokerManagedToolDefinition(
            tool_id="grep",
            execution_path="broker_direct_grep",
            path_access_kind="read",
            input_model_ref="GrepToolArgs",
            output_model_ref="GrepToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_read"],
            approval_policy="none",
            resource_kinds=["none"],
        ),
        "apply_patch": BrokerManagedToolDefinition(
            tool_id="apply_patch",
            execution_path="broker_direct_patch",
            path_access_kind="write",
            input_model_ref="ApplyPatchToolArgs",
            output_model_ref="ApplyPatchToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["scoped_write"],
            approval_policy="prompt_each_time",
            resource_kinds=["lock"],
        ),
        "bash": BrokerManagedToolDefinition(
            tool_id="bash",
            execution_path="broker_sandbox_command",
            path_access_kind="exec",
            input_model_ref="BashToolArgs",
            output_model_ref="BashToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["process_exec_local"],
            approval_policy="prompt_each_time",
            resource_kinds=["process_group", "temp_dir"],
        ),
        "run_python": BrokerManagedToolDefinition(
            tool_id="run_python",
            execution_path="broker_sandbox_python",
            path_access_kind="exec",
            input_model_ref="RunPythonToolArgs",
            output_model_ref="RunPythonToolOutput",
            error_model_ref="ToolError",
            required_capabilities=["process_exec_local"],
            approval_policy="prompt_each_time",
            resource_kinds=["process_group", "temp_dir"],
        ),
    }
)


def validate_broker_registry() -> None:
    for tool_id, definition in BROKER_TOOL_REGISTRY.definitions.items():
        if definition.tool_id != tool_id:
            raise RuntimeError("broker tool registry key mismatch")
        if definition.input_model_ref not in BROKER_MODEL_REGISTRY.input_models:
            raise RuntimeError(f"missing input model ref: {definition.input_model_ref}")
        if definition.output_model_ref not in BROKER_MODEL_REGISTRY.output_models:
            raise RuntimeError(
                f"missing output model ref: {definition.output_model_ref}"
            )
        if definition.error_model_ref not in BROKER_MODEL_REGISTRY.error_models:
            raise RuntimeError(f"missing error model ref: {definition.error_model_ref}")

"""ActionAgent prompt rendering service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.common import (
    SUPERVISOR_SCOPE_HANDLE,
    get_history_for_scope,
    project_latest_history_entries,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    HistoryEntry,
)
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.local_runtime.artifacts.paths import resolve_artifact_path
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.record_context import (
    render_context_epoch,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    ReadAccessScope,
)

MEMORY_CONTEXT_MODEL_TEXT = """Memory sources describe the same user reality at different levels of organization and freshness.

- Stock knowledge: organized, durable context such as long-term insights, structured facts, and registered workspace context. Use it to understand projects, user preferences, recurring tendencies, accepted facts, constraints, and long-lived decisions. It may be stale.
- Flow knowledge: timestamped context such as activity descriptions, activity summaries, and short-term insights. Use it to understand recent behavior, current state, and evidence trails. It is fresher, but noisier and less organized.
- Agent work records: prior suggestions and actions. Use them to trace previous recommendations, execution results, and unresolved work, but do not treat them as direct observations unless backed by evidence.
- The same project, repository, document, person, issue, decision, or topic may appear across stock and flow memory. Search by shared anchors such as project names, repo paths, file paths, issue IDs, people, and domain terms. Reconcile stock and flow evidence before acting when context materially affects the answer or action."""

WORKSPACE_CONTEXT_RULES_TEXT = """- Use Workspace Context only as optional context when it helps interpret the task, repositories, project names, organization names, or local paths.
- Do not assume Workspace Context is complete. If no item matches the observed evidence, infer from the observed evidence instead."""

_PENDING_GOAL_EVIDENCE_REF_LIMIT = 20
_PENDING_GOAL_EVIDENCE_SUMMARY_MAX_CHARS = 160


@dataclass(frozen=True)
class PromptRenderingDeps:
    """Dependencies required by the prompt rendering service."""

    formatter: ActionAgentFormatter


class PromptRenderingService:
    """Prompt and display formatting for ActionAgent runtime."""

    def __init__(self, deps: PromptRenderingDeps) -> None:
        self._deps = deps

    def build_insight_text(self, profile_brief: str | None) -> str:
        return self._deps.formatter.build_insight_text(profile_brief)

    def render_workspace_context_prompt(
        self,
        state: ActionAgentState | None = None,
    ) -> str:
        if state is None:
            return ""
        context = state.get("context")
        if not isinstance(context, dict):
            return ""
        value = context.get("workspace_context_prompt")
        return value if isinstance(value, str) else ""

    def render_request_summary(self, state: ActionAgentState | None = None) -> str:
        if state is None:
            return "(none)"
        context = state.get("context")
        if not isinstance(context, dict):
            return "(none)"
        value = context.get("request_summary")
        if not isinstance(value, str) or not value.strip():
            return "(none)"
        return value.strip()

    def render_target_context(self, state: ActionAgentState | None = None) -> str:
        if state is None:
            return _format_target_context({})
        context = state.get("context")
        if not isinstance(context, dict):
            return _format_target_context({})
        raw_target_context = context.get("target_context")
        if not isinstance(raw_target_context, Mapping):
            return _format_target_context({})
        return _format_target_context(raw_target_context)

    def render_memory_context_model(self) -> str:
        return MEMORY_CONTEXT_MODEL_TEXT

    def render_workspace_context_rules(self) -> str:
        return WORKSPACE_CONTEXT_RULES_TEXT

    def format_memory_source_coverage(self, state: ActionAgentState) -> str:
        return self._deps.formatter.format_memory_source_coverage(state)

    def render_memory_artifact_references(self, state: ActionAgentState) -> str:
        references = state["memory_artifact_references"]
        if not references:
            return "No linked memory artifacts."
        artifact_root = read_local_runtime_artifact_root()
        lines: list[str] = []
        for reference in references:
            lines.append(
                f"- {reference.source_type} record={reference.source_record_id} "
                f"memory_key={reference.memory_key}"
            )
            for file in reference.files:
                path = resolve_artifact_path(
                    root_path=artifact_root,
                    relative_path=file.storage_path,
                )
                lines.append(
                    f"  - path={path} sha256={file.sha256} bytes={file.byte_size}"
                )
        return "\n".join(lines)

    def render_linkable_memory_context(self, state: ActionAgentState) -> str:
        epoch = state.get("memory_context_epoch")
        if epoch is None:
            return "N/A"
        return render_context_epoch(deserialize_memory_epoch(epoch))

    def history_entries(
        self,
        state: ActionAgentState,
        *,
        scope_handle: str = SUPERVISOR_SCOPE_HANDLE,
    ) -> list[HistoryEntry]:
        """The rows one scope shows the Supervisor, newest projection per ref."""

        return project_latest_history_entries(
            get_history_for_scope(state, scope_handle)
        )

    def format_history(
        self,
        state: ActionAgentState,
        *,
        scope_handle: str = SUPERVISOR_SCOPE_HANDLE,
        omit_before_step_number: int = 0,
    ) -> str:
        return self._deps.formatter.format_history(
            state,
            scope_handle=scope_handle,
            omit_before_step_number=omit_before_step_number,
        )

    def resolve_history_omission_boundary(
        self,
        state: ActionAgentState,
        *,
        scope_handle: str = SUPERVISOR_SCOPE_HANDLE,
        byte_budget: int,
        omit_before_step_number: int = 0,
    ) -> int:
        return self._deps.formatter.resolve_history_omission_boundary(
            state,
            scope_handle=scope_handle,
            byte_budget=byte_budget,
            omit_before_step_number=omit_before_step_number,
        )

    def render_workspace_path_contract(
        self,
        state: ActionAgentState | None = None,
    ) -> str:
        if state is None:
            return ""
        context = state.get("context")
        if not isinstance(context, dict):
            return ""
        return _render_workspace_path_contract(
            root_catalog=_string_context_value(context, "workspace_root_catalog"),
            read_access_scope=_read_access_scope_context_value(context),
        )


def _string_context_value(context: Mapping[str, JSONValue], key: str) -> str:
    value = context.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _format_target_context(raw_target_context: Mapping[str, object]) -> str:
    organization_name = _target_context_text(raw_target_context, "organization_name")
    project_name = _target_context_text(raw_target_context, "project_name")
    lines = [
        f"Organization: {organization_name or '(none)'}",
        f"Project: {project_name or '(none)'}",
    ]
    if organization_name or project_name:
        lines.extend(
            [
                "",
                "Treat Target Context as the execution scope.",
                (
                    "Do not broaden the task to other registered projects unless "
                    "the request or Suggestion Summary clearly requires it."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                "No specific organization or project was selected for this suggestion.",
            ]
        )
    return "\n".join(lines)


def _target_context_text(
    raw_target_context: Mapping[str, object], key: str
) -> str | None:
    value = raw_target_context.get(key)
    return value.strip() or None if isinstance(value, str) else None


def _render_workspace_path_contract(
    *,
    root_catalog: str,
    read_access_scope: ReadAccessScope,
) -> str:
    lines = ["# Workspace Path Rules"]
    if read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        lines.append(
            "Read/search paths (`read`, `list`, `glob`, `grep`): any local filesystem path is allowed."
        )
    else:
        lines.append(
            "Read/search paths (`read`, `list`, `glob`, `grep`): use Workspace Roots below or paths relative to the current workspace cwd."
        )
    lines.append(
        "Edit/command paths (`apply_patch`, `bash.cwd`, `run_python.cwd`): use Workspace Roots below or paths relative to the current workspace cwd."
    )
    lines.append(
        "An `apply_patch` path, `bash.cwd`, or `run_python.cwd` outside Workspace Roots waits for the user "
        "to approve that one call; use one only when the user asked for that location."
    )
    lines.append(
        "A `bash` or `run_python` call that must write a folder outside Workspace Roots names it in "
        "`additional_write_folders` with a `justification`, and waits for the user to approve that one call."
    )
    lines.append(
        "All relative paths use the same session cwd; bash.cwd/run_python.cwd override it for that call only. "
        "Absolute paths and '..' are valid when the resolved target has the required capability. "
        "Use apply_patch applied_paths to locate saved files; a relative path in scratch does not publish into a repository."
    )
    lines.append(
        "When the task works in a project, repository, folder, file, document, or URL, identify it from the request, Suggestion Summary, "
        "Target Context, conversation, recorded activity, and each candidate's git remote. "
        "If the evidence leaves more than one plausible target, or none, ask the user where to work with `draft_final_answer` "
        "before working in any location. Do not choose by guess or name similarity, and do not create a new project location on your own. "
        "Before editing a repository, verify its git root/branch."
    )
    if root_catalog:
        lines.append("")
        if root_catalog.startswith("# Workspace Roots"):
            lines.append(root_catalog)
        else:
            lines.append("# Workspace Roots")
            lines.append(root_catalog)
    return "\n".join(lines)


def _read_access_scope_context_value(
    context: Mapping[str, JSONValue],
) -> ReadAccessScope:
    value = context.get("read_access_scope")
    if value == READ_ACCESS_SCOPE_WORKSPACE:
        return READ_ACCESS_SCOPE_WORKSPACE
    if value == READ_ACCESS_SCOPE_FULL_ACCESS:
        return READ_ACCESS_SCOPE_FULL_ACCESS
    raise RuntimeError("ActionAgent context requires read_access_scope")

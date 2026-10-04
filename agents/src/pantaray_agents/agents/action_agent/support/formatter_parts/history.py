"""Chronological tool history with durable, output-only omission boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pantaray_agents.agents.action_agent.runtime.handlers.nodes import common
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    HistoryEntry,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    coerce_tool_attachments,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue

from .shared import HistoryDisplayEntry

_ATTACHMENT_DATA_URL_OMITTED = "<attachment data URL omitted>"
_BLOCK_SEPARATOR = "\n\n"


class HistoryFormattingMixin:
    def format_history(
        self,
        state: ActionAgentState,
        *,
        scope_handle: str = common.SUPERVISOR_SCOPE_HANDLE,
        omit_before_step_number: int = 0,
    ) -> str:
        """Keep every step; omit only tool outputs before the saved boundary."""
        blocks = [
            block
            for entry in _scope_history(state, scope_handle)
            if (
                block := self._format_history_entry(
                    entry,
                    omit_output=entry["step_number"] < omit_before_step_number,
                )
            )
        ]
        return _BLOCK_SEPARATOR.join(blocks) or "(no history)"

    def resolve_history_omission_boundary(
        self,
        state: ActionAgentState,
        *,
        scope_handle: str = common.SUPERVISOR_SCOPE_HANDLE,
        byte_budget: int,
        omit_before_step_number: int = 0,
    ) -> int:
        """Drop oldest batch outputs toward the budget, protecting the latest batch.

        Retained notes, arguments, requests and attachments may exceed the budget;
        the caller owns the total-input capacity check.
        """
        history = _scope_history(state, scope_handle)
        remaining_bytes = len(
            self.format_history(
                state,
                scope_handle=scope_handle,
                omit_before_step_number=omit_before_step_number,
            ).encode("utf-8")
        )
        boundary = omit_before_step_number
        for group in _tool_call_groups(history)[:-1]:
            if remaining_bytes <= byte_budget:
                break
            for entry in group:
                if entry["step_number"] < boundary:
                    continue
                full = self._format_history_entry(entry)
                omitted = self._format_history_entry(entry, omit_output=True)
                remaining_bytes -= len(full.encode("utf-8")) - len(
                    omitted.encode("utf-8")
                )
            boundary = max(boundary, group[-1]["step_number"] + 1)
        return boundary

    def _format_history_entry(
        self, entry: HistoryEntry, *, omit_output: bool = False
    ) -> str:
        display = self._build_history_display_entry(entry=entry)
        if display.step_type == StepType.LLM_OUTPUT and not display.result_line:
            # The note already rides on the TOOL row this THINK produced; only a
            # THINK that failed validation has something of its own to report.
            return ""
        lines = [f"### Step {display.step_number} [{display.phase}]"]
        if display.step_type == StepType.ASSISTANT_MESSAGE:
            phase_label = (
                f" (phase: {entry['assistant_phase']})"
                if "assistant_phase" in entry
                else ""
            )
            lines.append(
                f"- Assistant Message{phase_label}:\n"
                + self._indent_text(entry["assistant_message_text"], 4)
            )
        elif display.step_type == StepType.USER_REQUEST:
            lines.extend(self._format_user_request(display))
        else:
            lines.extend(self._format_note_lines(display))
        if display.tool_id:
            lines.append(f"- Tool: {display.tool_id}")
        if display.step_type == StepType.TOOL_EXECUTION:
            if display.args_present:
                lines += self._json_lines("Args", display.args_value)
            if display.output_present:
                if omit_output:
                    # Even null/empty results must shrink; the adjacent Ref explains retrieval.
                    lines.append("- Output: …")
                else:
                    lines += self._json_lines(
                        "Output", omit_attachment_data_urls(display.output_value)
                    )
            if agents_md := entry.get("agents_md"):
                # Each file is attached once per Action, so omission keeps it.
                lines.append("- AGENTS.md:\n" + self._indent_text(agents_md, 4))
        if display.attachment_refs:
            # File-input interleaving needs these refs even when output is omitted.
            lines.append("- Attached images: " + ", ".join(display.attachment_refs))
        if display.history_ref:
            lines.append(f"- History Ref: {display.history_ref} (use history_fetch)")
        lines.append(f"- Time: {display.started_at} → {display.completed_at}")
        return "\n".join(lines)

    def _format_user_request(self, entry: HistoryDisplayEntry) -> list[str]:
        request_text = entry.user_request_text
        if not request_text:
            return []
        return [f"- User Request:\n{self._indent_text(request_text, 4)}"]

    def _build_history_display_entry(
        self,
        *,
        entry: HistoryEntry,
    ) -> HistoryDisplayEntry:
        short_step_id = entry.get("short_step_id")
        history_ref = (
            short_step_id if isinstance(short_step_id, str) and short_step_id else None
        )
        return HistoryDisplayEntry(
            step_number=int(entry.get("step_number", 0)),
            phase=str(entry.get("phase", "unknown")),
            step_type=entry["step_type"],
            user_request_text=entry.get("user_request_text"),
            tool_id=entry.get("tool_id"),
            result_line=entry.get("result_line"),
            args_value=entry.get("args"),
            args_present="args" in entry,
            summary=entry.get("summary"),
            output_present="output" in entry,
            output_value=entry.get("output"),
            history_ref=history_ref,
            started_at=str(entry.get("started_at", "")),
            completed_at=str(entry.get("completed_at", "")),
            attachment_refs=tuple(
                attachment["ref"]
                for attachment in coerce_tool_attachments(entry.get("attachments"))
            ),
        )

    def _format_note_lines(self, entry: HistoryDisplayEntry) -> list[str]:
        lines: list[str] = []
        if entry.summary:
            lines.append(f"- Note: {entry.summary}")
        if entry.result_line:
            lines.append(f"- Result: {entry.result_line}")
        return lines

    def _json_lines(self, label: str, value: JSONValue) -> list[str]:
        # The tool-result boundary already bounds output and retains continuation.
        text = json.dumps(value, ensure_ascii=False, indent=2)
        return [f"- {label}:\n{self._indent_text(text, 4)}"]

    def _indent_text(self, text: str, spaces: int) -> str:
        indent = " " * spaces
        return "\n".join(indent + line for line in text.split("\n"))


def _scope_history(state: ActionAgentState, scope_handle: str) -> list[HistoryEntry]:
    return common.project_latest_history_entries(
        common.get_history_for_scope(state, scope_handle)
    )


def _tool_call_groups(history: Sequence[HistoryEntry]) -> list[list[HistoryEntry]]:
    """A THINK batch is the tool rows between successive non-tool rows."""
    groups: list[list[HistoryEntry]] = []
    entries: list[HistoryEntry] = []
    for entry in history:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entries.append(entry)
        elif entries:
            groups.append(entries)
            entries = []
    if entries:
        groups.append(entries)
    return groups


def omit_attachment_data_urls(value: JSONValue | Mapping[str, JSONValue]) -> JSONValue:
    if isinstance(value, Mapping):
        return {
            key: (
                _ATTACHMENT_DATA_URL_OMITTED
                if key == "url" and _is_data_url(item)
                else omit_attachment_data_urls(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [omit_attachment_data_urls(item) for item in value]
    return value


def _is_data_url(value: JSONValue) -> bool:
    return isinstance(value, str) and value.startswith("data:")

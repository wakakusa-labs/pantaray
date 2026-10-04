"""Action terminal/event support for ``MockSuggestionAgentRepository``."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ..action_status import ACTION_STATUS_SUCCESS, FinalizeActionTerminalCommand
from ..repositories.suggestion_runtime_results import (
    AppendProcessEventResult,
    FinalizeActionTerminalAndProjectHistoryResult,
)
from ..schema.repositories.repository import JSONValue, RepositoryResult


class MockSuggestionAgentActionRuntimeMixin:
    """Mock the remaining Suggestion-owned Action event projections."""

    data: dict[str, list[dict[str, Any]]]
    _process_event_sequences: dict[str, int]

    async def append_process_event_and_project_history(
        self,
        *,
        event_id: str,
        suggestion_id: str | None,
        user_id: str,
        event_name: str,
        payload: dict[str, JSONValue],
        action_id: str | None = None,
    ) -> RepositoryResult[AppendProcessEventResult]:
        existing = await self.get_data("agent_process_events", "event_id", event_id)
        existing_row = existing.data if isinstance(existing.data, dict) else None
        if existing_row is not None:
            sequence = existing_row.get("sequence")
            if (
                existing_row.get("suggestion_id") != suggestion_id
                or existing_row.get("action_id") != action_id
                or existing_row.get("user_id") != user_id
                or existing_row.get("event_name") != event_name
                or existing_row.get("payload") != payload
            ):
                return RepositoryResult(
                    error=(
                        "mock append_process_event_and_project_history: "
                        "mismatched duplicate event_id"
                    )
                )
            if isinstance(sequence, int):
                return RepositoryResult(
                    data=AppendProcessEventResult(sequence=sequence, inserted=False)
                )
            return RepositoryResult(
                error="mock append_process_event_and_project_history: invalid existing sequence"
            )

        sequence_owner = suggestion_id or action_id
        if sequence_owner is None:
            return RepositoryResult(
                error="Action or Suggestion event owner is required"
            )
        next_sequence = self._process_event_sequences.get(sequence_owner, 0) + 1
        self._process_event_sequences[sequence_owner] = next_sequence
        await self.save_data(
            "agent_process_events",
            {
                "event_id": event_id,
                "suggestion_id": suggestion_id,
                "action_id": action_id,
                "user_id": user_id,
                "sequence": next_sequence,
                "event_name": event_name,
                "payload": payload,
                "created_at": now_utc_iso(),
            },
        )
        return RepositoryResult(
            data=AppendProcessEventResult(sequence=next_sequence, inserted=True)
        )

    async def finalize_action_terminal_and_project_history(
        self,
        *,
        command: FinalizeActionTerminalCommand,
    ) -> RepositoryResult[FinalizeActionTerminalAndProjectHistoryResult]:
        suggestion_row = None
        if command.suggestion_id is not None:
            suggestion_row = next(
                (
                    row
                    for row in self.data.setdefault("suggestions", [])
                    if row.get("suggestion_id") == command.suggestion_id
                    and row.get("user_id") == command.user_id
                ),
                None,
            )
            if suggestion_row is None:
                return RepositoryResult(error="Suggestion not found")

        action_row = next(
            (
                row
                for row in self.data.setdefault("actions", [])
                if row.get("action_id") == command.action_id
                and row.get("user_id") == command.user_id
            ),
            None,
        )
        if action_row is None:
            return RepositoryResult(error="Action not found")

        staged_action_row = deepcopy(action_row)
        staged_suggestion_row = (
            deepcopy(suggestion_row) if suggestion_row is not None else None
        )
        if staged_suggestion_row is not None:
            staged_suggestion_row.update(
                {
                    "accepted_at": command.accepted_at,
                    "action_command_id": command.command_id,
                    "action_process_id": command.process_id,
                    "action_status": command.action_status,
                    "action_failure_code": command.failure_code,
                    "action_failure_stage": command.failure_stage,
                    "action_failure_message_public": command.failure_message_public,
                    "updated_at": command.completed_at,
                }
            )

        staged_action_row["status"] = command.action_status
        staged_action_row["updated_at"] = command.completed_at
        for field_name in (
            "final_output",
            "final_prompt_text",
            "prompt_name",
            "prompt_version",
            "total_steps",
            "total_llm_steps",
            "total_tool_steps",
            "total_prompt_tokens",
            "total_completion_tokens",
        ):
            value = getattr(command, field_name)
            if value is not None:
                staged_action_row[field_name] = value
        if command.failure_code is not None:
            staged_action_row["error"] = {
                "error_code": command.failure_code,
                "error_type": "runtime_error",
                "error_message": command.failure_message_public or "",
                "severity": "error",
            }
        elif command.action_status == ACTION_STATUS_SUCCESS:
            staged_action_row["error"] = None

        completed_result = await self.append_process_event_and_project_history(
            event_id=command.process_completed_event_id,
            suggestion_id=command.suggestion_id,
            user_id=command.user_id,
            event_name="process_completed",
            payload=command.process_completed_payload,
            action_id=command.action_id,
        )
        if completed_result.error or completed_result.data is None:
            return RepositoryResult(
                error=completed_result.error or "process_completed append failed"
            )

        if suggestion_row is not None and staged_suggestion_row is not None:
            suggestion_row.update(staged_suggestion_row)
        action_row.update(staged_action_row)
        return RepositoryResult(
            data=FinalizeActionTerminalAndProjectHistoryResult(
                process_completed_sequence=completed_result.data.sequence,
                action_status=command.action_status,
                action_failure_code=command.failure_code,
                final_output=command.final_output,
                failure_stage=command.failure_stage,
                failure_message_public=command.failure_message_public,
            )
        )

    async def get_process_events_for_detail(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> RepositoryResult[list[dict[str, Any]]]:
        rows = [
            row
            for row in (self.data.get("agent_process_events", []) or [])
            if row.get("user_id") == user_id
            and row.get("suggestion_id") == suggestion_id
        ]
        rows.sort(
            key=lambda row: (
                row.get("sequence") if isinstance(row.get("sequence"), int) else 0
            )
        )
        return RepositoryResult(data=rows)

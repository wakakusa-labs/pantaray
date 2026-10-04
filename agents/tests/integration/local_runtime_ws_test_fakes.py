from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pantaray_agents.mock.mock_suggestion_agent_repository import (
    MockSuggestionAgentRepository,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.utils.prompt_loader import PromptConfig, PromptLoader


def load_test_prompt_config(prompt_name: str) -> PromptConfig:
    if prompt_name == "activity_description":
        return PromptConfig(
            prompt=(
                "activity task\n"
                "period={period_start}..{period_end}\n"
                "count={screenshot_count}\n"
                "{screen_captures}\n"
            ),
            system_instruction=None,
        )
    if prompt_name == "suggestion/suggestion":
        return PromptConfig(
            prompt=(
                "suggestion task\n"
                "{short_term_insight}\n"
                "{reconsideration_reason}\n"
                "{context_density_signal}\n"
                "{recent_activity_descriptions}\n"
            ),
            system_instruction=None,
        )
    if prompt_name == "suggestion/suggestion_writer":
        return PromptConfig(
            prompt="suggestion writer\n{kind}\n{key_point}\n",
            system_instruction="Write one message in {answer_language}.",
        )
    if prompt_name in (
        "suggestion/suggestion_lenses",
        "suggestion/suggestion_selector",
    ):
        return PromptLoader().load_config(prompt_name)
    raise AssertionError(f"Unexpected prompt requested in E2E test: {prompt_name}")


class LocalWsSuggestionRepository(MockSuggestionAgentRepository):
    def __init__(self, *, db_path: Path) -> None:
        super().__init__()
        self._db_path = db_path

    async def create_processing_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        created_at: str | None = None,
    ) -> RepositoryResult[dict[str, object]]:
        existing = await self.get_suggestion(
            user_id=user_id, suggestion_id=suggestion_id
        )
        if isinstance(existing.data, dict) and existing.data:
            return RepositoryResult(data=dict(existing.data))
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        row = {
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "status": "processing",
            "created_at": created_at or now_iso,
            "updated_at": now_iso,
            "answer": None,
            "thinking": None,
            "prompt_text": None,
            "response_text": None,
            "prompt_name": None,
            "prompt_version": None,
            "has_suggestion": None,
            "request_images_count": 0,
            "used_images_count": 0,
            "interaction_contract": None,
            "error": None,
            "user_reaction": None,
            "accepted_at": None,
            "rejected_at": None,
            "action_status": None,
            "action_failure_code": None,
            "action_failure_stage": None,
            "action_failure_message_public": None,
            "action_request_payload": None,
            "action_process_id": None,
            "action_id": None,
            "action_command_id": None,
            "action_started_at": None,
            "process_event_sequence": 0,
        }
        await self.save_data("suggestions", row)
        self._upsert_local_suggestion_row(row)
        return RepositoryResult(data=row)

    async def get_suggestion(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> RepositoryResult[dict[str, Any]]:
        """Read the Suggestion row, falling back to the local runtime table.

        The runtime's short Insight transaction creates the `agent_suggestions`
        row directly, so a Suggestion this repository never wrote still exists.
        """
        result = await super().get_suggestion(
            user_id=user_id, suggestion_id=suggestion_id
        )
        if isinstance(result.data, dict) and result.data:
            return result
        return RepositoryResult(
            data=self._read_local_suggestion_row(
                user_id=user_id,
                suggestion_id=suggestion_id,
            )
        )

    def _read_local_suggestion_row(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> dict[str, Any] | None:
        with sqlite3.connect(self._db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM agent_suggestions WHERE user_id = ? AND suggestion_id = ?",
                (user_id, suggestion_id),
            ).fetchone()
        if row is None:
            return None
        stored = dict(row)
        has_suggestion = stored.get("has_suggestion")
        stored["has_suggestion"] = (
            None if has_suggestion is None else bool(has_suggestion)
        )
        error_json = stored.get("error")
        stored["error"] = json.loads(error_json) if error_json else None
        return stored

    async def finalize_suggestion_start_error_if_processing(
        self,
        *,
        user_id: str,
        suggestion_id: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RepositoryResult[object]:
        current = await self.get_suggestion(
            user_id=user_id, suggestion_id=suggestion_id
        )
        row = current.data if isinstance(current.data, dict) else None
        if row is None:
            return RepositoryResult(
                data=type(
                    "FinalizeSuggestionStartErrorResult",
                    (),
                    {"outcome": "not_found", "row": None},
                )()
            )
        if str(row.get("status") or "").strip().lower() != "processing":
            return RepositoryResult(
                data=type(
                    "FinalizeSuggestionStartErrorResult",
                    (),
                    {"outcome": "already_terminal", "row": dict(row)},
                )()
            )
        row.update(
            {
                "status": "error",
                "answer": "",
                "has_suggestion": False,
                "interaction_contract": None,
                "prompt_name": "suggestion",
                "prompt_version": "1.0",
                "prompt_text": "",
                "response_text": "",
                "error": {
                    "error_code": error_code,
                    "error_message": error_message,
                    "error_details": error_details,
                    "metadata": metadata,
                },
            }
        )
        await self.save_data("suggestions", row)
        self._upsert_local_suggestion_row(row)
        return RepositoryResult(
            data=type(
                "FinalizeSuggestionStartErrorResult",
                (),
                {"outcome": "updated", "row": dict(row)},
            )()
        )

    async def save_suggestion(
        self,
        suggestion: Any,
        prompt_name: str,
        prompt_version: str,
        prompt_text: str | None = None,
        response_text: str | None = None,
        request_images_count: int = 0,
        used_images_count: int = 0,
    ) -> RepositoryResult[dict[str, Any]]:
        result = await super().save_suggestion(
            suggestion=suggestion,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            prompt_text=prompt_text,
            response_text=response_text,
            request_images_count=request_images_count,
            used_images_count=used_images_count,
        )
        row = result.data if isinstance(result.data, dict) else None
        if row is not None:
            self._upsert_local_suggestion_row(row)
        return result

    def _upsert_local_suggestion_row(self, row: dict[str, Any]) -> None:
        created_at = str(row.get("created_at") or datetime.now(UTC).isoformat())
        updated_at = str(row.get("updated_at") or created_at)
        status = str(row.get("status") or "processing").strip().lower()
        has_suggestion = row.get("has_suggestion")
        has_suggestion_value = (
            None if has_suggestion is None else int(bool(has_suggestion))
        )
        error_payload = row.get("error")
        error_json = json.dumps(error_payload) if error_payload is not None else None
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    thinking,
                    error,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    has_suggestion,
                    request_images_count,
                    used_images_count,
                    interaction_contract,
                    user_reaction,
                    accepted_at,
                    rejected_at,
                    action_status,
                    action_failure_code,
                    action_failure_stage,
                    action_failure_message_public,
                    action_request_payload,
                    action_process_id,
                    action_command_id,
                    action_started_at,
                    process_event_sequence,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(suggestion_id) DO UPDATE SET
                    status = excluded.status,
                    answer = excluded.answer,
                    thinking = excluded.thinking,
                    error = excluded.error,
                    prompt_text = excluded.prompt_text,
                    response_text = excluded.response_text,
                    prompt_name = excluded.prompt_name,
                    prompt_version = excluded.prompt_version,
                    has_suggestion = excluded.has_suggestion,
                    request_images_count = excluded.request_images_count,
                    used_images_count = excluded.used_images_count,
                    interaction_contract = excluded.interaction_contract,
                    user_reaction = excluded.user_reaction,
                    accepted_at = excluded.accepted_at,
                    rejected_at = excluded.rejected_at,
                    action_status = excluded.action_status,
                    action_failure_code = excluded.action_failure_code,
                    action_failure_stage = excluded.action_failure_stage,
                    action_failure_message_public = excluded.action_failure_message_public,
                    action_request_payload = excluded.action_request_payload,
                    action_process_id = excluded.action_process_id,
                    action_command_id = excluded.action_command_id,
                    action_started_at = excluded.action_started_at,
                    updated_at = excluded.updated_at
                """,
                (
                    str(row["suggestion_id"]),
                    str(row["user_id"]),
                    status,
                    row.get("answer"),
                    row.get("thinking"),
                    error_json,
                    row.get("prompt_text"),
                    row.get("response_text"),
                    row.get("prompt_name"),
                    row.get("prompt_version"),
                    has_suggestion_value,
                    int(row.get("request_images_count") or 0),
                    int(row.get("used_images_count") or 0),
                    row.get("interaction_contract"),
                    row.get("user_reaction"),
                    row.get("accepted_at"),
                    row.get("rejected_at"),
                    row.get("action_status"),
                    row.get("action_failure_code"),
                    row.get("action_failure_stage"),
                    row.get("action_failure_message_public"),
                    row.get("action_request_payload"),
                    row.get("action_process_id"),
                    row.get("action_command_id"),
                    row.get("action_started_at"),
                    created_at,
                    updated_at,
                ),
            )

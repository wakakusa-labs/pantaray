"""MockSuggestionAgentRepository の分離定義。"""

import uuid
from datetime import datetime
from typing import Any

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ..repositories.runtime_ports import PatchRunStepKind, PatchRunStepStatus
from ..schema.agent.base import JSONValue
from ..schema.repositories.repository import DBRow, RepositoryResult
from .mock_repository import MockRepository
from .mock_suggestion_agent_action_runtime import (
    MockSuggestionAgentActionRuntimeMixin,
)


class MockSuggestionAgentRepository(
    MockSuggestionAgentActionRuntimeMixin, MockRepository
):
    """サジェスチョンエージェント用のモックリポジトリ"""

    def __init__(self) -> None:
        super().__init__()
        self._next_save_suggestion_error: str | None = None
        self._process_event_sequences: dict[str, int] = {}

    def set_next_save_suggestion_error(self, error_message: str) -> None:
        """次回のsave_suggestion呼び出しでエラーを発生させる

        Args:
            error_message (str): エラーメッセージ
        """
        self._next_save_suggestion_error = error_message

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
        """サジェスチョンレスポンスを保存する。"""

        if self._next_save_suggestion_error:
            error_msg = self._next_save_suggestion_error
            self._next_save_suggestion_error = None
            raise ConnectionError(error_msg)

        if hasattr(suggestion, "model_dump"):
            record = suggestion.model_dump()
        else:
            record = dict(suggestion)

        record.setdefault("suggestion_id", str(uuid.uuid4()))
        record.setdefault("created_at", now_utc_iso())
        record["prompt_text"] = prompt_text
        record["response_text"] = response_text
        record["request_images_count"] = request_images_count
        record["used_images_count"] = used_images_count
        record["prompt_name"] = prompt_name
        record["prompt_version"] = prompt_version
        record["user_reaction"] = None
        record["accepted_at"] = None
        record["rejected_at"] = None
        record["action_status"] = None
        record["action_failure_code"] = None
        record["action_command_id"] = None
        record["action_process_id"] = None
        record["action_id"] = None
        record["action_started_at"] = None
        record["action_request_payload"] = None
        record["reserved_process_id"] = None
        record["reserved_action_id"] = None

        await self.save_data("suggestions", record)
        return RepositoryResult(data=record)

    async def save_suggestion_run_step(
        self,
        *,
        suggestion_id: str,
        step_number: int,
        step_kind: PatchRunStepKind,
        status: PatchRunStepStatus,
        llm_prompt_text: str | None = None,
        llm_response_text: str | None = None,
        tool_name: str | None = None,
        tool_call_envelope: JSONValue = None,
        tool_output: JSONValue = None,
        error_code: str | None = None,
        error_message: str | None = None,
        created_at: str | None = None,
    ) -> RepositoryResult[DBRow]:
        record: DBRow = {
            "suggestion_id": suggestion_id,
            "step_number": step_number,
            "step_kind": step_kind,
            "status": status,
            "llm_prompt_text": llm_prompt_text,
            "llm_response_text": llm_response_text,
            "tool_name": tool_name,
            "tool_call_envelope": tool_call_envelope,
            "tool_output": tool_output,
            "error_code": error_code,
            "error_message": error_message,
            "created_at": created_at or now_utc_iso(),
        }
        rows = self.data.setdefault("suggestion_run_steps", [])
        rows[:] = [
            row
            for row in rows
            if not (
                row.get("suggestion_id") == suggestion_id
                and row.get("step_number") == step_number
            )
        ]
        rows.append(record)
        return RepositoryResult(data=record)

    async def get_suggestion(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, Any]]:
        """サジェスチョンを取得する

        Args:
            user_id (str): ユーザーID（所有検証に使用）。
            suggestion_id (str): 取得するサジェスチョンID

        Returns:
            RepositoryResult[dict[str, Any]]: 取得したサジェスチョンデータを含むレスポンス
        """
        res = await self.get_data("suggestions", "suggestion_id", suggestion_id)
        row = res.data if isinstance(res.data, dict) else None
        if row is None:
            return res
        if str(row.get("user_id") or "") != str(user_id or ""):
            return RepositoryResult(data=None)
        state = dict(row)
        action_row = next(
            (
                action
                for action in self.data.get("actions", [])
                if action.get("suggestion_id") == suggestion_id
                and action.get("user_id") == user_id
            ),
            None,
        )
        state["action_id"] = action_row.get("action_id") if action_row else None
        state["latest_public_event_sequence"] = self._process_event_sequences.get(
            suggestion_id, 0
        )
        return RepositoryResult(data=state)

    async def get_suggestion_history_row(
        self, *, user_id: str, suggestion_id: str
    ) -> RepositoryResult[dict[str, Any]]:
        """overlay/history 用の read model を現在状態から合成する。"""

        suggestion_res = await self.get_suggestion(
            user_id=user_id,
            suggestion_id=suggestion_id,
        )
        suggestion_row = (
            suggestion_res.data if isinstance(suggestion_res.data, dict) else None
        )
        if suggestion_row is None:
            return RepositoryResult(data=None)

        action_id = suggestion_row.get("action_id")
        action_row = None
        if isinstance(action_id, str) and action_id:
            action_row = next(
                (
                    row
                    for row in self.data.get("actions", [])
                    if str(row.get("action_id") or "") == action_id
                    and str(row.get("user_id") or "") == str(user_id or "")
                ),
                None,
            )

        event_rows = [
            row
            for row in self.data.get("agent_process_events", [])
            if str(row.get("suggestion_id") or "") == str(suggestion_id or "")
            and str(row.get("user_id") or "") == str(user_id or "")
        ]
        last_sequence = max(
            (
                int(row.get("sequence"))
                for row in event_rows
                if isinstance(row.get("sequence"), int)
            ),
            default=None,
        )

        history_row = {
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "answer": suggestion_row.get("answer"),
            "interaction_contract": suggestion_row.get("interaction_contract"),
            "user_reaction": suggestion_row.get("user_reaction"),
            "accepted_at": suggestion_row.get("accepted_at"),
            "rejected_at": suggestion_row.get("rejected_at"),
            "action_status": suggestion_row.get("action_status"),
            "action_failure_code": suggestion_row.get("action_failure_code"),
            "action_failure_stage": suggestion_row.get("action_failure_stage"),
            "action_failure_message_public": suggestion_row.get(
                "action_failure_message_public"
            ),
            "action_id": suggestion_row.get("action_id"),
            "final_output": action_row.get("final_output") if action_row else None,
            "action_updated_at": action_row.get("updated_at") if action_row else None,
            "suggestion_updated_at": suggestion_row.get("updated_at")
            or suggestion_row.get("created_at"),
            "last_sequence": last_sequence,
        }
        return RepositoryResult(data=history_row)

    async def get_latest_insights(
        self, user_id: str, limit: int = 5
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """ユーザーの最新インサイトを取得する

        Args:
            user_id (str): ユーザーID
            limit (int, optional): 取得する最大件数. デフォルトは5.

        Returns:
            RepositoryResult[list[dict[str, Any]]]: 取得したインサイトデータを含むレスポンス
        """
        return await self.get_latest_data("insights", "user_id", user_id, limit)

    async def get_recent_suggestions(
        self, user_id: str, days: int = 7, limit: int = 20
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """ユーザーの提案履歴を簡易的に取得する。"""

        suggestions = [
            row
            for row in self.data.get("suggestions", [])
            if row.get("user_id") == user_id and row.get("has_suggestion", True)
        ]

        if not suggestions:
            return RepositoryResult(data=[])

        def _created_at(row: dict[str, Any]) -> datetime:
            value = row.get("created_at")
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    pass
            return datetime.min

        suggestions.sort(key=_created_at, reverse=True)
        limited = suggestions[:limit]

        normalized: list[dict[str, Any]] = []
        for item in limited:
            created_at = item.get("created_at")
            if isinstance(created_at, datetime):
                created_at = created_at.isoformat()
            normalized.append(
                {
                    "answer": item.get("answer", ""),
                    "thinking": item.get("thinking"),
                    "created_at": created_at,
                    "user_id": item.get("user_id"),
                }
            )

        return RepositoryResult(data=normalized)

    async def get_recent_suggestions_for_prompt(
        self, user_id: str, *, days: int = 3, limit: int = 50
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """プロンプト用に直近期の提案履歴を整形する。"""

        base = await self.get_recent_suggestions(user_id, days=days, limit=limit)
        if base.error:
            return base

        shaped: list[dict[str, Any]] = []
        for idx, item in enumerate(base.data or []):
            entry = {
                "answer": item.get("answer", ""),
                "created_at": item.get("created_at", ""),
            }
            thinking = item.get("thinking")
            if idx < 2 and thinking:
                entry["thinking"] = thinking
            shaped.append(entry)

        return RepositoryResult(data=shaped)

    async def get_recent_activity_descriptions(
        self, user_id: str, limit: int = 3
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """直近の Activity Descriptions を返す（モック）。

        本番は `activity_descriptions` テーブル相当。
        モックでは `self.data["activity_descriptions"]` を参照し、無ければ空配列。
        """

        rows = [
            row
            for row in (self.data.get("activity_descriptions", []) or [])
            if row.get("user_id") == user_id
            and (row.get("status") in (None, "success"))
        ]
        rows.sort(key=lambda r: r.get("created_at"), reverse=True)
        return RepositoryResult(data=rows[:limit])

    async def get_recent_activity_summary_1h(
        self, user_id: str, limit: int = 1
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """直近の 1h Activity Summary を返す（モック）。"""
        return await self.get_recent_activity_summary(
            user_id, summary_type="1h", limit=limit
        )

    async def get_recent_activity_summary(
        self, user_id: str, *, summary_type: str, limit: int = 1
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """指定種別の直近 Activity Summary を返す（モック）。

        - 本番は `activity_summaries` テーブル相当。
        - モックでは `self.data["activity_summaries"]` を参照し、無ければ空配列。
        """

        target_types: tuple[str, ...]
        if summary_type == "1h":
            # 互換: 旧データが "60m" を使うケースを吸収
            target_types = ("1h", "60m")
        else:
            target_types = (summary_type,)

        rows = [
            row
            for row in (self.data.get("activity_summaries", []) or [])
            if row.get("user_id") == user_id
            and row.get("summary_type") in target_types
            and (row.get("status") in (None, "success"))
        ]
        rows.sort(
            key=lambda r: r.get("period_end") or r.get("created_at"), reverse=True
        )
        return RepositoryResult(data=rows[:limit])

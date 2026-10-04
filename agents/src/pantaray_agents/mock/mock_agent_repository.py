"""モックエージェントリポジトリクラス"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ..schema.agent.base import StatusType
from ..schema.repositories.repository import RepositoryResult
from .mock_action_agent_repository import MockActionAgentRepository
from .mock_repository import MockRepository
from .mock_suggestion_agent_repository import MockSuggestionAgentRepository

__all__ = [
    "MockActionAgentRepository",
    "MockInsightAgentRepository",
    "MockSuggestionAgentRepository",
]


def parse_mock_iso_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("mock timestamp must be a non-empty ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("mock timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


class MockInsightAgentRepository(MockRepository):
    """インサイトエージェント用のモックリポジトリ"""

    async def save_insight_response(
        self,
        response: Any,
        prompt_name: str,
        prompt_version: str,
        source_activity_summary_id: str | None,
        prompt_text: str | None = None,
        response_text: str | None = None,
        memory_draft: object | None = None,
    ) -> RepositoryResult[dict[str, Any]]:
        """InsightAgentResponse 風のデータを保存する。"""

        del memory_draft

        created_at = getattr(response, "created_at", datetime.now(UTC))
        if not isinstance(created_at, datetime):
            try:
                created_at = datetime.fromisoformat(
                    str(created_at).replace("Z", "+00:00")
                )
            except ValueError:
                created_at = datetime.now(UTC)

        record = {
            "insight_id": getattr(response, "insight_id", str(uuid.uuid4())),
            "short_term_insight_data": getattr(response, "short_term_insight_data", ""),
            "thinking": getattr(response, "thinking", None),
            "facts": getattr(response, "facts", ""),
            "user_id": getattr(response, "user_id", "mock-user"),
            "source_activity_summary_id": source_activity_summary_id,
            "prompt_text": prompt_text,
            "response_text": response_text,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "created_at": created_at.isoformat(),
            "updated_at": now_utc_iso(),
            "status": getattr(response, "status", StatusType.SUCCESS),
            "error": None,
            "insight_update_id": None,
        }

        error_obj = getattr(response, "error", None)
        if error_obj is not None and hasattr(error_obj, "model_dump"):
            record["error"] = error_obj.model_dump()

        await self.save_data("insights", record)
        return RepositoryResult(data=record)

    async def save_insight(
        self,
        insight_id: str,
        current_insight_data: str,
        insight_data: str,
        thinking: str | None,
        facts: str,
        created_at: datetime,
        user_id: str | None = None,
        suggestion_id: str | None = None,
        error: dict[str, Any] | None = None,
        status: StatusType = StatusType.SUCCESS,
    ) -> None:
        """インサイトを保存する

        Args:
            insight_id (str): インサイトのID
            current_insight_data (str): 生成されたインサイトデータ
            insight_data (str): 最終的なインサイトデータ（更新時に使用）
            thinking (str | None): 思考プロセス
            facts (str): 事実分析
            created_at (datetime): 作成日時
            user_id (Optional[str], optional): ユーザーID. デフォルトはNone.
            suggestion_id (Optional[str], optional): サジェスチョンID. デフォルトはNone.
            error (Optional[dict[str, Any]], optional): エラー情報. デフォルトはNone.
            status (StatusType, optional): 処理ステータス. デフォルトはStatusType.SUCCESS.
        """
        # 空文字列ではなく空白スペースを入れて、"データがありません"エラーを回避
        if insight_data == "":
            insight_data = " "
        if current_insight_data == "":
            current_insight_data = " "

        data = {
            "insight_id": insight_id,
            "current_insight_data": current_insight_data,
            "insight_data": insight_data,
            "thinking": thinking,
            "facts": facts,
            "created_at": created_at,
            "user_id": user_id,
            "suggestion_id": suggestion_id,
            "error": error,
            "status": status,
        }
        await self.save_data("insights", data)

    async def get_insight(self, insight_id: str) -> RepositoryResult[dict[str, Any]]:
        """インサイトを取得する

        Args:
            insight_id (str): 取得するインサイトID

        Returns:
            RepositoryResult[dict[str, Any]]: 取得したインサイトデータを含むレスポンス
        """
        result = await self.get_data("insights", "insight_id", insight_id)
        if result.error is not None:
            return RepositoryResult(data=None)
        return result

    async def get_activity_summary(
        self,
        *,
        user_id: str,
        summary_id: str,
    ) -> RepositoryResult[dict[str, Any]]:
        rows = self.data.get("activity_summaries", [])
        row = next(
            (
                item
                for item in rows
                if item.get("user_id") == user_id
                and item.get("summary_id") == summary_id
            ),
            None,
        )
        return RepositoryResult(data=row)

    async def get_previous_insight_before_summary(
        self,
        *,
        user_id: str,
        source_period_end: str,
    ) -> RepositoryResult[dict[str, Any]]:
        source_period_end_timestamp = parse_mock_iso_timestamp(source_period_end)
        summary_periods = {
            str(item.get("summary_id")): parse_mock_iso_timestamp(
                item.get("period_end")
            )
            for item in self.data.get("activity_summaries", [])
            if item.get("user_id") == user_id
        }
        candidates = [
            item
            for item in self.data.get("insights", [])
            if item.get("user_id") == user_id
            and item.get("status") in {"success", StatusType.SUCCESS}
            and summary_periods.get(str(item.get("source_activity_summary_id")))
            is not None
            and summary_periods[str(item.get("source_activity_summary_id"))]
            < source_period_end_timestamp
        ]
        candidates.sort(
            key=lambda item: summary_periods[
                str(item.get("source_activity_summary_id"))
            ],
            reverse=True,
        )
        return RepositoryResult(data=candidates[0] if candidates else None)

    async def get_latest_activity_summary_as_of(
        self,
        *,
        user_id: str,
        summary_type: str,
        as_of: str,
    ) -> RepositoryResult[dict[str, Any]]:
        as_of_timestamp = parse_mock_iso_timestamp(as_of)
        candidates = [
            item
            for item in self.data.get("activity_summaries", [])
            if item.get("user_id") == user_id
            and item.get("summary_type") == summary_type
            and item.get("status") in {"success", StatusType.SUCCESS}
            and parse_mock_iso_timestamp(item.get("period_end")) <= as_of_timestamp
            and parse_mock_iso_timestamp(item.get("created_at")) <= as_of_timestamp
        ]
        candidates.sort(
            key=lambda item: (
                parse_mock_iso_timestamp(item.get("period_end")),
                parse_mock_iso_timestamp(item.get("created_at")),
            ),
            reverse=True,
        )
        return RepositoryResult(data=candidates[0] if candidates else None)

    async def get_user_actions(
        self, user_id: str, limit: int = 10
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """ユーザーのアクションを取得する

        Args:
            user_id (str): ユーザーID
            limit (int, optional): 取得する最大件数. デフォルトは10.

        Returns:
            RepositoryResult[list[dict[str, Any]]]: 取得したアクションデータを含むレスポンス
        """
        return await self.get_latest_data("user_actions", "user_id", user_id, limit)

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

    async def get_insights(
        self, user_id: str, limit: int = 10
    ) -> RepositoryResult[list[dict[str, Any]]]:
        """ユーザーのインサイトを取得する

        Args:
            user_id (str): ユーザーID
            limit (int, optional): 取得する最大件数. デフォルトは10.

        Returns:
            RepositoryResult[list[dict[str, Any]]]: 取得したインサイトデータを含むレスポンス
        """
        return await self.get_latest_data("insights", "user_id", user_id, limit)

    async def update_insight(
        self,
        insight_id: str,
        insight_data: str,
        updated_at: datetime | None = None,
        status: StatusType = StatusType.SUCCESS,
    ) -> RepositoryResult[dict[str, Any]]:
        """インサイトを更新する

        Args:
            insight_id (str): 更新するインサイトID
            insight_data (str): 更新するインサイトデータ
            updated_at (datetime | None, optional): 更新日時. デフォルトはNone.
            status (StatusType, optional): ステータス. デフォルトはSUCCESS.

        Returns:
            RepositoryResult[dict[str, Any]]: 更新結果を含むレスポンス
        """
        if updated_at is None:
            updated_at = datetime.now(UTC)

        # 既存のインサイトを検索
        existing_insight = None
        if "insights" in self.data:
            for insight in self.data["insights"]:
                if insight.get("insight_id") == insight_id:
                    existing_insight = insight
                    break

        if existing_insight is None:
            return RepositoryResult(error=f"Insight with ID {insight_id} not found")

        # インサイトデータを更新
        existing_insight["insight_data"] = insight_data
        existing_insight["updated_at"] = updated_at
        existing_insight["status"] = status

        return RepositoryResult(data=existing_insight)

    # テスト用ヘルパーメソッド
    async def save_suggestion(self, data: dict[str, Any]) -> None:
        """サジェスチョンを保存する（テスト用ヘルパー）

        Args:
            data (dict[str, Any]): 保存するサジェスチョンデータ
        """
        await self.save_data("suggestions", data)

    async def save_action(self, data: dict[str, Any]) -> None:
        """アクションを保存する（テスト用ヘルパー）

        Args:
            data (dict[str, Any]): 保存するアクションデータ
        """
        await self.save_data("actions", data)

    async def save_insight_data(self, data: dict[str, Any]) -> None:
        """インサイトデータを保存する（テスト用ヘルパー）

        Args:
            data (dict[str, Any]): 保存するインサイトデータ
        """
        await self.save_data("insights", data)

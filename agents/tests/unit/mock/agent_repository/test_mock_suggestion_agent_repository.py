import pytest

from pantaray_agents.action_status import build_finalize_action_terminal_command
from pantaray_agents.schema.agent.suggestion import SuggestionTodo

from .shared import (
    UTC,
    MockInsightAgentRepository,
    MockSuggestionAgentRepository,
    StatusType,
    SuggestionAgentResponse,
    datetime,
    timedelta,
)


class TestMockSuggestionAgentRepository:
    @pytest.mark.asyncio
    async def test_save_and_get_suggestion(self) -> None:
        repo = MockSuggestionAgentRepository()
        suggestion = SuggestionAgentResponse(
            suggestion_id="test-suggestion-1",
            user_id="test-user-1",
            answer="Test suggestion content",
            thinking="Test thinking process",
            has_suggestion=True,
            todo=SuggestionTodo(text="Reply", outcome="done", note=None),
            status=StatusType.SUCCESS,
            error=None,
            created_at=datetime.now().isoformat(),
        )

        await repo.save_suggestion(
            suggestion,
            prompt_text="prompt",
            response_text="Test suggestion content",
            prompt_name="suggestion",
            prompt_version="1.0",
        )
        result = await repo.get_suggestion(
            user_id="test-user-1",
            suggestion_id="test-suggestion-1",
        )

        assert result.error is None
        assert result.data is not None
        assert result.data["answer"] == "Test suggestion content"
        assert result.data["thinking"] == "Test thinking process"
        assert result.data["has_suggestion"] is True

    @pytest.mark.asyncio
    async def test_get_suggestion_not_found(self) -> None:
        repo = MockSuggestionAgentRepository()

        result = await repo.get_suggestion(
            user_id="test-user-1",
            suggestion_id="non-existent-id",
        )

        assert result.error is not None
        assert "not found" in result.error
        assert result.data is None

    @pytest.mark.asyncio
    async def test_get_latest_insights(self) -> None:
        repo = MockSuggestionAgentRepository()
        insight_repo = MockInsightAgentRepository()
        now = datetime.now()
        user_id = "test-user-insights"
        await insight_repo.save_insight(
            insight_id="insight-1",
            current_insight_data="Current data 1",
            insight_data="Insight data 1",
            thinking="Thinking 1",
            facts="Facts 1",
            created_at=now,
            user_id=user_id,
        )
        await insight_repo.save_insight(
            insight_id="insight-2",
            current_insight_data="Current data 2",
            insight_data="Insight data 2",
            thinking="Thinking 2",
            facts="Facts 2",
            created_at=now + timedelta(seconds=1),
            user_id=user_id,
        )

        result = await repo.get_latest_insights(user_id, limit=1)

        assert result.error is None
        assert result.data is not None
        assert [row["insight_id"] for row in result.data] == ["insight-2"]

    @pytest.mark.asyncio
    async def test_append_process_event_duplicate_id_mismatch_fails_closed(
        self,
    ) -> None:
        repo = MockSuggestionAgentRepository()
        first = await repo.append_process_event_and_project_history(
            event_id="event-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
            event_name="suggestion_chunk",
            payload={"data": {"content": "first"}},
            action_id=None,
        )
        second = await repo.append_process_event_and_project_history(
            event_id="event-1",
            suggestion_id="suggestion-1",
            user_id="user-1",
            event_name="suggestion_chunk",
            payload={"data": {"content": "second"}},
            action_id=None,
        )

        assert first.error is None
        assert first.data is not None and first.data.inserted is True
        assert second.data is None
        assert second.error is not None
        assert "mismatched duplicate event_id" in second.error

    @pytest.mark.asyncio
    async def test_save_suggestion_starts_without_action_state(self) -> None:
        repo = MockSuggestionAgentRepository()
        result = await repo.save_suggestion(
            SuggestionAgentResponse(
                suggestion_id="suggestion-1",
                user_id="user-1",
                answer="Actionable suggestion",
                thinking="thinking",
                has_suggestion=True,
                todo=SuggestionTodo(text="Reply", outcome="done", note=None),
                interaction_contract="action_offer",
                status=StatusType.SUCCESS,
                error=None,
                created_at=datetime.now().isoformat(),
            ),
            prompt_text="prompt",
            response_text="Actionable suggestion",
            prompt_name="suggestion",
            prompt_version="1.0",
        )

        assert result.data is not None
        assert result.data["user_reaction"] is None
        assert result.data["action_status"] is None
        assert result.data["action_id"] is None

    @pytest.mark.asyncio
    async def test_terminal_command_rejects_non_terminal_status(self) -> None:
        timestamp = datetime.now(UTC).isoformat()

        with pytest.raises(
            ValueError,
            match="FinalizeActionTerminalCommand: unsupported terminal status",
        ):
            build_finalize_action_terminal_command(
                process_completed_event_id="event-1",
                suggestion_id="suggestion-1",
                user_id="user-1",
                command_id="command-1",
                process_id="process-1",
                action_id="action-1",
                accepted_at=timestamp,
                completed_at=timestamp,
                action_status="timeout",
            )

    @pytest.mark.asyncio
    async def test_finalize_action_terminal_rolls_back_on_event_conflict(self) -> None:
        repo = MockSuggestionAgentRepository()
        timestamp = datetime.now(UTC).isoformat()
        repo.data["suggestions"] = [
            {
                "suggestion_id": "suggestion-1",
                "user_id": "user-1",
                "accepted_at": timestamp,
                "action_status": "processing",
                "action_failure_code": None,
            }
        ]
        repo.data["actions"] = [
            {
                "action_id": "action-1",
                "user_id": "user-1",
                "status": "processing",
                "final_output": "",
                "error": None,
                "updated_at": timestamp,
            }
        ]
        first = await repo.append_process_event_and_project_history(
            event_id="terminal-event",
            suggestion_id="suggestion-1",
            user_id="user-1",
            event_name="process_completed",
            payload={"data": {"final_output": "first"}},
            action_id="action-1",
        )
        assert first.error is None

        finalized = await repo.finalize_action_terminal_and_project_history(
            command=build_finalize_action_terminal_command(
                process_completed_event_id="terminal-event",
                suggestion_id="suggestion-1",
                user_id="user-1",
                command_id="command-1",
                process_id="process-1",
                action_id="action-1",
                accepted_at=timestamp,
                completed_at=timestamp,
                action_status="success",
                final_output="second",
            )
        )

        assert finalized.data is None
        assert finalized.error is not None
        assert "mismatched duplicate event_id" in finalized.error
        assert repo.data["actions"][0]["status"] == "processing"
        assert repo.data["actions"][0]["final_output"] == ""

"""SuggestionAgent コンテキスト関連テスト"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.agents.suggestion_agent.prompt_support import (
    prompt_configs,
    serve_lens_runs,
)

from pantaray_agents.agents.activity_summary_agent.agent import NO_ACTIVITY_TEMPLATE
from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.context_density import (
    build_context_density_signal,
    build_context_density_slot_status,
    build_recent_activity_summaries_24h_1w_1m,
    classify_context_density,
    format_ended_ago,
)
from pantaray_agents.agents.suggestion_agent.context_formatters import (
    NO_ACTIVITY_SUMMARY_1H_TEXT,
    format_activity_descriptions,
    format_activity_summary,
    format_recent_suggestions,
    normalize_recent_suggestion_entry,
)
from pantaray_agents.agents.suggestion_agent.context_types import (
    ActivitySummaryRow,
    SuggestionStableMemoryContext,
    SummaryType,
    normalize_activity_description_rows,
    normalize_activity_summary_row,
)
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
)
from pantaray_agents.mock.mock_agent_repository import MockSuggestionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentRequest,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult

_STABLE_MEMORY = SuggestionStableMemoryContext(
    prompt="Stable memory summary",
    has_facts=True,
    has_insights=True,
)


def _no_suggestion_output() -> dict[str, object]:
    return {
        "has_suggestion": False,
        "answer": "",
        "interaction_contract": None,
        "suggestion_summary": None,
        "target_context": None,
    }


def test_normalize_recent_suggestion_entry_coerces_to_history_entry() -> None:
    entry = normalize_recent_suggestion_entry(
        {
            "answer": 123,
            "created_at": None,
            "thinking": {"note": "x"},
        }
    )
    assert entry is not None
    assert entry.answer == "123"
    assert entry.created_at == ""
    assert isinstance(entry.thinking, str)
    assert "note" in entry.thinking


@pytest.mark.asyncio
async def test_fetch_context_data_propagates_summary_fetch_connection_error(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    test_prompt = "Density:\n{context_density_signal}\n"
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )
        mock_repository.data["activity_descriptions"] = [
            {
                "user_id": "user-test",
                "description": "desc",
                "period_start": "2025-01-01T00:00:00Z",
                "period_end": "2025-01-01T00:04:00Z",
                "created_at": "2025-01-01T00:04:00Z",
                "status": "success",
            }
        ]
        req = SuggestionAgentRequest(
            suggestion_id="sug-summary-fetch-failure",
            user_id="user-test",
            short_term_insight="# Insight\nThe user is fixing a parser bug.",
            reconsideration_reason="The user switched goals.",
        )
        with (
            patch.object(
                mock_repository,
                "get_recent_activity_summary_1h",
                new=AsyncMock(
                    return_value=RepositoryResult(
                        data=[
                            {
                                "summary": "S1H",
                                "period_start": "2025-01-01T00:00:00Z",
                                "period_end": "2025-01-01T01:00:00Z",
                            }
                        ]
                    )
                ),
            ),
            patch.object(
                mock_repository,
                "get_recent_activity_summary",
                new=AsyncMock(side_effect=ConnectionError("network down")),
            ),
        ):
            with pytest.raises(ConnectionError, match="network down"):
                await agent._fetch_context_data(req)  # noqa: SLF001


@pytest.mark.asyncio
async def test_fetch_context_data_raises_unexpected_summary_fetch_exception(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    test_prompt = "Density:\n{context_density_signal}\n"
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )
        req = SuggestionAgentRequest(
            suggestion_id="sug-summary-fetch-unexpected",
            user_id="user-test",
            short_term_insight="# Insight\nThe user is fixing a parser bug.",
            reconsideration_reason="The user switched goals.",
        )
        with (
            patch.object(
                mock_repository,
                "get_recent_activity_summary_1h",
                new=AsyncMock(return_value=RepositoryResult(data=[])),
            ),
            patch.object(
                mock_repository,
                "get_recent_activity_summary",
                new=AsyncMock(side_effect=TypeError("unexpected bug")),
            ),
        ):
            with pytest.raises(TypeError, match="unexpected bug"):
                _ = await agent._fetch_context_data(req)  # noqa: SLF001


def test_normalize_recent_suggestion_entry_skips_non_mapping_row() -> None:
    entry = normalize_recent_suggestion_entry(["invalid-row-shape"])
    assert entry is None


def test_format_ended_ago_minutes() -> None:
    reference_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    period_end = reference_time - timedelta(seconds=660)
    assert format_ended_ago(reference_time, period_end) == "11m (660s)"


def test_format_ended_ago_hours() -> None:
    reference_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    period_end = reference_time - timedelta(seconds=68400)
    assert format_ended_ago(reference_time, period_end) == "19h (68400s)"


def test_format_ended_ago_days() -> None:
    reference_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    period_end = reference_time - timedelta(seconds=1036800)
    assert format_ended_ago(reference_time, period_end) == "12d (1036800s)"


@pytest.mark.usefixtures("tokyo_local_zone")
def test_build_recent_activity_summaries_24h_1w_1m_missing() -> None:
    reference_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    text = build_recent_activity_summaries_24h_1w_1m(
        reference_time=reference_time,
        rows_by_type={"24h": None, "1w": None, "1m": None},
    )
    assert "Reference time: 2025-01-01T09:00+09:00 (Asia/Tokyo)" in text
    assert "- [24h] MISSING (no summary available)" in text
    assert "- [1w] MISSING (no summary available)" in text
    assert "- [1m] MISSING (no summary available)" in text


def test_normalize_activity_summary_row_non_string_fields_treated_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        normalized = normalize_activity_summary_row(
            {
                "summary": 123,
                "period_start": "2025-01-01T00:00:00Z",
                "period_end": {"invalid": "shape"},
                "source_ids": "sum-1w",
            }
        )

    assert normalized == {"period_start": "2025-01-01T00:00:00Z"}
    assert (
        "Suggestion context normalization: invalid field treated as missing"
        in caplog.text
    )


def test_normalize_activity_description_rows_raises_on_non_mapping_row() -> None:
    with pytest.raises(TypeError, match="ActivityDescription row must be a mapping"):
        _ = normalize_activity_description_rows(
            [
                {
                    "description": "ok",
                    "period_end": "2025-01-01T00:00:00Z",
                },
                "invalid-row",
            ]
        )


def test_context_density_slot_status_blank_and_invalid_timestamp_are_missing() -> None:
    slot_status = build_context_density_slot_status(
        has_long_term_insight=False,
        has_long_term_facts=False,
        activity_desc_rows=[{"description": "work", "period_end": "invalid"}],
        summary_1h_row={
            "summary": "ready",
            "period_end": "invalid",
            "source_ids": ["log-1h"],
        },
        rows_by_type={
            "24h": {
                "summary": " ",
                "period_end": "2025-01-01T00:00:00Z",
                "source_ids": ["sum-1h"],
            },
            "1w": {
                "summary": "weekly",
                "period_end": "invalid",
                "source_ids": ["sum-24h"],
            },
            "1m": None,
        },
    )
    assert slot_status == {
        "activity_descriptions": False,
        "activity_summary_1h": False,
        "activity_summary_24h": False,
        "activity_summary_1w": False,
        "activity_summary_1m": False,
        "long_term_insight": False,
        "long_term_facts": False,
    }


def test_context_density_slot_status_valid_rows_are_present() -> None:
    slot_status = build_context_density_slot_status(
        has_long_term_insight=True,
        has_long_term_facts=True,
        activity_desc_rows=[
            {"description": "investigating", "period_end": "2025-01-02T00:00:00Z"}
        ],
        summary_1h_row={
            "summary": "hourly",
            "period_end": "2025-01-02T00:00:00Z",
            "source_ids": ["log-1h"],
        },
        rows_by_type={
            "24h": {
                "summary": "daily",
                "period_end": "2025-01-01T00:00:00Z",
                "source_ids": ["sum-1h"],
            },
            "1w": {
                "summary": "weekly",
                "period_end": "2025-01-01T00:00:00Z",
                "source_ids": ["sum-24h"],
            },
            "1m": {
                "summary": "monthly",
                "period_end": "2024-12-01T00:00:00Z",
                "source_ids": ["sum-1w"],
            },
        },
    )
    assert all(slot_status.values())


def test_context_density_slot_status_no_activity_summary_is_missing() -> None:
    """source_ids が空の「活動なし」定型文サマリは present に数えない。"""

    def _no_activity_summary(
        *, summary_type: str, period_start: str, period_end: str
    ) -> dict[str, object]:
        return {
            "summary": NO_ACTIVITY_TEMPLATE.format(
                summary_type=summary_type,
                period_start=period_start,
                period_end=period_end,
            ),
            "period_start": period_start,
            "period_end": period_end,
            "source_ids": [],
        }

    rows_by_type: dict[SummaryType, ActivitySummaryRow | None] = {
        "24h": normalize_activity_summary_row(
            {
                "summary": "S24",
                "period_start": "2026-09-15T00:00:00Z",
                "period_end": "2026-09-16T00:00:00Z",
                "source_ids": ["sum-1h"],
            }
        ),
        "1w": normalize_activity_summary_row(
            _no_activity_summary(
                summary_type="1w",
                period_start="2026-09-09T00:00:00Z",
                period_end="2026-09-16T00:00:00Z",
            )
        ),
        "1m": normalize_activity_summary_row(
            _no_activity_summary(
                summary_type="1m",
                period_start="2026-08-16T00:00:00Z",
                period_end="2026-09-16T00:00:00Z",
            )
        ),
    }
    slot_status = build_context_density_slot_status(
        has_long_term_insight=True,
        has_long_term_facts=True,
        activity_desc_rows=[
            {"description": "investigating", "period_end": "2026-09-16T00:10:00Z"}
        ],
        summary_1h_row=normalize_activity_summary_row(
            {
                "summary": "S1H",
                "period_start": "2026-09-15T23:00:00Z",
                "period_end": "2026-09-16T00:00:00Z",
                "source_ids": ["log-1h"],
            }
        ),
        rows_by_type=rows_by_type,
    )

    assert slot_status["activity_summary_1w"] is False
    assert slot_status["activity_summary_1m"] is False
    present_count = sum(1 for is_present in slot_status.values() if is_present)
    assert present_count == 5
    assert classify_context_density(present_count) == "high"

    # スロットが missing なら、プロンプトの本文にも「活動なし」の定型文を載せない。
    body = build_recent_activity_summaries_24h_1w_1m(
        reference_time=datetime(2026, 9, 16, 0, 20, tzinfo=UTC),
        rows_by_type=rows_by_type,
    )
    assert "- [24h] period:" in body
    assert "- [1w] MISSING (no summary available)" in body
    assert "- [1m] MISSING (no summary available)" in body
    assert "No activity was recorded" not in body
    assert format_activity_summary([rows_by_type["1m"]]) == NO_ACTIVITY_SUMMARY_1H_TEXT


@pytest.mark.parametrize(
    ("present_count", "expected"),
    [
        (0, "low"),
        (2, "low"),
        (3, "medium"),
        (4, "medium"),
        (5, "high"),
        (6, "high"),
        (7, "high"),
    ],
)
def test_classify_context_density_boundaries(present_count: int, expected: str) -> None:
    assert classify_context_density(present_count) == expected


def test_build_context_density_signal_does_not_include_policy() -> None:
    slot_status = {
        "activity_descriptions": True,
        "activity_summary_1h": True,
        "activity_summary_24h": False,
        "activity_summary_1w": False,
        "activity_summary_1m": False,
        "long_term_insight": True,
        "long_term_facts": False,
    }
    signal = build_context_density_signal(
        density="medium",
        present_count=3,
        slot_status=slot_status,
    )
    assert "policy:" not in signal
    assert "context_density: medium" in signal
    assert "present_count: 3/7" in signal
    assert "- Activity Summary (24h): missing" in signal


def test_build_context_density_signal_raises_on_slot_key_mismatch() -> None:
    invalid_slot_status = {
        "activity_descriptions": True,
        "activity_summary_1h": True,
        "activity_summary_24h": False,
        "activity_summary_1w": False,
        "activity_summary_1m": False,
        "long_term_insight": False,
        "unexpected_slot": True,
    }
    with pytest.raises(ValueError, match="slot_status keys mismatch"):
        _ = build_context_density_signal(
            density="medium",
            present_count=3,
            slot_status=invalid_slot_status,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary_types", "expected_density", "expected_present_count"),
    [
        (("1h", "24h"), "high", "5/7"),
        (("1h", "24h", "1w", "1m"), "high", "7/7"),
    ],
)
async def test_suggestion_agent_process_includes_context_density_in_prompt(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
    fixed_suggestion_research_tools: FixedSuggestionResearchTools,
    summary_types: tuple[str, ...],
    expected_density: str,
    expected_present_count: str,
) -> None:
    test_prompt = "Density:\n{context_density_signal}\n"
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=fixed_suggestion_research_tools,
            stable_memory=_STABLE_MEMORY,
        )
        agent.client = mock_llm_client

        mock_repository.data["activity_descriptions"] = [
            {
                "user_id": "user-test",
                "description": "desc",
                "period_start": "2025-01-01T00:00:00Z",
                "period_end": "2025-01-01T00:04:00Z",
                "created_at": "2025-01-01T00:04:00Z",
                "status": "success",
            }
        ]
        summary_rows_by_type = {
            "1h": {
                "summary_id": "sum-1h",
                "user_id": "user-test",
                "summary_type": "1h",
                "summary": "S1H",
                "period_start": "2025-01-01T00:00:00Z",
                "period_end": "2025-01-01T01:00:00Z",
                "status": "success",
                "source_ids": ["log-1h"],
            },
            "24h": {
                "summary_id": "sum-24h",
                "user_id": "user-test",
                "summary_type": "24h",
                "summary": "S24",
                "period_start": "2024-12-31T00:00:00Z",
                "period_end": "2025-01-01T00:00:00Z",
                "status": "success",
                "source_ids": ["sum-1h"],
            },
            "1w": {
                "summary_id": "sum-1w",
                "user_id": "user-test",
                "summary_type": "1w",
                "summary": "S1W",
                "period_start": "2024-12-25T00:00:00Z",
                "period_end": "2025-01-01T00:00:00Z",
                "status": "success",
                "source_ids": ["sum-24h"],
            },
            "1m": {
                "summary_id": "sum-1m",
                "user_id": "user-test",
                "summary_type": "1m",
                "summary": "S1M",
                "period_start": "2024-12-01T00:00:00Z",
                "period_end": "2025-01-01T00:00:00Z",
                "status": "success",
                "source_ids": ["sum-1w"],
            },
        }
        mock_repository.data["activity_summaries"] = [
            summary_rows_by_type[summary_type] for summary_type in summary_types
        ]

        req = SuggestionAgentRequest(
            suggestion_id=f"sug-density-{expected_density}",
            user_id="user-test",
            short_term_insight="# Insight\nThe user is fixing a parser bug.",
            reconsideration_reason="The user switched goals.",
        )
        serve_lens_runs(mock_llm_client, _no_suggestion_output())
        _ = await agent.process(req)
        prompt_used = mock_llm_client.last_prompt or ""
        assert f"context_density: {expected_density}" in prompt_used
        assert f"present_count: {expected_present_count}" in prompt_used


def test_context_density_slot_status_with_facts_only_marks_facts_present() -> None:
    slot_status = build_context_density_slot_status(
        has_long_term_insight=False,
        has_long_term_facts=True,
        activity_desc_rows=None,
        summary_1h_row=None,
        rows_by_type={"24h": None, "1w": None, "1m": None},
    )

    assert slot_status == {
        "activity_descriptions": False,
        "activity_summary_1h": False,
        "activity_summary_24h": False,
        "activity_summary_1w": False,
        "activity_summary_1m": False,
        "long_term_insight": False,
        "long_term_facts": True,
    }


@pytest.mark.asyncio
async def test_fetch_context_data_counts_facts_in_context_density(
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    test_prompt = (
        "Density:\n{context_density_signal}\nMemory:\n{stable_memory_context}\n"
    )
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=prompt_configs(test_prompt),
    ):
        agent = SuggestionAgent(
            config={"llm_client": mock_llm_client},
            repository=mock_repository,
            research_tools=build_mock_suggestion_research_tools(),
            stable_memory=_STABLE_MEMORY,
        )
        req = SuggestionAgentRequest(
            suggestion_id="sug-facts-density",
            user_id="user-test",
            short_term_insight="# Insight\nThe user is fixing a parser bug.",
            reconsideration_reason="The user switched goals.",
        )
        with (
            patch.object(
                mock_repository,
                "get_recent_activity_summary_1h",
                new=AsyncMock(return_value=RepositoryResult(data=[])),
            ),
            patch.object(
                mock_repository,
                "get_recent_activity_summary",
                new=AsyncMock(return_value=RepositoryResult(data=[])),
            ),
        ):
            context = await agent._fetch_context_data(req)  # noqa: SLF001

        assert context["stable_memory_context"] == "Stable memory summary"
        assert "context_density: low" in context["context_density_signal"]
        assert "present_count: 2/7" in context["context_density_signal"]
        assert "- Long-term Facts: present" in context["context_density_signal"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_suggestion_agent_builds_recent_activity_summaries_24h_1w_1m_context(
    suggestion_agent: SuggestionAgent,
    mock_repository: MockSuggestionAgentRepository,
    mock_llm_client: MockLLMClient,
) -> None:
    """24h/1w/1m の Activity Summary を ended_ago 付きでコンテキストに含めること。"""

    fixed_now = datetime(2025, 1, 2, 0, 0, 0, tzinfo=UTC)

    # 1hは既存のキー（recent_activity_summary_1h）でそのまま使う
    mock_repository.data["activity_summaries"] = [
        {
            "summary_id": "sum-1h",
            "user_id": "user-test",
            "summary_type": "1h",
            "summary": "S1H",
            "period_start": "2025-01-01T23:00:00Z",
            "period_end": "2025-01-02T00:00:00Z",
            "status": "success",
            "source_ids": ["log-1h"],
        },
        {
            "summary_id": "sum-24h",
            "user_id": "user-test",
            "summary_type": "24h",
            "summary": "S24",
            "period_start": "2024-12-31T05:00:00Z",
            "period_end": "2025-01-01T05:00:00Z",
            "status": "success",
            "source_ids": ["sum-1h"],
        },
        {
            "summary_id": "sum-1w",
            "user_id": "user-test",
            "summary_type": "1w",
            "summary": "S1W",
            "period_start": "2024-12-25T00:00:00Z",
            "period_end": "2025-01-01T00:00:00Z",
            "status": "success",
            "source_ids": ["sum-24h"],
        },
        {
            "summary_id": "sum-1m",
            "user_id": "user-test",
            "summary_type": "1m",
            "summary": "S1M",
            "period_start": "2024-11-21T00:00:00Z",
            "period_end": "2024-12-21T00:00:00Z",
            "status": "success",
            "source_ids": ["sum-1w"],
        },
    ]

    request = SuggestionAgentRequest(
        suggestion_id="sug-ctx-24h-1w-1m",
        user_id="user-test",
        short_term_insight="# Insight\nThe user is fixing a parser bug.",
        reconsideration_reason="The user switched goals.",
    )

    with (
        patch.object(suggestion_agent, "_get_reference_time", return_value=fixed_now),
        patch.object(
            suggestion_agent,
            "_build_prompt",
            wraps=suggestion_agent._build_prompt,
        ) as mock_build_prompt,
    ):
        serve_lens_runs(mock_llm_client, _no_suggestion_output())
        _ = await suggestion_agent.process(request)

        ctx = mock_build_prompt.call_args[0][0]
        assert "recent_activity_summary_1h" in ctx
        assert "recent_activity_summaries_24h_1w_1m" in ctx

        extended = ctx["recent_activity_summaries_24h_1w_1m"]
        assert "Reference time: 2025-01-02T09:00+09:00 (Asia/Tokyo)" in extended
        assert "period: 2024-12-31T14:00+09:00 .. 2025-01-01T14:00+09:00" in extended
        assert "ended_ago: 19h (68400s)" in extended
        assert "ended_ago: 24h (86400s)" in extended
        assert "ended_ago: 12d (1036800s)" in extended


@pytest.mark.asyncio
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_prompt_receives_pending_work_direction_and_user_feedback(
    suggestion_agent,
) -> None:
    from unittest.mock import AsyncMock

    from pantaray_agents.utils.prompt_loader import PromptLoader

    suggestion_agent._prompt_config = PromptLoader().load_config(
        "suggestion/suggestion"
    )
    suggestion_agent.stable_memory = SuggestionStableMemoryContext(
        prompt="Long-term direction: grow sustainably.",
        has_facts=False,
        has_insights=True,
        pending_work=(
            "Observed: prepare the estimate for the other project.\n"
            "- **Contract**: return it by 9/12.\n"
        ),
    )
    suggestion_agent.repository.get_recent_suggestions = AsyncMock(
        return_value=RepositoryResult(
            data=[
                {
                    "answer": "Repeat the review?",
                    "created_at": "2026-09-10T00:00:00Z",
                    "user_reaction": "rejected",
                    "user_reply": "The review is complete.",
                    "action_status": "success",
                    "action_result": "Review finished; no findings remain.",
                    "action_followups": ["Only review,\n do not merge."],
                }
            ]
        )
    )
    context = await suggestion_agent._fetch_context_data(
        SuggestionAgentRequest(
            user_id="user-1",
            suggestion_id="sug-1",
            short_term_insight="Currently coding.",
            reconsideration_reason="Periodic review.",
        )
    )
    with patch.object(
        suggestion_agent,
        "_get_reference_time",
        return_value=datetime(2026, 9, 10, tzinfo=UTC),
    ):
        prompt = suggestion_agent._build_prompt(context)
    # Deadlines in the TODO file are counted from the run's own date.
    assert "## Now\n2026-09-10T09:00+09:00 (Asia/Tokyo)." in prompt
    assert "- **Contract**: return it by 9/12." in prompt
    assert "prepare the estimate for the other project" in prompt
    assert "grow sustainably" in prompt
    assert "User reaction: rejected" in prompt
    assert "The review is complete." in prompt
    assert "Action status: success" in prompt
    assert "Latest Action result: Review finished; no findings remain." in prompt
    assert (
        "  User instructions during the Action (oldest first):\n"
        "    - Only review, do not merge.\n"
    ) in prompt


def test_reply_and_result_previews_are_bounded_and_missing_reaction_is_not_rejection() -> (
    None
):
    from pantaray_agents.agents.suggestion_agent.context_formatters import (
        RECENT_SUGGESTION_ACTION_RESULT_MAX_CHARS,
        RECENT_SUGGESTION_REPLY_MAX_CHARS,
    )

    entry = normalize_recent_suggestion_entry(
        {
            "answer": "A suggestion",
            "created_at": "2026-09-10T00:00:00Z",
            "user_reply": "x" * (RECENT_SUGGESTION_REPLY_MAX_CHARS + 100),
            "action_result": "y" * (RECENT_SUGGESTION_ACTION_RESULT_MAX_CHARS + 100),
        }
    )
    assert entry is not None
    rendered = format_recent_suggestions([entry])
    assert "User reaction: not recorded" in rendered
    assert "[reply truncated]" in rendered
    assert "x" * (RECENT_SUGGESTION_REPLY_MAX_CHARS + 1) not in rendered
    assert "[result truncated]" in rendered
    assert "y" * (RECENT_SUGGESTION_ACTION_RESULT_MAX_CHARS + 1) not in rendered


@pytest.mark.usefixtures("tokyo_local_zone")
def test_suggestion_context_shows_the_local_date_of_each_stored_instant() -> None:
    # 21:50Z is already the next morning in Tokyo; the model must see the 27th.
    period = {
        "period_start": "2026-09-26T21:50:00Z",
        "period_end": "2026-09-26T21:54:00Z",
    }
    local_period = "2026-09-27T06:50+09:00 - 2026-09-27T06:54+09:00"
    assert format_activity_descriptions(
        [{"description": "Editing the estimate", **period}]
    ) == (f"- [{local_period}] Editing the estimate")
    assert format_activity_summary(
        [{"summary": "S1H", "source_ids": ["log-1"], **period}]
    ) == (f"[{local_period}] S1H")
    entry = normalize_recent_suggestion_entry(
        {"answer": "A suggestion", "created_at": "2026-09-26T21:50:00.123Z"}
    )
    assert entry is not None
    assert format_recent_suggestions([entry]).startswith(
        "- [2026-09-27T06:50+09:00] answer: A suggestion"
    )

    body = build_recent_activity_summaries_24h_1w_1m(
        reference_time=datetime(2026, 9, 26, 21, 50, tzinfo=UTC),
        rows_by_type={
            "24h": {
                "summary": "S24",
                "source_ids": ["sum-1h"],
                "period_start": "2026-09-25T21:00:00Z",
                "period_end": "2026-09-26T21:00:00Z",
            },
            "1w": None,
            "1m": None,
        },
    )
    assert body.splitlines()[:2] == [
        "Reference time: 2026-09-27T06:50+09:00 (Asia/Tokyo)",
        "- [24h] period: 2026-09-26T06:00+09:00 .. 2026-09-27T06:00+09:00"
        " | ended_ago: 50m (3000s) | summary: S24",
    ]

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pantaray_agents.schema.agent.base import TaskStatusType
from pantaray_agents.schema.agent.streaming import ActionStreamEndData, StreamEndData
from pantaray_agents.utils.streaming_helpers import coerce_task_status


def test_coerce_task_status_maps_queued_to_processing() -> None:
    """外部に queued を出さないため、queued は processing に丸められること。"""
    assert coerce_task_status("queued") == TaskStatusType.PROCESSING


def test_stream_end_data_rejects_queued_status() -> None:
    """StreamEndData は queued を受け付けない（外部APIへ出さないため）。"""
    with pytest.raises(ValidationError):
        StreamEndData(
            suggestion_id="sug-1",
            has_suggestion=True,
            user_id="user-1",
            completed_at="2025-01-01T00:00:00Z",
            status="queued",
            total_chunks=0,
            duration_ms=1,
        )


def test_action_stream_end_data_rejects_queued_status() -> None:
    """ActionStreamEndData は queued を受け付けない（外部APIへ出さないため）。"""
    with pytest.raises(ValidationError):
        ActionStreamEndData(
            action_id="act-1",
            suggestion_id="sug-1",
            user_id="user-1",
            completed_at="2025-01-01T00:00:00Z",
            status="queued",
            total_chunks=0,
            duration_ms=1,
        )

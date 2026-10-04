"""ActionAgentFormatter の memory_source_coverage 整形テスト。"""

import pytest

from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.utils.memory_source_policy import MEMORY_SOURCE_ORDER


def _build_slots() -> list[dict[str, str | None]]:
    slots = [
        {"source": source, "status": "missing", "latest_at": None}
        for source in MEMORY_SOURCE_ORDER
    ]
    slots[0]["status"] = "present"
    slots[0]["latest_at"] = "2026-09-26T21:50:00.000000Z"
    slots[1]["status"] = "unknown"
    return slots


@pytest.mark.usefixtures("tokyo_local_zone")
def test_format_memory_source_coverage_formats_valid_snapshot() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "context": {
            "memory_source_coverage": {
                "evaluated_at": "2026-09-26T21:51:00+00:00",
                "slots": _build_slots(),
            }
        }
    }

    text = formatter.format_memory_source_coverage(state)  # type: ignore[arg-type]

    assert "- evaluated_at: 2026-09-27T06:51+09:00" in text
    assert "- present_count: 1/7" in text
    assert "- unknown_count: 1/7" in text
    assert "- Stock knowledge:" in text
    assert "  - long_term_insight: present (latest_at: 2026-09-27T06:50+09:00)" in text
    assert "  - facts: missing (latest_at: null)" in text
    assert "- Flow knowledge:" in text
    assert "  - short_term_insight: unknown (latest_at: null)" in text
    assert "  - activity_summary: missing (latest_at: null)" in text
    assert "- Agent work records:" in text
    assert "  - suggestions: missing (latest_at: null)" in text


def test_format_memory_source_coverage_rejects_empty_evaluated_at() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "context": {
            "memory_source_coverage": {
                "evaluated_at": "   ",
                "slots": _build_slots(),
            }
        }
    }

    with pytest.raises(RuntimeError, match="evaluated_at"):
        formatter.format_memory_source_coverage(state)  # type: ignore[arg-type]

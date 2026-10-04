"""ActionAgentFormatter の短縮ID表示テスト。

LLMに渡す履歴が短縮IDのみで、UUID step_id を露出しないことを保証する。
"""

from __future__ import annotations

import re

from pantaray_agents.agents.action_agent.runtime.state import HistoryEntry
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.schema.agent.action import StepType

# UUID v4 のパターン（8-4-4-4-12 の16進数）
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
LEGACY_TOOL_REF_PATTERN = re.compile(
    r"\b[a-zA-Z0-9_]+#[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def test_format_history_contains_no_uuid() -> None:
    """format_history 出力にUUID形式が含まれないこと。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                HistoryEntry(
                    step_id="d41363fb-9dd2-46bb-b9ba-5ac7c9f581df",  # UUIDだが出力に出ない
                    step_number=1,
                    phase="executing",
                    step_type=StepType.LLM_OUTPUT,
                    summary="Test step",
                    started_at="2025-01-01T00:00:00Z",
                    completed_at="2025-01-01T00:00:01Z",
                    short_step_id="S-1-THINK",  # 短縮ID
                ),
                HistoryEntry(
                    step_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",  # UUIDだが出力に出ない
                    step_number=1,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Tool executed",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:01Z",
                    completed_at="2025-01-01T00:00:02Z",
                    short_step_id="S-1-TOOL",  # 短縮ID
                ),
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    # UUID パターンが含まれていないことを確認
    matches = UUID_PATTERN.findall(text)
    assert len(matches) == 0, f"UUID が出力に含まれています: {matches}"


def test_format_history_displays_short_ids() -> None:
    """format_history 出力にスコープ別の短縮IDが表示されること。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                HistoryEntry(
                    step_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    step_number=3,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Meta think",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:01Z",
                    completed_at="2025-01-01T00:00:02Z",
                    short_step_id="S-3-TOOL",
                ),
            ],
            "G1": [
                HistoryEntry(
                    step_id="d41363fb-9dd2-46bb-b9ba-5ac7c9f581df",
                    step_number=2,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Tool executed",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:00Z",
                    completed_at="2025-01-01T00:00:01Z",
                    short_step_id="G1-2-TOOL",
                ),
            ],
        }
    }

    supervisor_text = formatter.format_history(  # type: ignore[arg-type]
        state, scope_handle="S"
    )
    goal_text = formatter.format_history(state, scope_handle="G1")  # type: ignore[arg-type]

    # 短縮IDが表示されることを確認
    assert "S-3-TOOL" in supervisor_text
    assert "G1-2-TOOL" not in supervisor_text
    assert "G1-2-TOOL" in goal_text
    assert "S-3-TOOL" not in goal_text


def test_format_history_reference_uses_short_id_not_uuid() -> None:
    """履歴中の参照表記（Tool Result Ref / Thinking Ref）が短縮IDに統一され、tool_id#uuid 形式が出ないこと。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                HistoryEntry(
                    step_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                    step_number=2,
                    phase="executing",
                    step_type=StepType.LLM_OUTPUT,
                    summary="Meta think",
                    started_at="2025-01-01T00:00:01Z",
                    completed_at="2025-01-01T00:00:02Z",
                    short_step_id="S-2-THINK",
                ),
            ],
            "G1": [
                HistoryEntry(
                    step_id="d41363fb-9dd2-46bb-b9ba-5ac7c9f581df",
                    step_number=1,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Tool executed",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:00Z",
                    completed_at="2025-01-01T00:00:01Z",
                    short_step_id="G1-1-TOOL",
                ),
            ],
        }
    }

    text = formatter.format_history(state, scope_handle="G1")  # type: ignore[arg-type]

    # tool_id#uuid 形式が含まれていないことを確認
    assert LEGACY_TOOL_REF_PATTERN.search(text) is None
    # 短縮IDが参照として表示されることを確認
    assert "G1-1-TOOL" in text
    # G1 指定時は supervisor スコープ(S)を含めない仕様
    assert "S-2-THINK" not in text
    # UUID が直接表示されていないことを確認
    assert "d41363fb-9dd2-46bb-b9ba-5ac7c9f581df" not in text
    assert "a1b2c3d4-e5f6-7890-abcd-ef1234567890" not in text


def test_format_history_uses_one_history_ref_contract_for_all_step_types() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                HistoryEntry(
                    step_id="00000000-0000-0000-0000-000000000000",
                    step_number=1,
                    phase="executing",
                    step_type=StepType.LLM_OUTPUT,
                    summary="Selecting next tool",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:00Z",
                    completed_at="2025-01-01T00:00:01Z",
                    short_step_id="S-1-THINK",
                ),
                HistoryEntry(
                    step_id="11111111-1111-1111-1111-111111111111",
                    step_number=1,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Tool executed",
                    tool_id="memory_search",
                    started_at="2025-01-01T00:00:01Z",
                    completed_at="2025-01-01T00:00:02Z",
                    short_step_id="S-1-TOOL",
                ),
                HistoryEntry(
                    step_id="22222222-2222-2222-2222-222222222222",
                    step_number=2,
                    phase="executing",
                    step_type=StepType.LLM_OUTPUT,
                    summary="Selecting next tool",
                    tool_id="close_requirement",
                    started_at="2025-01-01T00:00:02Z",
                    completed_at="2025-01-01T00:00:03Z",
                    short_step_id="S-2-THINK",
                ),
                HistoryEntry(
                    step_id="33333333-3333-3333-3333-333333333333",
                    step_number=2,
                    phase="executing",
                    step_type=StepType.TOOL_EXECUTION,
                    summary="Tool executed",
                    tool_id="close_requirement",
                    started_at="2025-01-01T00:00:03Z",
                    completed_at="2025-01-01T00:00:04Z",
                    short_step_id="S-2-TOOL",
                ),
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    assert "History Ref: S-1-TOOL (use history_fetch)" in text
    assert "History Ref: S-2-TOOL (use history_fetch)" in text
    assert "Thinking Ref" not in text
    assert "Tool Result Ref" not in text


def test_format_history_displays_user_request_as_numbered_history_entry() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                HistoryEntry(
                    step_id="00000000-0000-0000-0000-000000000000",
                    step_number=1,
                    phase="init",
                    step_type=StepType.USER_REQUEST,
                    summary="",
                    user_request_text="Build history_fetch",
                    tool_id=None,
                    started_at="2025-01-01T00:00:00Z",
                    completed_at="2025-01-01T00:00:00Z",
                    short_step_id="S-1-USER",
                )
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    assert "### Step 1 [init]" in text
    assert "User Request:\n    Build history_fetch" in text
    assert "History Ref: S-1-USER (use history_fetch)" in text
    assert "Summary:" not in text

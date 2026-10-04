"""ActionAgentFormatter の履歴出力（history）整形テスト。

方針:
    tool の output は JSONValue であり、0 / False / None / "" / [] / {} などの
    falsy 値も「有効な結果」として扱う。
    そのため、truthy 判定で表示が欠落しないことを保証する。
    結果本文は各ツールの履歴ブロックが持つ。
"""

from __future__ import annotations

import pytest

from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.schema.agent.action import StepType


@pytest.mark.parametrize(
    ("output_value", "expected_fragment"),
    [
        (0, "\n    0"),
        (False, "\n    false"),
        (None, "\n    null"),
        ("", '\n    ""'),
        ([], "\n    []"),
        ({}, "\n    {}"),
    ],
)
def test_format_history_includes_falsy_tool_output(
    output_value: object, expected_fragment: str
) -> None:
    """直近ツール結果の output が falsy でも表示されること。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Tool executed",
                    "output": output_value,
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                }
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]
    assert "- Output:" in text
    assert expected_fragment in text


def test_format_history_omits_output_section_when_key_missing() -> None:
    """output キーが無い場合は Output セクションを出さないこと。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Tool executed",
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                }
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]
    assert "- Output:" not in text


def test_format_history_omits_attachment_data_urls() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Read attachment",
                    "output": {
                        "kind": "attachment",
                        "attachments": [
                            {
                                "type": "file",
                                "mime_type": "application/pdf",
                                "url": "data:application/pdf;base64,AAAA",
                            }
                        ],
                    },
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                }
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    assert "data:application/pdf;base64" not in text
    assert "<attachment data URL omitted>" in text


def test_format_history_filters_by_scope() -> None:
    """scope_handle が指定された場合、該当スコープの履歴のみが出力されること。"""
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Supervisor decision",
                    "tool_id": "read",
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                    "short_step_id": "S-1-TOOL",
                }
            ],
            "G1": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Goal Worker tool executed",
                    "tool_id": "memory_search",
                    "started_at": "2025-01-01T00:00:02Z",
                    "completed_at": "2025-01-01T00:00:03Z",
                    "short_step_id": "G1-1-TOOL",
                }
            ],
        }
    }

    supervisor_text = formatter.format_history(  # type: ignore[arg-type]
        state, scope_handle="S"
    )
    assert "S-1-TOOL" in supervisor_text
    assert "G1-1-TOOL" not in supervisor_text

    goal_text = formatter.format_history(state, scope_handle="G1")  # type: ignore[arg-type]
    assert "G1-1-TOOL" in goal_text
    assert "S-1-TOOL" not in goal_text


def test_format_history_preserves_tool_history_ref() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "G1": [
                {
                    "step_number": 4,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Tool executed",
                    "tool_id": "bash",
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                    "short_step_id": "G1-4-TOOL",
                }
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="G1")  # type: ignore[arg-type]

    assert "History Ref: G1-4-TOOL (use history_fetch)" in text


def test_format_history_keeps_earlier_user_turn_visible_after_tool_steps() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "init",
                    "step_type": StepType.USER_REQUEST,
                    "user_request_text": "Original user request",
                    "started_at": "2026-08-16T00:00:00Z",
                    "completed_at": "2026-08-16T00:00:00Z",
                    "short_step_id": "S-1-USER",
                },
                {
                    "step_number": 2,
                    "phase": "executing",
                    "step_type": StepType.LLM_OUTPUT,
                    "summary": "Choose a tool",
                    "started_at": "2026-08-16T00:00:01Z",
                    "completed_at": "2026-08-16T00:00:02Z",
                    "short_step_id": "S-2-THINK",
                },
                {
                    "step_number": 2,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "Tool completed",
                    "started_at": "2026-08-16T00:00:03Z",
                    "completed_at": "2026-08-16T00:00:04Z",
                    "short_step_id": "S-2-TOOL",
                },
                {
                    "step_number": 3,
                    "phase": "init",
                    "step_type": StepType.USER_REQUEST,
                    "user_request_text": "Follow-up request",
                    "started_at": "2026-08-16T01:00:00Z",
                    "completed_at": "2026-08-16T01:00:00Z",
                    "short_step_id": "S-3-USER",
                },
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    assert "User Request:\n    Original user request" in text
    assert "User Request:\n    Follow-up request" in text


@pytest.mark.parametrize("tool_result", [False, True])
def test_format_history_preserves_requests_and_attachment_refs_when_pruning(
    tool_result: bool,
) -> None:
    """本文を省略しても画像参照を保ち、USER 指示は全文を保持する。"""

    formatter = ActionAgentFormatter()
    ref = "user_attachment:" + "c" * 24
    storage_path = "user-1/2026-09-08/1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed.png"
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "init",
                    "step_type": StepType.USER_REQUEST,
                    "user_request_text": "x" * 4000,
                    "started_at": "2026-09-08T00:00:00Z",
                    "completed_at": "2026-09-08T00:00:00Z",
                    "short_step_id": "S-1-USER",
                    "attachments": [
                        {
                            "type": "file",
                            "source_kind": "local_image_blob",
                            "ref": ref,
                            "blob_ref": "attachment_blob_" + "c" * 24,
                            "display_path": "image-1.png",
                            "storage_path": storage_path,
                            "mime_type": "image/png",
                            "byte_size": 12,
                            "sha256": "c" * 64,
                        }
                    ],
                }
            ]
        }
    }

    if tool_result:
        entry = state["history_by_scope"]["S"][0]
        entry["step_type"] = StepType.TOOL_EXECUTION
        entry["output"] = {"content": "older result"}
    text = formatter.format_history(  # type: ignore[arg-type]
        state, scope_handle="S", omit_before_step_number=2
    )

    if tool_result:
        assert "older result" not in text
    else:
        assert "x" * 4000 in text
    assert f"- Attached images: {ref}" in text
    assert storage_path not in text


def test_format_history_omits_note_and_result_lines_when_unset() -> None:
    formatter = ActionAgentFormatter()
    state = {
        "history_by_scope": {
            "S": [
                {
                    "step_number": 1,
                    "phase": "executing",
                    "step_type": StepType.TOOL_EXECUTION,
                    "summary": "",
                    "tool_id": "read",
                    "started_at": "2025-01-01T00:00:00Z",
                    "completed_at": "2025-01-01T00:00:01Z",
                }
            ]
        }
    }

    text = formatter.format_history(state, scope_handle="S")  # type: ignore[arg-type]

    assert "- Note:" not in text
    assert "- Result:" not in text
    assert "- Tool: read" in text

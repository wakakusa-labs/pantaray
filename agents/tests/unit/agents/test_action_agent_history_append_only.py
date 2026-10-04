"""Action History の本文保持と通常ターンの追記規約。"""

from __future__ import annotations

import json
from typing import Any, cast

from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.schema.agent.action import StepType

_FORMATTER = ActionAgentFormatter()


def _entry(index: int, suffix: str, **overrides: Any) -> dict[str, Any]:
    step_type = StepType.LLM_OUTPUT if suffix == "THINK" else StepType.TOOL_EXECUTION
    return {
        "step_id": f"{suffix}-{index}",
        "step_number": index,
        "phase": "executing",
        "step_type": step_type,
        "summary": f"note {index}",
        "tool_id": "read",
        "started_at": f"2026-09-08T00:00:{index:02d}Z",
        "completed_at": f"2026-09-08T00:00:{index:02d}Z",
        "short_step_id": f"S-{index}-{suffix}",
    } | overrides


def _think(index: int, **overrides: Any) -> dict[str, Any]:
    return _entry(index, "THINK", **overrides)


def _tool(index: int, **overrides: Any) -> dict[str, Any]:
    return (
        _entry(index, "TOOL")
        | {
            "result_line": f"read: ok, {index} chars",
            "args": {"path": f"/tmp/{index}"},
            "output": {"kind": "file", "content": f"body {index}"},
        }
        | overrides
    )


def _turns(count: int) -> list[dict[str, Any]]:
    return [e for i in range(1, count + 1) for e in (_think(i), _tool(i))]


def _state(entries: list[dict[str, Any]]) -> Any:
    return cast(Any, {"history_by_scope": {"S": entries}})


def test_body_is_byte_identical_and_only_grows_when_a_step_is_added() -> None:
    before = _FORMATTER.format_history(_state(_turns(2)), scope_handle="S")

    assert before.encode("utf-8") == _FORMATTER.format_history(
        _state(_turns(2)), scope_handle="S"
    ).encode("utf-8")

    after = _FORMATTER.format_history(_state(_turns(3)), scope_handle="S")
    appended = after[len(before) :]
    assert after.startswith(before)
    assert appended.count("### Step ") == 1
    assert "### Step 3 [executing]" in appended


def test_every_body_row_keeps_note_result_tool_ref_and_time() -> None:
    text = _FORMATTER.format_history(_state(_turns(7)), scope_handle="S")

    for index in range(1, 8):
        assert f"- Note: note {index}" in text
        assert f"- Result: read: ok, {index} chars" in text
        assert f"- History Ref: S-{index}-TOOL (use history_fetch)" in text
    assert text.count("- Tool: read") == 7
    assert text.count("- Time: ") == 7
    assert "S-1-THINK" not in text
    assert text.count("- Args:") == 7
    assert text.count("- Output:") == 7
    for index in range(1, 8):
        assert f'"body {index}"' in text
        assert f'"/tmp/{index}"' in text


def test_think_row_is_rendered_only_when_validation_failed() -> None:
    failed = _think(1, result_line="Execution THINK output invalid")

    text = _FORMATTER.format_history(
        _state([failed, _think(2), _tool(2)]), scope_handle="S"
    )

    assert "- Result: Execution THINK output invalid" in text
    assert "S-2-THINK" not in text


def test_latest_batch_preserves_each_inline_result_and_its_continuation() -> None:
    """保存上限内の3件の結果を、バッチ数に応じてさらに切らない。"""
    body = {
        "kind": "file",
        "path": "/tmp/page.txt",
        "content": ("z" * 100 + "\n") * 180,
        "offset": 1,
        "column": 1,
        "end_line": 180,
        "end_column": 100,
        "total_lines": 300,
        "next_offset": 181,
        "next_column": 1,
        "truncated": True,
        "truncation_reason": "page_limit",
        "retry_hint": "Continue with offset=next_offset and column=next_column.",
    }
    entries = [_think(1), *(_tool(index, output=body) for index in (1, 2, 3))]

    text = _FORMATTER.format_history(_state(entries), scope_handle="S")

    assert text.count(json.dumps(body["content"])) == 3
    assert text.count('"next_offset": 181') == 3
    assert text.count('"next_column": 1') == 3

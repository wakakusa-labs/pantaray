"""実行系が決定論的に書く結果行（build_outcome_line）の規則テスト。"""

from __future__ import annotations

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.outcome_line import (
    OutcomeStatus,
    build_outcome_line,
)
from pantaray_agents.schema.agent.base import JSONValue


@pytest.mark.parametrize(
    ("tool_id", "output", "expected"),
    [
        ("read", {"kind": "file", "content": "x" * 12_340}, "read: ok, 12,340 chars"),
        ("read", {"kind": "directory", "entries": [{}]}, "read: ok, 1 entry"),
        ("glob", {"matches": [{}, {}, {}]}, "glob: ok, 3 matches"),
        ("grep", {"matches": []}, "grep: ok, 0 matches"),
        (
            "bash",
            {"exit_code": 2, "stdout": "abc", "stderr": "de"},
            "bash: ok, exit 2, 5 chars",
        ),
        (
            "apply_patch",
            {"applied_paths": ["a.py"], "diff": "--- a\n+++ b\n+one\n+two\n-old\n"},
            "apply_patch: ok, 1 file, +2 -1",
        ),
        (
            "web_extract",
            {"results": [{"url": "https://example.test/a", "raw_content": "y" * 2300}]},
            "web_extract: ok, 2,300 chars, https://example.test/a",
        ),
        (
            "history_fetch",
            {"refs": ["S-1-TOOL", "S-2-TOOL"], "content": "abc", "next_cursor": None},
            "history_fetch: ok, 2 refs, 3 chars",
        ),
        (
            "history_fetch",
            {"refs": ["S-1-THINK"], "content": "abc", "next_cursor": "a" * 64 + ":3"},
            "history_fetch: ok, 1 ref, 3 chars, more available",
        ),
        (
            "wait_subagents",
            {"results": [{"status": "completed"}, {"status": "running"}]},
            "wait_subagents: ok, 1 done, 1 running",
        ),
        ("zanei_timeline", {"events": [{}]}, "zanei_timeline: ok, 1 event"),
        (
            "render_pdf_page",
            {"kind": "pdf_pages", "attachments": [{}, {}]},
            "render_pdf_page: ok, 2 pages",
        ),
        # Not an empty render: the model is told the pages can come later.
        (
            "render_pdf_page",
            {"kind": "renderer_preparing", "path": "a.pptx", "message": "..."},
            "render_pdf_page: ok, no pages yet, viewer preparing",
        ),
        ("thinking", {"thought": "..."}, "thinking: ok"),
        ("some_new_tool", {"anything": 1}, "some_new_tool: ok"),
        (
            "bash",
            {
                "storage": "action_file",
                "path": "/x/out.json",
                "character_count": 40_000,
            },
            "bash: ok, spilled to /x/out.json, 40,000 chars",
        ),
    ],
)
def test_success_outcome_lines(tool_id: str, output: JSONValue, expected: str) -> None:
    assert build_outcome_line(tool_id, output, status="ok") == expected


@pytest.mark.parametrize(
    ("output", "error_code", "expected"),
    [
        # ツール自身が申告したコードを優先する。
        (
            {"error": {"type": "conflict", "code": "PATCH_LOCK_CONFLICT"}},
            "ACTION_TOOL_APPLY_PATCH_FAILED",
            "apply_patch: failed PATCH_LOCK_CONFLICT",
        ),
        # 例外クラス名はコードとして扱わず、呼び出し側のコードへ落とす。
        (
            {"error": {"error_type": "ToolValidationError", "message": "bad args"}},
            "ACTION_TOOL_ARGS_INVALID",
            "apply_patch: failed ACTION_TOOL_ARGS_INVALID",
        ),
        (None, None, "apply_patch: failed UNKNOWN"),
    ],
)
def test_failure_outcome_lines(
    output: JSONValue, error_code: str | None, expected: str
) -> None:
    line = build_outcome_line(
        "apply_patch", output, status="failed", error_code=error_code
    )

    assert line == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("awaiting_approval", "bash: awaiting approval"),
        ("stopped", "bash: stopped"),
    ],
)
def test_non_terminal_outcomes(status: OutcomeStatus, expected: str) -> None:
    assert build_outcome_line("bash", {"exit_code": 0}, status=status) == expected


@pytest.mark.parametrize(
    ("status", "output", "expected"),
    [
        # 発行される前に止まった呼び出しだけが「実行されなかった」と言い切れる。
        ("not_executed", {}, "bash: not executed, stopped by user"),
        # 発行済みの呼び出しは結果が不明。1 行にもそう書く。
        ("interrupted", {}, "bash: interrupted by user, outcome unknown"),
        (
            "interrupted",
            {"exit_code": -1, "stdout": "abc", "stderr": "de"},
            "bash: interrupted by user, outcome unknown, 5 chars captured",
        ),
    ],
)
def test_stopped_call_outcomes(
    status: OutcomeStatus, output: JSONValue, expected: str
) -> None:
    assert build_outcome_line("bash", output, status=status) == expected

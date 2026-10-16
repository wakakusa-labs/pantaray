"""history_fetch の短縮 ID 配列入力契約。"""

from __future__ import annotations

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    ToolValidationError,
    _validate_tool_args,
)
from pantaray_agents.agents.action_agent.tools import HISTORY_FETCH_TOOL
from pantaray_agents.agents.action_agent.tools.history_fetch_tool import (
    HISTORY_FETCH_MAX_REFS,
)


def test_history_fetch_accepts_mixed_numbered_history_refs() -> None:
    args = {"refs": ["S-1-USER", "S-2-THINK", "G1-3-TOOL"]}

    _validate_tool_args(HISTORY_FETCH_TOOL, args)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"refs": []},
        {"refs": "S-1-USER"},
        {"refs": ["S-1-USER", "S-1-USER"]},
        {"refs": ["SUGGESTION:sug-1"]},
        {"refs": ["CRITIC:critic-1"]},
        {"refs": ["d41363fb-9dd2-46bb-b9ba-5ac7c9f581df"]},
        {"refs": ["S-1-user"]},
        {"refs": ["G0-1-TOOL"]},
        {"refs": ["S-0-THINK"]},
        {"refs": [" S-1-USER"]},
        {"refs": ["S-1-USER "]},
        {"refs": ["S-1-USER"], "extra": True},
        {"refs": ["S-1-USER"], "cursor": True},
        {"refs": ["S-1-USER"], "cursor": "not-a-cursor"},
        {"refs": ["S-1-USER"], "cursor": "a" * 64 + ":-1"},
        {"refs": ["S-1-USER"], "cursor": "a" * 64 + ":" + "9" * 100},
        {
            "refs": [
                f"S-{index + 1}-THINK" for index in range(HISTORY_FETCH_MAX_REFS + 1)
            ]
        },
    ],
)
def test_history_fetch_rejects_noncanonical_or_unbounded_input(
    args: dict[str, object],
) -> None:
    with pytest.raises(ToolValidationError):
        _validate_tool_args(HISTORY_FETCH_TOOL, args)


def test_history_fetch_cursor_bounds_its_length_in_the_pattern_alone() -> None:
    # A strict tool schema whose string carries both `pattern` and `maxLength` made
    # gpt-6.1-sol, gpt-6-luna and gpt-6-astra answer `incomplete: max_output_tokens`
    # with zero tokens, so every Action turn that offered the tool failed.
    cursor = HISTORY_FETCH_TOOL.input_schema["properties"]["cursor"]

    assert "pattern" in cursor
    assert "maxLength" not in cursor

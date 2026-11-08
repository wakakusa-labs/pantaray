"""The TODO list's switch is one decision kept in two runtimes.

The main window reads `frontend/src/todos/todoUxFeature.ts` and this backend
reads `pantaray_agents.todo_ux_feature`. Flipped on one side only, the window
would show the TODO list while nothing picks its TODOs up, or the reverse.
"""

from __future__ import annotations

import re
from pathlib import Path

from pantaray_agents import todo_ux_feature

FRONTEND_SWITCH_PATH = (
    Path(__file__).resolve().parents[3] / "frontend/src/todos/todoUxFeature.ts"
)


def test_the_backend_and_the_main_window_read_the_same_switch() -> None:
    declared = re.findall(
        r"^export const TODO_UX_ENABLED = (true|false);$",
        FRONTEND_SWITCH_PATH.read_text("utf-8"),
        re.M,
    )

    assert declared == [str(todo_ux_feature.TODO_UX_ENABLED).lower()]

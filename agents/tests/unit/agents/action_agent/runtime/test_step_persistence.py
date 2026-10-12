from __future__ import annotations

from pathlib import Path


def test_runtime_persists_action_steps_only_through_step_helpers() -> None:
    runtime_root = (
        Path(__file__).parents[5] / "src/pantaray_agents/agents/action_agent/runtime"
    )
    allowed_roots = {runtime_root / "steps"}

    offenders: list[str] = []
    for path in runtime_root.rglob("*.py"):
        if any(path.is_relative_to(allowed_root) for allowed_root in allowed_roots):
            continue
        text = path.read_text(encoding="utf-8")
        if ".repository.save_action_step(" in text:
            offenders.append(str(path.relative_to(runtime_root)))

    assert offenders == []

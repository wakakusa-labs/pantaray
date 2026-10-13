from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pantaray_agents.local_runtime.tooling.brokering.broker_patch_read_gate import (
    build_needs_read_output,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchUpdateChange,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    release_stored_tool_result,
    store_tool_result,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    build_action_subagent_broker_tools,
)
from pantaray_agents.tools.contract import validate_json_value


def _response_schema(tmp_path: Path, tool_name: str) -> object:
    tools = build_action_subagent_broker_tools(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        payload=cast(Any, {}),
        authority=cast(Any, None),
        agents_md=cast(Any, None),
    )
    return next(tool for tool in tools if tool.name == tool_name).response_schema


def _stored(tmp_path: Path, output: JSONValue) -> JSONValue:
    stored = store_tool_result(
        action_tool_results_path=tmp_path, invocation_id="bash", output=output
    )
    release_stored_tool_result(stored)
    return stored.output_json


def test_child_tool_accepts_its_result_with_long_texts_moved_out(
    tmp_path: Path,
) -> None:
    schema = cast(Any, _response_schema(tmp_path, "bash"))
    output: dict[str, JSONValue] = {
        "status": "success",
        "exit_code": 0,
        "stdout": "line\n" * 5_000,
        "stderr": "",
    }
    moved = _stored(tmp_path, output)
    assert isinstance(moved, dict)
    assert isinstance(moved["stdout"], dict)

    assert validate_json_value(schema=schema, value=output) is None
    assert validate_json_value(schema=schema, value=moved) is None
    broken = dict(moved)
    broken["stdout"] = {**cast(dict[str, JSONValue], moved["stdout"]), "extra": 1}
    assert validate_json_value(schema=schema, value=broken) is not None


def test_child_patch_accepts_target_windows_with_their_text_moved_out(
    tmp_path: Path,
) -> None:
    schema = cast(Any, _response_schema(tmp_path, "apply_patch"))
    lines = [f"{number:04d} " + "x" * 300 for number in range(1_500)]
    lines[750] = "old"
    output = build_needs_read_output(
        path="large.txt",
        text="\n".join(lines) + "\n",
        change=ApplyPatchUpdateChange.model_validate(
            {
                "op": "update",
                "path": "large.txt",
                "edits": [{"old_lines": ["old"], "new_lines": ["new"]}],
            }
        ),
    )
    assert output["read_scope"] == "target_windows"
    moved = _stored(tmp_path, output)
    assert isinstance(moved, dict)
    windows = cast(list[dict[str, JSONValue]], moved["windows"])
    assert isinstance(windows[0]["text"], dict)

    # The window's schema is a $ref into the output schema's $defs.
    assert validate_json_value(schema=schema, value=output) is None
    assert validate_json_value(schema=schema, value=moved) is None

"""Canonical tool-output contracts shared by runtime projectors."""

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pantaray_agents.schema.agent.base import JSONValue

type UnprojectedToolOutput = JSONValue | bytes
type ToolOutputStorageKind = Literal["inline_json", "action_file"]
type ToolOutputOwnerKind = Literal["tool_invocation", "action_step"]


class FormalToolStepOutput(BaseModel):
    """Exact durable envelope stored in ``agent_action_steps.tool_output``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    status: Literal["success", "error", "processing"]
    output: JSONValue
    output_storage_kind: ToolOutputStorageKind
    output_owner_kind: ToolOutputOwnerKind


def serialize_json_tool_output(output: JSONValue) -> str:
    """Serialize one JSON tool output exactly as it is measured for projection."""

    return json.dumps(
        output,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def largest_fitting_size(
    build: Callable[[int], JSONValue], *, high: int, limit: int
) -> int:
    """The largest n in 0..high whose ``build(n)`` serializes within ``limit``
    characters, or 0 when none does.

    The search halves, taking the output to grow with n; where it does not, the
    n found still fits but may not be the largest. The output is measured
    serialized because escaping lengthens text by an amount only its content
    decides.
    """

    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if len(serialize_json_tool_output(build(middle))) <= limit:
            low = middle
        else:
            high = middle - 1
    return low


def build_runtime_tool_error_output(
    *,
    error_type: str,
    message: str,
    details: dict[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    error: dict[str, JSONValue] = {
        "error_type": error_type,
        "message": message,
        "attempt": 1,
        "max_attempts": 1,
        "traceback": "",
    }
    if details is not None:
        error["details"] = details
    return {"status": "error", "error": error}


__all__ = [
    "FormalToolStepOutput",
    "ToolOutputOwnerKind",
    "ToolOutputStorageKind",
    "UnprojectedToolOutput",
    "build_runtime_tool_error_output",
    "largest_fitting_size",
    "serialize_json_tool_output",
]

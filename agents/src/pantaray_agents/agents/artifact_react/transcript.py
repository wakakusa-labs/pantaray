from __future__ import annotations

import json

from pantaray_agents.tools.contract import ReactToolResult


def build_prompt_with_transcript(
    *,
    initial_prompt: str,
    tool_results: tuple[ReactToolResult, ...],
    last_error: str | None,
) -> str:
    parts = [initial_prompt]
    if tool_results:
        parts.append("# Tool Results")
        for index, result in enumerate(tool_results, start=1):
            response_text = json.dumps(result.output, ensure_ascii=False)
            parts.append(f"{index}. {result.tool_name}: {response_text}")
    if last_error:
        parts.append(f"# Previous Error\n{last_error}")
    return "\n\n".join(part for part in parts if part)

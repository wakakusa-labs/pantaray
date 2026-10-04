"""thinking ツール実行。"""

from __future__ import annotations

from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    optional_string_arg as _optional_string_arg,
)
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    StateTokenSink,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from .shared import ThinkingPayload, UnprojectedToolExecutionResult


async def run_thinking_tool(
    agent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
    *,
    sink: StateTokenSink,
) -> UnprojectedToolExecutionResult:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("thinking tool requires `query`.")

    context_text = _optional_string_arg(args, "context") or "(none)"
    runtime_cfg = tool_def.runtime_config or {}
    prompt_template = str(
        runtime_cfg.get(
            "prompt_template",
            (
                "You are the Action Agent's internal reasoning tool.\n"
                "Think deeply about the topic. Do NOT use any special tags.\n"
                "## Topic\n{query}\n\n"
                "## Reference\n{context_json}\n"
            ),
        )
    )
    prompt = prompt_template.format(query=query, context_json=context_text)
    system_instruction = str(
        runtime_cfg.get(
            "system_instruction",
            "As internal reasoning, analyze in English in detail. Output plain text only.",
        )
    )
    usage_before = sink.delta
    runner = agent.create_tool_llm_runner(tool_id=tool_def.tool_id)
    # The internal reasoning tool builds its prompt from `query`/`context` only, so
    # no attachment ref can appear in it and no image is passed here.
    response_text = await runner.generate_text(
        prompt=prompt,
        sink=sink,
        system_instruction=system_instruction,
        stage=f"tool::{tool_def.tool_id}",
    )
    usage_after = sink.delta
    prompt_tokens = (usage_after.prompt_tokens or 0) - (usage_before.prompt_tokens or 0)
    completion_tokens = (usage_after.completion_tokens or 0) - (
        usage_before.completion_tokens or 0
    )

    result_payload: ThinkingPayload = {
        "text": str(response_text or "").strip(),
    }

    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, result_payload),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

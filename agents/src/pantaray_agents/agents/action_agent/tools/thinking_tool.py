"""thinking ツール定義。"""

from __future__ import annotations

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

THINKING_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="thinking",
        name="Deep Thought",
        description=(
            "Internal deep reasoning tool that returns detailed thinking and a concise "
            "answer (not shown to the user)."
        ),
        guide=ToolGuideSpec(
            what=(
                "Use this for hidden internal reasoning and drafting only. It helps "
                "the agent analyze, compare options, and derive conclusions without "
                "fetching external facts or stored memories."
            ),
            when=(
                "Use when you need deep thinking: analysis, synthesis, prioritization, "
                "idea divergence/convergence, or simulating logic/implications. External "
                "facts are not required."
            ),
            pitfalls="Do not use for fact acquisition. Avoid vague queries; keep one topic per call.",
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="query",
                    schema={"type": "string"},
                    required=True,
                    description="Question or topic to examine in depth.",
                ),
                field_spec(
                    name="context",
                    schema={"type": "string"},
                    description="Optional supplemental context (free text).",
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Deep reasoning output (plain text).",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        runtime_config={
            "prompt_template": (
                "You are the Action Agent's internal reasoning tool.\n"
                "Think deeply about the topic. Do NOT use any special tags.\n"
                "## Topic\n{query}\n\n"
                "## Reference\n{context_json}\n"
            ),
            "system_instruction": "As internal reasoning, analyze in English in detail. Output plain text only.",
        },
    )
)

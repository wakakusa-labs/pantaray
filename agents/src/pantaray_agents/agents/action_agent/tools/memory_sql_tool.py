"""memory_sql ツール定義。文面とスキーマは共有ツール側が持つ。"""

from __future__ import annotations

from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.memory.sql import (
    DEFAULT_MEMORY_SQL_LIMIT,
    MAX_MEMORY_SQL_LIMIT,
)
from pantaray_agents.tools.memory.sql_tool import (
    MEMORY_SQL_DESCRIPTION,
    MEMORY_SQL_LIMIT_FIELD,
    MEMORY_SQL_LIMIT_FIELD_DESCRIPTION,
    MEMORY_SQL_PITFALLS,
    MEMORY_SQL_RESULT_SCHEMA,
    MEMORY_SQL_SQL_FIELD,
    MEMORY_SQL_SQL_FIELD_DESCRIPTION,
    MEMORY_SQL_TOOL_NAME,
    MEMORY_SQL_WHAT,
    MEMORY_SQL_WHEN,
)

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

MEMORY_SQL_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=MEMORY_SQL_TOOL_NAME,
        name="Memory SQL",
        description=MEMORY_SQL_DESCRIPTION,
        guide=ToolGuideSpec(
            what=MEMORY_SQL_WHAT, when=MEMORY_SQL_WHEN, pitfalls=MEMORY_SQL_PITFALLS
        ),
        concurrency=ToolConcurrency("parallel"),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="sql",
                    schema=MEMORY_SQL_SQL_FIELD,
                    required=True,
                    description=MEMORY_SQL_SQL_FIELD_DESCRIPTION,
                ),
                field_spec(
                    name="limit",
                    schema=MEMORY_SQL_LIMIT_FIELD,
                    description=MEMORY_SQL_LIMIT_FIELD_DESCRIPTION,
                ),
            )
        ),
        output_schema=MEMORY_SQL_RESULT_SCHEMA,
        runtime_config={
            "default_limit": DEFAULT_MEMORY_SQL_LIMIT,
            "max_limit": MAX_MEMORY_SQL_LIMIT,
        },
    )
)

__all__ = ["MEMORY_SQL_TOOL"]

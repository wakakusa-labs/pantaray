"""web_extract ツール定義。文面とスキーマは共有ツール側が持つ。"""

from __future__ import annotations

from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.web.definitions import (
    WEB_EXTRACT_FIELDS,
    WEB_EXTRACT_PITFALLS,
    WEB_EXTRACT_REQUIRED,
    WEB_EXTRACT_RESULT_SCHEMA,
    WEB_EXTRACT_TOOL_NAME,
    WEB_EXTRACT_WHAT,
    WEB_EXTRACT_WHEN,
)

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .web_content_validation import shared_fields, validate_web_extract_args

WEB_EXTRACT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WEB_EXTRACT_TOOL_NAME,
        name="Web Extract",
        description=WEB_EXTRACT_WHAT,
        guide=ToolGuideSpec(
            what=WEB_EXTRACT_WHAT, when=WEB_EXTRACT_WHEN, pitfalls=WEB_EXTRACT_PITFALLS
        ),
        concurrency=ToolConcurrency("parallel"),
        execution_policy=tool_execution_policy(
            intent_class="network_access",
            default_timeout_ms=60_000,
        ),
        input_spec=InputSpec(
            fields=shared_fields(WEB_EXTRACT_FIELDS, WEB_EXTRACT_REQUIRED)
        ),
        output_schema=WEB_EXTRACT_RESULT_SCHEMA,
        runtime_config={"max_retries": 3},
        pre_validate_args=validate_web_extract_args,
    )
)

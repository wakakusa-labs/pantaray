"""web_search ツール定義。文面とスキーマは共有ツール側が持つ。"""

from __future__ import annotations

from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.web.definitions import (
    WEB_SEARCH_FIELDS,
    WEB_SEARCH_PITFALLS,
    WEB_SEARCH_REQUIRED,
    WEB_SEARCH_RESULT_SCHEMA,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_WHAT,
    WEB_SEARCH_WHEN,
)

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .web_content_validation import shared_fields, validate_web_search_args

WEB_SEARCH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WEB_SEARCH_TOOL_NAME,
        name="Web Search",
        description=WEB_SEARCH_WHAT,
        guide=ToolGuideSpec(
            what=WEB_SEARCH_WHAT, when=WEB_SEARCH_WHEN, pitfalls=WEB_SEARCH_PITFALLS
        ),
        concurrency=ToolConcurrency("parallel"),
        execution_policy=tool_execution_policy(
            intent_class="network_access",
            default_timeout_ms=60_000,
        ),
        input_spec=InputSpec(
            fields=shared_fields(WEB_SEARCH_FIELDS, WEB_SEARCH_REQUIRED)
        ),
        output_schema=WEB_SEARCH_RESULT_SCHEMA,
        runtime_config={"max_retries": 3},
        pre_validate_args=validate_web_search_args,
    )
)

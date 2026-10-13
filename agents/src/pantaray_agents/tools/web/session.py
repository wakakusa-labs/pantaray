"""web_search and web_extract as tools of Suggestion research and the chat.

The words, schemas and execution are the shared ones the Action uses too. A
result too large to keep inline goes to the run's own folder the way the file
tools' results do, and the model reads the rest from there.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.tooling.tool_result_spill import (
    accepting_spilled_texts,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    release_stored_tool_result,
    store_tool_result,
)
from pantaray_agents.local_runtime.web_tools.client import (
    WebToolsWrapperContext,
    build_web_tools_wrapper_context,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
)

from .definitions import (
    WEB_EXTRACT_FIELDS,
    WEB_EXTRACT_PITFALLS,
    WEB_EXTRACT_REQUIRED,
    WEB_EXTRACT_RESULT_SCHEMA,
    WEB_EXTRACT_TOOL_NAME,
    WEB_EXTRACT_WHAT,
    WEB_EXTRACT_WHEN,
    WEB_SEARCH_FIELDS,
    WEB_SEARCH_PITFALLS,
    WEB_SEARCH_REQUIRED,
    WEB_SEARCH_RESULT_SCHEMA,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_WHAT,
    WEB_SEARCH_WHEN,
    request_schema,
)
from .execute import run_web_extract, run_web_search

# One provider attempt per call: research moves on rather than waiting.
_MAX_RETRIES = 1
_SPILL_ROOT_MODE = 0o700
_WHOLE_RESULT_STORED: dict[str, JSONValue] = {"required": ["storage", "path"]}

type _Run = Callable[..., Awaitable[dict[str, JSONValue]]]


@dataclass(frozen=True, slots=True)
class WebResearchToolSession:
    """Web research tools for one run.

    ``speaks_to_user`` is set where the model answers the user directly (the
    chat): only there does a failure the user can fix ask the model to tell
    them how. ``spill_root`` is the run's folder for large results.
    """

    user_id: str
    speaks_to_user: bool
    spill_root: Path

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("web research session user_id must not be empty")

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return (
            self._definition(
                name=WEB_SEARCH_TOOL_NAME,
                parts=(WEB_SEARCH_WHAT, WEB_SEARCH_WHEN, WEB_SEARCH_PITFALLS),
                request=request_schema(WEB_SEARCH_FIELDS, WEB_SEARCH_REQUIRED),
                result=WEB_SEARCH_RESULT_SCHEMA,
                run=run_web_search,
            ),
            self._definition(
                name=WEB_EXTRACT_TOOL_NAME,
                parts=(WEB_EXTRACT_WHAT, WEB_EXTRACT_WHEN, WEB_EXTRACT_PITFALLS),
                request=request_schema(WEB_EXTRACT_FIELDS, WEB_EXTRACT_REQUIRED),
                result=WEB_EXTRACT_RESULT_SCHEMA,
                run=run_web_extract,
            ),
        )

    def _definition(
        self,
        *,
        name: str,
        parts: tuple[str, ...],
        request: dict[str, JSONValue],
        result: dict[str, JSONValue],
        run: _Run,
    ) -> ReactToolDefinition:
        async def execute(call: ReactToolCall, _step: int) -> ReactToolResult:
            assert isinstance(call.tool_args, Mapping)  # the registry checked it
            output = await run(
                context=self._context(),
                args=call.tool_args,
                max_retries=_MAX_RETRIES,
                speaks_to_user=self.speaks_to_user,
            )
            return self._result(call.tool_name, output)

        return ReactToolDefinition(
            name=name,
            description="\n\n".join(parts),
            request_schema=request,
            response_schema=react_tool_response_schema(
                # Storage moves long texts out, or, failing that, the whole result.
                success_schema={
                    "anyOf": [accepting_spilled_texts(result), _WHOLE_RESULT_STORED]
                }
            ),
            execute=execute,
            # Each call is its own request, and a spill goes to a file of its own.
            concurrency=ToolConcurrency("parallel"),
        )

    def _result(self, tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
        if output["status"] != "success":
            message = output["message"]
            return ReactToolResult(
                tool_name=tool_name,
                status="error",
                output=output,
                error_message=message if isinstance(message, str) else None,
            )
        self.spill_root.mkdir(mode=_SPILL_ROOT_MODE, parents=True, exist_ok=True)
        stored = store_tool_result(
            action_tool_results_path=self.spill_root,
            invocation_id=uuid.uuid4().hex,
            output=output,
        )
        release_error = release_stored_tool_result(stored)
        if release_error is not None:
            raise release_error
        return ReactToolResult(
            tool_name=tool_name, status="success", output=stored.output_json
        )

    def _context(self) -> WebToolsWrapperContext:
        return build_web_tools_wrapper_context(user_id=self.user_id, action_id=None)


__all__ = ["WebResearchToolSession"]

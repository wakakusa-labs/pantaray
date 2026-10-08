from __future__ import annotations

from dataclasses import dataclass, field

from pantaray_agents.local_runtime.web_tools import (
    WebContentExecutionError,
    WebContentInvalidResponseError,
    WebToolsWrapperContext,
    build_web_tools_wrapper_context,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    ProxySurfaceId,
    build_proxy_agent_error,
)

from .definitions import build_web_research_definitions
from .fetch import (
    WebExtractFailure,
    WebExtractResponse,
    WebSearchResponse,
    fetch_web_extract,
    fetch_web_search,
)
from .paging import web_extract_pages, web_search_page


def _arguments(call: ReactToolCall) -> dict[str, JSONValue]:
    if not isinstance(call.tool_args, dict):
        raise AssertionError("validated tool arguments must be an object")
    return call.tool_args


def _string_arg(args: dict[str, JSONValue], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(f"validated {name} must be a non-empty string")
    return value


def _integer_arg(args: dict[str, JSONValue], name: str) -> int:
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError(f"validated {name} must be an integer")
    return value


def _optional_string_arg(args: dict[str, JSONValue], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AssertionError(f"validated {name} must be a non-empty string")
    return value


def _string_array_arg(args: dict[str, JSONValue], name: str) -> tuple[str, ...]:
    value = args.get(name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise AssertionError(f"validated {name} must be a non-empty string array")
    return tuple(item for item in value if isinstance(item, str))


def _success(tool_name: str, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=tool_name, status="success", output=output)


# A failure only the user can fix states its reason and the fix in the user's
# terms, keyed like the Action's connection terminals: one code has a different
# fix per connection and the raiser's suggested action says which. Every other
# failure is the system's, so the model gets no code or provider detail to pass
# on.
_USER_FIX_BY_FAILURE: dict[tuple[str, str], tuple[str, str]] = {
    (PROXY_CONNECTION_NOT_CONFIGURED, "configure_connection"): (
        "Web search is not set up",
        "they can turn it on by signing in to Pantaray or by saving a Tavily "
        "API key in Settings > AI connection > Web search",
    ),
    (PROXY_AUTHENTICATION_FAILED, "configure_connection"): (
        "The Tavily API key saved for web search was rejected",
        "they can save a working key in Settings > AI connection > Web search",
    ),
    (PROXY_AUTHENTICATION_FAILED, "reauthenticate"): (
        "The user's Pantaray sign-in has expired",
        "web search works again once they sign in",
    ),
}


def _web_tool_error(
    *,
    tool_name: str,
    surface_id: ProxySurfaceId,
    error: Exception,
    speaks_to_user: bool,
) -> ReactToolResult:
    user_fix = None
    if isinstance(error, WebContentExecutionError):
        suggested_action = build_proxy_agent_error(
            surface_id=surface_id,
            error_code=error.error_code,
            suggested_action=error.suggested_action,
        )["error_details"]["suggested_action"]
        user_fix = _USER_FIX_BY_FAILURE.get((error.error_code, suggested_action))
    if user_fix is None:
        return tool_error_response(
            tool_name=tool_name,
            error_code="WEB_TOOL_FAILED",
            message="Web search is unavailable right now, so nothing was looked up.",
        )
    reason, fix = user_fix
    return tool_error_response(
        tool_name=tool_name,
        error_code="WEB_TOOL_NEEDS_USER",
        message=f"{reason}, so nothing was looked up."
        + (f" Tell the user {fix}." if speaks_to_user else ""),
    )


@dataclass(slots=True)
class WebResearchToolSession:
    """Web research tools for one run.

    ``speaks_to_user`` is set where the model answers the user directly (the
    chat): only there does a failure the user can fix ask the model to tell
    them how. Suggestion research would otherwise turn it into a nag.
    """

    user_id: str
    speaks_to_user: bool
    search_snapshots: dict[str, WebSearchResponse] = field(default_factory=dict)
    extract_snapshots: dict[tuple[str, str | None], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("web research session user_id must not be empty")

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return build_web_research_definitions(
            web_search=self.web_search,
            web_extract=self.web_extract,
        )

    async def web_search(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _arguments(call)
        query = _string_arg(args, "query")
        snapshot = self.search_snapshots.get(query)
        if snapshot is None:
            try:
                snapshot = await fetch_web_search(
                    context=self._context(),
                    query=query,
                    max_retries=1,
                )
            except (WebContentExecutionError, WebContentInvalidResponseError) as exc:
                return _web_tool_error(
                    tool_name=call.tool_name,
                    surface_id="web_search",
                    error=exc,
                    speaks_to_user=self.speaks_to_user,
                )
            self.search_snapshots[query] = snapshot
        return _success(
            call.tool_name,
            web_search_page(
                snapshot=snapshot,
                offset=_integer_arg(args, "offset"),
                limit=_integer_arg(args, "limit"),
            ),
        )

    async def web_extract(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _arguments(call)
        requested_urls = _string_array_arg(args, "urls")
        query = _optional_string_arg(args, "query")
        missing_urls = tuple(
            url for url in requested_urls if (url, query) not in self.extract_snapshots
        )
        failures: tuple[WebExtractFailure, ...] = ()
        if missing_urls:
            try:
                response = await fetch_web_extract(
                    context=self._context(),
                    urls=missing_urls,
                    query=query,
                    max_retries=1,
                )
                contents = _contents_by_url(response)
                failures = response.failed_results
                _validate_web_extract_coverage(
                    requested_urls=missing_urls,
                    contents=contents,
                    failures=failures,
                )
            except (WebContentExecutionError, WebContentInvalidResponseError) as exc:
                return _web_tool_error(
                    tool_name=call.tool_name,
                    surface_id="web_extract",
                    error=exc,
                    speaks_to_user=self.speaks_to_user,
                )
            for url, content in contents.items():
                self.extract_snapshots[(url, query)] = content
        return _success(
            call.tool_name,
            web_extract_pages(
                urls=requested_urls,
                query=query,
                offset=_integer_arg(args, "offset"),
                limit=_integer_arg(args, "limit"),
                snapshots=self.extract_snapshots,
                failures=failures,
            ),
        )

    def _context(self) -> WebToolsWrapperContext:
        return build_web_tools_wrapper_context(user_id=self.user_id, action_id=None)


def _contents_by_url(response: WebExtractResponse) -> dict[str, str]:
    contents: dict[str, str] = {}
    for page in response.results:
        if page.url in contents:
            raise WebContentInvalidResponseError("duplicate result URL")
        contents[page.url] = page.raw_content
    return contents


def _validate_web_extract_coverage(
    *,
    requested_urls: tuple[str, ...],
    contents: dict[str, str],
    failures: tuple[WebExtractFailure, ...],
) -> None:
    returned_urls = [*contents, *(failure.url for failure in failures)]
    if len(returned_urls) != len(set(returned_urls)):
        raise WebContentInvalidResponseError("duplicate web_extract URL")
    if set(returned_urls) != set(requested_urls):
        raise WebContentInvalidResponseError("web_extract URL coverage")


__all__ = ["WebResearchToolSession"]

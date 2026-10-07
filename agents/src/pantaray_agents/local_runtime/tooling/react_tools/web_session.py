from __future__ import annotations

from dataclasses import dataclass, field

from pantaray_agents.local_runtime.web_tools import (
    WebContentExecutionError,
    WebContentInvalidResponseError,
    build_web_tools_wrapper_context,
    invoke_web_tools_wrapper,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_llm.profiles import (
    WEB_EXTRACT_PROFILE_ID,
    WEB_SEARCH_PROFILE_ID,
)

from .web_definitions import build_web_research_definitions
from .web_paging import (
    WebExtractFailure,
    WebSearchSnapshot,
    parse_web_extract_result,
    parse_web_search_snapshot,
    web_extract_pages,
    web_search_page,
)


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


def _expected_error(
    *, tool_name: str, error_code: str, error: Exception
) -> ReactToolResult:
    return tool_error_response(
        tool_name=tool_name,
        error_code=error_code,
        message=str(error) or type(error).__name__,
    )


@dataclass(slots=True)
class WebResearchToolSession:
    user_id: str
    search_snapshots: dict[str, WebSearchSnapshot] = field(default_factory=dict)
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
            result = await self._run_web_tool(
                call=call,
                profile_id=WEB_SEARCH_PROFILE_ID,
                wrapper_args={"query": query},
            )
            if result.status != "success":
                return result
            try:
                snapshot = parse_web_search_snapshot(
                    wrapper_result=_web_wrapper_result(result),
                    expected_query=query,
                )
            except WebContentInvalidResponseError as exc:
                return _expected_error(
                    tool_name=call.tool_name,
                    error_code="WEB_TOOL_FAILED",
                    error=exc,
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
            wrapper_args: dict[str, JSONValue] = {"urls": list(missing_urls)}
            if query is not None:
                wrapper_args["query"] = query
            result = await self._run_web_tool(
                call=call,
                profile_id=WEB_EXTRACT_PROFILE_ID,
                wrapper_args=wrapper_args,
            )
            if result.status != "success":
                return result
            try:
                contents, failures = parse_web_extract_result(
                    wrapper_result=_web_wrapper_result(result)
                )
                _validate_web_extract_coverage(
                    requested_urls=missing_urls,
                    contents=contents,
                    failures=failures,
                )
            except WebContentInvalidResponseError as exc:
                return _expected_error(
                    tool_name=call.tool_name,
                    error_code="WEB_TOOL_FAILED",
                    error=exc,
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

    async def _run_web_tool(
        self,
        *,
        call: ReactToolCall,
        profile_id: str,
        wrapper_args: dict[str, JSONValue],
    ) -> ReactToolResult:
        context = build_web_tools_wrapper_context(
            user_id=self.user_id,
            action_id=None,
        )
        try:
            response = await invoke_web_tools_wrapper(
                tool_id=call.tool_name,
                web_tool_profile=profile_id,
                args=wrapper_args,
                context=context,
                max_retries=1,
            )
            if response["status"] != "success" or response.get("result") is None:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code="WEB_TOOL_FAILED",
                    message="Web tool returned no successful result.",
                    details=response.get("error"),
                )
            return _success(
                call.tool_name,
                {"status": "success", "result": response["result"]},
            )
        except (WebContentExecutionError, WebContentInvalidResponseError) as exc:
            return _expected_error(
                tool_name=call.tool_name,
                error_code="WEB_TOOL_FAILED",
                error=exc,
            )


def _web_wrapper_result(result: ReactToolResult) -> dict[str, JSONValue]:
    if result.status != "success" or not isinstance(result.output, dict):
        raise AssertionError("web tool result must be successful")
    wrapper_result = result.output.get("result")
    if not isinstance(wrapper_result, dict):
        raise WebContentInvalidResponseError("result")
    return wrapper_result


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

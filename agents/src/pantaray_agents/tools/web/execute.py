"""Run web_search and web_extract and shape their results, once for every agent.

Each returns the tool's result: the success shape in ``definitions``, or the
shared tool error shape when nothing could be looked up. A failure the user can
fix states the fix in the user's terms, and only where the model answers the
user (``speaks_to_user``) is it asked to pass the fix on; Suggestion research
would otherwise turn it into a nag. Every other failure is the system's, so the
model gets no code or provider detail to relay.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from pantaray_agents.local_runtime.web_tools.client import (
    WebContentExecutionError,
    WebContentInvalidResponseError,
    WebToolsWrapperContext,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import tool_error_output
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_CONNECTION_NOT_CONFIGURED,
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_NO_RESULTS,
    PROXY_OUTCOME_PARTIAL_RESULTS,
    PROXY_SUGGESTED_ACTION_CHANGE_URL,
    PROXY_SUGGESTED_ACTION_REFINE_QUERY,
    ProxyResultOutcome,
    ProxySuggestedAction,
    ProxySurfaceId,
    build_proxy_agent_error,
    build_proxy_tool_result_meta,
)
from pantaray_llm.profiles import (
    WEB_EXCERPTS_PER_PAGE,
    WEB_EXTRACT_PROFILE_ID,
    WEB_SEARCH_PROFILE_ID,
)

from .fetch import fetch_web_extract, fetch_web_search
from .validation import (
    ArgumentProblem,
    extract_argument_problem,
    search_argument_problem,
)

QUERY_EXCERPTS_RETRY_HINT = (
    f"query was set, so each raw_content holds only up to {WEB_EXCERPTS_PER_PAGE} "
    "excerpts relevant to it, joined by [...], not the full page. Call "
    "web_extract again without query to read the full page."
)
_NOTHING_RETURNED = "No content was returned for this URL."
# Where the web search key is set, as the app labels it: the UI's i18n keys
# nav.settings > settings.aiConnection.title > settings.aiConnection.webSearch.title
# and settings.aiConnection.webSearch.keyLabel (frontend/src/i18n/messageCatalog).
# The UI language lives in the desktop app, not here, so both are given and the
# model uses the one it replies in.
WEB_SEARCH_KEY_PLACE = (
    "the Tavily API key in Settings > AI connection > Web search (in Japanese: "
    '"Tavily API キー" in 設定 > AI 接続 > Web 検索)'
)
# Keyed like the connection terminals: one code has a different fix per
# connection, and the raiser's suggested action says which.
_USER_FIX_BY_FAILURE: dict[tuple[str, str], tuple[str, str]] = {
    (PROXY_CONNECTION_NOT_CONFIGURED, "configure_connection"): (
        "Web search is not set up",
        "they can turn it on by signing in to Pantaray or by saving "
        f"{WEB_SEARCH_KEY_PLACE}",
    ),
    (PROXY_AUTHENTICATION_FAILED, "configure_connection"): (
        "The Tavily API key saved for web search was rejected",
        f"they can save a working key as {WEB_SEARCH_KEY_PLACE}",
    ),
    (PROXY_AUTHENTICATION_FAILED, "reauthenticate"): (
        "The user's Pantaray sign-in has expired",
        "web search works again once they sign in",
    ),
}


async def run_web_search(
    *,
    context: WebToolsWrapperContext,
    args: Mapping[str, JSONValue],
    max_retries: int,
    speaks_to_user: bool,
) -> dict[str, JSONValue]:
    problem = search_argument_problem(args)
    if problem is not None:
        return _invalid_arguments(problem)
    query = _text(args, "query")
    assert query is not None  # the request schema requires it
    try:
        response = await fetch_web_search(
            context=context,
            query=query,
            max_retries=max_retries,
            topic=_text(args, "topic"),
            country=_text(args, "country"),
        )
    except (WebContentExecutionError, WebContentInvalidResponseError) as exc:
        return _failure(exc, surface_id="web_search", speaks_to_user=speaks_to_user)
    return {
        "status": "success",
        "query": query,
        "results": [
            {
                "title": row.title,
                "url": row.url,
                "content": row.content,
                "score": row.score,
            }
            for row in response.results
        ],
        "meta": _meta(
            context=context,
            profile_id=WEB_SEARCH_PROFILE_ID,
            provider="tavily",
            outcome=PROXY_OUTCOME_COMPLETE
            if response.results
            else PROXY_OUTCOME_NO_RESULTS,
            upstream_request_id=response.upstream_request_id,
            on_nothing=PROXY_SUGGESTED_ACTION_REFINE_QUERY,
        ),
    }


async def run_web_extract(
    *,
    context: WebToolsWrapperContext,
    args: Mapping[str, JSONValue],
    max_retries: int,
    speaks_to_user: bool,
) -> dict[str, JSONValue]:
    problem = extract_argument_problem(args)
    if problem is not None:
        return _invalid_arguments(problem)
    urls = args.get("urls")
    assert isinstance(urls, list)  # the request schema requires it
    query = _text(args, "query")
    try:
        response = await fetch_web_extract(
            context=context,
            urls=[str(url) for url in urls],
            query=query,
            max_retries=max_retries,
        )
    except (WebContentExecutionError, WebContentInvalidResponseError) as exc:
        return _failure(exc, surface_id="web_extract", speaks_to_user=speaks_to_user)
    pages = [
        {"url": page.url, "raw_content": page.raw_content} for page in response.results
    ]
    failures = [
        {"url": failure.url, "error": failure.error}
        for failure in response.failed_results
    ]
    # A requested URL the provider answered for in neither list is named as a
    # failure rather than vanishing. A URL it returned that was not asked for
    # is kept: providers return a page under a normalized spelling of its URL.
    answered = {_page_key(row["url"]) for row in (*pages, *failures)}
    failures.extend(
        {"url": url, "error": _NOTHING_RETURNED}
        for url in dict.fromkeys(str(url) for url in urls)
        if _page_key(url) not in answered
    )
    outcome: ProxyResultOutcome = (
        PROXY_OUTCOME_PARTIAL_RESULTS
        if pages and failures
        else PROXY_OUTCOME_COMPLETE
        if pages or failures
        else PROXY_OUTCOME_NO_RESULTS
    )
    # Only the provider routes cut a page down to excerpts.
    excerpts = query is not None and response.provider == "tavily" and bool(pages)
    return {
        "status": "success",
        "results": pages,  # type: ignore[dict-item]
        "failed_results": failures,  # type: ignore[dict-item]
        "retry_hint": QUERY_EXCERPTS_RETRY_HINT if excerpts else None,
        "meta": _meta(
            context=context,
            profile_id=WEB_EXTRACT_PROFILE_ID,
            provider=response.provider,
            outcome=outcome,
            upstream_request_id=response.upstream_request_id,
            on_nothing=PROXY_SUGGESTED_ACTION_CHANGE_URL,
        ),
    }


def _page_key(url: str) -> str:
    """The URL as compared for a missing page, with only spelling differences removed.

    Case of the scheme and host, a fragment, one trailing slash and a default
    port; nothing that could name another page.
    """

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _meta(
    *,
    context: WebToolsWrapperContext,
    profile_id: str,
    provider: str,
    outcome: ProxyResultOutcome,
    upstream_request_id: str | None,
    on_nothing: ProxySuggestedAction,
) -> JSONValue:
    meta = build_proxy_tool_result_meta(
        request_id=context["request_id"],
        upstream_provider="local" if provider == "local" else "tavily",
        profile_id=profile_id,
        outcome=outcome,
        upstream_request_id=upstream_request_id,
        suggested_action=on_nothing if outcome == PROXY_OUTCOME_NO_RESULTS else None,
    )
    return cast(JSONValue, dict(meta))


def _failure(
    error: Exception, *, surface_id: ProxySurfaceId, speaks_to_user: bool
) -> dict[str, JSONValue]:
    user_fix = None
    if isinstance(error, WebContentExecutionError):
        suggested_action = build_proxy_agent_error(
            surface_id=surface_id,
            error_code=error.error_code,
            suggested_action=error.suggested_action,
        )["error_details"]["suggested_action"]
        user_fix = _USER_FIX_BY_FAILURE.get((error.error_code, suggested_action))
    if user_fix is None:
        return tool_error_output(
            error_code="WEB_TOOL_FAILED",
            message="Web search is unavailable right now, so nothing was looked up.",
        )
    reason, fix = user_fix
    # Without a provider only the search is gone: pages are still read here.
    aside = (
        " web_extract still reads pages whose URL is known."
        if surface_id == "web_search"
        and isinstance(error, WebContentExecutionError)
        and error.error_code == PROXY_CONNECTION_NOT_CONFIGURED
        else ""
    )
    return tool_error_output(
        error_code="WEB_TOOL_NEEDS_USER",
        message=f"{reason}, so nothing was looked up."
        + aside
        + (f" Tell the user {fix}." if speaks_to_user else ""),
    )


def _invalid_arguments(problem: ArgumentProblem) -> dict[str, JSONValue]:
    return tool_error_output(
        error_code="INVALID_ARGUMENTS",
        message=problem.summary,
        details={"path": list(problem.path), "message": problem.message},
    )


def _text(args: Mapping[str, JSONValue], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value.strip() else None


__all__ = [
    "QUERY_EXCERPTS_RETRY_HINT",
    "WEB_SEARCH_KEY_PLACE",
    "run_web_extract",
    "run_web_search",
]

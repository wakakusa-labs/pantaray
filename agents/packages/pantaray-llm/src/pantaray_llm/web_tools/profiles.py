from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pantaray_llm.errors import PROXY_INVALID_INPUT, ProviderError
from pantaray_llm.profiles import (
    WEB_CRAWL_ADVANCED_EXTRACT_DEPTH,
    WEB_CRAWL_MAX_BREADTH,
    WEB_CRAWL_MAX_DEPTH,
    WEB_CRAWL_PAGE_LIMIT,
    WEB_CRAWL_PROFILE_ID,
    WEB_EXCERPTS_PER_PAGE,
    WEB_EXTRACT_ADVANCED_DEPTH,
    WEB_EXTRACT_PROFILE_ID,
    WEB_SEARCH_ADVANCED_DEPTH,
    WEB_SEARCH_PROFILE_ID,
    WEB_SEARCH_RESULT_LIMIT,
    WEB_SEARCH_TOPIC_GENERAL,
)

type WebToolId = Literal["web_search", "web_extract", "web_crawl"]


@dataclass(frozen=True, slots=True)
class WebSearchProfile:
    profile_id: str
    tool_id: Literal["web_search"]
    search_depth: str
    default_topic: str
    max_results: int


@dataclass(frozen=True, slots=True)
class WebExtractProfile:
    profile_id: str
    tool_id: Literal["web_extract"]
    extract_depth: str
    query_chunks_per_source: int


@dataclass(frozen=True, slots=True)
class WebCrawlProfile:
    profile_id: str
    tool_id: Literal["web_crawl"]
    extract_depth: str
    max_depth: int
    max_breadth: int
    limit: int
    instructions_chunks_per_source: int


type ResolvedWebToolProfile = WebSearchProfile | WebExtractProfile | WebCrawlProfile


@lru_cache
def _build_profiles() -> dict[str, ResolvedWebToolProfile]:
    return {
        WEB_SEARCH_PROFILE_ID: WebSearchProfile(
            profile_id=WEB_SEARCH_PROFILE_ID,
            tool_id="web_search",
            search_depth=WEB_SEARCH_ADVANCED_DEPTH,
            default_topic=WEB_SEARCH_TOPIC_GENERAL,
            max_results=WEB_SEARCH_RESULT_LIMIT,
        ),
        WEB_EXTRACT_PROFILE_ID: WebExtractProfile(
            profile_id=WEB_EXTRACT_PROFILE_ID,
            tool_id="web_extract",
            extract_depth=WEB_EXTRACT_ADVANCED_DEPTH,
            query_chunks_per_source=WEB_EXCERPTS_PER_PAGE,
        ),
        WEB_CRAWL_PROFILE_ID: WebCrawlProfile(
            profile_id=WEB_CRAWL_PROFILE_ID,
            tool_id="web_crawl",
            extract_depth=WEB_CRAWL_ADVANCED_EXTRACT_DEPTH,
            max_depth=WEB_CRAWL_MAX_DEPTH,
            max_breadth=WEB_CRAWL_MAX_BREADTH,
            limit=WEB_CRAWL_PAGE_LIMIT,
            instructions_chunks_per_source=WEB_EXCERPTS_PER_PAGE,
        ),
    }


def get_web_tool_profile(profile_id: str) -> ResolvedWebToolProfile:
    profile = _build_profiles().get(profile_id)
    if profile is None:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=f"Unknown web_tool_profile: {profile_id}",
        )
    return profile


__all__ = [
    "ResolvedWebToolProfile",
    "WebCrawlProfile",
    "WebExtractProfile",
    "WebSearchProfile",
    "WebToolId",
    "get_web_tool_profile",
]

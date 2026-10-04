from __future__ import annotations

from typing import Literal

WEB_SEARCH_PROFILE_ID = "web_search.default"
WEB_EXTRACT_PROFILE_ID = "web_extract.default"
WEB_CRAWL_PROFILE_ID = "web_crawl.default"

WEB_SEARCH_ADVANCED_DEPTH = "advanced"
WEB_EXTRACT_ADVANCED_DEPTH = "advanced"
WEB_CRAWL_ADVANCED_EXTRACT_DEPTH = "advanced"
WEB_CRAWL_MAX_DEPTH = 5
# The counts below are the Tavily API defaults (docs.tavily.com, 2026-10), passed
# explicitly so the limits the tool descriptions state stay true on every route.
WEB_SEARCH_RESULT_LIMIT = 10
WEB_CRAWL_MAX_BREADTH = 20
WEB_CRAWL_PAGE_LIMIT = 50
# Excerpts (at most 500 characters each) returned per page when web_extract has
# a query or web_crawl has instructions.
WEB_EXCERPTS_PER_PAGE = 3

WEB_SEARCH_TOPIC_GENERAL = "general"
WEB_SEARCH_TOPIC_NEWS = "news"
WEB_SEARCH_TOPIC_FINANCE = "finance"
WEB_SEARCH_TOPICS = (
    WEB_SEARCH_TOPIC_GENERAL,
    WEB_SEARCH_TOPIC_NEWS,
    WEB_SEARCH_TOPIC_FINANCE,
)

WEB_SEARCH_COUNTRY_UNITED_STATES = "united states"
WEB_SEARCH_COUNTRY_JAPAN = "japan"
WEB_SEARCH_COUNTRY_CHINA = "china"
WEB_SEARCH_COUNTRY_UNITED_KINGDOM = "united kingdom"
WEB_SEARCH_COUNTRY_GERMANY = "germany"
WEB_SEARCH_COUNTRY_FRANCE = "france"
WEB_SEARCH_COUNTRY_INDIA = "india"
WEB_SEARCH_COUNTRY_SOUTH_KOREA = "south korea"
WEB_SEARCH_COUNTRIES = (
    WEB_SEARCH_COUNTRY_UNITED_STATES,
    WEB_SEARCH_COUNTRY_JAPAN,
    WEB_SEARCH_COUNTRY_CHINA,
    WEB_SEARCH_COUNTRY_UNITED_KINGDOM,
    WEB_SEARCH_COUNTRY_GERMANY,
    WEB_SEARCH_COUNTRY_FRANCE,
    WEB_SEARCH_COUNTRY_INDIA,
    WEB_SEARCH_COUNTRY_SOUTH_KOREA,
)

type WebSearchTopic = Literal[
    "general",
    "news",
    "finance",
]
type WebSearchCountry = Literal[
    "united states",
    "japan",
    "china",
    "united kingdom",
    "germany",
    "france",
    "india",
    "south korea",
]

__all__ = [
    "WEB_CRAWL_ADVANCED_EXTRACT_DEPTH",
    "WEB_CRAWL_MAX_BREADTH",
    "WEB_CRAWL_MAX_DEPTH",
    "WEB_CRAWL_PAGE_LIMIT",
    "WEB_CRAWL_PROFILE_ID",
    "WEB_EXCERPTS_PER_PAGE",
    "WEB_EXTRACT_ADVANCED_DEPTH",
    "WEB_EXTRACT_PROFILE_ID",
    "WEB_SEARCH_ADVANCED_DEPTH",
    "WEB_SEARCH_COUNTRIES",
    "WEB_SEARCH_COUNTRY_CHINA",
    "WEB_SEARCH_COUNTRY_FRANCE",
    "WEB_SEARCH_COUNTRY_GERMANY",
    "WEB_SEARCH_COUNTRY_INDIA",
    "WEB_SEARCH_COUNTRY_JAPAN",
    "WEB_SEARCH_COUNTRY_SOUTH_KOREA",
    "WEB_SEARCH_COUNTRY_UNITED_KINGDOM",
    "WEB_SEARCH_COUNTRY_UNITED_STATES",
    "WEB_SEARCH_PROFILE_ID",
    "WEB_SEARCH_RESULT_LIMIT",
    "WEB_SEARCH_TOPICS",
    "WEB_SEARCH_TOPIC_FINANCE",
    "WEB_SEARCH_TOPIC_GENERAL",
    "WEB_SEARCH_TOPIC_NEWS",
    "WebSearchCountry",
    "WebSearchTopic",
]

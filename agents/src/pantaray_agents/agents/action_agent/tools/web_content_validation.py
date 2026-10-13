"""web_extract / web_crawl の URL 入力ガード。"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from urllib.parse import urlsplit

from pantaray_agents.agents.action_agent.tools.base import ToolPolicyValidationError
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.profiles import (
    WEB_SEARCH_COUNTRIES,
    WEB_SEARCH_TOPIC_GENERAL,
    WEB_SEARCH_TOPICS,
)


class NonPublicLikeUrlValidationError(ValueError):
    """ローカル/内部向けと判断した URL 入力。"""


_DISALLOWED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
)

_PUBLIC_URL_ERROR_MESSAGE = (
    "Use an http/https URL that is not obviously local or internal. "
    "This app rejects localhost, private literal IPs, link-local literals, "
    "and internal-only hostnames."
)


def _is_disallowed_hostname(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    if "." not in hostname:
        return True
    return any(hostname.endswith(suffix) for suffix in _DISALLOWED_HOST_SUFFIXES)


def require_non_local_http_url(value: object) -> str:
    """Tavily に渡す前の http/https URL 入力ガードを行う。"""

    if not isinstance(value, str):
        raise NonPublicLikeUrlValidationError("url")

    url = value.strip()
    if not url:
        raise NonPublicLikeUrlValidationError("url")

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise NonPublicLikeUrlValidationError("url")
    if not parsed.netloc or parsed.username or parsed.password:
        raise NonPublicLikeUrlValidationError("url")

    hostname = parsed.hostname
    if hostname is None:
        raise NonPublicLikeUrlValidationError("url")
    normalized_host = hostname.strip().lower()
    if not normalized_host:
        raise NonPublicLikeUrlValidationError("url")
    if _is_disallowed_hostname(normalized_host):
        raise NonPublicLikeUrlValidationError("url")

    try:
        host_ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        return url

    if host_ip.is_loopback or host_ip.is_private or host_ip.is_link_local:
        raise NonPublicLikeUrlValidationError("url")
    return url


def validate_web_search_args(args: Mapping[str, JSONValue]) -> None:
    topic = args.get("topic")
    country = args.get("country")

    if topic is not None and topic not in WEB_SEARCH_TOPICS:
        raise ToolPolicyValidationError(
            "topic: topic must be one of the supported Tavily search topics.",
            details={
                "path": ["topic"],
                "message": (
                    "topic must be one of: " + ", ".join(WEB_SEARCH_TOPICS) + "."
                ),
            },
        )
    if country is not None and country not in WEB_SEARCH_COUNTRIES:
        raise ToolPolicyValidationError(
            "country: country must be one of the supported Tavily search countries.",
            details={
                "path": ["country"],
                "message": (
                    "country must be one of: " + ", ".join(WEB_SEARCH_COUNTRIES) + "."
                ),
            },
        )
    if country is not None and topic not in (None, WEB_SEARCH_TOPIC_GENERAL):
        raise ToolPolicyValidationError(
            "country: country can only be used with the general search topic.",
            details={
                "path": ["country"],
                "message": "country is only allowed when topic is general.",
            },
        )


def validate_web_extract_args(args: Mapping[str, JSONValue]) -> None:
    urls = args.get("urls")
    if not isinstance(urls, list):
        return

    for index, value in enumerate(urls):
        try:
            require_non_local_http_url(value)
        except NonPublicLikeUrlValidationError as exc:
            raise ToolPolicyValidationError(
                f"urls.{index}: URL must be an http/https URL that is not obviously local or internal.",
                details={
                    "path": ["urls", index],
                    "message": _PUBLIC_URL_ERROR_MESSAGE,
                },
            ) from exc


def validate_web_crawl_args(args: Mapping[str, JSONValue]) -> None:
    try:
        require_non_local_http_url(args.get("url"))
    except NonPublicLikeUrlValidationError as exc:
        raise ToolPolicyValidationError(
            "url: URL must be an http/https URL that is not obviously local or internal.",
            details={
                "path": ["url"],
                "message": _PUBLIC_URL_ERROR_MESSAGE,
            },
        ) from exc

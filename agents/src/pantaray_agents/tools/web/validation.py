"""What a web tool request may not ask for, beyond what its schema says.

Each host turns a problem into its own feedback for the model: the Action as a
tool policy error, Suggestion and the chat as a tool error result.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.profiles import WEB_SEARCH_TOPIC_GENERAL

_DISALLOWED_HOST_SUFFIXES: Final = (".localhost", ".local", ".internal", ".home.arpa")
_PUBLIC_URL_MESSAGE: Final = (
    "Use an http/https URL that is not obviously local or internal. "
    "This app rejects localhost, private literal IPs, link-local literals, "
    "and internal-only hostnames."
)


@dataclass(frozen=True, slots=True)
class ArgumentProblem:
    path: tuple[str | int, ...]
    summary: str
    message: str


def search_argument_problem(args: Mapping[str, JSONValue]) -> ArgumentProblem | None:
    """country narrows only a general search; the provider refuses it otherwise."""

    if args.get("country") is not None and args.get("topic") not in (
        None,
        WEB_SEARCH_TOPIC_GENERAL,
    ):
        return ArgumentProblem(
            path=("country",),
            summary="country: country can only be used with the general search topic.",
            message="country is only allowed when topic is general.",
        )
    return None


def url_argument_problem(
    value: JSONValue, *, path: tuple[str | int, ...]
) -> ArgumentProblem | None:
    if is_public_like_http_url(value):
        return None
    label = ".".join(str(part) for part in path)
    return ArgumentProblem(
        path=path,
        summary=(
            f"{label}: URL must be an http/https URL that is not obviously local "
            "or internal."
        ),
        message=_PUBLIC_URL_MESSAGE,
    )


def extract_argument_problem(args: Mapping[str, JSONValue]) -> ArgumentProblem | None:
    urls = args.get("urls")
    if not isinstance(urls, list):
        return None
    for index, value in enumerate(urls):
        problem = url_argument_problem(value, path=("urls", index))
        if problem is not None:
            return problem
    return None


def is_public_like_http_url(value: object) -> bool:
    """An http(s) URL whose host is not obviously local, private or internal.

    A first check on the address the model wrote: the fetch itself still decides
    by the address a name resolves to.
    """

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
    except ValueError:
        # A malformed bracketed host, such as https://[invalid]/, is no URL.
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or parsed.username or parsed.password:
        return False
    host = (hostname or "").strip().lower()
    if not host or host == "localhost" or "." not in host:
        return False
    if any(host.endswith(suffix) for suffix in _DISALLOWED_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local)


__all__ = [
    "ArgumentProblem",
    "extract_argument_problem",
    "is_public_like_http_url",
    "search_argument_problem",
    "url_argument_problem",
]

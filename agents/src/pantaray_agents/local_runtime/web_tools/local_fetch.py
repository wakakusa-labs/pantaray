"""Read web_extract pages from this computer when no web provider is connected.

Pantaray Cloud and a stored Tavily key both have Tavily fetch the page. With
neither, the page is fetched here instead, and every outcome is per URL: one
page refusing, timing out or running too large never hides the others.

Nothing upstream enforces a fetch policy on this route, so this module owns it:
each hop of a redirect chain may only connect to a public address, and the
connection goes to the very address that was checked, so a host name cannot
resolve to a public address for the check and a private one for the request.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Final, Literal
from urllib.parse import urljoin

import httpx2
from pydantic import ValidationError

from pantaray_agents.local_runtime.tooling.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentExtractionError,
    extract_document,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.errors import PROXY_INVALID_INPUT, ProxyProvider
from pantaray_llm.web_tools.schemas import WebExtractArgs

from .contract import (
    WebContentExecutionError,
    WebToolsWrapperContext,
    WebToolsWrapperResponse,
)
from .html_markdown import html_to_markdown

# Some sites, Cloudflare's bot rules among them, refuse a library's default
# User-Agent (Python-urllib was refused with 403 where a browser's was served).
FETCH_HEADERS: Final = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,"
        "*/*;q=0.5"
    ),
}
# One page, every redirect and the whole body included. The tool allows the
# whole call 60 s and pages are fetched in parallel, so this leaves room for the
# tool to answer with each page's own outcome.
FETCH_TIMEOUT_SECONDS: Final = 30.0
MAX_REDIRECTS: Final = 5
# Counted on the decoded body, so a compressed page cannot expand past it.
MAX_PAGE_BYTES: Final = 5 * 1024 * 1024
LOCAL_PROVIDER: ProxyProvider = "local"

type _PageKind = Literal["html", "text", "pdf"]

_META_CHARSET: Final = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_.:-]+)""", re.I
)
_SNIFF_BYTES: Final = 4096


class _PageFailure(Exception):
    """One page could not be read; the message is what the model is told."""


@dataclass(frozen=True, slots=True)
class _Download:
    url: str
    kind: _PageKind
    content_type: str
    body: bytes


async def extract_pages_locally(
    *,
    args: dict[str, JSONValue],
    context: WebToolsWrapperContext,
    web_tool_profile: str,
) -> WebToolsWrapperResponse:
    """Fetch every URL from this computer, in the result shape Tavily's has.

    ``query`` is not applied: there is no ranking here, so each page is
    returned whole, which the tool description states for this route.
    """

    try:
        request = WebExtractArgs.model_validate(args)
    except ValidationError as exc:
        raise WebContentExecutionError(
            error_code=PROXY_INVALID_INPUT,
            error_message="The web_extract request is invalid.",
            retryable=False,
            request_id=context["request_id"],
            profile_id=web_tool_profile,
            upstream_provider=LOCAL_PROVIDER,
        ) from exc
    started_at = time.perf_counter()
    outcomes = await asyncio.gather(*(_read_page(url) for url in request.urls))
    elapsed = time.perf_counter() - started_at
    results: list[JSONValue] = []
    failed_results: list[JSONValue] = []
    for url, outcome in zip(request.urls, outcomes, strict=True):
        if isinstance(outcome, _PageFailure):
            failed_results.append({"url": url, "error": str(outcome)})
        else:
            results.append({"url": url, "raw_content": outcome})
    return {
        "tool_id": "web_extract",
        "status": "success",
        "request_id": context["request_id"],
        "response_time_ms": int(elapsed * 1000),
        "result": {
            "results": results,
            "failed_results": failed_results,
            "response_time": elapsed,
            "meta": {
                "request_id": context["request_id"],
                "upstream_provider": LOCAL_PROVIDER,
                "profile_id": web_tool_profile,
            },
        },
    }


async def _read_page(url: str) -> str | _PageFailure:
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            download = await _download(url)
        return await asyncio.to_thread(_page_text, download)
    except _PageFailure as failure:
        return failure
    except (TimeoutError, httpx2.TimeoutException):
        return _PageFailure(
            f"No complete response within {FETCH_TIMEOUT_SECONDS:g} seconds."
        )
    except httpx2.HTTPError as exc:
        return _PageFailure(f"The connection failed ({type(exc).__name__}).")


async def _download(url: str) -> _Download:
    current = url
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(FETCH_TIMEOUT_SECONDS),
        # A proxy would resolve the host itself, past the address check.
        trust_env=False,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            target = httpx2.URL(current)
            if target.scheme not in {"http", "https"} or not target.host:
                raise _PageFailure("Only http and https URLs can be read.")
            address = await _public_address(target.host, target.port, target.scheme)
            response = await client.send(
                client.build_request(
                    "GET",
                    target.copy_with(host=address),
                    headers={**FETCH_HEADERS, "Host": target.netloc.decode("ascii")},
                    extensions=(
                        {"sni_hostname": target.host}
                        if target.scheme == "https"
                        else {}
                    ),
                ),
                stream=True,
            )
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise _PageFailure(
                            f"HTTP {response.status_code} redirect without a target."
                        )
                    current = urljoin(str(target), location)
                    continue
                return await _read_body(response, url=str(target))
            finally:
                await response.aclose()
    raise _PageFailure(f"More than {MAX_REDIRECTS} redirects.")


async def _read_body(response: httpx2.Response, *, url: str) -> _Download:
    if not response.is_success:
        raise _PageFailure(
            f"The site answered HTTP {response.status_code} {response.reason_phrase}."
        )
    content_type = response.headers.get("content-type", "")
    kind = _page_kind(content_type)
    limit = MAX_DOCUMENT_BYTES if kind == "pdf" else MAX_PAGE_BYTES
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise _PageFailure(
                f"The page is larger than {limit // (1024 * 1024)} MB, "
                "the most this tool reads."
            )
    return _Download(url=url, kind=kind, content_type=content_type, body=bytes(body))


async def _public_address(host: str, port: int | None, scheme: str) -> str:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise _PageFailure("The host name could not be resolved.") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(_is_public(address) for address in addresses):
        raise _PageFailure(
            "The address is on this computer or a private network, "
            "which this tool does not read."
        )
    return str(addresses[0])


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_global
    return address.is_global


def _page_kind(content_type: str) -> _PageKind:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type in {"text/html", "application/xhtml+xml"}:
        return "html"
    if media_type == "application/pdf":
        return "pdf"
    if media_type.startswith("text/") or media_type == "application/json":
        return "text"
    raise _PageFailure(
        f"The page is {media_type or 'of no stated type'}; this tool reads HTML, "
        "text and PDF."
    )


def _page_text(download: _Download) -> str:
    if download.kind == "pdf":
        return _pdf_text(download.body)
    text = download.body.decode(_charset(download), errors="replace")
    if download.kind == "html":
        text = html_to_markdown(text, base_url=download.url)
    if not text.strip():
        raise _PageFailure(
            "The page has no readable text; it may build its content with "
            "JavaScript, which this tool does not run."
        )
    return text


def _pdf_text(body: bytes) -> str:
    try:
        document = extract_document(source=BytesIO(body), document_format="pdf")
    except DocumentExtractionError as exc:
        raise _PageFailure(f"The PDF could not be read: {exc}") from exc
    if not document.text.strip():
        raise _PageFailure("The PDF has no text layer; it may be scanned images.")
    if document.next_start_unit is None:
        return document.text
    return (
        f"{document.text}\n\n[Only pages 1-{document.next_start_unit - 1} of "
        f"{document.total_units} fit; the rest of this PDF was not read.]"
    )


def _charset(download: _Download) -> str:
    declared = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)", download.content_type, re.I
    )
    sniffed = (
        _META_CHARSET.search(download.body[:_SNIFF_BYTES])
        if declared is None and download.kind == "html"
        else None
    )
    name = (
        declared.group(1)
        if declared
        else sniffed.group(1).decode("ascii")
        if sniffed
        else "utf-8"
    )
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "utf-8"


__all__ = ["LOCAL_PROVIDER", "extract_pages_locally"]

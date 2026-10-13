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
import ipaddress
import logging
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
from .page_encodings import page_codec

logger = logging.getLogger(__name__)

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
# A byte order mark outranks any declared charset (WHATWG "decode").
_BYTE_ORDER_MARKS: Final = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_INVALID_URL: Final = "The URL, or a redirect's target, is not a valid URL."


class _PageFailure(Exception):
    """One page could not be read; the message is what the model is told."""


@dataclass(frozen=True, slots=True)
class _Download:
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
    except _PageFailure as failure:
        return failure
    except (TimeoutError, httpx2.TimeoutException):
        return _PageFailure(
            f"No complete response within {FETCH_TIMEOUT_SECONDS:g} seconds."
        )
    except httpx2.HTTPError as exc:
        return _PageFailure(f"The connection failed ({type(exc).__name__}).")
    except Exception as exc:
        # Every input past the URL -- redirect targets, headers, the address a
        # name resolves to -- is the site's, and the network stack raises more
        # than the errors above on some of them (an out-of-range port in a
        # redirect surfaces as an ExceptionGroup of OverflowError). Results are
        # per URL, so whatever fetching one page raises fails that page alone.
        # Cancellation is a BaseException and still stops the call.
        logger.warning("web_extract could not fetch a page: %s", type(exc).__name__)
        return _PageFailure("The page could not be fetched.")
    try:
        return await asyncio.to_thread(_page_text, download)
    except _PageFailure as failure:
        return failure
    except Exception as exc:
        # The body is untrusted, and the parsers that turn it into text (lxml,
        # pypdf) raise more than their documented errors on malformed input:
        # pypdf raises AssertionError on a broken inline image. Results are per
        # URL, so whatever converting one page raises fails that page alone.
        logger.warning(
            "web_extract could not convert a page to text: %s", type(exc).__name__
        )
        return _PageFailure("The page could not be converted to text.")


async def _download(url: str) -> _Download:
    current = url
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(FETCH_TIMEOUT_SECONDS),
        # A proxy would resolve the host itself, past the address check.
        trust_env=False,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            # Requests go to the checked IP, so the jar would key a cookie by
            # that IP and hand it to another host on it after a redirect. A
            # page read keeps no session, so no cookie outlives its response.
            client.cookies.clear()
            target = _parse_url(current)
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
                    try:
                        current = urljoin(str(target), location)
                    except ValueError as exc:
                        raise _PageFailure(_INVALID_URL) from exc
                    continue
                return await _read_body(response)
            finally:
                await response.aclose()
    raise _PageFailure(f"More than {MAX_REDIRECTS} redirects.")


def _parse_url(url: str) -> httpx2.URL:
    try:
        return httpx2.URL(url)
    except httpx2.InvalidURL as exc:
        raise _PageFailure(_INVALID_URL) from exc


async def _read_body(response: httpx2.Response) -> _Download:
    if not response.is_success:
        raise _PageFailure(
            f"The site answered HTTP {response.status_code} {response.reason_phrase}."
        )
    encodings = [
        encoding
        for encoding in response.headers.get_list("content-encoding", split_commas=True)
        if encoding.strip().lower() not in {"", "identity"}
    ]
    if len(encodings) > 1:
        # Each layer is decoded whole before the size limit sees its output,
        # synchronously, so stacked layers can expand far past the limit while
        # holding the event loop that every other page is fetched on.
        raise _PageFailure(
            "The page uses more than one content encoding, which this tool "
            "does not read."
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
    return _Download(kind=kind, content_type=content_type, body=bytes(body))


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
        # An over-long label fails IDNA encoding before any lookup is made.
        except (socket.gaierror, UnicodeError) as exc:
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
    text = _decode(download)
    if download.kind == "html":
        text = html_to_markdown(text)
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


def _decode(download: _Download) -> str:
    """Decode by the byte order mark when there is one, as WHATWG does."""

    for bom, codec in _BYTE_ORDER_MARKS:
        if download.body.startswith(bom):
            return download.body[len(bom) :].decode(codec, errors="replace")
    return download.body.decode(_charset(download), errors="replace")


def _charset(download: _Download) -> str:
    declared = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)", download.content_type, re.I
    )
    if declared is not None:
        return page_codec(declared.group(1))
    sniffed = (
        _META_CHARSET.search(download.body[:_SNIFF_BYTES])
        if download.kind == "html"
        else None
    )
    return page_codec(sniffed.group(1).decode("ascii") if sniffed else None)


__all__ = ["LOCAL_PROVIDER", "extract_pages_locally"]

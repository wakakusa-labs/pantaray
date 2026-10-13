"""Fetch one web page and turn it into text, in a process that is allowed to die.

This file is run as a script -- ``python -I -B <this file>`` -- by
``local_fetch``, once per page, and never imported by the helper. Everything a
page controls happens here: the redirects it answers with, its headers, its
encoding layers, its markup and the PDF parser it is fed to. The parent bounds
this process from outside by wall-clock time and by the size of its reply, so a
page that loops, expands or crashes a parser costs only its own result.

Running it as a script keeps the process small: it imports httpx2, lxml and,
for a PDF, pypdf, where importing the package this file sits in pulls in the
whole local runtime. The few sibling modules it needs are loaded by file path
for the same reason.

One request on stdin (``{"url": ...}``), one JSON reply on stdout, then exit:
``{"status": "read", "text": ...}`` or ``{"status": "failed", "reason": ...}``
for an outcome this process recognises. Anything else -- an exception, a
signal -- leaves no reply, and the parent reports the page as unreadable.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import re
import socket
import sys
import types
from io import BytesIO
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urljoin

import httpx2

_HERE: Final = Path(__file__).resolve().parent
_DOCUMENTS_DIR: Final = _HERE.parent / "tooling" / "documents"
# Every file this worker loads besides itself; the parent lets the sandboxed
# process read exactly these.
SIBLING_MODULE_FILES: Final = (
    _HERE / "html_markdown.py",
    _HERE / "page_encodings.py",
    _DOCUMENTS_DIR / "document_model.py",
    _DOCUMENTS_DIR / "pdf.py",
)

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
# Per network operation, so a site that stops answering is reported as such
# before the parent's wall-clock limit for the whole page runs out.
NETWORK_TIMEOUT_SECONDS: Final = 15.0
MAX_REDIRECTS: Final = 5
# Counted on the decoded body, so a compressed page cannot expand past it.
MAX_PAGE_BYTES: Final = 5 * 1024 * 1024

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


class PageFailure(Exception):
    """A page outcome this worker recognises; the message is what the model is told."""


def main() -> int:
    url = str(json.loads(sys.stdin.readline())["url"])
    try:
        reply = {"status": "read", "text": read_page(url)}
    except PageFailure as failure:
        reply = {"status": "failed", "reason": str(failure)}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def read_page(url: str) -> str:
    content_type, body = _download(url)
    kind = _page_kind(content_type)
    if kind == "pdf":
        return _pdf_text(body)
    text = _decode(body, content_type=content_type, kind=kind)
    if kind == "html":
        text = _module("html_markdown").html_to_markdown(text)
    if not text.strip():
        raise PageFailure(
            "The page has no readable text; it may build its content with "
            "JavaScript, which this tool does not run."
        )
    return text


def _download(url: str) -> tuple[str, bytes]:
    current = url
    # The client keeps no cookies: requests go to the checked IP, so a jar
    # would key a cookie by that IP and hand it to another host on it.
    with httpx2.Client(
        timeout=httpx2.Timeout(NETWORK_TIMEOUT_SECONDS),
        # A proxy would resolve the host itself, past the address check.
        trust_env=False,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            client.cookies.clear()
            try:
                target = httpx2.URL(current)
            except httpx2.InvalidURL as exc:
                raise PageFailure("The URL is not a valid URL.") from exc
            if target.scheme not in {"http", "https"} or not target.host:
                raise PageFailure("Only http and https URLs can be read.")
            address = _public_address(target.host, target.port, target.scheme)
            request = client.build_request(
                "GET",
                target.copy_with(host=address),
                headers={**FETCH_HEADERS, "Host": target.netloc.decode("ascii")},
                extensions=(
                    {"sni_hostname": target.host} if target.scheme == "https" else {}
                ),
            )
            try:
                response = client.send(request, stream=True)
                try:
                    if not response.is_redirect:
                        return _read_body(response)
                    current = urljoin(str(target), response.headers["location"])
                finally:
                    response.close()
            except httpx2.TimeoutException as exc:
                raise PageFailure(
                    f"The site did not answer within {NETWORK_TIMEOUT_SECONDS:g} "
                    "seconds."
                ) from exc
            except httpx2.HTTPError as exc:
                raise PageFailure(
                    f"The connection failed ({type(exc).__name__})."
                ) from exc
    raise PageFailure(f"More than {MAX_REDIRECTS} redirects.")


def _read_body(response: httpx2.Response) -> tuple[str, bytes]:
    if not response.is_success:
        raise PageFailure(
            f"The site answered HTTP {response.status_code} {response.reason_phrase}."
        )
    content_type = response.headers.get("content-type", "")
    limit = (
        _module("document_model").MAX_DOCUMENT_BYTES
        if _page_kind(content_type) == "pdf"
        else MAX_PAGE_BYTES
    )
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > limit:
            raise PageFailure(
                f"The page is larger than {limit // (1024 * 1024)} MB, "
                "the most this tool reads."
            )
    return content_type, bytes(body)


def _public_address(host: str, port: int | None, scheme: str) -> str:
    """The address to connect to, refused unless every address is public."""

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise PageFailure("The host name could not be resolved.") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(_is_public(address) for address in addresses):
        raise PageFailure(
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
    raise PageFailure(
        f"The page is {media_type or 'of no stated type'}; this tool reads HTML, "
        "text and PDF."
    )


def _decode(body: bytes, *, content_type: str, kind: _PageKind) -> str:
    for bom, codec in _BYTE_ORDER_MARKS:
        if body.startswith(bom):
            return body[len(bom) :].decode(codec, errors="replace")
    declared = re.search(r"charset\s*=\s*[\"']?([^;\"'\s]+)", content_type, re.I)
    if declared is not None:
        label: str | None = declared.group(1)
    else:
        sniffed = _META_CHARSET.search(body[:_SNIFF_BYTES]) if kind == "html" else None
        label = sniffed.group(1).decode("ascii") if sniffed else None
    codec = _module("page_encodings").page_codec(label)
    return body.decode(codec, errors="replace")


def _pdf_text(body: bytes) -> str:
    model = _module("document_model")
    try:
        document = _module("pdf").extract_pdf(BytesIO(body), None)
    except model.DocumentExtractionError as exc:
        raise PageFailure(f"The PDF could not be read: {exc}") from exc
    if not document.text.strip():
        raise PageFailure("The PDF has no text layer; it may be scanned images.")
    if document.next_start_unit is None:
        return str(document.text)
    return (
        f"{document.text}\n\n[Only pages 1-{document.next_start_unit - 1} of "
        f"{document.total_units} fit; the rest of this PDF was not read.]"
    )


_PACKAGE: Final = "_page_worker"


def _module(name: str) -> types.ModuleType:
    """A sibling module, loaded from its file without importing its package.

    They are registered under one synthetic package so the PDF extractor's
    relative import of the document model resolves to the copy loaded here.
    """

    qualified = f"{_PACKAGE}.{name}"
    if qualified in sys.modules:
        return sys.modules[qualified]
    if _PACKAGE not in sys.modules:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = []
        sys.modules[_PACKAGE] = package
    if name == "pdf":
        _module("document_model")
    path = next(path for path in SIBLING_MODULE_FILES if path.stem == name)
    spec = importlib.util.spec_from_file_location(qualified, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    sys.exit(main())

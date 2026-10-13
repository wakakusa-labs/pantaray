"""web_extract reads pages from this computer when no web provider is connected.

Every page goes through the real page worker process (sandboxed on macOS).
Pages come from a server on 127.0.0.1 under names ending in ``.test``; the
worker is started through a small wrapper that resolves only those names to the
server, so every other name and address meets the real address check.

Each case fetches a page beside a well-formed one: what a page does may fail
that page, and must never take the well-formed one down with it.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.connection_store import (
    WebSearchCredential,
    set_web_search_credential,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    mark_configured,
    restore_expired_cloud_identity,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.web_tools import local_fetch
from pantaray_agents.local_runtime.web_tools.client import (
    WebContentExecutionError,
    WebToolsWrapperContext,
    invoke_web_tools_wrapper,
)
from pantaray_agents.local_runtime.web_tools.html_markdown import html_to_markdown
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)
from pantaray_agents.tools.web.session import WebResearchToolSession
from pantaray_llm.profiles import WEB_EXTRACT_PROFILE_ID

from .migrated_db import prepare_test_database

CONTEXT: WebToolsWrapperContext = {"user_id": "user-1", "request_id": "request-1"}
ARTICLE = b"""<!doctype html><html><head><title>t</title>
<script>var hidden = "script text";</script></head>
<body><nav>Menu</nav>
<main><h1>Providers</h1><p>Use <a href="/docs/zen">Zen</a> or <code>/connect</code>.</p>
<ul><li>First</li><li>Second</li></ul></main>
<footer>Copyright</footer></body></html>"""
ARTICLE_TEXT = "# Providers\n\nUse [Zen](/docs/zen) or `/connect`.\n\n- First\n- Second"
# Shift_JIS as browsers read it is code page 932, which has these two; Python's
# own shift_jis codec does not.
JAPANESE = "日本語髙①"
_TEXT = {"Content-Type": "text/plain"}
_HTML = {"Content-Type": "text/html"}
_PDF = {"Content-Type": "application/pdf"}
_TEXT_PAGE = b"BT /F1 12 Tf 72 720 Td (Readable words) Tj ET"
_IMAGE_PAGE = b"q 100 0 0 100 72 600 cm /Im0 Do Q"


def _pdf(*contents: bytes) -> bytes:
    """A PDF with one page per content stream, each with a font and an image."""

    count = len(contents)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>"
        % (b" ".join(b"%d 0 R" % (5 + 2 * i) for i in range(count)), count),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace "
        b"/DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x00\nendstream",
    ]
    for index, content in enumerate(contents):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> /XObject << /Im0 4 0 R >> >> >>"
            % (6 + 2 * index)
        )
        objects.append(
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)
        )
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


_WRAPPER = """
import importlib.util, io, json, sys
request = sys.stdin.readline()
if json.loads(request)["url"].endswith("/flood"):
    # A reply still being written when the time limit falls.
    while True:
        sys.stdout.buffer.write(b"x" * (1024 * 1024))
sys.stdin = io.StringIO(request)
spec = importlib.util.spec_from_file_location("page_worker", {worker!r})
worker = importlib.util.module_from_spec(spec)
sys.modules["page_worker"] = worker
spec.loader.exec_module(worker)
real_check = worker._public_address

def resolve(host, port, scheme):
    return "127.0.0.1" if host.endswith(".test") else real_check(host, port, scheme)

worker._public_address = resolve
sys.exit(worker.main())
"""


@functools.cache
def _pages(port: int) -> dict[str, tuple[int, dict[str, str], bytes]]:
    return {
        "/article": (200, {"Content-Type": "text/html; charset=utf-8"}, ARTICLE),
        "/blocked": (403, _HTML, b"denied"),
        "/script-only": (200, _HTML, b"<div id=app>"),
        "/large": (200, _TEXT, b"x" * (6 * 1024 * 1024)),
        # Codex's resource cases: stacked encodings, a decompression bomb, and
        # markup that repeats an element a quadratic renderer chokes on.
        "/gzip-twice": (
            200,
            {**_TEXT, "Content-Encoding": "gzip, gzip"},
            gzip.compress(gzip.compress(b"plain")),
        ),
        "/gzip-bomb": (
            200,
            {**_TEXT, "Content-Encoding": "gzip, gzip"},
            gzip.compress(gzip.compress(b"\0" * (64 * 1024 * 1024))),
        ),
        "/scripts": (200, _HTML, b"<script></script>a" * 250_000),
        "/broken.pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 junk"),
        "/scan.pdf": (200, _PDF, _pdf(_IMAGE_PAGE, _IMAGE_PAGE)),
        "/mixed.pdf": (200, _PDF, _pdf(_TEXT_PAGE, _IMAGE_PAGE, _TEXT_PAGE)),
        "/far-port": (302, {"Location": "http://site.test:65536/"}, b""),
        "/to-private": (302, {"Location": f"http://127.0.0.1:{port}/article"}, b""),
        "/set-cookie": (
            302,
            {
                "Set-Cookie": "session=secret; Path=/",
                "Location": f"http://other.test:{port}/echo-cookie",
            },
            b"",
        ),
        "/bad-link": (200, _HTML, b'<p>See <a href="https://[broken">this</a>.</p>'),
        # Encodings: labels Python knows but browsers do not, labels browsers
        # map to another codec, and a byte order mark with no label at all.
        "/punycode": (200, {"Content-Type": "text/plain; charset=punycode"}, b"x\xff"),
        "/windows-31j": (
            200,
            {"Content-Type": "text/plain; charset=windows-31j"},
            JAPANESE.encode("cp932"),
        ),
        "/meta-shift-jis": (
            200,
            _HTML,
            b'<meta charset="Shift_JIS"><p>' + JAPANESE.encode("cp932"),
        ),
        "/utf-16": (200, _TEXT, "﻿日本語".encode("utf-16-le")),
        "/gbk": (
            200,
            {"Content-Type": "text/plain; charset=gbk"},
            "\U00020000".encode("gb18030"),
        ),
    }


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's hook name
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        if self.path == "/slow":
            time.sleep(3)
        if self.path == "/echo-cookie":
            cookie = self.headers.get("Cookie", "none")
            status, headers, body = 200, _TEXT, f"cookie: {cookie}".encode()
        else:
            status, headers, body = _pages(self.server.server_address[1]).get(
                self.path, (404, {}, b"")
            )
        self.send_response(status)
        for name, value in {**headers, "Content-Length": str(len(body))}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture
def site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[str, list[str]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wrapper = tmp_path / "test_page_worker.py"
    wrapper.write_text(_WRAPPER.format(worker=str(local_fetch.WORKER_SCRIPT)))
    monkeypatch.setattr(
        local_fetch,
        "SIBLING_MODULE_FILES",
        (*local_fetch.SIBLING_MODULE_FILES, local_fetch.WORKER_SCRIPT),
    )
    monkeypatch.setattr(local_fetch, "WORKER_SCRIPT", wrapper)
    mark_configured()
    yield f"http://site.test:{server.server_address[1]}", server.requests  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()


async def _extract(urls: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """The text read and the failure reported, each by URL."""

    response = await invoke_web_tools_wrapper(
        tool_id="web_extract",
        web_tool_profile=WEB_EXTRACT_PROFILE_ID,
        args={"urls": urls},  # type: ignore[dict-item]
        context=CONTEXT,
        max_retries=1,
    )
    result = response["result"]
    assert isinstance(result, dict)
    assert result["meta"]["upstream_provider"] == "local"  # type: ignore[index,call-overload]
    read = {row["url"]: row["raw_content"] for row in result["results"]}  # type: ignore[union-attr,index]
    failed = {row["url"]: row["error"] for row in result["failed_results"]}  # type: ignore[union-attr,index]
    return read, failed


async def _beside_article(
    base: str, pages: list[str]
) -> tuple[dict[str, str], dict[str, str]]:
    read, failed = await _extract([f"{base}/article", *(base + page for page in pages)])
    assert read.pop(f"{base}/article") == ARTICLE_TEXT
    return (
        {url.removeprefix(base): text for url, text in read.items()},
        {url.removeprefix(base): reason for url, reason in failed.items()},
    )


@pytest.mark.asyncio
async def test_pages_the_site_refuses_or_cannot_serve_fail_with_the_reason(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site

    read, failed = await _beside_article(
        base, ["/blocked", "/script-only", "/large", "/bad-link"]
    )

    assert read == {"/bad-link": "See [this](https://[broken)."}
    assert failed["/blocked"] == "The site answered HTTP 403 Forbidden."
    assert "JavaScript" in failed["/script-only"]
    assert "larger than 5 MB" in failed["/large"]


@pytest.mark.asyncio
async def test_a_page_that_hangs_expands_or_breaks_a_parser_fails_alone(
    site: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _requests = site
    monkeypatch.setattr(local_fetch, "PAGE_WALL_CLOCK_SECONDS", 2.0)

    read, failed = await _beside_article(
        base, ["/slow", "/gzip-bomb", "/broken.pdf", "/far-port"]
    )
    more_read, _more_failed = await _beside_article(base, ["/gzip-twice", "/scripts"])

    assert read == {}
    assert failed["/slow"] == "The page could not be read."
    assert "larger than 5 MB" in failed["/gzip-bomb"]
    assert failed["/broken.pdf"] == "The page could not be read."
    assert "/far-port" in failed
    assert more_read == {"/gzip-twice": "plain", "/scripts": "a" * 250_000}


@pytest.mark.asyncio
async def test_a_pdf_says_which_pages_have_no_text_and_a_scan_fails(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site

    read, failed = await _beside_article(base, ["/scan.pdf", "/mixed.pdf"])

    assert failed == {
        "/scan.pdf": "The PDF has no text layer; it may be scanned images."
    }
    assert read["/mixed.pdf"].count("Readable words") == 2
    assert read["/mixed.pdf"].endswith(
        "[Pages without a text layer (scans or figures): 2]"
    )


@pytest.mark.asyncio
async def test_a_worker_cut_off_mid_reply_is_collected_within_the_limit(
    site: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _requests = site
    monkeypatch.setattr(local_fetch, "PAGE_WALL_CLOCK_SECONDS", 1.0)
    monkeypatch.setattr(local_fetch, "MAX_WORKER_REPLY_BYTES", 1024**3)

    # Left unread in the pipe, the reply kept the killed worker from ever
    # being collected, and the whole call from returning.
    read, failed = await asyncio.wait_for(_beside_article(base, ["/flood"]), 10)

    assert read == {}
    assert failed == {"/flood": "The page could not be read."}


@pytest.mark.asyncio
async def test_a_page_is_decoded_the_way_a_browser_would(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site

    read, failed = await _beside_article(
        base, ["/punycode", "/windows-31j", "/meta-shift-jis", "/utf-16"]
    )
    gbk, _ = await _beside_article(base, ["/gbk"])

    assert failed == {}
    assert read == {
        "/punycode": "x�",
        "/windows-31j": JAPANESE,
        "/meta-shift-jis": JAPANESE,
        "/utf-16": "日本語",
    }
    assert gbk == {"/gbk": "\U00020000"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.100/",
    ],
)
async def test_this_computer_and_private_networks_are_never_requested(
    site: tuple[str, list[str]], url: str
) -> None:
    base, _requests = site

    read, failed = await _extract([f"{base}/article", url])

    assert list(read) == [f"{base}/article"]
    assert "private network" in failed[url]


@pytest.mark.asyncio
async def test_a_redirect_keeps_neither_private_addresses_nor_cookies(
    site: tuple[str, list[str]],
) -> None:
    base, requests = site

    read, failed = await _beside_article(base, ["/to-private", "/set-cookie"])

    assert "private network" in failed["/to-private"]
    assert read == {"/set-cookie": "cookie: none"}
    assert "/article" in requests and requests.count("/article") == 1


@pytest.mark.asyncio
async def test_a_lapsed_sign_in_is_not_read_here_instead(
    site: tuple[str, list[str]],
    tmp_path: Path,
) -> None:
    base, requests = site
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    set_web_search_credential(WebSearchCredential(api_key="tvly-key"))
    restore_expired_cloud_identity(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=CONTEXT["user_id"],
        session_version="1",
    )

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _extract([f"{base}/article"])

    assert exc_info.value.suggested_action == "reauthenticate"
    assert requests == []


@pytest.mark.asyncio
async def test_research_does_not_present_a_whole_page_as_query_excerpts(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site
    args: dict[str, object] = {
        "urls": [f"{base}/article"],
        "query": "providers",
        "offset": 1,
        "limit": 1_000,
    }
    call = ReactToolCall(
        tool_name="web_extract",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(
            tool_id="web_extract",
            reason=None,
            args=args,  # type: ignore[arg-type]
        ),
    )
    registry = ReactToolRegistry(
        WebResearchToolSession(user_id="user-1", speaks_to_user=True).definitions()
    )

    result = await registry.execute(call, 1)

    page = result.output["results"][0]  # type: ignore[index]
    assert page["raw_content"] == ARTICLE_TEXT
    assert page["truncation_reason"] is None
    assert page["retry_hint"] is None


def test_page_markup_keeps_structure_and_drops_nothing_but_non_text() -> None:
    page = """<html><body><header><h1>Site</h1></header><nav>Menu</nav>
    <div><h2>Install</h2><ol><li>Run<pre><code><div>a</div><div>b</div></code></pre></li>
    <li>Done</li></ol><table><tr><th>Name</th><th>Use</th></tr>
    <tr><td>zen</td><td>models</td></tr></table></div>
    <article><p>One</p></article><article><p>Two</p></article>
    <footer>Copyright</footer></body></html>"""

    assert html_to_markdown(page) == (
        "# Site\n\n## Install\n\n"
        "1. Run\n   ```\n   a\n   b\n   ```\n2. Done\n\n"
        "| Name | Use |\n| --- | --- |\n| zen | models |\n\n"
        "One\n\nTwo\n\nCopyright"
    )


def test_a_page_marking_its_main_region_is_read_from_there() -> None:
    page = """<body><header>Blog</header><div role="main">
    <article><h1>First post</h1></article><article><h1>Second post</h1></article>
    </div><footer>Footer</footer></body>"""

    assert html_to_markdown(page) == "# First post\n\n# Second post"


def test_a_page_deeper_than_the_parser_default_keeps_its_end() -> None:
    page = "<body>" + "<div>" * 260 + "x" + "</div>" * 260 + "<p>end</p></body>"

    assert html_to_markdown(page).endswith("end")

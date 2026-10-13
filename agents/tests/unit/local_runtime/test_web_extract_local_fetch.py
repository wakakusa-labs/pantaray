"""web_extract reads pages from this computer when no web provider is connected.

Pages come from a server on 127.0.0.1 under the name ``site.test``, which the
fetch's address check is told resolves there; every other name and address
goes through the real check.
"""

from __future__ import annotations

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
<body><header>Site banner</header><nav>Menu</nav>
<main><h1>Providers</h1><p>Use <a href="/docs/zen">Zen</a> or <code>/connect</code>.</p>
<ul><li>First</li><li>Second</li></ul></main>
<footer>Copyright</footer></body></html>"""
# Shift_JIS as browsers read it is code page 932, which has these two; Python's
# own shift_jis codec does not.
JAPANESE = "日本語髙①"
UTF16_WITH_BOM = "\ufeff日本語".encode("utf-16-le")
# WHATWG decodes a gbk label with gb18030, which has four-byte sequences.
FOUR_BYTE_GBK = "\U00020000".encode("gb18030")
BAD_LINK = b'<p>See <a href="https://[broken">this</a>.</p>'


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's hook name
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        if self.path == "/slow":
            time.sleep(2)
        port = self.server.server_address[1]
        private = f"http://127.0.0.1:{port}/article"
        other_host = f"http://other.test:{port}/echo-cookie"
        status, headers, body = {
            "/set-cookie": (
                302,
                {"Set-Cookie": "session=secret; Path=/", "Location": other_host},
                b"",
            ),
            "/echo-cookie": (
                200,
                {"Content-Type": "text/plain"},
                f"cookie: {self.headers.get('Cookie', 'none')}".encode(),
            ),
            "/broken.pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4"),
            "/article": (200, {"Content-Type": "text/html; charset=utf-8"}, ARTICLE),
            "/blocked": (403, {"Content-Type": "text/html"}, b"denied"),
            "/large": (200, {"Content-Type": "text/plain"}, b"x" * 4096),
            "/script-only": (200, {"Content-Type": "text/html"}, b"<div id=app>"),
            "/to-private": (302, {"Location": private}, b""),
            "/bad-redirect": (302, {"Location": "http://[::1/"}, b""),
            "/bad-link": (200, {"Content-Type": "text/html"}, BAD_LINK),
            "/rot13": (200, {"Content-Type": "text/plain; charset=rot_13"}, b"plain"),
            "/idna": (200, {"Content-Type": "text/plain; charset=idna"}, b"plain"),
            "/punycode": (
                200,
                {"Content-Type": "text/plain; charset=punycode"},
                b"plain\xff",
            ),
            "/windows-31j": (
                200,
                {"Content-Type": "text/plain; charset=windows-31j"},
                JAPANESE.encode("cp932"),
            ),
            "/meta-shift-jis": (
                200,
                {"Content-Type": "text/html"},
                b'<meta charset="Shift_JIS"><p>' + JAPANESE.encode("cp932"),
            ),
            "/far-port": (302, {"Location": "http://site.test:65536/"}, b""),
            "/gzip-twice": (
                200,
                {"Content-Type": "text/plain", "Content-Encoding": "gzip, gzip"},
                gzip.compress(gzip.compress(b"plain")),
            ),
            "/utf-16": (200, {"Content-Type": "text/plain"}, UTF16_WITH_BOM),
            "/gbk": (200, {"Content-Type": "text/plain; charset=gbk"}, FOUR_BYTE_GBK),
            "/deep": (200, {"Content-Type": "text/html"}, b"<div>" * 3_000),
        }.get(self.path, (404, {}, b""))
        self.send_response(status)
        for name, value in {**headers, "Content-Length": str(len(body))}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_check = local_fetch._public_address

    async def resolve(host: str, port: int | None, scheme: str) -> str:
        if host in {"site.test", "other.test"}:
            return "127.0.0.1"
        return await real_check(host, port, scheme)

    monkeypatch.setattr(local_fetch, "_public_address", resolve)
    yield f"http://site.test:{server.server_address[1]}", server.requests  # type: ignore[attr-defined]
    server.shutdown()
    server.server_close()


async def _extract(urls: list[str]) -> dict[str, object]:
    response = await invoke_web_tools_wrapper(
        tool_id="web_extract",
        web_tool_profile=WEB_EXTRACT_PROFILE_ID,
        args={"urls": urls},  # type: ignore[dict-item]
        context=CONTEXT,
        max_retries=1,
    )
    result = response["result"]
    assert isinstance(result, dict)
    return result


def _failures(result: dict[str, object]) -> dict[str, object]:
    rows = result["failed_results"]
    assert isinstance(rows, list)
    return {row["url"]: row["error"] for row in rows}


@pytest.mark.asyncio
async def test_with_no_provider_a_page_is_read_here_and_says_so(
    site: tuple[str, list[str]],
) -> None:
    base, requests = site
    mark_configured()

    result = await _extract([f"{base}/article"])

    assert result["failed_results"] == []
    assert result["results"] == [
        {
            "url": f"{base}/article",
            "raw_content": "# Providers\n\n"
            "Use [Zen](/docs/zen) or `/connect`.\n\n- First\n- Second",
        }
    ]
    assert result["meta"]["upstream_provider"] == "local"  # type: ignore[index]
    assert requests == ["/article"]


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
    mark_configured()

    with pytest.raises(WebContentExecutionError) as exc_info:
        await _extract([f"{base}/article"])

    assert exc_info.value.suggested_action == "reauthenticate"
    assert requests == []


@pytest.mark.asyncio
async def test_each_page_fails_on_its_own_with_the_reason(
    site: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _requests = site
    mark_configured()
    monkeypatch.setattr(local_fetch, "FETCH_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(local_fetch, "MAX_PAGE_BYTES", 1024)
    urls = [
        f"{base}/article",
        f"{base}/blocked",
        f"{base}/slow",
        f"{base}/large",
        f"{base}/script-only",
    ]

    result = await _extract(urls)

    assert [row["url"] for row in result["results"]] == [f"{base}/article"]  # type: ignore[union-attr,index]
    failures = _failures(result)
    assert failures[f"{base}/blocked"] == "The site answered HTTP 403 Forbidden."
    assert failures[f"{base}/slow"] == "No complete response within 0.5 seconds."
    assert "larger than" in str(failures[f"{base}/large"])
    assert "JavaScript" in str(failures[f"{base}/script-only"])


@pytest.mark.asyncio
async def test_a_malformed_page_never_takes_the_other_pages_down(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site
    mark_configured()
    bad_port = "http://site.test:abc/"
    pages = ("/article", "/bad-link", "/rot13", "/bad-redirect")

    result = await _extract([*(base + page for page in pages), bad_port])
    charsets = await _extract(
        [
            f"{base}/article",
            f"{base}/idna",
            f"{base}/punycode",
            f"{base}/windows-31j",
            f"{base}/meta-shift-jis",
        ]
    )
    more = await _extract(
        [
            f"{base}/article",
            f"{base}/far-port",
            f"{base}/utf-16",
            f"{base}/gbk",
            f"{base}/gzip-twice",
        ]
    )

    read = {row["url"]: row["raw_content"] for row in result["results"]}  # type: ignore[union-attr,index]
    assert read[f"{base}/article"].startswith("# Providers")
    assert read[f"{base}/bad-link"] == "See [this](https://[broken)."
    assert read[f"{base}/rot13"] == "plain"
    failures = _failures(result)
    assert set(failures) == {f"{base}/bad-redirect", bad_port}
    assert "not a valid URL" in str(failures[bad_port])
    # A label outside the WHATWG table reads as UTF-8; a Shift_JIS label reads
    # as code page 932, whether the header or the page declares it.
    assert [row["raw_content"] for row in charsets["results"]][1:] == [  # type: ignore[index,union-attr]
        "plain",
        "plain\ufffd",
        JAPANESE,
        JAPANESE,
    ]
    # A byte order mark outranks the missing charset.
    assert [row["raw_content"] for row in more["results"]][1:] == [  # type: ignore[index,union-attr]
        "日本語",
        "\U00020000",
    ]
    assert _failures(more) == {
        f"{base}/far-port": "The page could not be fetched.",
        f"{base}/gzip-twice": "The page uses more than one content encoding, "
        "which this tool does not read.",
    }


@pytest.mark.asyncio
async def test_a_converter_failing_on_one_page_leaves_the_others(
    site: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _requests = site
    mark_configured()

    def broken_pdf(**_kwargs: object) -> None:
        # What pypdf raised on a malformed inline image, outside its own errors.
        raise AssertionError

    monkeypatch.setattr(local_fetch, "extract_document", broken_pdf)

    result = await _extract([f"{base}/article", f"{base}/broken.pdf", f"{base}/deep"])

    assert [row["url"] for row in result["results"]] == [f"{base}/article"]  # type: ignore[union-attr,index]
    # A tree deeper than rendering recurses fails its own page the same way.
    assert _failures(result) == {
        f"{base}/broken.pdf": "The page could not be converted to text.",
        f"{base}/deep": "The page could not be converted to text.",
    }


@pytest.mark.asyncio
async def test_a_cookie_does_not_follow_a_redirect_to_another_host_on_the_same_ip(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site
    mark_configured()

    result = await _extract([f"{base}/set-cookie"])

    assert result["results"][0]["raw_content"] == "cookie: none"  # type: ignore[index]


def test_skipped_elements_repeated_up_to_the_size_limit_render_quickly() -> None:
    page = "<body>" + "<script></script>a" * 250_000 + "</body>"
    assert len(page.encode()) < local_fetch.MAX_PAGE_BYTES

    started = time.perf_counter()
    text = html_to_markdown(page)

    assert time.perf_counter() - started < 5
    assert text == "a" * 250_000


def test_a_page_deeper_than_the_parser_default_keeps_its_end() -> None:
    page = "<body>" + "<div>" * 260 + "x" + "</div>" * 260 + "<p>end</p></body>"

    assert html_to_markdown(page).endswith("end")


def test_every_article_of_a_listing_page_is_read() -> None:
    page = """<body><header>Blog</header><div id="primary"><div role="main">
    <article><h1>First post</h1><p>First body.</p></article>
    <article><h1>Second post</h1><p>Second body.</p></article>
    </div></div><footer>Footer</footer></body>"""

    assert html_to_markdown(page) == (
        "# First post\n\nFirst body.\n\n# Second post\n\nSecond body."
    )


def test_the_text_grows_no_faster_than_the_markup() -> None:
    lines = "line<br>" * 2_000
    pages = (
        "<body>" + '<a href="a">x</a> ' * 2_000 + "</body>",
        "<body>" + "<blockquote>" * 60 + lines + "</blockquote>" * 60 + "</body>",
        "<body>" + "<ul><li>" * 60 + lines + "</li></ul>" * 60 + "</body>",
        "<body>" + "<table><tr><td>" * 30 + "x" * 2_000 + "</td></tr></table>" * 30,
    )

    for page in pages:
        assert 0 < len(html_to_markdown(page)) < 3 * len(page)


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
    url: str,
) -> None:
    mark_configured()

    result = await _extract([url])

    assert "private network" in str(_failures(result)[url])


@pytest.mark.asyncio
async def test_a_redirect_into_a_private_network_is_not_followed(
    site: tuple[str, list[str]],
) -> None:
    base, requests = site
    mark_configured()

    result = await _extract([f"{base}/to-private"])

    assert "private network" in str(_failures(result)[f"{base}/to-private"])
    assert requests == ["/to-private"]


@pytest.mark.asyncio
async def test_research_does_not_present_a_whole_page_as_query_excerpts(
    site: tuple[str, list[str]],
) -> None:
    base, _requests = site
    mark_configured()
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
    assert page["raw_content"].startswith("# Providers")
    assert page["truncation_reason"] is None
    assert page["retry_hint"] is None


def test_page_markup_keeps_structure_and_drops_site_chrome() -> None:
    page = """<html><body><header><h1>Site</h1></header><nav>Menu</nav>
    <div><h2>Install</h2><ol><li>Run<pre><code><div>a</div><div>b</div></code></pre></li>
    <li>Done</li></ol><table><tr><th>Name</th><th>Use</th></tr>
    <tr><td>zen</td><td>models</td></tr></table></div>
    <footer>Copyright</footer></body></html>"""

    assert html_to_markdown(page) == (
        "## Install\n\n"
        "1. Run\n   ```\n   a\n   b\n   ```\n2. Done\n\n"
        "| Name | Use |\n| --- | --- |\n| zen | models |"
    )

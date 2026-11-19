"""`browser` `screenshot {printed_pages}`: the run's page printed, then drawn.

Electron is played as in the browser broker tests, answering the print with a
real PDF; the pages are then drawn by the real renderer. What is checked is
what printed pages add: the page is printed under the run's page policy, the
pages asked for come back as stored images, and the message says what was drawn
without naming anything the page says.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.browser_artifact import (
    BROWSER_TEMP_DIRECTORY,
)
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationFailed,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.tooling.brokering import printed_pages
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    FinalizedBrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.browser_protocol import (
    BrowserScreenshotOutput,
    BrowserToolOutput,
)
from pantaray_agents.local_runtime.tooling.documents import PageRenderTimeoutError
from pantaray_agents.tools.files import render_pages

from .test_browser_broker import (
    _NETWORK,
    Runtime,
    _call,
    _electron,
    _network_off,
    _succeeded,
    runtime,  # noqa: F401 - the shared fixture
)
from .test_pdf_page_render import write_pdf

_MARKER = "INJECTED-MARKER"


def _pdf(tmp_path: Path, page_count: int) -> bytes:
    path = tmp_path / "printed.pdf"
    write_pdf(path, marks=[(0, 0, 0)] * page_count)
    return path.read_bytes()


async def _printed_pages(
    env: Runtime, pages: list[int], pdf: bytes
) -> tuple[object, dict[str, object]]:
    return await asyncio.gather(
        _call(env, {"action": "screenshot", "printed_pages": pages}),
        _electron(env, _succeeded(pdf=pdf, marker=_MARKER), pdf=pdf),
    )


def _parsed(outcome: object) -> BrowserScreenshotOutput:
    parsed = BrowserToolOutput.model_validate(outcome.output).root  # type: ignore[attr-defined]
    assert isinstance(parsed, BrowserScreenshotOutput)
    return parsed


# Catches: the page not printed for the pages, pages drawn but not stored or
# attached, a page past the end refused instead of left out, the page's own
# text in the message, or a PDF left behind.
@pytest.mark.parametrize(
    ("pages", "past_the_end"),
    [
        pytest.param([3, 1, 5], "5", id="asked-with-one-past-the-end"),
        pytest.param([1, 2, 3], None, id="every-page"),
    ],
)
async def test_printed_pages_are_drawn_and_attached(
    runtime: Runtime,  # noqa: F811
    tmp_path: Path,
    pages: list[int],
    past_the_end: str | None,
) -> None:
    outcome, payload = await _printed_pages(runtime, pages, _pdf(tmp_path, 3))

    assert payload["operation"] == {"action": "print"}
    assert payload["policy"]["allow_network"] is True  # type: ignore[index]
    parsed = _parsed(outcome)
    drawn = [page for page in pages if page <= 3]
    assert parsed.page_count == 3
    assert [page.page_number for page in parsed.attachments] == drawn
    attachments = outcome.attachments  # type: ignore[attr-defined]
    assert [a["ref"] for a in attachments] == [p.ref for p in parsed.attachments]
    stored = read_local_runtime_artifact_root() / "generated" / "images"
    for page in parsed.attachments:
        assert (stored / page.storage_path).stat().st_size == page.byte_size
        assert page.ref in str(parsed.message)
    assert outcome.output["network"] == _NETWORK  # type: ignore[attr-defined]
    if past_the_end is None:
        assert "past the end" not in str(parsed.message)
    else:
        assert f"past the end, not drawn: {past_the_end}." in str(parsed.message)
    # Refused requests and the page's title and URL stay in the untrusted data.
    output = dict(outcome.output)  # type: ignore[attr-defined]
    assert _MARKER in json.dumps(output.pop("data"))
    assert _MARKER not in json.dumps(output)
    # Neither Electron's PDF nor the copy that was drawn is left behind.
    temp = read_local_runtime_artifact_root() / BROWSER_TEMP_DIRECTORY
    assert not list(temp.iterdir())
    assert not list(runtime[0].parent.rglob("*.pdf"))


# Catches: pages that are all past the end failing the output contract
# instead of coming back as a page count with nothing drawn.
async def test_only_pages_past_the_end_draw_nothing(
    runtime: Runtime,  # noqa: F811
    tmp_path: Path,
) -> None:
    outcome, _ = await _printed_pages(runtime, [4, 5], _pdf(tmp_path, 3))

    parsed = _parsed(outcome)
    assert (parsed.page_count, parsed.attachments) == (3, [])
    assert "past the end, not drawn: 4, 5." in str(parsed.message)


# Catches: a print that does not follow the read scope or the user's network
# setting, as every other operation does.
@pytest.mark.parametrize("runtime", ["full_access"], indirect=True)
async def test_full_access_opens_every_folder_and_the_network_setting_is_followed(
    runtime: Runtime,  # noqa: F811
    tmp_path: Path,
) -> None:
    _network_off(runtime)

    _, payload = await _printed_pages(runtime, [1], _pdf(tmp_path, 1))

    assert payload["policy"]["file_roots"] == ["/"]  # type: ignore[index]
    assert payload["policy"]["allow_network"] is False  # type: ignore[index]


# Catches: a failed print reaching the model without its code, its hint or the
# hosts the page reached before it failed.
async def test_a_failed_print_keeps_its_code_hint_and_network(
    runtime: Runtime,  # noqa: F811
) -> None:
    failure = BrowserOperationFailed.model_validate(
        {
            "status": "failed",
            "code": "BROWSER_NO_PAGE",
            "message": "No page is open.",
            "network": _NETWORK,
        }
    )

    error, _ = await asyncio.gather(
        _call(runtime, {"action": "screenshot", "printed_pages": [1]}),
        _electron(runtime, failure),
        return_exceptions=True,
    )

    assert isinstance(error, FinalizedBrokerPolicyError)
    assert error.code == "BROWSER_NO_PAGE"
    assert "No page is open." in str(error)
    assert error.fix_hint
    assert isinstance(error.finalized_output, dict)
    assert error.finalized_output["network"] == _NETWORK


async def _render_timed_out(**_: object) -> object:
    raise PageRenderTimeoutError("still drawing")


# Catches: a print that reached hosts but whose PDF could not be drawn losing
# those hosts, which Electron has already reported once.
@pytest.mark.parametrize(
    ("patch", "code"),
    [
        pytest.param(
            (printed_pages, "MAX_DOCUMENT_BYTES", 10),
            "HTML_PDF_TOO_LARGE",
            id="over-cap",
        ),
        pytest.param(
            (render_pages, "render_pdf_pages", _render_timed_out),
            "RENDER_TIMED_OUT",
            id="draw-timeout",
        ),
    ],
)
async def test_a_refused_draw_keeps_the_hosts_the_print_reached(
    runtime: Runtime,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch: tuple[object, str, object],
    code: str,
) -> None:
    monkeypatch.setattr(*patch)

    with pytest.raises(FinalizedBrokerPolicyError) as caught:
        await _printed_pages(runtime, [1], _pdf(tmp_path, 1))

    error = caught.value
    assert error.code == code
    assert error.fix_hint
    assert isinstance(error.finalized_output, dict)
    assert error.finalized_output["network"] == _NETWORK

"""`browser` with ``browser: "user"`` through the broker: which browser is used,
what is refused before Electron or the user is asked, and the per-run approvals
that keep the user's tab apart from the app's page."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_approval_review import (
    pending_operation,
)
from pantaray_agents.local_runtime.tooling.approval_review import (
    ApprovalReviewRequest,
    build_approval_review_payload,
)
from pantaray_agents.local_runtime.tooling.brokering import broker_user_browser
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
)
from pantaray_agents.schema.user_browser import UserBrowserId
from pantaray_agents.tools.contract import BrokerPolicyError

from . import test_browser_broker
from .test_browser_broker import (
    Runtime,
    _allow_writes,
    _call,
    _electron,
    _network_off,
    _refused,
    _succeeded,
)
from .test_outside_workspace_patch_approval import _approval_rows, _decide

runtime = test_browser_broker.runtime  # The fixture, under its own name.

_READ = "Read the invoice you asked me to check in your account."


@pytest.fixture
def debugging_on(monkeypatch: pytest.MonkeyPatch) -> list[UserBrowserId]:
    """The browsers with remote debugging on, as the host would read them."""

    browsers: list[UserBrowserId] = ["edge", "chrome"]
    monkeypatch.setattr(
        broker_user_browser, "browsers_with_debugging_on", lambda: list(browsers)
    )
    return browsers


def _open(url: str, *, justification: str | None = _READ) -> dict[str, str]:
    args = {"action": "navigate", "url": url, "browser": "user"}
    if justification is not None:
        args["justification"] = justification
    return args


async def _approved_open(runtime: Runtime, url: str, *, step: int) -> dict:
    """Navigate the user's tab to a site not yet approved, approving it."""

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _call(runtime, _open(url), step=step)
    _decide(runtime[0], asked.value, f"action-1:step-{step}:1", "approved_once")
    _, payload = await asyncio.gather(
        _call(runtime, _open(url), step=step), _electron(runtime, _succeeded())
    )
    return payload


# Catches: with remote debugging on in none, a run waiting on a browser the
# user does not run while another one runs, or on one not installed; and a call
# sent on with no browser at all.
def test_with_debugging_off_the_run_waits_on_a_running_then_an_installed_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debugging_on: list[UserBrowserId]
) -> None:
    debugging_on.clear()
    dirs = {
        browser_id: tmp_path / browser_id
        for browser_id in ("chrome", "chrome_beta", "edge", "brave")
    }
    monkeypatch.setattr(broker_user_browser, "user_browser_data_dirs", lambda: dirs)
    pick = lambda: broker_user_browser.user_browser_for_run("unbound-run")  # noqa: E731

    with pytest.raises(BrokerPolicyError) as refused:
        pick()
    assert refused.value.code == "BROWSER_USER_NOT_INSTALLED"

    dirs["chrome_beta"].mkdir()
    dirs["edge"].mkdir()
    assert pick() == "chrome_beta"
    # A lock left by a browser that quit does not count; a live one does.
    (dirs["chrome_beta"] / "SingletonLock").symlink_to("host-999999999")
    (dirs["edge"] / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    assert pick() == "edge"


# Catches: an approval asked for, or Electron sent, a site the user's tab must
# never open: a local file, a loopback host, or a print of the user's page.
@pytest.mark.parametrize(
    "args",
    [
        _open("notes.html"),
        _open("http://localhost:5173/"),
        _open("http://app.localhost/"),
        _open("http://127.0.0.2:8080/"),
        _open("http://[::1]/"),
        {"action": "print", "pdf_path": "page.pdf", "browser": "user"},
        {"action": "screenshot", "printed_pages": [1], "browser": "user"},
    ],
)
async def test_what_the_users_browser_is_not_used_for_is_refused_first(
    runtime: Runtime, debugging_on: list[UserBrowserId], args: dict
) -> None:
    error = await _refused(runtime, args)

    assert error.code == "BROWSER_USER_NOT_OFFERED"
    assert _approval_rows(runtime[0]) == []


# Catches: a site opened in the user's signed-in browser without the user's
# approval, the summary the review reads missing the browser, an approved call
# run in another browser than the one approved, an approved host asked about
# again in the run, and a new host not asked about.
async def test_each_site_in_the_users_browser_is_approved_once_per_run(
    runtime: Runtime, debugging_on: list[UserBrowserId]
) -> None:
    db_path = runtime[0]
    with pytest.raises(BrokerPolicyError):
        await _call(runtime, _open("https://mail.example.com/", justification=None))
    assert _approval_rows(db_path) == []

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _call(runtime, _open("https://Mail.example.com/inbox"))
    [(_, _, summary)] = _approval_rows(db_path)
    assert summary == {
        "summary_kind": "user_browser_read",
        "browser": "Microsoft Edge",
        "host": "mail.example.com",
        "reason": _READ,
    }
    review = build_approval_review_payload(
        ApprovalReviewRequest(
            conversation=(),
            request_text_not_by_user=None,
            operation=pending_operation(tool_id="browser", summary=summary, args={}),  # type: ignore[arg-type]
            workspace_folders=(),
        )
    )
    assert json.loads(review)["operation"]["requires"] == ["users_browser_read"]
    _decide(db_path, asked.value, "action-1:step-1:1", "approved_once")
    debugging_on.remove("edge")
    with pytest.raises(BrokerPolicyError, match="does not match"):
        await _call(runtime, _open("https://Mail.example.com/inbox"))
    debugging_on.insert(0, "edge")

    _, opened = await asyncio.gather(
        _call(runtime, _open("https://Mail.example.com/inbox")),
        _electron(runtime, _succeeded()),
    )
    assert opened["operation"]["target"]["url"] == "https://Mail.example.com/inbox"  # type: ignore[index]
    assert opened["user_browser"] == {
        "browser": "edge",
        "read_hosts": ["mail.example.com"],
    }
    assert opened["action_id"] == "action-1"
    debugging_on.remove("edge")
    _, again = await asyncio.gather(
        _call(runtime, _open("https://mail.example.com/2", justification=None), step=2),
        _electron(runtime, _succeeded()),
    )
    assert again["user_browser"]["browser"] == "edge"  # type: ignore[index]
    assert len(_approval_rows(db_path)) == 1
    with pytest.raises(BrokerApprovalRequiredError):
        await _call(runtime, _open("https://docs.example.com/"), step=3)
    assert len(_approval_rows(db_path)) == 2


# Catches: an approval to write from the app's page covering the user's
# signed-in browser, and a user-browser write approved but never sent to the
# user's tab.
async def test_a_write_from_the_users_browser_is_approved_apart(
    runtime: Runtime, debugging_on: list[UserBrowserId]
) -> None:
    db_path = runtime[0]
    with pytest.raises(BrokerApprovalRequiredError) as app_asked:
        await _call(runtime, _allow_writes())
    _decide(db_path, app_asked.value, "action-1:step-1:1", "approved_once")
    await _call(runtime, _allow_writes())

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _call(runtime, {**_allow_writes(), "browser": "user"}, step=2)
    assert _approval_rows(db_path)[1][2] == {
        "summary_kind": "browser_write",
        "host": "api.example.com",
        "page_host": None,
        "reason": "Send the signup form you asked me to test.",
        "browser": "Microsoft Edge",
    }
    _decide(db_path, asked.value, "action-1:step-2:1", "approved_once")
    await _call(runtime, {**_allow_writes(), "browser": "user"}, step=2)

    _, payload = await asyncio.gather(
        _call(runtime, {"action": "snapshot", "browser": "user"}, step=3),
        _electron(runtime, _succeeded()),
    )
    assert payload["policy"]["write_hosts"] == ["api.example.com"]  # type: ignore[index]


async def _open_both_pages(runtime: Runtime) -> tuple[str, str]:
    """The session ids of the app's page and the user's tab, each opened."""

    _, app = await asyncio.gather(
        _call(runtime, {"action": "navigate", "url": "https://example.com/"}),
        _electron(runtime, _succeeded()),
    )
    user = await _approved_open(runtime, "https://mail.example.com/", step=2)
    return str(app["session_id"]), str(user["session_id"])


# Catches: the user's tab left open, and signed in, after the user turned the
# network off, the app's page closed in its place, and close refused with the
# network off.
async def test_network_off_refuses_the_users_browser_and_closes_only_its_tab(
    runtime: Runtime, debugging_on: list[UserBrowserId]
) -> None:
    _, user_session = await _open_both_pages(runtime)
    _network_off(runtime)

    with pytest.raises(BrokerPolicyError) as caught:
        await _call(runtime, {"action": "snapshot", "browser": "user"}, step=3)

    assert caught.value.code == "BROWSER_NETWORK_OFF"
    assert runtime[2].requests == [{"session_id": user_session}]
    closed = await _call(runtime, {"action": "close", "browser": "user"}, step=4)
    assert closed.output == {"status": "ok", "action": "close"}  # type: ignore[attr-defined]


# Catches: close on the user's browser closing the app's page instead, or both.
async def test_close_on_the_users_browser_closes_only_its_tab(
    runtime: Runtime, debugging_on: list[UserBrowserId]
) -> None:
    _, user_session = await _open_both_pages(runtime)

    await _call(runtime, {"action": "close", "browser": "user"}, step=3)

    assert runtime[2].requests == [{"session_id": user_session}]

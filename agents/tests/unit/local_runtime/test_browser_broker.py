"""`browser` through the broker, with Electron played by a task that answers
the relay's announcements: what reaches Electron and what is refused before,
the page policy, screenshots, where page text may appear, and failures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import typing
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.tools import (
    TOOL_REGISTRY,
    select_supervisor_act_tool_ids,
)
from pantaray_agents.local_runtime.runtime import browser_relay as relay_module
from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationFailed,
    BrowserOperationSucceeded,
)
from pantaray_agents.local_runtime.runtime.browser_relay import (
    BROWSER_TEMP_DIRECTORY,
    BrowserAnswer,
)
from pantaray_agents.local_runtime.runtime.one_shot_requests import OneShotRequests
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.brokering.broker_browser import FIX_HINTS
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    FinalizedBrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.browser_protocol import (
    BrowserFailureCode,
    BrowserToolArgs,
    BrowserToolOutput,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.secret_paths import secret_path_patterns

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    RecordingDesktopSession,
    _bootstrap_runtime_db,
    desktop_session,
)

_USER_ID = "user-1"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 40
_NETWORK = [{"host": "localhost", "port": 5173}]
# Page text that must stay inside ``data``: one marker per page-derived field.
_PAGE_FIELDS = ("url", "title", "snapshot", "console", "popup", "blocked")

type Runtime = tuple[Path, object, RecordingDesktopSession]

_ELECTRON_BROKER = OneShotRequests[BrowserAnswer]()


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[Runtime]:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(
        relay_module, "browser_operation_broker", lambda: _ELECTRON_BROKER
    )
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope=getattr(request, "param", "workspace"),
        allowed_tool_ids=("browser",),
    )
    with desktop_session(_USER_ID) as desktop:
        yield db_path, context, desktop


def _workspace(runtime: Runtime) -> Path:
    return Path(runtime[1].workspace_path)  # type: ignore[attr-defined]


async def _call(
    runtime: Runtime, args: dict, *, step: int = 1, preflight_only: bool = False
) -> object:
    db_path, context, _ = runtime
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="browser",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        tool_request_id=f"action-1:step-{step}:1",
        args=args,
        preflight_only=preflight_only,
    )


def _page_data(marker: str = "page") -> dict[str, object]:
    return {
        "url": f"http://localhost:5173/{marker}-url",
        "title": f"{marker}-title",
        "snapshot": f'- button "{marker}-snapshot" [ref=e1]',
        "console": [{"level": "log", "text": f"{marker}-console"}],
        "popups": [f"https://popup.example/{marker}-popup"],
        "blocked": [
            {
                "reason": "write_not_approved",
                "url": f"https://api.example/{marker}-blocked",
            }
        ],
    }


def _succeeded(
    *,
    png: bytes | None = None,
    pdf: bytes | None = None,
    marker: str = "page",
    page: dict | None = None,
) -> BrowserAnswer:
    artifact = None
    if pdf is not None:
        artifact = {
            "kind": "pdf",
            "byte_size": len(pdf),
            "sha256": hashlib.sha256(pdf).hexdigest(),
        }
    if png is not None:
        artifact = {
            "kind": "png",
            "byte_size": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            "width_px": 1280,
            "height_px": 800,
        }
    return BrowserOperationSucceeded.model_validate(
        {
            "status": "ok",
            "page_data": {**_page_data(marker), **(page or {})},
            "notes": ["dialog_dismissed"],
            "truncated": None,
            "network": _NETWORK,
            "artifact": artifact,
        }
    )


async def _electron(
    runtime: Runtime,
    answer: BrowserAnswer,
    *,
    png: bytes | None = None,
    pdf: bytes | None = None,
) -> dict[str, object]:
    """Answer the next announced operation as Electron main would."""

    desktop = runtime[2]
    while not desktop.requests:
        await asyncio.sleep(0.005)
    payload = desktop.requests.pop(0)
    request_id = str(payload["operation_request_id"])
    for kind, artifact in (("png", png), ("pdf", pdf)):
        if artifact is not None:
            path = read_local_runtime_artifact_root() / BROWSER_TEMP_DIRECTORY
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{request_id}.{kind}").write_bytes(artifact)
    status = _ELECTRON_BROKER.answer(
        request_id=request_id, user_id=_USER_ID, outcome=answer
    )
    assert status == "accepted"
    return payload


def _network_off(runtime: Runtime) -> None:
    update_command_network_enabled(
        db_path=runtime[0],
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        command_network_enabled=False,
        now="2026-03-23T00:00:01Z",
    )


async def _refused(runtime: Runtime, args: dict) -> BrokerPolicyError:
    with pytest.raises(BrokerPolicyError) as caught:
        await _call(runtime, args)
    assert runtime[2].requests == []  # Electron was not asked
    return caught.value


# Catches: a policy looser than read's (no private roots, a missing secret tier,
# approved write hosts), or reached hosts left out of the output.
async def test_navigate_reaches_electron_with_reads_policy(
    runtime: Runtime,
) -> None:
    db_path = runtime[0]
    outcome, payload = await asyncio.gather(
        _call(runtime, {"action": "navigate", "url": "http://localhost:5173/"}),
        _electron(runtime, _succeeded()),
    )

    assert payload["operation"] == {
        "action": "navigate",
        "target": {"kind": "web", "url": "http://localhost:5173/"},
    }
    policy: dict = payload["policy"]  # type: ignore[assignment]
    assert policy["allow_network"] is True
    assert policy["write_hosts"] == []
    assert Path(policy["private_roots"][0]) == db_path.parent
    assert str(_workspace(runtime)) in policy["private_root_exceptions"]
    assert any(
        _workspace(runtime).is_relative_to(root) for root in policy["file_roots"]
    )
    assert policy["secret_paths"] == [
        {"withheld": rule.withheld, "excepted": rule.excepted}
        for tier in ("app_secret", "cli_credential")
        for rule in secret_path_patterns(tier)
    ]
    assert outcome.status == "success"  # type: ignore[attr-defined]
    parsed = BrowserToolOutput.model_validate(outcome.output).root  # type: ignore[attr-defined]
    assert parsed.action == "navigate"
    assert outcome.output["network"] == _NETWORK  # type: ignore[attr-defined]


# Catches: a local path handed to Electron unresolved, or resolved by another
# rule than read's.
async def test_a_local_file_opens_by_its_real_path_with_the_network_off(
    runtime: Runtime,
) -> None:
    _network_off(runtime)
    page = _workspace(runtime) / "deck.html"
    page.write_text("<h1>Deck</h1>")

    _, payload = await asyncio.gather(
        _call(runtime, {"action": "navigate", "url": "deck.html"}),
        _electron(runtime, _succeeded()),
    )

    assert payload["operation"] == {
        "action": "navigate",
        "target": {"kind": "file", "path": str(page.resolve())},
    }
    assert payload["policy"]["allow_network"] is False  # type: ignore[index]


# Catches: a URL sent to Electron while the user has the network off, loopback
# included as the command sandbox treats it.
@pytest.mark.parametrize("url", ["http://localhost:5173/", "https://example.com/"])
async def test_network_off_refuses_every_url_before_electron(
    runtime: Runtime, url: str
) -> None:
    _network_off(runtime)

    error = await _refused(runtime, {"action": "navigate", "url": url})

    assert error.code == "BROWSER_NETWORK_OFF"
    assert error.fix_hint == FIX_HINTS["BROWSER_NETWORK_OFF"]


# Catches: a page opened with the network on keeping it after the user turned
# it off, because the refused navigate never sent Electron the new setting.
async def test_network_off_closes_a_page_opened_with_the_network_on(
    runtime: Runtime,
) -> None:
    _, opened = await asyncio.gather(
        _call(runtime, {"action": "navigate", "url": "https://example.com/"}),
        _electron(runtime, _succeeded()),
    )
    _network_off(runtime)

    with pytest.raises(BrokerPolicyError) as caught:
        await _call(
            runtime, {"action": "navigate", "url": "https://x.example/"}, step=2
        )

    assert caught.value.code == "BROWSER_NETWORK_OFF"
    desktop = runtime[2]
    assert desktop.requests == [{"session_id": opened["session_id"]}]


# Catches: a file read may not open (outside the workspace, Pantaray's own
# storage, a secret) handed to Electron to load.
@pytest.mark.parametrize("subject", ["outside", "private-storage", "secret"])
async def test_a_path_read_may_not_open_is_refused_before_electron(
    runtime: Runtime, tmp_path: Path, subject: str
) -> None:
    if subject == "outside":
        target = tmp_path / "outside.html"
    elif subject == "private-storage":
        target = runtime[0].parent / "app.html"
    else:
        target = _workspace(runtime) / ".env"
    target.write_text("<p>x</p>")

    error = await _refused(runtime, {"action": "navigate", "url": str(target)})

    assert error.code == "BROWSER_OUTSIDE_READ_SCOPE"
    assert error.fix_hint


# Catches: a screenshot whose bytes are not stored, or whose outcome
# attachment and output entry name different images.
async def test_a_screenshot_is_stored_and_attached_through_the_assembler(
    runtime: Runtime,
) -> None:
    outcome, payload = await asyncio.gather(
        _call(runtime, {"action": "screenshot", "full_page": True}),
        _electron(runtime, _succeeded(png=_PNG), png=_PNG),
    )

    assert payload["operation"]["full_page"] is True  # type: ignore[index]
    parsed = BrowserToolOutput.model_validate(outcome.output).root  # type: ignore[attr-defined]
    [entry] = parsed.attachments  # type: ignore[union-attr]
    [attachment] = outcome.attachments  # type: ignore[attr-defined]
    assert attachment["ref"] == entry.ref
    assert attachment["storage_path"] == entry.storage_path
    assert attachment["sha256"] == hashlib.sha256(_PNG).hexdigest()
    assert entry.byte_size == len(_PNG)
    stored = read_local_runtime_artifact_root() / "generated" / "images"
    assert (stored / entry.storage_path).read_bytes() == _PNG


# Catches: page text copied into a field outside ``data`` (a message, a note,
# an attachment name), where it would read as the runtime's own words.
@pytest.mark.parametrize("args", [{"action": "snapshot"}, {"action": "console"}])
async def test_page_text_appears_only_inside_untrusted_data(
    runtime: Runtime, args: dict
) -> None:
    marker = "INJECTED-MARKER"
    outcome, _ = await asyncio.gather(
        _call(runtime, args), _electron(runtime, _succeeded(marker=marker))
    )

    output = dict(outcome.output)  # type: ignore[attr-defined]
    assert output["untrusted"] is True
    data = json.dumps(output.pop("data"))
    for field in _PAGE_FIELDS:
        assert f"{marker}-{field}" in data
    assert marker not in json.dumps(output)


_LONG_URL = "https://evil.example/" + "y" * 1_970
# More page text than a result keeps inline, in many values too short for
# storage to move out one by one.
_LARGE_PAGES: dict[str, dict] = {
    "console": {
        "console": [
            {"level": "log", "text": f"line {index} " + "x" * 190}
            for index in range(100)
        ]
    },
    "popups": {
        "popups": [_LONG_URL] * 10,
        "blocked": [{"reason": "write_not_approved", "url": _LONG_URL}] * 10,
    },
}


# Catches: a result past the inline limit stored away and replaced by a preview
# outside ``data``, so page text reaches the model without the untrusted mark.
@pytest.mark.parametrize(
    ("args", "field"),
    [({"action": "console"}, "console"), ({"action": "back"}, "popups")],
)
async def test_page_text_past_the_inline_limit_stays_untrusted_in_data(
    runtime: Runtime, args: dict, field: str
) -> None:
    answer = _succeeded(page=_LARGE_PAGES[field])
    assert len(answer.page_data.model_dump_json()) > 20_000  # type: ignore[union-attr]

    outcome, _ = await asyncio.gather(_call(runtime, args), _electron(runtime, answer))

    assert outcome.output_storage_kind == "action_file"  # type: ignore[attr-defined]
    output = dict(outcome.output)  # type: ignore[attr-defined]
    assert output["untrusted"] is True
    data = output.pop("data")
    assert data["storage"] == "action_file" and data["preview"]
    assert "evil.example" not in json.dumps(output)
    assert "line 0 " not in json.dumps(output)


# Catches: an Electron failure that reaches the model without its code, its
# repair hint or the hosts the page reached before it failed.
async def test_an_electron_failure_keeps_its_code_hint_and_network(
    runtime: Runtime,
) -> None:
    failure = BrowserOperationFailed.model_validate(
        {
            "status": "failed",
            "code": "BROWSER_TIMED_OUT",
            "message": "The page did not answer in 10s.",
            "network": _NETWORK,
        }
    )

    error, _ = await asyncio.gather(
        _call(runtime, {"action": "click", "ref": "e3"}),
        _electron(runtime, failure),
        return_exceptions=True,
    )

    assert isinstance(error, FinalizedBrokerPolicyError)
    assert error.code == "BROWSER_TIMED_OUT"
    assert "Navigate again" in str(error.fix_hint)
    assert "The page did not answer in 10s." in str(error)
    assert isinstance(error.finalized_output, dict)
    assert error.finalized_output["network"] == _NETWORK


# Catches: a failure code added to the contract with no hint, which would fail
# the call with a KeyError instead of the code.
def test_every_failure_code_has_a_fix_hint() -> None:
    codes = {
        code
        for part in typing.get_args(BrowserFailureCode)
        for code in typing.get_args(part)
    }
    assert set(FIX_HINTS) == codes


# Catches: close not reaching the relay, so the page outlives the model's close.
async def test_close_closes_the_runs_session(runtime: Runtime) -> None:
    _, payload = await asyncio.gather(
        _call(runtime, {"action": "back"}), _electron(runtime, _succeeded())
    )

    outcome = await _call(runtime, {"action": "close"}, step=2)

    assert outcome.output == {"status": "ok", "action": "close"}  # type: ignore[attr-defined]
    assert runtime[2].requests == [{"session_id": payload["session_id"]}]


# Catches: allow_writes, whose approval does not exist yet, reaching Electron
# through the broker.
async def test_allow_writes_is_refused(runtime: Runtime) -> None:
    error = await _refused(
        runtime,
        {"action": "allow_writes", "host": "api.example.com", "justification": "x"},
    )

    assert error.code == "BROWSER_ACTION_UNAVAILABLE"


# Catches: the model offered an action or field the broker cannot parse, or one
# of the actions later releases add, and a field the broker parses missing from
# what the model is offered.
def test_browser_is_offered_to_the_action_with_the_brokered_actions() -> None:
    assert "browser" in select_supervisor_act_tool_ids(
        integrations=False, todo=False, todo_import=False
    )
    schema = TOOL_REGISTRY["browser"].build_validation_input_schema()
    offered = set(schema["properties"]["action"]["enum"])  # type: ignore[index]
    variants = typing.get_args(BrowserToolArgs.model_fields["root"].annotation)
    parsed = {
        typing.get_args(variant.model_fields["action"].annotation)[0]
        for variant in variants
    }
    assert offered == parsed - {"allow_writes"}
    fields = {name for variant in variants for name in variant.model_fields}
    assert set(schema["properties"]) == fields - {"host", "justification"}  # type: ignore[call-overload]

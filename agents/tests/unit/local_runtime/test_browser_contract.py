"""The browser tool's arguments and output, and its wire contract with Electron,
whose contract test reads the same fixture so the two sides cannot drift apart."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationAnswer,
)
from pantaray_agents.local_runtime.tooling.brokering.browser_protocol import (
    BrowserToolArgs,
    BrowserToolOutput,
)
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    BrowserOperationRequestedMessage,
    BrowserSessionCloseRequestedMessage,
)

# Any: the fixture is JSON test data, read as it is written.
_CASES: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "frontend/tests/fixtures/browser_contract_cases.json"
    ).read_text(encoding="utf-8")
)
_MODELS: dict[str, type[BaseModel]] = {
    "operation_requested": BrowserOperationRequestedMessage,
    "session_close_requested": BrowserSessionCloseRequestedMessage,
    "operation_answer": BrowserOperationAnswer,
}


@pytest.mark.parametrize(
    ("model", "data", "valid"),
    [
        pytest.param(
            _MODELS[section], case["data"], validity == "valid", id=case["name"]
        )
        for section in _MODELS
        for validity in ("valid", "invalid")
        for case in _CASES[section][validity]
    ],
)
def test_wire_case(model: type[BaseModel], data: object, valid: bool) -> None:
    if not valid:
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(data))
        return
    # The backend sends ``model_dump(mode="json")``: a valid case written in that
    # form is exactly what Electron must accept.
    assert model.model_validate_json(json.dumps(data)).model_dump(mode="json") == data


def test_event_names_match_the_fixture() -> None:
    assert _CASES["events"] == {
        "operation_requested": OutboundEvent.BROWSER_OPERATION_REQUESTED.value,
        "session_close_requested": OutboundEvent.BROWSER_SESSION_CLOSE_REQUESTED.value,
    }


@pytest.mark.parametrize(
    "args",
    [
        {"action": "navigate", "url": "./deck.html"},
        {"action": "back"},
        {"action": "snapshot", "ref": "e4"},
        {"action": "click", "ref": "e12"},
        {"action": "drag", "ref": "e12", "to_ref": "e13"},
        {"action": "type", "ref": "e2", "text": "hi", "submit": True},
        {"action": "press", "key": "Enter"},
        {"action": "select", "ref": "e3", "values": ["a"]},
        {"action": "scroll", "direction": "down"},
        {"action": "wait", "text": "Saved"},
        {"action": "wait", "seconds": 3},
        {"action": "screenshot"},
        {"action": "screenshot", "printed_pages": [1, 2]},
        {"action": "print", "pdf_path": "out/deck.pdf"},
        {"action": "console"},
        {"action": "allow_writes", "host": "api.x.test", "justification": "j"},
        {"action": "close"},
    ],
)
def test_tool_args_accept_every_action(args: dict[str, object]) -> None:
    assert BrowserToolArgs.model_validate(args).model_dump(exclude_unset=True) == args


@pytest.mark.parametrize(
    "args",
    [
        {"action": "evaluate", "script": "1"},
        {"action": "back", "url": "https://x.test/"},
        {"action": "navigate", "target": {"kind": "web", "url": "https://x.test/"}},
        {"action": "screenshot", "ref": "e1", "printed_pages": [1]},
        {"action": "screenshot", "full_page": True, "printed_pages": [1]},
        {"action": "print", "pdf_path": "out/deck.html"},
        {"action": "allow_writes", "host": "https://x.test", "justification": "j"},
        {"action": "allow_writes", "host": "x.test:443", "justification": "j"},
        {"action": "allow_writes", "host": "x.test", "justification": " "},
        {"action": "wait"},
    ],
)
def test_tool_args_refuse_what_no_action_takes(args: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BrowserToolArgs.model_validate(args)


_PAGE = {"url": "https://x.test/", "title": "X", "snapshot": None, "console": None}
_OK: dict[str, object] = {
    "status": "ok",
    "truncated": None,
    "notes": [],
    "network": [{"host": "x.test", "port": 443}],
    "untrusted": True,
    "data": {**_PAGE, "popups": [], "blocked": []},
}
_IMAGE = {
    "type": "file",
    "source_kind": "local_image_blob",
    "mime_type": "image/png",
    "path": "screenshot.png",
    "storage_path": "generated/images/u/2026-11-19/x.png",
    "ref": "tool-attachment:abc",
    "byte_size": 10,
}


@pytest.mark.parametrize(
    ("output", "valid"),
    [
        ({**_OK, "action": "navigate"}, True),
        ({**_OK, "action": "screenshot", "attachments": [_IMAGE]}, True),
        (
            {**_OK, "action": "print", "pdf_path": "a.pdf", "replaced": False},
            True,
        ),
        ({"status": "ok", "action": "close"}, True),
        # Page text outside ``data``.
        ({**_OK, "action": "navigate", "title": "X"}, False),
        ({**_OK, "action": "navigate", "untrusted": False}, False),
        ({**_OK, "action": "screenshot", "attachments": []}, False),
        ({**_OK, "action": "click", "attachments": [_IMAGE]}, False),
        ({**_OK, "action": "close"}, False),
        ({"status": "ok", "action": "navigate"}, False),
    ],
)
def test_tool_output_shapes(output: dict[str, object], valid: bool) -> None:
    if valid:
        BrowserToolOutput.model_validate(output)
    else:
        with pytest.raises(ValidationError):
            BrowserToolOutput.model_validate(output)

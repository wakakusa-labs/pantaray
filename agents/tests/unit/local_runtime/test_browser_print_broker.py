"""`browser` `print`: the run's page printed by the desktop app, saved like apply_patch.

Electron is played as in the browser broker tests. What is checked is what
print adds: the PDF is written under apply_patch's rules and approval, the
destination is checked again before the write, under the workspace lock, and a
refused or failed call leaves no file behind.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.browser_contract import (
    BrowserOperationFailed,
)
from pantaray_agents.local_runtime.tooling.brokering import (
    broker_browser_print as print_module,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerExecutionError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerPreflightOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.browser_protocol import (
    BrowserPrintOutput,
)
from pantaray_agents.local_runtime.tooling.locks.workspace_lock_coordinator import (
    WorkspaceLockConflictError,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .test_browser_broker import (
    _NETWORK,
    Runtime,
    _call,
    _electron,
    _succeeded,
    _workspace,
    runtime,  # noqa: F401 - the shared fixture
)
from .test_outside_workspace_patch_approval import _approval_rows, _decide

_PDF = b"%PDF-1.7\n" + b"\x02" * 64 + b"\n%%EOF\n"
_REQUEST_ID = "action-1:step-1:1"


async def _save(env: Runtime, pdf_path: str, *, preflight_only: bool = False) -> object:
    return await _call(
        env, {"action": "print", "pdf_path": pdf_path}, preflight_only=preflight_only
    )


async def _printed(env: Runtime) -> dict[str, object]:
    return await _electron(env, _succeeded(pdf=_PDF), pdf=_PDF)


def _files(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.is_file())


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("outside-folder").resolve()


@pytest.mark.parametrize("existing", [False, True], ids=["new", "overwrite"])
async def test_saves_in_the_workspace_without_asking(
    runtime: Runtime,  # noqa: F811
    existing: bool,
) -> None:
    pdf = _workspace(runtime) / "deck.pdf"
    if existing:
        pdf.write_bytes(b"old")
        pdf.chmod(0o640)

    outcome, payload = await asyncio.gather(
        _save(runtime, "deck.pdf"), _printed(runtime)
    )

    assert payload["operation"] == {"action": "print"}
    assert outcome.status == "success"  # type: ignore[attr-defined]
    saved = BrowserPrintOutput.model_validate(outcome.output)  # type: ignore[attr-defined]
    assert saved.pdf_path == str(pdf)
    assert saved.replaced is existing
    assert pdf.read_bytes() == _PDF
    if existing:
        assert pdf.stat().st_mode & 0o777 == 0o640
    assert outcome.output["network"] == _NETWORK  # type: ignore[attr-defined]
    # Approved by the setting, under apply_patch's summary naming the file.
    assert _approval_rows(runtime[0]) == [
        (
            _REQUEST_ID,
            "approved_once",
            {"summary_kind": "apply_patch", "target_paths": ["deck.pdf"]},
        )
    ]
    # No temporary file is left beside the PDF.
    assert _files(pdf.parent) == ["deck.pdf"]


async def test_outside_the_workspace_asks(
    runtime: Runtime,  # noqa: F811
    outside: Path,
) -> None:
    pdf = outside / "deck.pdf"

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _save(runtime, str(pdf))

    # Asked like apply_patch, with the folder named, before anything is printed.
    assert _approval_rows(runtime[0]) == [
        (
            _REQUEST_ID,
            "pending",
            {
                "summary_kind": "apply_patch",
                "target_paths": [str(pdf)],
                "outside_workspace": {
                    "folders": [{"path": str(outside), "display_name": outside.name}],
                    "can_allow_for_conversation": True,
                },
            },
        )
    ]
    assert runtime[2].requests == []
    assert not pdf.exists()

    _decide(runtime[0], asked.value, _REQUEST_ID, "approved_once")
    outcome, _ = await asyncio.gather(_save(runtime, str(pdf)), _printed(runtime))

    assert outcome.output["pdf_path"] == str(pdf)  # type: ignore[attr-defined]
    assert pdf.read_bytes() == _PDF


# Every browser call is preflighted by the runtime, so the approval binds to the
# request id a resumed call reuses; only print may ask.
async def test_the_preflight_asks_only_for_print(
    runtime: Runtime,  # noqa: F811
    outside: Path,
) -> None:
    navigate = {"action": "navigate", "url": "https://example.com/"}

    passed = await _call(runtime, navigate, preflight_only=True)

    assert isinstance(passed, BrokerPreflightOutcome)
    assert _approval_rows(runtime[0]) == []
    with pytest.raises(BrokerApprovalRequiredError):
        await _save(runtime, str(outside / "deck.pdf"), preflight_only=True)
    assert runtime[2].requests == []


async def test_a_destination_moved_while_printing_is_not_written(
    runtime: Runtime,  # noqa: F811
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moved = False
    resolve_plan = print_module.resolve_patch_mount_plan

    def moving_plan(**kwargs: object) -> print_module.PatchMountPlan:
        plan = resolve_plan(**kwargs)  # type: ignore[arg-type]
        return replace(plan, outside_workspace_folder=outside) if moved else plan

    async def electron_then_move() -> None:
        nonlocal moved
        await _printed(runtime)
        moved = True

    monkeypatch.setattr(print_module, "resolve_patch_mount_plan", moving_plan)
    results = await asyncio.gather(
        _save(runtime, "deck.pdf"), electron_then_move(), return_exceptions=True
    )

    error = results[0]
    assert isinstance(error, BrokerPolicyError)
    assert error.code == "PATCH_PATH_RETARGETED"
    assert not (_workspace(runtime) / "deck.pdf").exists()


async def test_a_held_workspace_lock_writes_nothing(
    runtime: Runtime,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def held(**_: object) -> object:
        raise WorkspaceLockConflictError("workspace root is locked")

    monkeypatch.setattr(print_module, "acquire_workspace_root_lock", held)
    results = await asyncio.gather(
        _save(runtime, "deck.pdf"), _printed(runtime), return_exceptions=True
    )

    error = results[0]
    assert isinstance(error, BrokerExecutionError)
    assert "PATCH_LOCK_CONFLICT" in str(error)
    assert not (_workspace(runtime) / "deck.pdf").exists()


async def test_a_failed_print_writes_nothing(
    runtime: Runtime,  # noqa: F811
) -> None:
    failure = BrowserOperationFailed.model_validate(
        {
            "status": "failed",
            "code": "BROWSER_ACTION_FAILED",
            "message": "Printing failed.",
            "network": [],
        }
    )

    results = await asyncio.gather(
        _save(runtime, "deck.pdf"),
        _electron(runtime, failure),
        return_exceptions=True,
    )

    error = results[0]
    assert isinstance(error, BrokerPolicyError)
    assert error.code == "BROWSER_ACTION_FAILED"
    assert error.fix_hint
    assert _files(_workspace(runtime)) == []


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("not-pdf", "BROKER_TOOL_ARGS_INVALID"),
        ("missing-folder", "WRITE_PATH_NOT_FOUND"),
        ("link-to-text", "WRITE_PATH_DENIED"),
        ("private-storage", "WRITE_PATH_DENIED"),
    ],
)
async def test_refused_before_the_user_or_electron_is_asked(
    runtime: Runtime,  # noqa: F811
    subject: str,
    expected: str,
) -> None:
    notes = _workspace(runtime) / "notes.txt"
    notes.write_text("keep me")
    pdf_paths: dict[str, str] = {
        "not-pdf": "deck.txt",
        "missing-folder": "missing/deck.pdf",
        "link-to-text": "notes.pdf",
        "private-storage": str(runtime[0].parent / "deck.pdf"),
    }
    (notes.parent / "notes.pdf").symlink_to(notes)

    with pytest.raises(BrokerPolicyError) as caught:
        await _save(runtime, pdf_paths[subject])

    assert caught.value.code == expected
    assert _approval_rows(runtime[0]) == []
    assert runtime[2].requests == []
    assert notes.read_text() == "keep me"
    assert not (runtime[0].parent / "deck.pdf").exists()

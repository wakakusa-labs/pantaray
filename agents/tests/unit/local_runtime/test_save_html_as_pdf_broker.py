"""`save_html_as_pdf`: an HTML file printed by the desktop app, saved like apply_patch.

Electron is played as in the render_html_page tests. What is checked is what
this tool adds: the PDF is written under apply_patch's rules and approval, the
destination is checked again before the write, and a refused or failed call
leaves no file behind.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.tools import (
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
)
from pantaray_agents.local_runtime.runtime.html_render_relay import HtmlRenderFailed
from pantaray_agents.local_runtime.tooling.brokering import (
    broker_direct_save_pdf as save_module,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    SaveHtmlAsPdfOutput,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    _CHILD_BROKER_TOOLS,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _grant_workspace_full_access,
)
from .test_outside_workspace_patch_approval import _approval_rows, _decide
from .test_render_html_page_broker import (
    Runtime,
    _announced,
    _electron,
    _workspace,
    runtime,  # noqa: F401 - the shared fixture
)


async def _save(env: Runtime, pdf_path: str, *, request_id: str = "req-1") -> object:
    db_path, _, context, _ = env
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="save_html_as_pdf",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        invocation_id=f"invocation-{request_id}",
        tool_request_id=request_id,
        requested_at=BROKER_ACTOR_TIMESTAMP,
        args={"path": "deck.html", "pdf_path": pdf_path},
    )


def _files(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.is_file())


@pytest.fixture
def deck(runtime: Runtime) -> Path:  # noqa: F811
    _grant_workspace_full_access(db_path=runtime[0], capability="scoped_write")
    html = _workspace(runtime) / "deck.html"
    html.write_text("<h1>Deck</h1>")
    return html


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("outside-folder").resolve()


@pytest.mark.parametrize("existing", [False, True], ids=["new", "overwrite"])
async def test_saves_in_the_workspace_without_asking_under_always_allow(
    runtime: Runtime,  # noqa: F811
    deck: Path,
    existing: bool,
) -> None:
    pdf = deck.parent / "deck.pdf"
    if existing:
        pdf.write_bytes(b"old")
        pdf.chmod(0o640)

    outcome, _ = await asyncio.gather(
        _save(runtime, "deck.pdf"), _electron(runtime, page_count=2)
    )

    assert outcome.status == "success"  # type: ignore[attr-defined]
    saved = SaveHtmlAsPdfOutput.model_validate(outcome.output)  # type: ignore[attr-defined]
    assert saved.pdf_path == str(pdf)
    assert saved.replaced is existing
    assert saved.byte_size == pdf.stat().st_size
    assert pdf.read_bytes().startswith(b"%PDF-")
    if existing:
        assert pdf.stat().st_mode & 0o777 == 0o640
    assert [(b.reason, b.url) for b in saved.blocked_requests] == [
        ("network_off", "https://cdn.example/a.css")
    ]
    # Approved by the setting, under apply_patch's summary naming the file.
    assert _approval_rows(runtime[0]) == [
        (
            "req-1",
            "approved_once",
            {"summary_kind": "apply_patch", "target_paths": ["deck.pdf"]},
        )
    ]
    # No temporary file is left beside the PDF.
    assert _files(deck.parent) == ["deck.html", "deck.pdf"]


async def test_outside_the_workspace_asks_even_under_always_allow(
    runtime: Runtime,  # noqa: F811
    deck: Path,
    outside: Path,
) -> None:
    pdf = outside / "deck.pdf"

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _save(runtime, str(pdf))

    # Asked like apply_patch, with the folder named, before anything is printed.
    assert _approval_rows(runtime[0]) == [
        (
            "req-1",
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
    assert _announced(runtime) == []
    assert not pdf.exists()

    _decide(runtime[0], asked.value, "req-1", "approved_once")
    outcome, _ = await asyncio.gather(
        _save(runtime, str(pdf)), _electron(runtime, page_count=1)
    )

    assert outcome.output["pdf_path"] == str(pdf)  # type: ignore[attr-defined]
    assert pdf.read_bytes().startswith(b"%PDF-")


async def test_a_destination_moved_while_printing_is_not_written(
    runtime: Runtime,  # noqa: F811
    deck: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moved = False
    resolve_plan = save_module.resolve_patch_mount_plan

    def moving_plan(**kwargs: object) -> save_module.PatchMountPlan:
        plan = resolve_plan(**kwargs)  # type: ignore[arg-type]
        return replace(plan, outside_workspace_folder=outside) if moved else plan

    async def electron_then_move() -> None:
        nonlocal moved
        await _electron(runtime, page_count=1)
        moved = True

    monkeypatch.setattr(save_module, "resolve_patch_mount_plan", moving_plan)
    results = await asyncio.gather(
        _save(runtime, "deck.pdf"), electron_then_move(), return_exceptions=True
    )

    error = results[0]
    assert isinstance(error, BrokerPolicyError)
    assert error.code == "PATCH_PATH_RETARGETED"
    assert not (deck.parent / "deck.pdf").exists()


async def test_a_failed_print_writes_nothing(
    runtime: Runtime,  # noqa: F811
    deck: Path,
) -> None:
    failure = HtmlRenderFailed(
        code="HTML_PRINT_FAILED", message="Printing failed.", blocked_requests=()
    )

    results = await asyncio.gather(
        _save(runtime, "deck.pdf"),
        _electron(runtime, failure=failure),
        return_exceptions=True,
    )

    error = results[0]
    assert isinstance(error, BrokerPolicyError)
    assert error.code == "HTML_PRINT_FAILED"
    assert error.fix_hint
    assert _files(deck.parent) == ["deck.html"]


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
    deck: Path,
    subject: str,
    expected: str,
) -> None:
    notes = deck.parent / "notes.txt"
    notes.write_text("keep me")
    pdf_paths: dict[str, str] = {
        "not-pdf": "deck.txt",
        "missing-folder": "missing/deck.pdf",
        "link-to-text": "notes.pdf",
        "private-storage": str(runtime[0].parent / "deck.pdf"),
    }
    (deck.parent / "notes.pdf").symlink_to(notes)

    with pytest.raises(BrokerPolicyError) as caught:
        await _save(runtime, pdf_paths[subject])

    assert caught.value.code == expected
    assert _approval_rows(runtime[0]) == []
    assert _announced(runtime) == []
    assert notes.read_text() == "keep me"
    assert not (runtime[0].parent / "deck.pdf").exists()


def test_offered_to_the_root_action_alone() -> None:
    """Electron answers only the root Action's stream."""

    assert "save_html_as_pdf" in SUPERVISOR_SINGLE_REACT_TOOL_IDS
    assert "save_html_as_pdf" not in {tool.tool_id for tool in _CHILD_BROKER_TOOLS}

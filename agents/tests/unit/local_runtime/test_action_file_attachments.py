"""Staged document attachments move into the Action workspace with the USER row."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.runtime import action_file_attachments
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentLinks,
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    NewActionTarget,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.office_runtime import (
    OFFICE_RUNTIME,
    OfficeRuntimePreparing,
    OfficeRuntimeSnapshot,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderPdfPageOutput,
)
from pantaray_agents.local_runtime.tooling.documents import RenderedPage
from pantaray_agents.schema.agent.action_message import (
    ActionUserMessageInput,
    FileAttachmentInput,
)

from .action_seed import ensure_user_row
from .migrated_db import prepare_test_database
from .read_tool_broker_support import bootstrap_read_runtime_db, execute_read_tool
from .test_pdf_page_render import write_pdf
from .test_read_document_broker import write_sample_docx
from .test_render_pdf_page_broker import render_pages, stub_renderer

USER = "user-1"
PDF_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
DOCX_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
PAYLOAD = b"%PDF-1.7 staged"


@dataclass
class OfficeRuntimeCalls:
    """Each call that would start LibreOffice discovery, and its thread's name."""

    calls: list[tuple[Path, str]] = field(default_factory=list)
    called: threading.Event = field(default_factory=threading.Event)


@pytest.fixture(autouse=True)
def office_runtime(monkeypatch: pytest.MonkeyPatch) -> OfficeRuntimeCalls:
    """Replaced for every test here: the real call runs Spotlight and may start
    a download of LibreOffice."""

    recorded = OfficeRuntimeCalls()

    def ensure_available(storage_base: Path) -> OfficeRuntimeSnapshot:
        recorded.calls.append((storage_base, threading.current_thread().name))
        recorded.called.set()
        return OfficeRuntimePreparing()

    monkeypatch.setattr(OFFICE_RUNTIME, "ensure_available", ensure_available)
    return recorded


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        ensure_user_row(connection, user_id=USER, timestamp="2026-09-29T00:00:00Z")
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    register_logged_out_owner(USER)
    yield db_path
    reset_logged_out_owner()


def _stage(tmp_path: Path, name: str, payload: bytes = PAYLOAD) -> Path:
    staged = tmp_path / "artifacts/generated/attachments" / USER / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(payload)
    return staged


def _command(
    *, byte_size: int = len(PAYLOAD), name: str = "Q3 report.PDF"
) -> SubmitActionMessageCommand:
    return SubmitActionMessageCommand(
        user_id=USER,
        target=NewActionTarget(),
        message=ActionUserMessageInput(
            message_id="message-1",
            content="Summarize the report",
            files=(
                FileAttachmentInput(
                    attachment_id=PDF_ID, name=name, byte_size=byte_size
                ),
            ),
        ),
    )


def _attachment(db_path: Path, action_id: str) -> Path:
    workspace = resolve_action_storage_paths(
        db_path=db_path, user_id=USER, action_id=action_id
    ).workspace
    return workspace / f"attachments/{PDF_ID}/Q3 report.PDF"


def _row_counts(db_path: Path) -> tuple[int, int]:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            "SELECT (SELECT COUNT(*) FROM agent_actions),"
            " (SELECT COUNT(*) FROM agent_action_steps)"
        ).fetchone()


def test_submit_links_the_staged_file_and_consumes_it_after_commit(
    runtime: Path, tmp_path: Path
) -> None:
    staged = _stage(tmp_path, f"{PDF_ID}.pdf")

    result = submit_action_message(_command())

    linked = _attachment(runtime, result.action_id)
    assert linked.read_bytes() == PAYLOAD
    assert linked.stat().st_nlink == 1
    assert not staged.exists()
    with closing(sqlite3.connect(runtime)) as connection:
        (request_text,) = connection.execute(
            "SELECT user_request_text FROM agent_action_steps"
        ).fetchone()
    assert request_text.endswith(
        f"- Q3 report.PDF (PDF, 15 B): attachments/{PDF_ID}/Q3 report.PDF"
    )
    # A retry after a lost response replays the committed turn untouched.
    replay = submit_action_message(_command())
    assert (replay.action_id, replay.inserted) == (result.action_id, False)
    assert linked.read_bytes() == PAYLOAD


def test_an_office_attachment_starts_getting_libreoffice_ready_after_commit(
    runtime: Path, tmp_path: Path, office_runtime: OfficeRuntimeCalls
) -> None:
    _stage(tmp_path, f"{PDF_ID}.xlsx")

    submit_action_message(_command(name="Budget.xlsx"))

    # Off the request's thread, so a slow Spotlight never delays the submit.
    assert office_runtime.called.wait(timeout=5)
    assert office_runtime.calls == [(runtime.resolve().parent, "office-runtime-ensure")]


def test_a_pdf_attachment_leaves_libreoffice_alone(
    runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Path] = []
    monkeypatch.setattr(
        action_file_attachments, "_prepare_office_renderer", started.append
    )
    _stage(tmp_path, f"{PDF_ID}.pdf")

    submit_action_message(_command())

    assert started == []


def _replace_with_symlink(staged: Path) -> None:
    target = staged.with_name("elsewhere.pdf")
    staged.rename(target)
    staged.symlink_to(target)


@pytest.mark.parametrize(
    ("prepare", "byte_size"),
    [
        (Path.unlink, len(PAYLOAD)),
        (lambda _staged: None, len(PAYLOAD) + 1),
        (_replace_with_symlink, len(PAYLOAD)),
    ],
    ids=["missing", "size_mismatch", "symlink"],
)
def test_unusable_staged_file_rejects_the_turn_and_creates_nothing(
    runtime: Path,
    tmp_path: Path,
    prepare: Callable[[Path], None],
    byte_size: int,
) -> None:
    prepare(_stage(tmp_path, f"{PDF_ID}.pdf"))

    with pytest.raises(ActionFileAttachmentUnavailableError):
        submit_action_message(_command(byte_size=byte_size))

    assert _row_counts(runtime) == (0, 0)
    assert not (tmp_path / "local_runtime_workspaces").exists()


def test_a_turn_that_does_not_commit_removes_what_it_linked(
    runtime: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _stage(tmp_path, f"{PDF_ID}.pdf")

    def register_then_fail(
        *, connection: sqlite3.Connection, callback: Callable[[], None]
    ) -> None:
        register_after_commit(connection=connection, callback=callback)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(
        action_file_attachments, "register_after_commit", register_then_fail
    )

    with pytest.raises(sqlite3.OperationalError):
        submit_action_message(_command())

    assert _row_counts(runtime) == (0, 0)
    (scratch_user,) = (tmp_path / "local_runtime_workspaces/scratch/user-1").iterdir()
    assert list((scratch_user / "scratch/attachments").iterdir()) == []
    assert staged.read_bytes() == PAYLOAD


def test_deleting_the_conversation_removes_its_attachments(
    runtime: Path, tmp_path: Path
) -> None:
    _stage(tmp_path, f"{PDF_ID}.pdf")
    result = submit_action_message(_command())
    with closing(sqlite3.connect(runtime)) as connection, connection:
        connection.execute("UPDATE agent_actions SET status = 'success'")
        connection.execute("UPDATE processes SET status = 'completed'")
        connection.execute("UPDATE jobs SET status = 'completed'")
    linked = _attachment(runtime, result.action_id)
    assert linked.exists()

    delete_history_item(
        db_path=runtime,
        busy_timeout_ms=1_000,
        artifact_root=tmp_path / "artifacts",
        user_id=USER,
        kind="conversation",
        item_id=result.action_id,
    )

    assert not linked.exists()


@pytest.mark.asyncio
async def test_read_tools_open_an_attachment_at_its_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    write_sample_docx(tmp_path / "review.docx")
    write_pdf(tmp_path / "scan.pdf", marks=[(1, 0, 0), (0, 1, 0)])
    files = (
        FileAttachmentInput(
            attachment_id=DOCX_ID,
            name="review.docx",
            byte_size=(tmp_path / "review.docx").stat().st_size,
        ),
        FileAttachmentInput(
            attachment_id=PDF_ID,
            name="scan.pdf",
            byte_size=(tmp_path / "scan.pdf").stat().st_size,
        ),
    )
    _stage(tmp_path, f"{DOCX_ID}.docx", (tmp_path / "review.docx").read_bytes())
    _stage(tmp_path, f"{PDF_ID}.pdf", (tmp_path / "scan.pdf").read_bytes())
    with closing(sqlite3.connect(db_path)) as connection:
        with ActionFileAttachmentLinks() as links, immediate_transaction(connection):
            links.link(
                connection=connection,
                db_path=db_path,
                artifact_root=artifact_root,
                user_id=USER,
                action_id="action-1",
                files=files,
            )

    document = await execute_read_tool(
        db_path=db_path, context=context, args={"path": files[0].workspace_path}
    )
    assert document.status == "success"
    assert document.output["document_format"] == "docx"
    assert "# Quarterly review" in document.output["content"]

    asked = stub_renderer(
        monkeypatch,
        pages=(RenderedPage(number=2, width_px=10, height_px=10, payload=b"RIFF"),),
    )
    rendered = await render_pages(
        db_path=db_path,
        context=context,
        args={"path": files[1].workspace_path, "pages": [2]},
    )
    assert rendered.status == "success"
    assert asked["pdf_path"] == context.workspace_path / files[1].workspace_path
    parsed = RenderPdfPageOutput.model_validate(rendered.output).root
    assert parsed.kind == "pdf_pages"
    assert [page.page_number for page in parsed.attachments] == [2]

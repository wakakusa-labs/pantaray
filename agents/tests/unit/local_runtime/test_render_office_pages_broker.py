"""`render_pdf_page` on a Word, PowerPoint or Excel file.

The converter and the renderer have tests of their own that run them for real;
here both are stubbed at the broker's boundary. What is checked is what the
broker owns: when it waits on the LibreOffice install instead of failing, when
it reports the install as failed, that one file is converted once and drawn
from the PDF it keeps with the Action, and how a conversion's failures read.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.runtime.office_runtime import (
    OfficeRuntimeAbsent,
    OfficeRuntimePreparing,
    OfficeRuntimeReady,
    OfficeRuntimeSnapshot,
    OfficeRuntimeUnavailable,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.brokering import broker_direct_render_pdf
from pantaray_agents.local_runtime.tooling.brokering.broker import BrokerPolicyError
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderPdfPageOutput,
)
from pantaray_agents.local_runtime.tooling.documents import RenderedPage
from pantaray_agents.local_runtime.tooling.documents.office_convert import (
    OfficeConversionTimeoutError,
    OfficeDocumentUnreadableError,
    OfficeFormat,
)
from pantaray_agents.local_runtime.tooling.repository.tool_definitions import (
    load_tool_definition,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    validate_successful_tool_output,
)
from pantaray_agents.schema.action_conversation import RENDERER_PREPARING_OUTPUT_KIND

from .read_tool_broker_support import ReadRuntimeContext, bootstrap_read_runtime_db
from .test_read_document_broker import write_sample_pptx
from .test_render_pdf_page_broker import render_pages, stub_renderer

_USER_ID = "user-1"
_ACTION_ID = "action-1"
_WEBP = b"RIFF--WEBP--drawn-page"


class FakeOfficeRuntime:
    """The runtime's two calls, with ensure_available moving to a set state."""

    def __init__(
        self,
        state: OfficeRuntimeSnapshot,
        *,
        after_ensure: OfficeRuntimeSnapshot | None = None,
    ) -> None:
        self.state = state
        self.after_ensure = after_ensure or state
        self.ensured: list[Path] = []

    def snapshot(self) -> OfficeRuntimeSnapshot:
        return self.state

    def ensure_available(self, storage_base: Path) -> OfficeRuntimeSnapshot:
        self.ensured.append(storage_base)
        self.state = self.after_ensure
        return self.state


def use_runtime(
    monkeypatch: pytest.MonkeyPatch, runtime: FakeOfficeRuntime
) -> FakeOfficeRuntime:
    monkeypatch.setattr(broker_direct_render_pdf, "OFFICE_RUNTIME", runtime)
    return runtime


def stub_converter(
    monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def fake_convert_office_to_pdf(
        *,
        db_path: Path,
        libreoffice_app: Path,
        source: Path,
        document_format: OfficeFormat,
        destination: Path,
    ) -> None:
        calls.append(
            {
                "libreoffice_app": libreoffice_app,
                "source": source.read_bytes(),
                "document_format": document_format,
                "destination": destination,
            }
        )
        if error is not None:
            raise error
        destination.write_bytes(b"%PDF-1.7 converted")

    monkeypatch.setattr(
        broker_direct_render_pdf, "convert_office_to_pdf", fake_convert_office_to_pdf
    )
    return calls


def rendered_documents(db_path: Path) -> Path:
    return (
        resolve_action_storage_paths(
            db_path=db_path, user_id=_USER_ID, action_id=_ACTION_ID
        ).action_root
        / "rendered_documents"
    )


def setup_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ReadRuntimeContext]:
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    db_path, context = bootstrap_read_runtime_db(tmp_path)
    write_sample_pptx(context.workspace_path / "deck.pptx")
    return db_path, context


def ready_runtime(tmp_path: Path) -> FakeOfficeRuntime:
    bundle = tmp_path / "LibreOffice.app"
    bundle.mkdir()
    return FakeOfficeRuntime(OfficeRuntimeReady(bundle_path=bundle))


async def test_an_office_file_is_converted_once_and_drawn_from_the_kept_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    source = context.workspace_path / "deck.pptx"
    runtime = use_runtime(monkeypatch, ready_runtime(tmp_path))
    conversions = stub_converter(monkeypatch)
    asked = stub_renderer(
        monkeypatch,
        pages=(RenderedPage(number=2, width_px=10, height_px=10, payload=_WEBP),),
        page_count=2,
    )
    args = {"path": "deck.pptx", "pages": [2]}

    first = await render_pages(db_path=db_path, context=context, args=args)
    second = await render_pages(db_path=db_path, context=context, args=args)

    kept = rendered_documents(db_path) / (
        hashlib.sha256(source.read_bytes()).hexdigest() + ".pdf"
    )
    # Converted once, from a copy of exactly the file's bytes, with the app by
    # its resolved path; the second call draws the kept PDF without LibreOffice.
    assert len(conversions) == 1
    assert conversions[0]["libreoffice_app"] == (tmp_path / "LibreOffice.app")
    assert conversions[0]["document_format"] == "pptx"
    assert conversions[0]["source"] == source.read_bytes()
    assert conversions[0]["destination"] == kept
    assert asked["pdf_path"] == kept
    # Only the conversion asked for LibreOffice; the kept PDF did not.
    assert len(runtime.ensured) == 1
    # Only the kept PDF remains: the copy that was converted is gone.
    assert [path.name for path in rendered_documents(db_path).iterdir()] == [kept.name]
    for outcome in (first, second):
        parsed = RenderPdfPageOutput.model_validate(outcome.output).root
        assert parsed.kind == "pdf_pages"
        assert parsed.path == str(source)
        assert parsed.page_count == 2
        assert [page.page_number for page in parsed.attachments] == [2]

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("UPDATE agent_actions SET status = 'success'")
        connection.execute("UPDATE processes SET status = 'completed'")
        connection.execute("UPDATE jobs SET status = 'completed'")
        connection.execute("UPDATE execution_sessions SET status = 'completed'")
    delete_history_item(
        db_path=db_path,
        busy_timeout_ms=1_000,
        artifact_root=tmp_path / "artifacts",
        user_id=_USER_ID,
        kind="conversation",
        item_id=_ACTION_ID,
    )

    assert not kept.exists()


@pytest.mark.parametrize(
    "state",
    [OfficeRuntimePreparing(), OfficeRuntimeUnavailable(reason="download_failed")],
    ids=["preparing", "unavailable"],
)
async def test_a_kept_pdf_is_drawn_whatever_state_libreoffice_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: OfficeRuntimeSnapshot
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    source = context.workspace_path / "deck.pptx"
    kept = rendered_documents(db_path) / (
        hashlib.sha256(source.read_bytes()).hexdigest() + ".pdf"
    )
    kept.parent.mkdir()
    kept.write_bytes(b"%PDF-1.7 converted earlier")
    runtime = use_runtime(monkeypatch, FakeOfficeRuntime(state))
    conversions = stub_converter(monkeypatch)
    asked = stub_renderer(
        monkeypatch,
        pages=(RenderedPage(number=1, width_px=10, height_px=10, payload=_WEBP),),
    )

    outcome = await render_pages(
        db_path=db_path, context=context, args={"path": "deck.pptx", "pages": [1]}
    )

    assert outcome.output["kind"] == "pdf_pages"
    assert asked["pdf_path"] == kept
    # No discovery, no install attempt and no conversion for a kept PDF.
    assert runtime.ensured == [] and conversions == []


async def test_a_render_while_libreoffice_installs_says_it_is_preparing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    runtime = use_runtime(
        monkeypatch,
        FakeOfficeRuntime(OfficeRuntimeAbsent(), after_ensure=OfficeRuntimePreparing()),
    )
    conversions = stub_converter(monkeypatch)
    asked = stub_renderer(monkeypatch)

    outcome = await render_pages(
        db_path=db_path, context=context, args={"path": "deck.pptx", "pages": [1]}
    )

    # The render itself starts the install, and is a success without pages
    # that the model is told to continue past with read.
    assert runtime.ensured == [db_path.resolve().parent]
    assert outcome.status == "success"
    assert outcome.attachments == ()
    parsed = RenderPdfPageOutput.model_validate(outcome.output).root
    assert parsed.kind == RENDERER_PREPARING_OUTPUT_KIND
    assert "read" in parsed.message
    # The body passes the schema a completed step is stored against, which is
    # what the conversation row reads as preparing.
    validate_successful_tool_output(
        tool_id="render_pdf_page",
        output=outcome.output,
        output_schema=load_tool_definition(
            db_path=db_path, busy_timeout_ms=1_000, tool_id="render_pdf_page"
        ).output_schema_json,
    )
    assert conversions == [] and asked == {}


async def test_a_failed_install_is_reported_and_retried_for_the_next_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    runtime = use_runtime(
        monkeypatch,
        FakeOfficeRuntime(
            OfficeRuntimeUnavailable(reason="download_failed"),
            after_ensure=OfficeRuntimePreparing(),
        ),
    )
    stub_converter(monkeypatch)
    args = {"path": "deck.pptx", "pages": [1]}

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(db_path=db_path, context=context, args=args)

    assert caught.value.code == "RENDER_CONVERTER_UNAVAILABLE"
    assert "read" in (caught.value.fix_hint or "")
    # The failure is read before the retry it starts, which the next render
    # then finds preparing.
    assert len(runtime.ensured) == 1
    again = await render_pages(db_path=db_path, context=context, args=args)
    assert again.output["kind"] == RENDERER_PREPARING_OUTPUT_KIND


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OfficeDocumentUnreadableError("exit status 1"), "RENDER_DOCUMENT_UNREADABLE"),
        (OfficeConversionTimeoutError("did not finish"), "RENDER_TIMED_OUT"),
    ],
    ids=["unreadable", "timed-out"],
)
async def test_a_failed_conversion_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    use_runtime(monkeypatch, ready_runtime(tmp_path))
    stub_converter(monkeypatch, error=error)
    asked = stub_renderer(monkeypatch)

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(
            db_path=db_path, context=context, args={"path": "deck.pptx", "pages": [1]}
        )

    assert caught.value.code == code
    assert "read" in (caught.value.fix_hint or "")
    assert list(rendered_documents(db_path).iterdir()) == []
    assert asked == {}


async def test_an_office_file_outside_the_read_scope_is_refused_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = setup_workspace(tmp_path, monkeypatch)
    outside = tmp_path / "outside.pptx"
    write_sample_pptx(outside)
    runtime = use_runtime(monkeypatch, ready_runtime(tmp_path))
    conversions = stub_converter(monkeypatch)

    with pytest.raises(BrokerPolicyError) as caught:
        await render_pages(
            db_path=db_path, context=context, args={"path": str(outside), "pages": [1]}
        )

    assert caught.value.code in {"READ_PATH_DENIED", "READ_SCOPE_DENIED"}
    assert runtime.ensured == [] and conversions == []

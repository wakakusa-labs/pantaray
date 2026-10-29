"""The make stage writes into the Suggestion's artifact folder and nowhere else."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.html_render_relay import (
    HTML_RENDER_TEMP_DIRECTORY,
    HtmlRenderClaimed,
    html_render_broker,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_suggestion_artifacts_root,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    render_seatbelt_profile,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    LocalSuggestionResearchTools,
)
from pantaray_agents.local_runtime.tooling.suggestion_research.artifact_patch import (
    SuggestionArtifactPatch,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolRegistry,
    ToolCallEnvelope,
)

from .broker_test_support import RecordingDesktopSession, desktop_session
from .test_pdf_page_render import write_pdf
from .test_suggestion_commands import _EXITED, _capture_request, _command
from .test_suggestion_commands import allowed_db as allowed_db  # the fixture
from .test_suggestion_research_tools import BUSY_TIMEOUT_MS, _snapshot
from .test_workspace_settings_repository import TIMESTAMP


def _patch(change: dict[str, object]) -> ReactToolCall:
    args = {"changes": [change]}
    return ReactToolCall(
        tool_name="apply_patch",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope("apply_patch", None, args),  # type: ignore[arg-type]
    )


def _add(path: str) -> ReactToolCall:
    return _patch(
        {"op": "add", "path": path, "new_lines": ["hi"], "trailing_newline": True}
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "made").mkdir()
    return (tmp_path / "made").resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["notes/plan.md", "{folder}/notes/plan.md"])
async def test_a_file_is_written_by_a_path_inside_the_folder(
    folder: Path, path: str
) -> None:
    patch = SuggestionArtifactPatch(folder)

    result = await patch.run(_add(path.format(folder=folder)), 1)

    assert result.output["path"] == "notes/plan.md"
    assert (folder / "notes" / "plan.md").read_text() == "hi\n"


@pytest.mark.asyncio
async def test_writes_that_would_leave_the_folder_are_refused(
    folder: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.md").write_text("theirs\n")
    (folder / "link").symlink_to(outside, target_is_directory=True)
    (folder / "file-link.md").symlink_to(outside / "real.md")
    patch = SuggestionArtifactPatch(folder)
    edit = {"old_lines": ["theirs"], "new_lines": ["ours"]}

    results = [
        await patch.run(call, 1)
        for call in (
            _add(str(outside / "new.md")),
            _add("../outside/new.md"),
            _add(f"{folder}/../outside/new.md"),
            _add("link/new.md"),
            _patch({"op": "update", "path": "file-link.md", "edits": [edit]}),
        )
    ]

    assert [result.status for result in results] == ["error"] * len(results)
    assert [path.name for path in outside.iterdir()] == ["real.md"]
    assert (outside / "real.md").read_text() == "theirs\n"


@pytest.mark.asyncio
async def test_make_stage_commands_write_only_into_the_folder_offline(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user lets research commands reach the network; making never does.
    update_command_network_enabled(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        command_network_enabled=True,
        now=TIMESTAMP,
    )
    temp_dir = (tmp_path / "temp").resolve()
    requests = _capture_request(monkeypatch, temp_dir, _EXITED)
    tools = LocalSuggestionResearchTools(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=allowed_db),
        activity_start=None,
    )

    research = await tools.build_tool_definitions(user_id="user-1", run_id="sug-1")
    make = await tools.build_make_tools(user_id="user-1", run_id="sug-1")
    await ReactToolRegistry(make.definitions).execute(_command("pandoc -o a.docx"), 1)

    assert "apply_patch" not in {tool.name for tool in research}
    assert "apply_patch" in {tool.name for tool in make.definitions}
    (request,) = requests
    folder = str(make.folder)
    assert request.cwd == folder
    assert request.real_write_roots == [folder, str(temp_dir)]
    assert (request.network_policy, request.run_outside_sandbox) == ("deny", False)
    # The folder sits in private storage: only it and the temp dir are excepted.
    profile = render_seatbelt_profile(request)
    write_deny = profile.split("(deny file-write* (require-all", 1)[1]
    excepted = write_deny.split("(require-not (require-any\n", 1)[1].split("))))")[0]
    assert sorted(line.strip() for line in excepted.strip().splitlines()) == sorted(
        [f'(subpath "{temp_dir}")', f'(subpath "{folder}")']
    )


def _tools(db_path: Path, *, scope: str = "workspace") -> LocalSuggestionResearchTools:
    snapshot = replace(_snapshot(db_path=db_path), read_access_scope=scope)
    return LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=snapshot,
        activity_start=None,
    )


def _read(path: Path) -> ReactToolCall:
    args = {"path": str(path)}
    return ReactToolCall(
        tool_name="read",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope("read", None, args),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["workspace", "full_access"])
async def test_the_make_stage_reads_its_own_folder_and_no_other(
    allowed_db: Path, scope: str
) -> None:
    other = resolve_suggestion_artifacts_root(
        db_path=allowed_db, user_id="user-1", suggestion_id="sug-other"
    )
    other.mkdir(parents=True)
    (other / "theirs.md").write_text("another Suggestion's file\n")
    make = await _tools(allowed_db, scope=scope).build_make_tools(
        user_id="user-1", run_id="sug-1"
    )
    (make.folder / "draft.md").write_text("my draft\n")
    registry = ReactToolRegistry(make.definitions)

    own = await registry.execute(_read(make.folder / "draft.md"), 1)
    theirs = await registry.execute(_read(other.resolve() / "theirs.md"), 2)

    assert own.status == "success", own.output
    assert "my draft" in own.output["content"]
    assert theirs.status == "error"


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires Darwin with /usr/bin/sandbox-exec",
)
@pytest.mark.asyncio
async def test_a_command_cannot_swap_the_folder_for_a_way_outside(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    temp_dir = (tmp_path / "temp").resolve()
    temp_dir.mkdir()
    requests = _capture_request(monkeypatch, temp_dir, _EXITED)
    make = await _tools(allowed_db).build_make_tools(user_id="user-1", run_id="sug-1")
    registry = ReactToolRegistry(make.definitions)
    folder = make.folder
    swap = f"rm -rf '{folder}' || mv '{folder}' '{temp_dir}/away'; ln -s '{outside}' '{folder}'"
    await registry.execute(_command(swap), 1)

    # Run under the profile that command really gets: its children are
    # writable, the folder itself can be neither removed nor renamed.
    profile = render_seatbelt_profile(requests[0])
    subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            "/bin/bash",
            "-c",
            f"{swap}; echo x > '{folder}/made.md'",
        ],
        capture_output=True,
        check=False,
    )
    assert folder.is_dir() and not folder.is_symlink()
    assert (folder / "made.md").is_file()
    # Swapped by any other means, the folder no longer runs a command at all.
    shutil.rmtree(folder)
    folder.symlink_to(outside, target_is_directory=True)
    refused = await registry.execute(_command("echo x > escaped.md"), 2)

    assert refused.output["error_code"] == "ARTIFACT_FOLDER_REPLACED"
    assert len(requests) == 1
    assert list(outside.iterdir()) == []


def _python(code: str) -> ReactToolCall:
    args = {"code": code, "cwd": None}
    return ReactToolCall(
        tool_name="run_python",
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope("run_python", None, args),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("commands_allowed", [True, False])
async def test_run_python_is_offered_to_the_make_stage_only_with_bash(
    allowed_db: Path, commands_allowed: bool
) -> None:
    snapshot = replace(_snapshot(db_path=allowed_db), commands_allowed=commands_allowed)
    tools = LocalSuggestionResearchTools(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=snapshot,
        activity_start=None,
    )

    research = await tools.build_tool_definitions(user_id="user-1", run_id="sug-1")
    make = await tools.build_make_tools(user_id="user-1", run_id="sug-1")

    made = {tool.name for tool in make.definitions}
    assert {"bash", "run_python"} & made == (
        {"bash", "run_python"} if commands_allowed else set()
    )
    assert "run_python" not in {tool.name for tool in research}
    # Neither asks anything, so both are made-stage tools whatever the setting.
    assert {"render_html_page", "save_html_as_pdf"} <= made


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires Darwin with /usr/bin/sandbox-exec",
)
@pytest.mark.asyncio
async def test_run_python_writes_only_into_the_folder_offline(
    allowed_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user lets research commands reach the network; run_python never does.
    update_command_network_enabled(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        command_network_enabled=True,
        now=TIMESTAMP,
    )
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    temp_dir = (tmp_path / "temp").resolve()
    temp_dir.mkdir()
    requests = _capture_request(monkeypatch, temp_dir, _EXITED)
    make = await _tools(allowed_db).build_make_tools(user_id="user-1", run_id="sug-1")
    code = f"""
import pathlib, socket
pathlib.Path("made.txt").write_text("ok")
for name, attempt in (
    ("outside", lambda: pathlib.Path({str(outside)!r}, "escaped.txt").write_text("x")),
    ("network", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)),
):
    try:
        attempt()
        print(name, "allowed")
    except OSError:
        print(name, "denied")
"""
    await ReactToolRegistry(make.definitions).execute(_python(code), 1)

    (request,) = requests
    assert request.network_policy == "deny"
    assert request.real_write_roots == [str(make.folder), str(temp_dir)]
    assert request.cwd == str(make.folder)
    # Run under the profile and argv the call really gets.
    ran = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            render_seatbelt_profile(request),
            *request.argv,
        ],
        cwd=request.cwd,
        env=request.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.stdout.split("\n")[:2] == ["outside denied", "network denied"], (
        ran.stderr
    )
    assert (make.folder / "made.txt").read_text() == "ok"
    assert list(outside.iterdir()) == []


def _call(name: str, args: dict[str, object]) -> ReactToolCall:
    return ReactToolCall(
        tool_name=name,
        tool_args=args,  # type: ignore[arg-type]
        tool_call_envelope=ToolCallEnvelope(name, None, args),  # type: ignore[arg-type]
        call_id="c1",
    )


async def _electron(desktop: RecordingDesktopSession, artifact_root: Path) -> None:
    """Answer the one render request as Electron main would."""

    while not desktop.requests:
        await asyncio.sleep(0.005)
    render_request_id = str(desktop.requests[0]["render_request_id"])
    pdf_path = artifact_root / HTML_RENDER_TEMP_DIRECTORY / f"{render_request_id}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    write_pdf(pdf_path, marks=[(0, 0, 0)] * 2)
    pdf = pdf_path.read_bytes()
    claimed = HtmlRenderClaimed(len(pdf), hashlib.sha256(pdf).hexdigest(), ())
    assert (
        html_render_broker().answer(
            request_id=render_request_id, user_id="user-1", outcome=claimed
        )
        == "accepted"
    )


@pytest.fixture
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "relay").resolve()
    root.mkdir()
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(root))
    return root


@pytest.mark.asyncio
async def test_html_is_printed_offline_and_saved_into_the_folder(
    allowed_db: Path, artifact_root: Path
) -> None:
    update_command_network_enabled(
        db_path=allowed_db,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        command_network_enabled=True,
        now=TIMESTAMP,
    )
    make = await _tools(allowed_db).build_make_tools(user_id="user-1", run_id="sug-1")
    (make.folder / "deck.html").write_text("<h1>Deck</h1>")
    registry = ReactToolRegistry(make.definitions)
    page = str(make.folder / "deck.html")

    with desktop_session() as desktop:
        saved, _ = await asyncio.gather(
            registry.execute(
                _call("save_html_as_pdf", {"path": page, "pdf_path": "deck.pdf"}), 1
            ),
            _electron(desktop, artifact_root),
        )
    with desktop_session() as desktop:
        rendered, _ = await asyncio.gather(
            registry.execute(
                _call("render_html_page", {"path": page, "pages": None}), 2
            ),
            _electron(desktop, artifact_root),
        )
        [asked] = desktop.requests

    assert saved.status == "success", saved.output
    assert (make.folder / "deck.pdf").read_bytes().startswith(b"%PDF-")
    assert rendered.status == "success", rendered.output
    assert rendered.output["page_count"] == 2
    assert len(rendered.images) == 2
    # Never the network, whatever the user's command setting says.
    assert (asked["kind"], asked["allow_network"]) == ("suggestion", False)


@pytest.mark.asyncio
async def test_a_pdf_is_never_saved_outside_the_folder(
    allowed_db: Path, tmp_path: Path, artifact_root: Path
) -> None:
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    make = await _tools(allowed_db).build_make_tools(user_id="user-1", run_id="sug-1")
    (make.folder / "deck.html").write_text("<h1>Deck</h1>")
    (make.folder / "link").symlink_to(outside, target_is_directory=True)
    registry = ReactToolRegistry(make.definitions)
    page = str(make.folder / "deck.html")

    with desktop_session() as desktop:
        refused = [
            await registry.execute(
                _call("save_html_as_pdf", {"path": page, "pdf_path": pdf_path}), 1
            )
            for pdf_path in (str(outside / "a.pdf"), "../outside/a.pdf")
        ]
        # A link inside the folder is refused when the PDF is written.
        linked, _ = await asyncio.gather(
            registry.execute(
                _call("save_html_as_pdf", {"path": page, "pdf_path": "link/a.pdf"}),
                2,
            ),
            _electron(desktop, artifact_root),
        )

    assert [result.status for result in (*refused, linked)] == ["error"] * 3
    assert len(desktop.requests) == 1  # Paths outside are refused before printing.
    assert list(outside.iterdir()) == []

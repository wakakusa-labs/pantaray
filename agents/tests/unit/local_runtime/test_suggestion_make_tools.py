"""The make stage writes into the Suggestion's artifact folder and nowhere else."""

from __future__ import annotations

from pathlib import Path

import pytest

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

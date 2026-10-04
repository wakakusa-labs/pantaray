"""grep and glob read only the read scope, whatever the tree does while they run.

ripgrep walks and reopens directories by name, so these run the real backend
under the real seatbelt profile: only the kernel's check on what it opens can
bind what it reads.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker_discovery_ripgrep
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_discovery_ripgrep import (
    RIPGREP_TRUSTED_PATH,
    RipgrepRunResult,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _bootstrap_runtime_db_with_registered_folder,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin"
    or not Path("/usr/bin/sandbox-exec").is_file()
    or shutil.which("rg", path=RIPGREP_TRUSTED_PATH) is None,
    reason="requires Darwin with /usr/bin/sandbox-exec and a trusted ripgrep",
)

SENTINEL_LINE = "needle from outside the read scope"
SENTINEL_NAME = "outside-sentinel.txt"


def _args(tool_id: str, base_path: str) -> dict[str, JSONValue]:
    if tool_id == "glob":
        return {"base_path": base_path, "pattern": "**/*.txt", "limit": 50}
    return {
        "base_path": base_path,
        "pattern": "needle",
        "include_glob": "**/*.txt",
        "max_matches": 50,
    }


async def _visible_text(
    *, db_path: Path, context: object, tool_id: str, base_path: str
) -> str:
    """Everything the model would see from one call, result or refusal."""

    try:
        outcome = await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id=tool_id,
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,  # type: ignore[attr-defined]
            execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
            args=_args(tool_id, base_path),
        )
    except BrokerPolicyError as error:
        return f"{error}\n{error.fix_hint}"
    return f"{json.dumps(outcome.output)}\n{outcome.search_text}"


def _seed_outside(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SENTINEL_NAME).write_text(f"{SENTINEL_LINE}\n", encoding="utf-8")
    return directory


def _swap_for_link_while_ripgrep_runs(
    monkeypatch: pytest.MonkeyPatch, *, directory: Path, target: Path
) -> None:
    """Point ``directory`` at ``target`` for the backend run, then restore a look-alike.

    Any process may do this to a folder it can write: the path is a real
    directory when the call is validated and again when results are mapped
    back, with the same file names in it, and a link only while ripgrep reads.
    """

    run = broker_discovery_ripgrep._run_ripgrep_lines

    def swapped(**kwargs: object) -> RipgrepRunResult:
        shutil.rmtree(directory)
        directory.symlink_to(target, target_is_directory=True)
        try:
            return run(**kwargs)  # type: ignore[arg-type]
        finally:
            directory.unlink()
            directory.mkdir()
            for entry in target.iterdir():
                if entry.is_file():
                    (directory / entry.name).write_text("harmless\n", encoding="utf-8")

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", swapped)


@pytest.mark.parametrize("tool_id", ["grep", "glob"])
@pytest.mark.asyncio
async def test_outward_link_in_the_workspace_is_not_searched(
    tmp_path: Path, tool_id: str
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    (repo / "inside.txt").write_text("needle inside\n", encoding="utf-8")
    (repo / "linked").symlink_to(_seed_outside(tmp_path / "outside"))

    visible = await _visible_text(
        db_path=db_path, context=context, tool_id=tool_id, base_path=str(repo)
    )

    assert str(repo / "inside.txt") in visible
    assert SENTINEL_LINE not in visible
    assert SENTINEL_NAME not in visible


@pytest.mark.asyncio
async def test_inherited_ripgrep_config_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    (repo / "inside.txt").write_text("needle inside\n", encoding="utf-8")
    config = repo / "ripgreprc"
    config.write_text("--invert-match\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))

    visible = await _visible_text(
        db_path=db_path, context=context, tool_id="grep", base_path=str(repo)
    )

    assert "needle inside" in visible


@pytest.mark.parametrize("tool_id", ["grep", "glob"])
@pytest.mark.asyncio
async def test_folder_swapped_for_outward_link_reads_nothing_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    searched = repo / "searched"
    searched.mkdir()
    _swap_for_link_while_ripgrep_runs(
        monkeypatch,
        directory=searched,
        target=_seed_outside(tmp_path / "outside"),
    )

    visible = await _visible_text(
        db_path=db_path, context=context, tool_id=tool_id, base_path=str(searched)
    )

    assert SENTINEL_LINE not in visible
    assert SENTINEL_NAME not in visible


@pytest.mark.parametrize("tool_id", ["grep", "glob"])
@pytest.mark.asyncio
async def test_folder_swapped_for_link_into_app_storage_reads_nothing_with_full_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path, read_access_scope="full_access")
    searched = tmp_path / "elsewhere" / "searched"
    searched.mkdir(parents=True)
    _swap_for_link_while_ripgrep_runs(
        monkeypatch,
        directory=searched,
        target=_seed_outside(db_path.parent),
    )

    visible = await _visible_text(
        db_path=db_path, context=context, tool_id=tool_id, base_path=str(searched)
    )

    assert SENTINEL_LINE not in visible
    assert SENTINEL_NAME not in visible

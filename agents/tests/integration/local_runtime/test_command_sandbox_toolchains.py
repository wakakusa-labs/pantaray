from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


@pytest.mark.asyncio
@pytest.mark.parametrize("toolchain", ["python", "node", "rustc", "go"])
async def test_real_toolchain_builds_inside_command_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toolchain: str
) -> None:
    if toolchain != "python" and shutil.which(toolchain) is None:
        pytest.skip(f"{toolchain} is not installed")
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    workspace = testbed.context.workspace_path
    if toolchain == "python":
        command = (
            shlex.quote(str(Path(sys.executable).resolve()))
            + " -c 'import json, pathlib, os; "
            'assert os.environ.get("PANTARAY_SYNTHETIC_SECRET") is None; '
            'pathlib.Path("result.txt").write_text(json.dumps([42]))\''
        )
        expected = "[42]"
    elif toolchain == "node":
        (workspace / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {"build": "node build.js"},
                }
            )
        )
        (workspace / "build.js").write_text(
            'require("fs").writeFileSync("result.txt", "node ok")'
        )
        command, expected = "npm run build", "node ok"
    elif toolchain == "rustc":
        (workspace / "main.rs").write_text('fn main(){println!("rust ok");}\n')
        command, expected = (
            "rustc main.rs -o program && ./program > result.txt",
            "rust ok\n",
        )
    else:
        (workspace / "main.go").write_text(
            'package main\nimport "fmt"\nfunc main(){fmt.Println("go ok")}\n'
        )
        command, expected = (
            "go build -o program main.go && ./program > result.txt",
            "go ok\n",
        )
    monkeypatch.setenv("PANTARAY_SYNTHETIC_SECRET", "never-inherit-this")
    outcome = await execute_bash(testbed=testbed, command=command)
    assert outcome.status == "success", outcome.output
    assert (workspace / "result.txt").read_text() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_child_file_denial_preserves_recovered_or_failed_exit(
    tmp_path: Path,
    outside_temp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    app_data = tmp_path / "app"
    app_data.mkdir()
    user_home = outside_temp_path / "home"
    credentials = user_home / ".cargo/credentials.toml"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("synthetic-private-token")
    runtime = user_home / ".cargo/bin"
    runtime.mkdir()
    runtime_file = runtime / "runtime.txt"
    runtime_file.write_text("installed runtime")
    alias = runtime / "credentials-link"
    alias.symlink_to(credentials)
    monkeypatch.setenv("HOME", str(user_home))
    testbed = bootstrap_runtime_testbed(tmp_path=app_data, monkeypatch=monkeypatch)
    command = (
        f"cat {shlex.quote(str(runtime_file))}; "
        f"cat {shlex.quote(str(credentials))} {shlex.quote(str(alias))}; "
        f"printf changed > {shlex.quote(str(runtime_file))}; exit {exit_code}"
    )
    outcome = await execute_bash(testbed=testbed, command=command)
    assert outcome.status == ("success" if exit_code == 0 else "error"), outcome.output
    assert outcome.output["stdout"] == "installed runtime"
    assert "synthetic-private-token" not in str(outcome.output)
    if exit_code == 0:
        assert outcome.output["exit_code"] == 0
        assert "Operation not permitted" in outcome.output["stderr"]
    assert credentials.read_text() == "synthetic-private-token"
    assert runtime_file.read_text() == "installed runtime"

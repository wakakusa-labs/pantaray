"""Commands cannot read or write files that hold secrets, at the kernel.

The profile is rendered here, with the home folder moved to a temp dir, and run
under the real sandbox-exec: a rule seatbelt cannot parse, or one that matches
nothing, fails these, as the Python check's tests cannot show.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    compose_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    render_seatbelt_profile,
)
from pantaray_agents.tools.files import secret_paths

from .support import (
    SEATBELT_SKIP_REASON,
    bootstrap_runtime_testbed,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)

SECRET = "s3cr3t-value"


def _run_sandboxed(
    *, home: Path, cwd: Path, script: str, use_login_environment: bool
) -> str:
    temp_dir = home.parent / "command-temp"
    temp_dir.mkdir(exist_ok=True)
    request = compose_sandbox_request(
        temp_dir=temp_dir,
        real_read_roots=[str(home)],
        real_write_roots=[str(cwd)],
        private_storage_roots=[str(home.parent / "app-data")],
        action_storage=None,
        app_runtime_python=Path(sys.executable),
        cwd=str(cwd),
        argv=["/bin/bash", "--noprofile", "--norc", "-c", script],
        env={"PATH": "/usr/bin:/bin"},
        timeout_ms=30_000,
        stdout_max_bytes=65_536,
        stderr_max_bytes=65_536,
        temp_storage_limit_bytes=1_048_576,
        network_policy="deny",
        use_login_environment=use_login_environment,
        run_outside_sandbox=False,
    )
    profile = temp_dir / "profile.sb"
    profile.write_text(render_seatbelt_profile(request), encoding="utf-8")
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), *request.argv],
        cwd=cwd,
        env=request.env,
        capture_output=True,
        text=True,
        check=False,
    )
    # sandbox-exec exits 65 without running anything for a profile it rejects.
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _probe_script(probes: dict[str, str]) -> str:
    return "\n".join(
        f"if ({probe}) >/dev/null 2>&1; then echo {name}=yes; else echo {name}=no; fi"
        for name, probe in probes.items()
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path.resolve() / "home"
    for relative in (
        ".aws/credentials",
        ".aws/config",
        ".ssh/config",
        ".ssh/id_ed25519_x",
        # security, and gh and git through it, read this file themselves.
        "Library/Keychains/login.keychain-db",
    ):
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text(f"{SECRET}\n", encoding="utf-8")
    monkeypatch.setattr(secret_paths, "_home", lambda: home)
    return home


@pytest.mark.parametrize("use_login_environment", [False, True])
def test_secret_files_are_withheld_and_everything_around_them_works(
    home: Path, use_login_environment: bool
) -> None:
    project = home / "project"
    project.mkdir()
    (project / ".env").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
    (project / ".ENV.local").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")
    (project / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    (project / "app.py").write_text("print()\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "app.py"]):
        subprocess.run(["/usr/bin/git", *args], cwd=project, check=True)
    aws = shlex.quote(str(home / ".aws"))
    ssh = shlex.quote(str(home / ".ssh"))
    keychains = shlex.quote(str(home / "Library" / "Keychains"))
    probes = {
        "env": "cat .env",
        "upper": "cat .ENV.local",
        "example": "cat .env.example",
        "listed": "ls -a | grep -qx .env",
        "stat": "test -s .env",
        # The login environment's real ~/.gitconfig is outside this test's read root.
        "git": "GIT_CONFIG_GLOBAL=/dev/null git status --porcelain | grep -q '?? .env'",
        "move": "mv .env moved",
        "link": "ln .env linked && cat linked",
        "create": "mkdir new && touch new/.env",
        "venv": "mkdir -p tool/.env/bin && echo ok > tool/.env/bin/activate"
        " && cat tool/.env/bin/activate",
        "aws": f"cat {aws}/credentials",
        "aws-config": f"cat {aws}/config",
        "ssh-config": f"cat {ssh}/config",
        "ssh-key": f"cat {ssh}/id_ed25519_x",
        "keychain": f"cat {keychains}/login.keychain-db",
    }

    stdout = _run_sandboxed(
        home=home,
        cwd=project,
        script=_probe_script(probes),
        use_login_environment=use_login_environment,
    )

    credentials = "yes" if use_login_environment else "no"
    assert stdout.splitlines() == [
        "env=no",
        "upper=no",
        "example=yes",
        "listed=yes",
        "stat=yes",
        "git=yes",
        "move=no",
        "link=no",
        "create=no",
        "venv=yes",
        f"aws={credentials}",
        "aws-config=yes",
        "ssh-config=yes",
        f"ssh-key={credentials}",
        f"keychain={credentials}",
    ]
    assert (project / ".env").read_text(encoding="utf-8") == f"TOKEN={SECRET}\n"
    assert not (project / "new" / ".env").exists()


@pytest.mark.asyncio
async def test_a_denied_secret_file_gets_the_withheld_hint_not_write_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    workspace = Path(testbed.context.workspace_path)
    (workspace / ".env").write_text(f"TOKEN={SECRET}\n", encoding="utf-8")

    outcome = await execute_bash(testbed=testbed, command="cat .env")

    assert outcome.status == "error"
    assert SECRET not in outcome.output["stdout"]
    feedback = outcome.output["error"]["llm_feedback"]
    assert "withheld" in feedback
    assert "additional_write_folders" not in feedback

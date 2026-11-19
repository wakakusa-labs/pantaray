"""Commands cannot drive the user's own browser through its debugging port.

The profile is rendered with the home folder moved to a temp dir holding a
stand-in Chrome profile, and run under the real sandbox-exec, so a rule
seatbelt cannot parse, or one that matches nothing, fails here.
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

from .support import SEATBELT_SKIP_REASON, seatbelt_available, start_tcp_probe_server

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)

LOCAL_STATE = '{"devtools":{}}\n'


@pytest.mark.parametrize("use_login_environment", [False, True])
def test_commands_cannot_reach_the_browser_port_or_its_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_login_environment: bool
) -> None:
    home = tmp_path.resolve() / "home"
    support = home / "Library" / "Application Support"
    chrome = support / "Google" / "Chrome"
    brave = support / "BraveSoftware" / "Brave-Browser"
    (chrome / "Default").mkdir(parents=True)
    brave.mkdir(parents=True)
    (chrome / "Default" / "Preferences").write_text("{}\n", encoding="utf-8")
    (chrome / "Local State").write_text(LOCAL_STATE, encoding="utf-8")
    (brave / "Local State").write_text(LOCAL_STATE, encoding="utf-8")
    monkeypatch.setattr(secret_paths, "_home", lambda: home)
    ordinary = start_tcp_probe_server()
    debugging = start_tcp_probe_server()
    (chrome / "DevToolsActivePort").write_text(
        f"{debugging.port}\n/devtools/browser/0b1c\n", encoding="ascii"
    )
    quoted_chrome = shlex.quote(str(chrome))
    probes = {
        "ordinary": f"printf connected > /dev/tcp/127.0.0.1/{ordinary.port}",
        "debugging": f"printf connected > /dev/tcp/127.0.0.1/{debugging.port}",
        "debugging-by-name": f"printf connected > /dev/tcp/localhost/{debugging.port}",
        "port-file": f"cat {quoted_chrome}/DevToolsActivePort",
        "local-state": f"cat {quoted_chrome}/'Local State'",
        "append": f"echo x >> {quoted_chrome}/'Local State'",
        "move": f"mv {quoted_chrome}/'Local State' {quoted_chrome}/moved",
        "brave": f"cat {shlex.quote(str(brave))}/'Local State'",
        "preferences": f"cat {quoted_chrome}/Default/Preferences",
        "create": f"touch {quoted_chrome}/Default/new",
        "rename-profile": f"mv {quoted_chrome} {quoted_chrome}-moved",
        "rename-parent": f"mv {shlex.quote(str(chrome.parent))}"
        f" {shlex.quote(str(support / 'Google-moved'))}",
    }
    try:
        stdout = _run_sandboxed(
            home=home,
            script="\n".join(
                f"if ({probe}) >/dev/null 2>&1; then echo {name}=yes;"
                f" else echo {name}=no; fi"
                for name, probe in probes.items()
            ),
            use_login_environment=use_login_environment,
        )
    finally:
        ordinary.close()
        debugging.close()

    assert stdout.splitlines() == [
        "ordinary=yes",
        "debugging=no",
        "debugging-by-name=no",
        "port-file=no",
        "local-state=no",
        "append=no",
        "move=no",
        "brave=no",
        "preferences=yes",
        "create=yes",
        "rename-profile=no",
        "rename-parent=no",
    ]
    assert ordinary.hits == [b"connected"]
    assert debugging.hits == []
    assert (chrome / "Local State").read_text(encoding="utf-8") == LOCAL_STATE


def _run_sandboxed(*, home: Path, script: str, use_login_environment: bool) -> str:
    temp_dir = home.parent / "command-temp"
    temp_dir.mkdir()
    request = compose_sandbox_request(
        temp_dir=temp_dir,
        real_read_roots=[str(home)],
        # Writable, so only the browser-file rule can stop the writes probed.
        real_write_roots=[str(home)],
        private_storage_roots=[str(home.parent / "app-data")],
        action_storage=None,
        app_runtime_python=Path(sys.executable),
        cwd=str(home),
        argv=["/bin/bash", "--noprofile", "--norc", "-c", script],
        env={"PATH": "/usr/bin:/bin"},
        timeout_ms=30_000,
        stdout_max_bytes=65_536,
        stderr_max_bytes=65_536,
        temp_storage_limit_bytes=1_048_576,
        network_policy="allow",
        use_login_environment=use_login_environment,
        run_outside_sandbox=False,
    )
    profile = temp_dir / "profile.sb"
    profile.write_text(render_seatbelt_profile(request), encoding="utf-8")
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), *request.argv],
        cwd=home,
        env=request.env,
        capture_output=True,
        text=True,
        check=False,
    )
    # sandbox-exec exits 65 without running anything for a profile it rejects.
    assert completed.returncode == 0, completed.stderr
    return completed.stdout

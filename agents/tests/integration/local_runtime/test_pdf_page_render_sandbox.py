"""What the page renderer's sandbox refuses, on the platform that has one.

The unit tests draw pages through the same profile whenever they run on macOS,
so what is left to establish is the other half: that a process which has been
handed a file the user did not write cannot read anything else, cannot reach
the network, and cannot write at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.documents.page_render_sandbox import (
    sandboxed_argv,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires Darwin with /usr/bin/sandbox-exec",
)

# Each attempt is reported rather than raised, so one denial cannot hide the
# next and a profile that denied everything including the PDF is visible too.
_PROBE = r"""
import json, socket, sys

def attempt(action):
    try:
        action()
    except OSError as error:
        return type(error).__name__
    return "allowed"

allowed, forbidden = sys.argv[1], sys.argv[2]
print(json.dumps({
    "named_file": attempt(lambda: open(allowed, "rb").read()),
    "other_file": attempt(lambda: open(forbidden, "rb").read()),
    "listing": attempt(lambda: __import__("os").listdir(str(__import__("pathlib").Path(forbidden).parent))),
    "network": attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=5)),
    "write": attempt(lambda: open(allowed + ".written", "wb").write(b"x")),
}))
"""


def run_probe(
    allowed: Path, forbidden: Path, *, allow_network: bool = False
) -> dict[str, str]:
    argv = sandboxed_argv(
        (sys.executable, "-I", "-B", "-c", _PROBE, str(allowed), str(forbidden)),
        read_files=(allowed,),
        python_executable=Path(sys.executable),
        allow_network=allow_network,
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report: dict[str, str] = json.loads(done.stdout)
    return report


def test_the_sandbox_allows_the_named_file_and_nothing_else_around_it(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "report.pdf"
    allowed.write_bytes(b"%PDF-1.7\n")
    forbidden = tmp_path / "salary.txt"
    forbidden.write_text("private\n", encoding="utf-8")

    report = run_probe(allowed, forbidden)

    assert report["named_file"] == "allowed"
    assert report["other_file"] == "PermissionError"
    assert report["listing"] == "PermissionError"
    assert report["network"] == "PermissionError"
    assert report["write"] == "PermissionError"


def test_the_web_page_worker_sandbox_opens_the_network_and_nothing_else(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "page_worker.py"
    allowed.write_text("", encoding="utf-8")
    forbidden = tmp_path / "salary.txt"
    forbidden.write_text("private\n", encoding="utf-8")

    report = run_probe(allowed, forbidden, allow_network=True)

    assert report["network"] == "allowed"
    assert report["named_file"] == "allowed"
    assert report["other_file"] == "PermissionError"
    assert report["listing"] == "PermissionError"
    assert report["write"] == "PermissionError"

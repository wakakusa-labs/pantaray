from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox import browser_debugging_ports
from pantaray_agents.local_runtime.tooling.sandbox.browser_debugging_ports import (
    browsers_with_debugging_on,
    protected_browser_ports,
)


def test_each_readable_port_file_counts_and_a_bad_one_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_debugging_ports, "_home", lambda: tmp_path)
    support = tmp_path / "Library" / "Application Support"
    # Brave's file first and with the lower port: neither may decide the order.
    contents: dict[str, bytes] = {
        "BraveSoftware/Brave-Browser": b"50001",
        "Google/Chrome": b"53110\n/devtools/browser/0b1c\n",
        "Microsoft Edge": b"not a port\n",
        "Google/Chrome Beta": b"70000\n",
        "Google/Chrome Canary": b"\xff\xfe\n",
    }
    for user_data_dir, content in contents.items():
        (support / user_data_dir).mkdir(parents=True)
        (support / user_data_dir / "DevToolsActivePort").write_bytes(content)
    # macOS withholding the file from this process reads as a permission error.
    unreadable = support / "Google/Chrome Dev" / "DevToolsActivePort"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("50505\n", encoding="ascii")
    unreadable.chmod(0)
    try:
        assert protected_browser_ports() == [9222, 50001, 53110]
        assert browsers_with_debugging_on() == ["chrome", "brave"]
    finally:
        unreadable.chmod(0o600)

"""Which of the user's browsers have remote debugging on, read from each one's
DevToolsActivePort: the ports every command is denied, and the browsers
Pantaray may connect to.

Once the user turns remote debugging on in a Chrome-family browser, a
connection to its port makes the browser ask the user to allow control of
their signed-in browser, without naming who asks. Only Pantaray's app may
make that connection, so every sandboxed command's profile denies these ports.
A missing, unreadable or malformed file counts as debugging off.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from pantaray_agents.schema.user_browser import KNOWN_USER_BROWSERS, UserBrowserId

# The port a Chrome-family browser's debugging is conventionally started on,
# denied even where no file names it: a browser run with another
# --user-data-dir, or files macOS keeps from this process.
DEFAULT_DEBUGGING_PORT = 9222
_MAX_PORT = 65_535


def protected_browser_ports() -> list[int]:
    """The port in each known browser's DevToolsActivePort, and 9222.

    Read by the host as each command starts, so a missing, unreadable or
    malformed file is skipped rather than stopping the command.
    """

    # Design limit: the ports are read once, as the profile is written, so a
    # long-running command keeps reaching a port the browser moves to after it
    # started (a browser restart). And where macOS keeps the files from this
    # process (Files & Folders), only 9222 is denied. Read the ports again for
    # a running command if a browser is seen moving its port mid-command.
    return sorted({DEFAULT_DEBUGGING_PORT, *_debugging_ports().values()})


def browsers_with_debugging_on() -> list[UserBrowserId]:
    """The known browsers whose DevToolsActivePort names a port, in table order."""

    return list(_debugging_ports())


def _debugging_ports() -> dict[UserBrowserId, int]:
    ports: dict[UserBrowserId, int] = {}
    support = _home() / "Library" / "Application Support"
    for browser_id, browser in KNOWN_USER_BROWSERS.items():
        try:
            text = (support / browser.user_data_dir / "DevToolsActivePort").read_text(
                encoding="ascii"
            )
        except (OSError, UnicodeDecodeError):
            continue
        # Line 1 is the port, line 2 the browser target's path.
        first_line = text.partition("\n")[0].strip()
        if first_line.isdigit() and 0 < int(first_line) <= _MAX_PORT:
            ports[browser_id] = int(first_line)
    return ports


def _home() -> Path:
    # The OS account's home, as the secret path rules use, not $HOME.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)

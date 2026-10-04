"""macOS system locations a sandboxed process may need beyond its workspace."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Final

# _CS_DARWIN_USER_TEMP_DIR in <unistd.h>; os.confstr_names does not list it.
_CS_DARWIN_USER_TEMP_DIR: Final = 65537


def app_python_runtime_root(executable: Path) -> Path:
    executable = executable.resolve()
    for parent in executable.parents:
        if parent.suffix == ".framework":
            return parent
    # The bundled standalone Python owns bin/ and lib/ under the same prefix.
    return (
        executable.parent.parent
        if executable.parent.name == "bin"
        else executable.parent
    )


def toolchain_read_roots() -> list[str]:
    home = Path.home()
    rustup = Path(os.environ.get("RUSTUP_HOME", str(home / ".rustup")))
    roots = [
        Path("/Library/Developer/CommandLineTools"),
        Path("/Applications/Xcode.app"),
        Path("/Library/Preferences/com.apple.dt.Xcode.plist"),
        Path("/Library/Preferences/com.apple.dt.CommandLineTools.plist"),
        Path("/private/var/db/xcode_select_link"),
        home / ".nvm/versions/node",
        home / ".cargo/bin",
        rustup,
        # actions/setup-node installs Node and npm here on macOS runners.
        # The runtime (including npm's JS files) must be readable in Seatbelt.
        Path("/Users/runner/hostedtoolcache/node"),
    ]
    # Homebrew's etc/ and user package credentials are not runtime installation data.
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        roots.extend(
            prefix / name
            for name in (
                "Cellar",
                "opt",
                "bin",
                "sbin",
                "lib",
                "share",
                "include",
                "Frameworks",
            )
        )
    return list(dict.fromkeys(str(path.resolve()) for path in roots if path.exists()))


def user_temp_dir() -> str:
    """The per-user temporary directory, by the name seatbelt will match.

    macOS gives each user a private (0700) temporary directory; it is asked for
    directly rather than read from TMPDIR, which the helper's launcher may have
    pointed anywhere. Resolved because ``/var`` is a link to ``/private/var``.
    """

    # Keep both paths type-checked on Linux CI; mypy folds a direct sys.platform guard.
    on_macos = sys.platform == "darwin"
    if on_macos:
        user_temp_dir = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
        if user_temp_dir is None:
            raise OSError("macOS reported no per-user temporary directory")
        return os.path.realpath(user_temp_dir)
    # Only the Linux unit tests come here; the app ships on macOS alone.
    return os.path.realpath(tempfile.gettempdir())

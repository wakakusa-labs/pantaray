"""Confine the page renderer to its interpreter and the one PDF it draws.

The renderer runs PDFium over a file the user did not write, so on macOS -- the
platform Pantaray ships on -- it runs under a seatbelt profile that denies the
network, denies every write, and allows reading only what the interpreter needs
to start and the single PDF that was asked for. The profile is written here
rather than reusing the one behind the bash and run_python tools: that one is
built around an approved command, its workspace and its audit trail, none of
which a page render has. What is shared is the list of system roots any process
on this machine reads to run at all.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final

from ..sandbox.seatbelt_profiles import MACOS_SYSTEM_RUNTIME_READ_ROOTS

_SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"
# The two directories the platform font mapper enumerates for a font a PDF
# names but does not embed. /System/Library/Fonts is already covered by the
# system roots; installed fonts live here and are not user data.
_FONT_READ_ROOTS: Final = ("/Library/Fonts",)
# macOS refuses to resolve a path through more links than this (MAXSYMLINKS).
_MAX_SYMLINK_HOPS: Final = 32
# Outbound connections and the resolver, as the command sandbox grants them
# when command networking is on.
_NETWORK_RULES: Final = (
    "(system-network)",
    '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
    '(allow network-outbound (remote tcp "*:*") (remote udp "*:*"))',
)


def sandboxed_argv(
    argv: Sequence[str],
    *,
    read_files: Sequence[Path],
    python_executable: Path,
    allow_network: bool = False,
) -> tuple[str, ...]:
    """``argv``, wrapped in whatever confinement this platform offers.

    Only macOS has sandbox-exec, and it is the only platform the app ships on.
    The unit tests also run on Linux, where this returns the command unchanged
    and the renderer's isolation is the separate process on its own; that is
    stated here rather than branched around at each call site.
    """

    # Keep both paths type-checked on Linux CI; mypy folds a direct sys.platform guard.
    on_macos = sys.platform == "darwin"
    if not on_macos:
        return tuple(argv)
    profile = seatbelt_profile(
        read_files=read_files,
        python_executable=python_executable,
        allow_network=allow_network,
    )
    return (_SANDBOX_EXEC, "-p", profile, *argv)


def seatbelt_profile(
    *,
    read_files: Sequence[Path],
    python_executable: Path,
    allow_network: bool = False,
) -> str:
    """Read the interpreter's installation and ``read_files``; nothing else, ever.

    ``read_files`` is every file named by the command itself -- the script that
    is run and the PDF it draws -- rather than a directory it may search.
    ``allow_network`` opens outbound connections and name resolution for the
    web page worker, which fetches the page itself; it still writes nothing and
    reads nothing of the user's.
    """

    read_roots = (
        *MACOS_SYSTEM_RUNTIME_READ_ROOTS,
        *_FONT_READ_ROOTS,
        *_interpreter_read_roots(python_executable),
    )
    named = tuple(str(path) for path in read_files)
    return "\n".join(
        (
            "(version 1)",
            '(import "system.sb")',
            "(allow process-exec process-fork)",
            "(allow file-map-executable)",
            "(deny file-read*)",
            _rule(
                "allow file-read*",
                (
                    *(f'(subpath "{_quote(root)}")' for root in read_roots),
                    *(f'(literal "{_quote(path)}")' for path in named),
                    '(literal "/dev/null")',
                    '(literal "/dev/random")',
                    '(literal "/dev/urandom")',
                ),
            ),
            # Opening anything means walking the directories above it, and exec
            # walks them before the interpreter has started, so every ancestor
            # of a readable path has to be stat-able even though its siblings
            # stay invisible.
            _rule(
                "allow file-read-metadata",
                (
                    f'(path-ancestors "{_quote(path)}")'
                    for path in (*read_roots, *named)
                ),
            ),
            "(deny file-write*)",
            "(deny network*)",
            *(_NETWORK_RULES if allow_network else ()),
            "",
        )
    )


def _interpreter_read_roots(python_executable: Path) -> tuple[str, ...]:
    """The directories the worker reads to start and to import pypdfium2 and Pillow.

    The worker runs the interpreter the helper itself runs on, so the helper's
    own installation is the answer. In the app that is one self-contained tree.
    A development virtualenv is spread over two -- its own site-packages and
    the separately installed CPython its bin/python links into -- and seatbelt
    matches the names the kernel walks rather than where they resolve to, so
    the link's own spelling of the interpreter's root is listed beside the
    resolved one. The helper's own ``sys.path`` is deliberately not listed: the
    worker starts isolated (``-I``) and imports nothing of the helper's, and a
    path entry can be any directory the helper was started from.
    """

    roots = [
        _installation_root(python_executable),
        *_linked_installation_root(python_executable),
        Path(sys.prefix),
        Path(sys.base_prefix),
    ]
    return tuple(dict.fromkeys(str(root) for root in roots if root.is_dir()))


def _installation_root(executable: Path) -> Path:
    """The tree that owns ``bin/`` and ``lib/`` for one interpreter.

    Deliberately not the sandbox package's own version of this: that one
    resolves symlinks first, and a resolved path is exactly what the kernel
    does not walk.
    """

    return (
        executable.parent.parent
        if executable.parent.name == "bin"
        else executable.parent
    )


def _linked_installation_root(executable: Path) -> Iterator[Path]:
    """The installation behind every name the kernel walks to reach the binary.

    A virtualenv's interpreter is a chain of links, not one: ``python3`` to
    ``python``, that to the base interpreter, and -- for a Homebrew Python --
    that on into the Cellar. exec follows each hop by name, so each hop's own
    spelling of its root has to be readable, and so does where it finally lands.
    """

    hop = executable
    # A link chain the filesystem itself resolves is at most this long.
    for _ in range(_MAX_SYMLINK_HOPS):
        if not hop.is_symlink():
            break
        hop = hop.parent / hop.readlink()
        yield _installation_root(hop)
        # A directory above the hop can itself be a link (Homebrew's
        # ``opt/python@3.12`` points into the Cellar), and the kernel then walks
        # the resolved spelling of that hop as well.
        yield _installation_root(hop.parent.resolve() / hop.name)
    yield _installation_root(executable.resolve())


def _rule(operation: str, clauses: Iterable[str]) -> str:
    body = "\n".join(dict.fromkeys(f"    {clause}" for clause in clauses))
    return f"({operation}\n{body}\n)"


def _quote(value: str) -> str:
    """Escape a path for a seatbelt string literal, as the profile renderer does."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["sandboxed_argv", "seatbelt_profile"]

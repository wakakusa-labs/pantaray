"""Confine LibreOffice to its own bundle and the one conversion it was started for.

LibreOffice lays out a Word, PowerPoint or Excel file the user did not write, so
on macOS it runs under a seatbelt profile that allows executing only the
LibreOffice bundle, reading only the system, the bundle and the conversion's
work directory, writing only that work directory, and no network beyond the
socket LibreOffice listens on for its own single-instance pipe.

Every rule below was established by converting all three formats with exactly
this profile. A read or lookup LibreOffice is refused at startup makes it abort,
and macOS then shows the user a crash dialog, so a change here needs the same
verification -- a successful conversion of each format -- rather than a probe.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from .page_render_sandbox import _SANDBOX_EXEC, _quote, _rule

# Not the shared MACOS_SYSTEM_RUNTIME_READ_ROOTS: LibreOffice reads more of
# /private/etc and /private/var/db than an interpreter does, and this is the set
# the conversions were verified with. Fonts installed per user
# (~/Library/Fonts) stay unreadable, so a document naming one gets a substitute.
_SYSTEM_READ_ROOTS: Final = (
    "/System",
    "/usr/lib",
    "/usr/share",
    "/private/var/db",
    "/Library/Fonts",
    "/bin",
    "/usr/bin",
    "/private/etc",
)
# LibreOffice's single-instance pipe is a unix socket it creates directly in
# /tmp, whatever TMPDIR says; creating it needs write on the directory entry.
_OSL_PIPE_REGEX: Final = '#"^/private/tmp/OSL_PIPE_"'
_OSL_PIPE_DIRECTORY: Final = "/private/tmp"
# The platform services every process uses, plus the ones LibreOffice needs for
# fonts, preferences and startup. Two are less obvious and both are required:
# com.apple.windowserver.active, which the toolkit checks in with even when
# headless, and com.apple.coreservices.launchservicesd, which registers the
# process as an app at startup. Asking LaunchServices to open anything is still
# refused by (deny lsopen), so a link or object in a document cannot launch
# another app or a URL; that refusal was verified with `open -b`.
_MACH_SERVICES: Final = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.analyticsd",
    "com.apple.analyticsd.messagetracer",
    "com.apple.appsleep",
    "com.apple.bsd.dirhelper",
    "com.apple.diagnosticd",
    "com.apple.dt.automationmode.reader",
    "com.apple.espd",
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.secinitd",
    "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.system.logger",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.membership",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.xpc.activity.unmanaged",
    "com.apple.PowerManagement.control",
    "com.apple.fonts",
    "com.apple.FontObjectsServer",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
    "com.apple.CoreServices.coreservicesd",
    "com.apple.tccd.system",
    "com.apple.DiskArbitration.diskarbitrationd",
    "com.apple.distributed_notifications@Uv3",
    "com.apple.SystemConfiguration.configd",
    "com.apple.windowserver.active",
    "com.apple.coreservices.launchservicesd",
)


def sandboxed_argv(
    argv: Sequence[str], *, libreoffice_app: Path, work_dir: Path
) -> tuple[str, ...]:
    """``argv``, wrapped in whatever confinement this platform offers.

    Only macOS has sandbox-exec, and it is the only platform the app ships on.
    The unit tests also run on Linux, where this returns the command unchanged.
    """

    # Keep both paths type-checked on Linux CI; mypy folds a direct sys.platform guard.
    on_macos = sys.platform == "darwin"
    if not on_macos:
        return tuple(argv)
    profile = seatbelt_profile(libreoffice_app=libreoffice_app, work_dir=work_dir)
    return (_SANDBOX_EXEC, "-p", profile, *argv)


def seatbelt_profile(*, libreoffice_app: Path, work_dir: Path) -> str:
    """Run ``libreoffice_app`` over ``work_dir`` and touch nothing else of the user's.

    Both paths are resolved real paths: seatbelt matches the names the kernel
    walks, so a path reached through a link (``/var`` on macOS) would admit
    nothing. LibreOffice lists every directory above its working directory at
    startup, so each ancestor of ``work_dir`` is readable as a directory entry
    while its siblings stay invisible.
    """

    app = _quote(str(libreoffice_app))
    work = _quote(str(work_dir))
    return "\n".join(
        (
            "(version 1)",
            '(import "system.sb")',
            "(allow process-fork)",
            f'(allow process-exec (subpath "{app}"))',
            "(allow file-map-executable)",
            _rule(
                "allow mach-lookup",
                (f'(global-name "{_quote(name)}")' for name in _MACH_SERVICES),
            ),
            "(allow user-preference-read)",
            "(deny lsopen)",
            "(allow ipc-posix-shm*)",
            "(allow sysctl-read)",
            "(allow signal (target self))",
            # Listening on its own pipe only: no outbound unix socket, so it
            # cannot reach a LibreOffice the user is running or any other
            # service's socket, and no IP at all.
            "(deny network*)",
            f"(allow network-bind (local unix-socket (path-regex {_OSL_PIPE_REGEX})))",
            f"(allow network-inbound (local unix-socket (path-regex {_OSL_PIPE_REGEX})))",
            "(deny file-read*)",
            _rule(
                "allow file-read*",
                (
                    *(f'(subpath "{root}")' for root in _SYSTEM_READ_ROOTS),
                    f'(subpath "{app}")',
                    f'(subpath "{work}")',
                    f"(regex {_OSL_PIPE_REGEX})",
                    f'(literal "{_OSL_PIPE_DIRECTORY}")',
                    '(literal "/dev/null")',
                    '(literal "/dev/random")',
                    '(literal "/dev/urandom")',
                    '(literal "/dev/autofs_nowait")',
                    *(
                        f'(literal "{_quote(str(ancestor))}")'
                        for ancestor in work_dir.parents
                    ),
                ),
            ),
            "(allow file-read-metadata)",
            "(deny file-write*)",
            _rule(
                "allow file-write*",
                (
                    f'(subpath "{work}")',
                    '(literal "/dev/null")',
                    f"(regex {_OSL_PIPE_REGEX})",
                    f'(literal "{_OSL_PIPE_DIRECTORY}")',
                ),
            ),
            "",
        )
    )


__all__ = ["sandboxed_argv", "seatbelt_profile"]

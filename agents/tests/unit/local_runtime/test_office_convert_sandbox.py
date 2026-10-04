"""The seatbelt profile LibreOffice converts under, rule for rule.

Every rule was verified by converting all three formats, and a rule LibreOffice
is refused makes it abort with a crash dialog on the user's screen, so the
profile is pinned whole: a change has to be made here on purpose, after the
conversions were verified again.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.documents.office_convert_sandbox import (
    seatbelt_profile,
)

_APP = Path("/Applications/LibreOffice.app")
_WORK = Path("/private/var/folders/ab/cd/T/pantaray-office-x1")

_EXPECTED_PROFILE = """\
(version 1)
(import "system.sb")
(allow process-fork)
(allow process-exec (subpath "/Applications/LibreOffice.app"))
(allow file-map-executable)
(allow mach-lookup
    (global-name "com.apple.system.opendirectoryd.libinfo")
    (global-name "com.apple.analyticsd")
    (global-name "com.apple.analyticsd.messagetracer")
    (global-name "com.apple.appsleep")
    (global-name "com.apple.bsd.dirhelper")
    (global-name "com.apple.diagnosticd")
    (global-name "com.apple.dt.automationmode.reader")
    (global-name "com.apple.espd")
    (global-name "com.apple.logd")
    (global-name "com.apple.logd.events")
    (global-name "com.apple.secinitd")
    (global-name "com.apple.system.DirectoryService.libinfo_v1")
    (global-name "com.apple.system.logger")
    (global-name "com.apple.system.notification_center")
    (global-name "com.apple.system.opendirectoryd.membership")
    (global-name "com.apple.trustd")
    (global-name "com.apple.trustd.agent")
    (global-name "com.apple.xpc.activity.unmanaged")
    (global-name "com.apple.PowerManagement.control")
    (global-name "com.apple.fonts")
    (global-name "com.apple.FontObjectsServer")
    (global-name "com.apple.cfprefsd.daemon")
    (global-name "com.apple.cfprefsd.agent")
    (global-name "com.apple.CoreServices.coreservicesd")
    (global-name "com.apple.tccd.system")
    (global-name "com.apple.DiskArbitration.diskarbitrationd")
    (global-name "com.apple.distributed_notifications@Uv3")
    (global-name "com.apple.SystemConfiguration.configd")
    (global-name "com.apple.windowserver.active")
    (global-name "com.apple.coreservices.launchservicesd")
)
(allow user-preference-read)
(deny lsopen)
(allow ipc-posix-shm*)
(allow sysctl-read)
(allow signal (target self))
(deny network*)
(allow network-bind (local unix-socket (path-regex #"^/private/tmp/OSL_PIPE_")))
(allow network-inbound (local unix-socket (path-regex #"^/private/tmp/OSL_PIPE_")))
(deny file-read*)
(allow file-read*
    (subpath "/System")
    (subpath "/usr/lib")
    (subpath "/usr/share")
    (subpath "/private/var/db")
    (subpath "/Library/Fonts")
    (subpath "/bin")
    (subpath "/usr/bin")
    (subpath "/private/etc")
    (subpath "/Applications/LibreOffice.app")
    (subpath "/private/var/folders/ab/cd/T/pantaray-office-x1")
    (regex #"^/private/tmp/OSL_PIPE_")
    (literal "/private/tmp")
    (literal "/dev/null")
    (literal "/dev/random")
    (literal "/dev/urandom")
    (literal "/dev/autofs_nowait")
    (literal "/private/var/folders/ab/cd/T")
    (literal "/private/var/folders/ab/cd")
    (literal "/private/var/folders/ab")
    (literal "/private/var/folders")
    (literal "/private/var")
    (literal "/private")
    (literal "/")
)
(allow file-read-metadata)
(deny file-write*)
(allow file-write*
    (subpath "/private/var/folders/ab/cd/T/pantaray-office-x1")
    (literal "/dev/null")
    (regex #"^/private/tmp/OSL_PIPE_")
    (literal "/private/tmp")
)
"""


def test_the_profile_is_the_verified_one_for_this_bundle_and_work_dir() -> None:
    """Exec only the bundle, the named services, no lsopen, no outbound socket,
    writes only in the work dir, and every ancestor of the work dir listable."""

    assert seatbelt_profile(libreoffice_app=_APP, work_dir=_WORK) == _EXPECTED_PROFILE


def test_a_path_is_quoted_rather_than_able_to_end_its_string() -> None:
    profile = seatbelt_profile(
        libreoffice_app=Path('/Applications/Libre"Office.app'), work_dir=_WORK
    )

    assert '(allow process-exec (subpath "/Applications/Libre\\"Office.app"))' in (
        profile
    )


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file(),
    reason="requires Darwin with /usr/bin/sandbox-exec",
)
def test_sandbox_exec_accepts_the_profile_and_refuses_any_other_binary(
    tmp_path: Path,
) -> None:
    """The profile compiles, and nothing outside the bundle can be started.

    This is the one part of the profile a test can exercise without LibreOffice
    itself: a refused exec is reported by sandbox-exec, where a profile it could
    not parse would be reported as a syntax error instead.
    """

    work_dir = tmp_path.resolve() / "work"
    work_dir.mkdir()
    profile = seatbelt_profile(
        libreoffice_app=tmp_path.resolve() / "LibreOffice.app", work_dir=work_dir
    )

    done = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode != 0
    assert "execvp() of '/usr/bin/true' failed: Operation not permitted" in (
        done.stderr
    )

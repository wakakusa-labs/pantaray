"""Find a usable LibreOffice, or install the pinned one in the background.

Office pages are drawn by converting the document with LibreOffice. The user's
own copy is used when it is recent enough; otherwise the official TDF build is
downloaded once into local-runtime storage without asking. The state lives in
this helper process only and is reported without progress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import plistlib
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

import httpx2

logger = logging.getLogger(__name__)

LIBREOFFICE_BUNDLE_ID: Final = "org.libreoffice.script"
LIBREOFFICE_APP_NAME: Final = "LibreOffice.app"
# 26.2 is the release the converter and its sandbox profile were verified
# against; older branches also carry document-parser CVEs fixed only later.
MINIMUM_LIBREOFFICE_VERSION: Final = (26, 2)
OFFICE_RENDERER_DIRNAME: Final = "office-renderer.noindex"
INSTALLED_MARKER_NAME: Final = "installed.json"
DEFAULT_APP_ROOTS: Final = (Path("/Applications"), Path.home() / "Applications")

# The installed app measured 794 MiB; the DMG stays on disk until the copy ends.
_INSTALLED_APP_BYTES: Final = 800 * 1024 * 1024
_FREE_SPACE_MARGIN_BYTES: Final = 512 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES: Final = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS: Final = 30.0
# Discovery runs on the render path; a stuck Spotlight must not block it.
_SPOTLIGHT_TIMEOUT_SECONDS: Final = 5.0
# Generous bounds so a wedged disk-image tool cannot keep the install preparing
# forever; a normal attach takes seconds and the ~800 MiB copy under a minute.
_ATTACH_TIMEOUT_SECONDS: Final = 120.0
_COPY_TIMEOUT_SECONDS: Final = 600.0
_DETACH_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class OfficeRelease:
    version: str
    url: str
    size_bytes: int
    sha256: str


# SHA-256 is the value TDF publishes beside the DMG (<url>.sha256).
PINNED_RELEASE: Final = OfficeRelease(
    version="26.2.2.2",
    url=(
        "https://downloadarchive.documentfoundation.org/libreoffice/old/26.2.2.2/"
        "mac/aarch64/LibreOffice_26.2.2.2_MacOS_aarch64.dmg"
    ),
    size_bytes=294_721_394,
    sha256="2a603303b0a7a17c2f6dd381d2039ca79fecc73cb7c25a13ebea15cf0919c751",
)

UnavailableReason = Literal[
    "insufficient_disk_space", "download_failed", "checksum_mismatch", "install_failed"
]


@dataclass(frozen=True, slots=True)
class OfficeRuntimeAbsent:
    kind: Literal["absent"] = "absent"


@dataclass(frozen=True, slots=True)
class OfficeRuntimePreparing:
    kind: Literal["preparing"] = "preparing"


@dataclass(frozen=True, slots=True)
class OfficeRuntimeReady:
    bundle_path: Path
    kind: Literal["ready"] = "ready"


@dataclass(frozen=True, slots=True)
class OfficeRuntimeUnavailable:
    reason: UnavailableReason
    kind: Literal["unavailable"] = "unavailable"


OfficeRuntimeSnapshot = (
    OfficeRuntimeAbsent
    | OfficeRuntimePreparing
    | OfficeRuntimeReady
    | OfficeRuntimeUnavailable
)


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]: ...


def _run_command(
    args: Sequence[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=False, timeout=timeout)


class _InstallFailure(Exception):
    def __init__(self, reason: UnavailableReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: UnavailableReason = reason


def _parse_bundle_version(value: object) -> tuple[int, int] | None:
    """Return (major, minor) of a CFBundleShortVersionString, or None."""

    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return None
    return int(parts[0]), int(parts[1])


def is_supported_bundle(bundle_path: Path) -> bool:
    try:
        with (bundle_path / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    if (
        not isinstance(info, dict)
        or info.get("CFBundleIdentifier") != LIBREOFFICE_BUNDLE_ID
    ):
        return False
    version = _parse_bundle_version(info.get("CFBundleShortVersionString"))
    return version is not None and version >= MINIMUM_LIBREOFFICE_VERSION


class OfficeRuntime:
    """Owns discovery and the single background install for this process."""

    def __init__(
        self,
        *,
        release: OfficeRelease = PINNED_RELEASE,
        app_roots: Sequence[Path] = DEFAULT_APP_ROOTS,
        run_command: CommandRunner = _run_command,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._release = release
        self._app_roots = tuple(app_roots)
        self._run_command = run_command
        self._transport = transport
        self._lock = threading.Lock()
        self._state: OfficeRuntimeSnapshot = OfficeRuntimeAbsent()

    def snapshot(self) -> OfficeRuntimeSnapshot:
        with self._lock:
            return self._state

    def ensure_available(self, storage_base: Path) -> OfficeRuntimeSnapshot:
        """Return ready when LibreOffice is usable; otherwise start one install.

        Returns immediately. Concurrent callers share the running install, and
        a call after a failed install starts a fresh attempt.
        """

        bundle = self._discover(storage_base)
        with self._lock:
            if isinstance(self._state, OfficeRuntimePreparing):
                return self._state
            # An install may have finished after discovery ran; starting another
            # would delete the copy it just completed.
            bundle = bundle or self._own_copy(storage_base)
            if bundle is not None:
                self._state = OfficeRuntimeReady(bundle_path=bundle)
                return self._state
            self._state = OfficeRuntimePreparing()
            threading.Thread(
                target=self._install_in_background,
                args=(storage_base,),
                name="office-runtime-install",
                daemon=True,
            ).start()
            return self._state

    def _version_dir(self, storage_base: Path) -> Path:
        return storage_base / OFFICE_RENDERER_DIRNAME / self._release.version

    def _own_copy(self, storage_base: Path) -> Path | None:
        version_dir = self._version_dir(storage_base)
        bundle = version_dir / LIBREOFFICE_APP_NAME
        if (version_dir / INSTALLED_MARKER_NAME).is_file() and is_supported_bundle(
            bundle
        ):
            return bundle
        return None

    def _discover(self, storage_base: Path) -> Path | None:
        # Our own copy is checked before Spotlight: mdfind is a subprocess, and
        # the .noindex directory keeps our copy out of its results anyway.
        for root in self._app_roots:
            if is_supported_bundle(root / LIBREOFFICE_APP_NAME):
                return root / LIBREOFFICE_APP_NAME
        own = self._own_copy(storage_base)
        if own is not None:
            return own
        for candidate in self._spotlight_candidates():
            if is_supported_bundle(candidate):
                return candidate
        return None

    def _spotlight_candidates(self) -> list[Path]:
        try:
            result = self._run_command(
                ["mdfind", f"kMDItemCFBundleIdentifier == '{LIBREOFFICE_BUNDLE_ID}'"],
                timeout=_SPOTLIGHT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "mdfind timed out after %s s; skipping Spotlight",
                _SPOTLIGHT_TIMEOUT_SECONDS,
            )
            return []
        # A non-zero exit means Spotlight is off or unavailable: no candidates.
        if result.returncode != 0:
            return []
        return [Path(line) for line in result.stdout.decode().splitlines() if line]

    def _install_in_background(self, storage_base: Path) -> None:
        # Stays unavailable if an unexpected error escapes, so the state never
        # remains preparing; the error itself reaches threading.excepthook.
        outcome: OfficeRuntimeSnapshot = OfficeRuntimeUnavailable(
            reason="install_failed"
        )
        try:
            outcome = OfficeRuntimeReady(bundle_path=self._install(storage_base))
        except _InstallFailure as error:
            logger.warning("LibreOffice install failed: %s", error)
            outcome = OfficeRuntimeUnavailable(reason=error.reason)
        except OSError as error:
            logger.warning("LibreOffice install failed: %s", error)
        finally:
            with self._lock:
                self._state = outcome

    def _install(self, storage_base: Path) -> Path:
        root = storage_base / OFFICE_RENDERER_DIRNAME
        version_dir = self._version_dir(storage_base)
        dmg_path = root / f"{self._release.version}.dmg.partial"
        root.mkdir(parents=True, exist_ok=True)
        # A previous attempt that died mid-way leaves no marker: start clean.
        shutil.rmtree(version_dir, ignore_errors=True)
        dmg_path.unlink(missing_ok=True)
        required = (
            self._release.size_bytes + _INSTALLED_APP_BYTES + _FREE_SPACE_MARGIN_BYTES
        )
        free = shutil.disk_usage(root).free
        if free < required:
            raise _InstallFailure(
                "insufficient_disk_space", f"{free} bytes free, {required} required"
            )
        completed = False
        try:
            self._download(dmg_path)
            version_dir.mkdir(parents=True)
            bundle = version_dir / LIBREOFFICE_APP_NAME
            self._copy_app_from_dmg(dmg_path, bundle)
            marker = version_dir / f"{INSTALLED_MARKER_NAME}.partial"
            marker.write_text(
                json.dumps(
                    {"version": self._release.version, "sha256": self._release.sha256}
                )
            )
            os.replace(marker, version_dir / INSTALLED_MARKER_NAME)
            completed = True
            return bundle
        finally:
            dmg_path.unlink(missing_ok=True)
            if not completed:
                shutil.rmtree(version_dir, ignore_errors=True)

    def _download(self, destination: Path) -> None:
        digest = hashlib.sha256()
        written = 0
        try:
            with (
                httpx2.Client(
                    transport=self._transport,
                    timeout=httpx2.Timeout(_DOWNLOAD_TIMEOUT_SECONDS),
                ) as client,
                client.stream("GET", self._release.url) as response,
                destination.open("wb") as handle,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > self._release.size_bytes:
                        raise _InstallFailure(
                            "checksum_mismatch", "larger than pinned size"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except httpx2.HTTPError as error:
            raise _InstallFailure("download_failed", str(error)) from error
        if (
            written != self._release.size_bytes
            or digest.hexdigest() != self._release.sha256
        ):
            raise _InstallFailure(
                "checksum_mismatch", f"{written} bytes, {digest.hexdigest()}"
            )

    def _copy_app_from_dmg(self, dmg_path: Path, bundle: Path) -> None:
        # Beside the DMG in private app storage: commands may write the OS temp dirs.
        mount_parent = Path(
            tempfile.mkdtemp(prefix="pantaray-office-mount-", dir=dmg_path.parent)
        )
        try:
            attach_output = self._run_install_step(
                [
                    "hdiutil",
                    "attach",
                    "-nobrowse",
                    "-readonly",
                    "-noautoopen",
                    "-mountrandom",
                    str(mount_parent),
                    "-plist",
                    str(dmg_path),
                ],
                timeout=_ATTACH_TIMEOUT_SECONDS,
            )
            partial = bundle.with_name(f"{bundle.name}.partial")
            self._run_install_step(
                [
                    "ditto",
                    str(_mount_point(attach_output) / LIBREOFFICE_APP_NAME),
                    str(partial),
                ],
                timeout=_COPY_TIMEOUT_SECONDS,
            )
            partial.rename(bundle)
        finally:
            # Detach whatever is mounted here, including an image that mounted
            # before a timed-out attach was killed.
            detached = [self._detach(path) for path in mount_parent.iterdir()]
            if all(detached):
                shutil.rmtree(mount_parent, ignore_errors=True)

    def _run_install_step(self, args: list[str], *, timeout: float) -> bytes:
        try:
            result = self._run_command(args, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise _InstallFailure(
                "install_failed", f"{args[:2]} timed out after {timeout} s"
            ) from error
        if result.returncode != 0:
            raise _InstallFailure("install_failed", f"{args[:2]}: {result.stderr!r}")
        return result.stdout

    def _detach(self, mount_point: Path) -> bool:
        args = ["hdiutil", "detach", str(mount_point)]
        try:
            result = self._run_command(args, timeout=_DETACH_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("hdiutil detach %s timed out", mount_point)
            return False
        if result.returncode != 0:
            logger.warning("hdiutil detach %s failed: %r", mount_point, result.stderr)
            return False
        return True


def _mount_point(attach_plist: bytes) -> Path:
    for entity in plistlib.loads(attach_plist)["system-entities"]:
        if "mount-point" in entity:
            return Path(entity["mount-point"])
    raise _InstallFailure("install_failed", "hdiutil reported no mount point")


OFFICE_RUNTIME: Final = OfficeRuntime()

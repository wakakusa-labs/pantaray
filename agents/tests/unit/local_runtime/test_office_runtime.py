from __future__ import annotations

import hashlib
import plistlib
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx2
import pytest

from pantaray_agents.local_runtime.runtime.office_runtime import (
    INSTALLED_MARKER_NAME,
    LIBREOFFICE_BUNDLE_ID,
    OFFICE_RENDERER_DIRNAME,
    CommandRunner,
    OfficeRelease,
    OfficeRuntime,
    OfficeRuntimePreparing,
    OfficeRuntimeReady,
    OfficeRuntimeSnapshot,
    OfficeRuntimeUnavailable,
    is_supported_bundle,
)

DMG_BYTES = b"fake libreoffice disk image"
RELEASE = OfficeRelease(
    version="26.2.2.2",
    url="https://downloads.example/LibreOffice.dmg",
    size_bytes=len(DMG_BYTES),
    sha256=hashlib.sha256(DMG_BYTES).hexdigest(),
)


def _make_app(
    path: Path, *, version: str = "26.2.2.2", bundle_id: str = LIBREOFFICE_BUNDLE_ID
) -> Path:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}
        )
    )
    return path


class FakeCommands:
    """Plays hdiutil, ditto and mdfind against a fake mounted volume."""

    def __init__(
        self,
        *,
        ditto_exit: int = 0,
        spotlight: Sequence[Path] = (),
        times_out: Literal["mdfind", "attach", "ditto"] | None = None,
        on_spotlight: Callable[[], None] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.detached: list[Path] = []
        self._ditto_exit = ditto_exit
        self._spotlight = spotlight
        self._times_out = times_out
        self._on_spotlight = on_spotlight

    def __call__(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        argv = list(args)
        self.calls.append(argv)
        assert argv[0] == "mdfind" or timeout is not None, f"{argv[:2]} has no timeout"
        step = "attach" if argv[:2] == ["hdiutil", "attach"] else argv[0]
        stdout = b""
        exit_code = 0
        if argv[0] == "mdfind":
            assert timeout is not None, "mdfind must run with a timeout"
            if self._on_spotlight is not None:
                self._on_spotlight()
            stdout = "".join(f"{path}\n" for path in self._spotlight).encode()
        elif step == "attach":
            volume = Path(argv[argv.index("-mountrandom") + 1]) / "volume"
            _make_app(volume / "LibreOffice.app")
            stdout = plistlib.dumps({"system-entities": [{"mount-point": str(volume)}]})
        elif argv[:2] == ["hdiutil", "detach"]:
            self.detached.append(Path(argv[2]))
            shutil.rmtree(argv[2])
        elif argv[0] == "ditto":
            exit_code = self._ditto_exit
            if exit_code == 0 or self._times_out == "ditto":
                shutil.copytree(argv[1], argv[2])
        if step == self._times_out:
            raise subprocess.TimeoutExpired(argv, timeout or 0)
        return subprocess.CompletedProcess(argv, exit_code, stdout, b"")

    def ran(self, *prefix: str) -> bool:
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


def _serving(
    body: bytes = DMG_BYTES, requests: list[httpx2.Request] | None = None
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        return httpx2.Response(200, content=body)

    return httpx2.MockTransport(handler)


def _runtime(
    tmp_path: Path,
    *,
    transport: httpx2.BaseTransport,
    commands: CommandRunner | None = None,
    app_roots: Sequence[Path] = (),
) -> OfficeRuntime:
    return OfficeRuntime(
        release=RELEASE,
        app_roots=app_roots or (tmp_path / "Applications",),
        run_command=commands or FakeCommands(),
        transport=transport,
    )


def _settle(runtime: OfficeRuntime) -> OfficeRuntimeSnapshot:
    deadline = time.monotonic() + 10
    while isinstance(runtime.snapshot(), OfficeRuntimePreparing):
        assert time.monotonic() < deadline, "install never finished"
        time.sleep(0.01)
    return runtime.snapshot()


def _version_dir(storage: Path) -> Path:
    return storage / OFFICE_RENDERER_DIRNAME / RELEASE.version


@pytest.mark.parametrize(
    ("version", "bundle_id", "supported"),
    [
        ("26.2.0.1", LIBREOFFICE_BUNDLE_ID, True),
        ("27.0", LIBREOFFICE_BUNDLE_ID, True),
        ("26.1.9.3", LIBREOFFICE_BUNDLE_ID, False),
        ("25.8.4", LIBREOFFICE_BUNDLE_ID, False),
        ("26", LIBREOFFICE_BUNDLE_ID, False),
        ("twenty-six", LIBREOFFICE_BUNDLE_ID, False),
        ("26.2.0.1", "org.example.other", False),
    ],
)
def test_version_gate(
    tmp_path: Path, version: str, bundle_id: str, supported: bool
) -> None:
    app = _make_app(tmp_path / "LibreOffice.app", version=version, bundle_id=bundle_id)

    assert is_supported_bundle(app) is supported


def test_first_supported_app_root_wins(tmp_path: Path) -> None:
    old = _make_app(tmp_path / "system" / "LibreOffice.app", version="26.1.0")
    user = _make_app(tmp_path / "user" / "LibreOffice.app")
    requests: list[httpx2.Request] = []
    runtime = _runtime(
        tmp_path,
        transport=_serving(requests=requests),
        app_roots=(old.parent, user.parent),
    )

    assert runtime.ensure_available(tmp_path / "storage") == OfficeRuntimeReady(user)
    assert requests == []


def test_own_copy_needs_installed_marker_and_precedes_spotlight(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    own = _make_app(_version_dir(storage) / "LibreOffice.app")
    spotlit = _make_app(tmp_path / "elsewhere" / "LibreOffice.app")
    commands = FakeCommands(spotlight=[spotlit])
    runtime = _runtime(tmp_path, transport=_serving(), commands=commands)

    assert runtime.ensure_available(storage) == OfficeRuntimeReady(spotlit)

    (_version_dir(storage) / INSTALLED_MARKER_NAME).write_text("{}")
    assert runtime.ensure_available(storage) == OfficeRuntimeReady(own)


def test_install_makes_own_copy_ready_for_later_processes(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    commands = FakeCommands()
    runtime = _runtime(tmp_path, transport=_serving(), commands=commands)

    assert runtime.ensure_available(storage) == OfficeRuntimePreparing()
    bundle = _version_dir(storage) / "LibreOffice.app"
    assert _settle(runtime) == OfficeRuntimeReady(bundle)
    assert commands.ran("hdiutil", "detach")
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == [
        _version_dir(storage)
    ]

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("an installed copy must not be downloaded again")

    restarted = _runtime(tmp_path, transport=httpx2.MockTransport(refuse))
    assert restarted.ensure_available(storage) == OfficeRuntimeReady(bundle)


def test_checksum_mismatch_is_unavailable_and_leaves_nothing(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    commands = FakeCommands()
    tampered = b"x" * len(DMG_BYTES)
    runtime = _runtime(tmp_path, transport=_serving(tampered), commands=commands)

    runtime.ensure_available(storage)

    assert _settle(runtime) == OfficeRuntimeUnavailable("checksum_mismatch")
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == []
    assert not commands.ran("hdiutil", "attach")


def test_oversized_download_stops_as_checksum_mismatch(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    runtime = _runtime(tmp_path, transport=_serving(DMG_BYTES + b"tail"))

    runtime.ensure_available(storage)

    assert _settle(runtime) == OfficeRuntimeUnavailable("checksum_mismatch")
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == []


def test_network_error_is_unavailable_and_next_call_retries(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    attempts: list[httpx2.Request] = []

    def flaky(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx2.ConnectError("offline", request=request)
        return httpx2.Response(200, content=DMG_BYTES)

    runtime = _runtime(tmp_path, transport=httpx2.MockTransport(flaky))

    runtime.ensure_available(storage)
    assert _settle(runtime) == OfficeRuntimeUnavailable("download_failed")
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == []

    assert runtime.ensure_available(storage) == OfficeRuntimePreparing()
    assert _settle(runtime) == OfficeRuntimeReady(
        _version_dir(storage) / "LibreOffice.app"
    )


def test_http_error_status_is_download_failed(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path, transport=httpx2.MockTransport(lambda request: httpx2.Response(404))
    )

    runtime.ensure_available(tmp_path / "storage")

    assert _settle(runtime) == OfficeRuntimeUnavailable("download_failed")


def test_disk_shortfall_is_unavailable_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=10**6))
    requests: list[httpx2.Request] = []
    runtime = _runtime(tmp_path, transport=_serving(requests=requests))

    runtime.ensure_available(tmp_path / "storage")

    assert _settle(runtime) == OfficeRuntimeUnavailable("insufficient_disk_space")
    assert requests == []


def test_concurrent_callers_share_one_install(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    release = threading.Event()
    requests: list[httpx2.Request] = []

    def slow(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert release.wait(10)
        return httpx2.Response(200, content=DMG_BYTES)

    runtime = _runtime(tmp_path, transport=httpx2.MockTransport(slow))
    results: list[OfficeRuntimeSnapshot] = []
    callers = [
        threading.Thread(
            target=lambda: results.append(runtime.ensure_available(storage))
        )
        for _ in range(8)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    release.set()

    assert results == [OfficeRuntimePreparing()] * 8
    assert isinstance(_settle(runtime), OfficeRuntimeReady)
    assert len(requests) == 1


def test_failed_copy_still_detaches_the_image(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    commands = FakeCommands(ditto_exit=1)
    runtime = _runtime(tmp_path, transport=_serving(), commands=commands)

    runtime.ensure_available(storage)

    assert _settle(runtime) == OfficeRuntimeUnavailable("install_failed")
    assert commands.ran("hdiutil", "detach")
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == []


def test_stuck_spotlight_falls_through_to_install(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    commands = FakeCommands(times_out="mdfind")
    runtime = _runtime(tmp_path, transport=_serving(), commands=commands)

    assert runtime.ensure_available(storage) == OfficeRuntimePreparing()
    assert _settle(runtime) == OfficeRuntimeReady(
        _version_dir(storage) / "LibreOffice.app"
    )


@pytest.mark.parametrize("step", ["attach", "ditto"])
def test_timed_out_image_step_detaches_and_leaves_nothing(
    tmp_path: Path, step: Literal["attach", "ditto"]
) -> None:
    storage = tmp_path / "storage"
    commands = FakeCommands(times_out=step)
    runtime = _runtime(tmp_path, transport=_serving(), commands=commands)

    runtime.ensure_available(storage)

    assert _settle(runtime) == OfficeRuntimeUnavailable("install_failed")
    assert [path.name for path in commands.detached] == ["volume"]
    assert list((storage / OFFICE_RENDERER_DIRNAME).iterdir()) == []


def test_install_finishing_during_discovery_keeps_the_new_copy(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    bundle = _version_dir(storage) / "LibreOffice.app"
    release = threading.Event()
    requests: list[httpx2.Request] = []

    def gated(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert release.wait(10)
        return httpx2.Response(200, content=DMG_BYTES)

    spotlight_runs: list[None] = []

    def finish_install_meanwhile() -> None:
        spotlight_runs.append(None)
        if len(spotlight_runs) == 2:
            release.set()
            _settle(runtime)

    runtime = _runtime(
        tmp_path,
        transport=httpx2.MockTransport(gated),
        commands=FakeCommands(on_spotlight=finish_install_meanwhile),
    )

    assert runtime.ensure_available(storage) == OfficeRuntimePreparing()
    assert runtime.ensure_available(storage) == OfficeRuntimeReady(bundle)
    assert _settle(runtime) == OfficeRuntimeReady(bundle)
    assert is_supported_bundle(bundle)
    assert len(requests) == 1

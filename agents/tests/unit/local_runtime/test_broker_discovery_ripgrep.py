from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker_discovery_ripgrep
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)


def test_ripgrep_files_uses_fixed_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        captured["argv"] = argv
        captured["cwd"] = cwd
        assert handle_line(b"src/action[1]/PLAN.MD") is True
        assert handle_line(b"src/app.py") is True
        assert handle_line(b"src/other.py") is False
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=0,
            stderr="",
            stderr_truncated=False,
            stderr_lines=0,
            timed_out=False,
            stdout_truncated=False,
            stopped_early=True,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    result = broker_discovery_ripgrep.run_ripgrep_files(
        cwd=tmp_path,
        sandbox_profile="",
        glob_pattern="src/*.py",
        limit=1,
        excluded_relative_path="src/action[1]/plan.md",
    )

    assert captured["cwd"] == tmp_path
    assert captured["argv"] == (
        "/trusted/bin/rg",
        "--files",
        "--no-config",
        "--hidden",
        "--no-ignore",
        "--color",
        "never",
        "--glob",
        "src/*.py",
        "--glob",
        r"!/[sS][rR][cC]/[aA][cC][tT][iI][oO][nN]\[1\]/[pP][lL][aA][nN].[mM][dD]",
        "--",
        ".",
    )
    assert result.relative_paths == ("src/app.py",)
    assert result.truncated is True


def test_ripgrep_grep_uses_fixed_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        captured["argv"] = argv
        captured["cwd"] = cwd
        assert handle_line(b"src/action[1]/PLAN.MD\x001:9:private needle")
        assert handle_line(b"src/app.py\x003:1:needle\r")
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=0,
            stderr="",
            stderr_truncated=False,
            stderr_lines=0,
            timed_out=False,
            stdout_truncated=False,
            stopped_early=False,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    result = broker_discovery_ripgrep.run_ripgrep_grep(
        cwd=tmp_path,
        sandbox_profile="",
        pattern="needle",
        include_glob="**/*.py",
        max_matches=10,
        excluded_relative_path="src/action[1]/plan.md",
    )

    assert captured["cwd"] == tmp_path
    assert captured["argv"] == (
        "/trusted/bin/rg",
        "--no-config",
        "--hidden",
        "--no-ignore",
        "--color",
        "never",
        "--no-heading",
        "--with-filename",
        "--null",
        "--line-number",
        "--column",
        "--max-columns",
        "65536",
        "--max-columns-preview",
        "--binary",
        "--glob",
        "**/*.py",
        "--glob",
        r"!/[sS][rR][cC]/[aA][cC][tT][iI][oO][nN]\[1\]/[pP][lL][aA][nN].[mM][dD]",
        "--",
        "needle",
        ".",
    )
    assert result.matches == (
        broker_discovery_ripgrep.RipgrepGrepMatch(
            relative_path="src/app.py",
            line_number=3,
            line="needle",
            line_truncated=False,
        ),
    )


def test_ripgrep_grep_timeout_truncates_without_pattern_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=-9,
            stderr="",
            stderr_truncated=False,
            stderr_lines=0,
            timed_out=True,
            stdout_truncated=False,
            stopped_early=False,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    result = broker_discovery_ripgrep.run_ripgrep_grep(
        cwd=tmp_path,
        sandbox_profile="",
        pattern="needle",
        include_glob=None,
        max_matches=10,
    )

    assert result.truncated is True
    assert result.timed_out is True


def test_ripgrep_files_classifies_glob_parse_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=2,
            stderr="rg: error parsing glob '[': unclosed character class",
            stderr_truncated=False,
            stderr_lines=0,
            timed_out=False,
            stdout_truncated=False,
            stopped_early=False,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    with pytest.raises(BrokerPolicyError) as exc_info:
        broker_discovery_ripgrep.run_ripgrep_files(
            cwd=tmp_path,
            sandbox_profile="",
            glob_pattern="[",
            limit=10,
        )

    assert exc_info.value.code == "GLOB_PATTERN_INVALID"
    assert exc_info.value.fix_hint == broker_discovery_ripgrep.GLOB_PATTERN_FIX_HINT
    assert exc_info.value.examples == broker_discovery_ripgrep.GLOB_PATTERN_EXAMPLES


def test_ripgrep_runner_drains_stderr_without_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broker_discovery_ripgrep, "RIPGREP_TIMEOUT_SECONDS", 0.5)
    script = (
        "import sys\n"
        "sys.stderr.write('x' * 200000)\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(2)\n"
    )

    result = broker_discovery_ripgrep._run_ripgrep_lines(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        sandbox_profile="(version 1)\n(allow default)",
        handle_line=lambda line: True,
    )

    assert result.exit_code == 2
    assert result.timed_out is False
    assert result.stderr_truncated is True
    assert len(result.stderr) <= broker_discovery_ripgrep.RIPGREP_MAX_STDERR_CHARS


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [
        ("regex parse error:\n    (\nerror: unclosed group", "GREP_PATTERN_INVALID"),
        (
            "glob parse error:\n    [\nerror: unclosed character class",
            "GREP_INCLUDE_GLOB_INVALID",
        ),
        ("sandbox-exec: sandbox_apply: Operation not permitted", "GREP_BACKEND_FAILED"),
        ("", "GREP_BACKEND_FAILED"),
    ],
)
def test_ripgrep_grep_classifies_backend_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    expected_code: str,
) -> None:
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=2,
            stderr=stderr,
            stderr_truncated=False,
            stderr_lines=0,
            timed_out=False,
            stdout_truncated=False,
            stopped_early=False,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    with pytest.raises(BrokerPolicyError) as exc_info:
        broker_discovery_ripgrep.run_ripgrep_grep(
            cwd=tmp_path,
            sandbox_profile="",
            pattern="needle",
            include_glob="**/*.py",
            max_matches=10,
        )

    assert exc_info.value.code == expected_code
    if expected_code == "GREP_PATTERN_INVALID":
        assert exc_info.value.fix_hint == broker_discovery_ripgrep.GREP_PATTERN_FIX_HINT
        assert exc_info.value.examples == broker_discovery_ripgrep.GREP_PATTERN_EXAMPLES
    elif expected_code == "GREP_INCLUDE_GLOB_INVALID":
        assert (
            exc_info.value.fix_hint
            == broker_discovery_ripgrep.GREP_INCLUDE_GLOB_FIX_HINT
        )
        assert (
            exc_info.value.examples
            == broker_discovery_ripgrep.GREP_INCLUDE_GLOB_EXAMPLES
        )


@pytest.mark.parametrize("installed", [True, False])
def test_ripgrep_selection_never_uses_parent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    trusted = tmp_path / "trusted"
    untrusted = tmp_path / "workspace"
    for directory in (trusted, untrusted):
        directory.mkdir()
    for directory in (trusted, untrusted) if installed else (untrusted,):
        executable = directory / "rg"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(untrusted))
    monkeypatch.setattr(broker_discovery_ripgrep, "RIPGREP_TRUSTED_PATH", str(trusted))
    if installed:
        assert broker_discovery_ripgrep._resolve_ripgrep_executable() == trusted / "rg"
    else:
        with pytest.raises(BrokerPolicyError) as exc_info:
            broker_discovery_ripgrep._resolve_ripgrep_executable()
        assert exc_info.value.code == "DISCOVERY_BACKEND_UNAVAILABLE"


def test_ripgrep_grep_excerpts_long_lines_and_reports_unreadable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker_discovery_ripgrep,
        "_resolve_ripgrep_executable",
        lambda: Path("/trusted/bin/rg"),
    )
    preview = b"a" * broker_discovery_ripgrep.RIPGREP_MAX_COLUMNS

    def fake_run(
        *,
        argv: tuple[str, ...],
        cwd: Path,
        sandbox_profile: str,
        handle_line: broker_discovery_ripgrep.LineHandler,
    ) -> broker_discovery_ripgrep.RipgrepRunResult:
        # A preview of a longer line whose first match is past its end.
        assert handle_line(
            b"./big.jsonl\x007:70001:" + preview + b" [... 1 more match]"
        )
        # A line of exactly the cap is previewed too; its match is in the preview.
        assert handle_line(
            b"./big.jsonl\x009:1001:" + preview + b" [... 0 more matches]"
        )
        # Shift_JIS text is shown lossily rather than dropped.
        assert handle_line(b"./sjis.txt\x001:8:\x93\xfa\x96\x7b\x8c\xea needle")
        assert handle_line(
            b'./db.sqlite: binary file matches (found "\\0" byte around offset 9)'
        )
        return broker_discovery_ripgrep.RipgrepRunResult(
            exit_code=2,
            stderr="rg: ./locked: Permission denied (os error 13)\n",
            stderr_truncated=False,
            stderr_lines=1,
            timed_out=False,
            stdout_truncated=False,
            stopped_early=False,
        )

    monkeypatch.setattr(broker_discovery_ripgrep, "_run_ripgrep_lines", fake_run)

    result = broker_discovery_ripgrep.run_ripgrep_grep(
        cwd=tmp_path,
        sandbox_profile="",
        pattern="needle",
        include_glob=None,
        max_matches=10,
    )

    limit = broker_discovery_ripgrep.GREP_MAX_LINE_CHARS
    assert [(match.line_number, match.line) for match in result.matches] == [
        (7, "a" * limit + "…"),
        (9, "…" + "a" * limit + "…"),
        (1, b"\x93\xfa\x96\x7b\x8c\xea needle".decode("utf-8", errors="replace")),
    ]
    assert [match.line_truncated for match in result.matches] == [True, True, False]
    assert result.binary_match_paths == ("./db.sqlite",)
    assert result.skipped_files == 1
    assert result.first_skip_error == "./locked: Permission denied (os error 13)"


@pytest.mark.parametrize(
    ("text", "match_start", "cut", "expected"),
    [
        ("short needle", 6, False, ("short needle", False)),
        (
            "x" * 1_000 + "needle" + "y" * 1_000,
            1_000,
            False,
            ("…" + "x" * 250 + "needle" + "y" * 244 + "…", True),
        ),
        ("needle" + "y" * 1_000, 0, False, ("needle" + "y" * 494 + "…", True)),
        ("x" * 1_000 + "needle", 1_000, False, ("…" + "x" * 494 + "needle", True)),
        ("x" * 10, None, True, ("x" * 10 + "…", True)),
    ],
)
def test_grep_match_centres_long_lines_on_the_first_match(
    text: str,
    match_start: int | None,
    cut: bool,
    expected: tuple[str, bool],
) -> None:
    match = broker_discovery_ripgrep.grep_match(
        relative_path="a.txt",
        line_number=1,
        text=text,
        match_start=match_start,
        cut=cut,
    )

    assert (match.line, match.line_truncated) == expected


_REAL_RIPGREP = pytest.mark.skipif(
    shutil.which("rg", path=broker_discovery_ripgrep.RIPGREP_TRUSTED_PATH) is None,
    reason="ripgrep is not installed in a trusted location",
)
_ALLOW_ALL_PROFILE = "(version 1)\n(allow default)"


def _real_grep(
    cwd: Path, *, max_matches: int = 100
) -> broker_discovery_ripgrep.RipgrepGrepResult:
    return broker_discovery_ripgrep.run_ripgrep_grep(
        cwd=cwd,
        sandbox_profile=_ALLOW_ALL_PROFILE,
        pattern="needle",
        include_glob=None,
        max_matches=max_matches,
    )


@_REAL_RIPGREP
def test_real_ripgrep_finds_matches_in_files_over_one_megabyte(tmp_path: Path) -> None:
    filler = '{"role":"tool","content":"' + "x" * 1_000 + '"}\n'
    lines = [filler] * 3_000
    for index in range(5):
        lines[600 * index + 599] = f'{{"role":"user","content":"needle {index}"}}\n'
    (tmp_path / "transcript.jsonl").write_text("".join(lines), encoding="utf-8")
    assert (tmp_path / "transcript.jsonl").stat().st_size > 2 * 1024 * 1024

    result = _real_grep(tmp_path)

    assert [(match.line_number, match.line) for match in result.matches] == [
        (600 * index + 600, f'{{"role":"user","content":"needle {index}"}}')
        for index in range(5)
    ]
    assert result.truncation_reason is None
    assert result.skipped_files == 0


@_REAL_RIPGREP
def test_real_ripgrep_excerpts_very_long_lines_around_the_match(
    tmp_path: Path,
) -> None:
    # Multibyte text before the match: ripgrep's column counts bytes.
    (tmp_path / "wide.jsonl").write_text(
        "あ" * 30_000 + "needle" + "い" * 200_000 + "\n", encoding="utf-8"
    )
    (tmp_path / "late.jsonl").write_text(
        "x" * 200_000 + "needle" + "\n", encoding="utf-8"
    )

    result = _real_grep(tmp_path)

    by_path = {match.relative_path: match for match in result.matches}
    wide = by_path["./wide.jsonl"]
    assert wide.line_truncated is True
    assert wide.line == "…" + "あ" * 250 + "needle" + "い" * 244 + "…"
    # Past ripgrep's preview the excerpt is the line's start, still marked cut.
    late = by_path["./late.jsonl"]
    assert late.line_truncated is True
    assert late.line == "x" * broker_discovery_ripgrep.GREP_MAX_LINE_CHARS + "…"


@_REAL_RIPGREP
def test_real_ripgrep_reports_the_match_limit_and_unreadable_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("needle 1\nneedle 2\nneedle 3\n", encoding="utf-8")
    (tmp_path / "b.bin").write_bytes(b"\0" * 100 + b" needle")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "b.txt").write_text("needle hidden\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        unlimited = _real_grep(tmp_path)
        limited = _real_grep(tmp_path, max_matches=2)
    finally:
        locked.chmod(0o755)

    assert [match.line for match in unlimited.matches] == [
        "needle 1",
        "needle 2",
        "needle 3",
    ]
    assert unlimited.truncation_reason is None
    assert unlimited.binary_match_paths == ("./b.bin",)
    assert unlimited.skipped_files == 1
    assert "Permission denied" in str(unlimited.first_skip_error)
    assert len(limited.matches) == 2
    assert limited.truncated is True
    assert limited.truncation_reason == "limit"

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal

from .broker_common import BrokerPolicyError
from .broker_grep_lines import (
    RIPGREP_MAX_COLUMNS,
    RipgrepGrepMatch,
    grep_match_from_ripgrep,
)

RIPGREP_COMMAND = "rg"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Discovery runs outside the command sandbox; never select a workspace executable.
RIPGREP_TRUSTED_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin"
RIPGREP_TIMEOUT_SECONDS = 5.0
# Bounds what is read from ripgrep in total. It must hold the previews of as
# many long matching lines as GREP_MAX_OUTPUT_BYTES can show excerpts of.
RIPGREP_MAX_STDOUT_BYTES = 8 * 1024 * 1024
RIPGREP_MAX_STDERR_CHARS = 8_192
_GREP_LINE_PATTERN = re.compile(rb"(\d+):(\d+):(.*)", re.DOTALL)
# With --binary, a file holding a NUL byte is searched too and reported once,
# by this message, instead of being skipped without a word.
_RIPGREP_BINARY_MATCH_PATTERN = re.compile(
    rb'(.+): binary file matches \(found "\\0" byte around offset \d+\)',
    re.DOTALL,
)
RIPGREP_ERROR_PREFIX = "rg: "
RIPGREP_REGEX_ERROR_MARKERS = (
    "regex parse error",
    "error parsing regex",
)
RIPGREP_GLOB_ERROR_MARKERS = (
    "glob parse error",
    "error parsing glob",
)
RIPGREP_COMMON_ARGS = (
    # A config file named by the inherited RIPGREP_CONFIG_PATH would change what
    # a search returns, and its --pre would run a program nobody approved.
    "--no-config",
    "--hidden",
    "--no-ignore",
    "--color",
    "never",
)
GLOB_PATTERN_FIX_HINT = (
    "Use a base_path-relative ripgrep glob such as **/*.py. Do not pass malformed "
    "character classes, absolute paths, or parent directory parts."
)
GLOB_PATTERN_EXAMPLES = ('{"base_path":".","pattern":"**/*.py","limit":100}',)
GREP_PATTERN_FIX_HINT = (
    "grep.pattern is a ripgrep regular expression. Escape regex metacharacters for "
    "literal text, for example use \\( for a literal opening parenthesis."
)
GREP_PATTERN_EXAMPLES = (
    '{"base_path":".","pattern":"TODO","include_glob":"**/*.py","max_matches":100}',
    '{"base_path":".","pattern":"\\\\(","include_glob":"**/*.txt","max_matches":100}',
)
GREP_INCLUDE_GLOB_FIX_HINT = (
    "grep.include_glob is a base_path-relative ripgrep glob such as **/*.py. "
    "Do not pass malformed character classes, absolute paths, or parent directory parts."
)
GREP_INCLUDE_GLOB_EXAMPLES = (
    '{"base_path":".","pattern":"TODO","include_glob":"**/*.py","max_matches":100}',
)


@dataclass(frozen=True, slots=True)
class RipgrepRunResult:
    exit_code: int | None
    stderr: str
    stderr_truncated: bool
    # Every stderr line, counted past the stored text too.
    stderr_lines: int
    timed_out: bool
    stdout_truncated: bool
    stopped_early: bool


@dataclass(frozen=True, slots=True)
class RipgrepGlobResult:
    relative_paths: tuple[str, ...]
    truncated: bool
    truncation_reason: RipgrepTruncationReason | None
    timed_out: bool
    skipped_files: int
    first_skip_error: str | None


@dataclass(frozen=True, slots=True)
class RipgrepGrepResult:
    matches: tuple[RipgrepGrepMatch, ...]
    truncated: bool
    truncation_reason: RipgrepTruncationReason | None
    timed_out: bool
    # Paths ripgrep could not read (permission denied, sandbox denial, I/O
    # error); a directory among them was not searched below either.
    skipped_files: int
    first_skip_error: str | None
    # Files with a NUL byte that match; their lines are not printed.
    binary_match_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StderrDrainResult:
    stderr: str
    truncated: bool
    lines: int


LineHandler = Callable[[bytes], bool]
RipgrepTruncationReason = Literal["limit", "timeout", "output_bytes"]


def run_ripgrep_files(
    *,
    cwd: Path,
    sandbox_profile: str,
    glob_pattern: str,
    limit: int,
    follow_symlinks: bool = False,
    excluded_relative_path: str | None = None,
    pruned_relative_paths: tuple[str, ...] = (),
    extra_search_paths: tuple[str, ...] = (),
    include_path: Callable[[Path], bool] | None = None,
) -> RipgrepGlobResult:
    matches: list[str] = []
    truncated = False

    def handle_line(raw_line: bytes) -> bool:
        nonlocal truncated
        line = raw_line.decode("utf-8", errors="replace")
        if not line:
            return True
        if _is_excluded_relative_path(line, excluded_relative_path):
            return True
        if include_path is not None and not include_path(cwd / PurePosixPath(line)):
            return True
        if len(matches) >= limit:
            truncated = True
            return False
        matches.append(line)
        return True

    result = _run_ripgrep_lines(
        argv=(
            str(_resolve_ripgrep_executable()),
            "--files",
            *RIPGREP_COMMON_ARGS,
            *(("--follow",) if follow_symlinks else ()),
            "--glob",
            glob_pattern,
            *(
                ("--glob", _literal_exclusion_glob(excluded_relative_path))
                if excluded_relative_path
                else ()
            ),
            *_pruning_args(pruned_relative_paths),
            "--",
            ".",
            *extra_search_paths,
        ),
        cwd=cwd,
        sandbox_profile=sandbox_profile,
        handle_line=handle_line,
    )
    _raise_if_ripgrep_glob_failed(result=result)
    return RipgrepGlobResult(
        relative_paths=tuple(matches),
        truncated=truncated or result.stdout_truncated or result.timed_out,
        truncation_reason=_ripgrep_truncation_reason(
            limit_reached=truncated,
            result=result,
        ),
        timed_out=result.timed_out,
        skipped_files=result.stderr_lines,
        first_skip_error=_first_skip_error(result.stderr),
    )


def run_ripgrep_grep(
    *,
    cwd: Path,
    sandbox_profile: str,
    pattern: str,
    include_glob: str | None,
    max_matches: int,
    follow_symlinks: bool = False,
    excluded_relative_path: str | None = None,
    pruned_relative_paths: tuple[str, ...] = (),
    extra_search_paths: tuple[str, ...] = (),
    include_path: Callable[[Path], bool] | None = None,
) -> RipgrepGrepResult:
    matches: list[RipgrepGrepMatch] = []
    binary_match_paths: list[str] = []
    truncated = False
    # --null ends a path with NUL but keeps a newline inside it, which splits
    # one record across lines; the start is held until the record completes.
    pending = b""

    def is_hidden(relative_path: str) -> bool:
        return _is_excluded_relative_path(relative_path, excluded_relative_path) or (
            include_path is not None
            and not include_path(cwd / PurePosixPath(relative_path))
        )

    def handle_line(raw_line: bytes) -> bool:
        nonlocal pending, truncated
        record = pending + raw_line
        pending = b""
        raw_path, separator, rest = record.partition(b"\0")
        parsed = _GREP_LINE_PATTERN.fullmatch(rest) if separator else None
        if parsed is None:
            binary = _RIPGREP_BINARY_MATCH_PATTERN.fullmatch(record)
            if binary is None:
                if not separator:
                    pending = record + b"\n"
                return True
            relative_path = binary.group(1).decode("utf-8", errors="replace")
            if not is_hidden(relative_path):
                binary_match_paths.append(relative_path)
            return True
        relative_path = raw_path.decode("utf-8", errors="replace")
        if is_hidden(relative_path):
            return True
        if len(matches) >= max_matches:
            truncated = True
            return False
        matches.append(
            grep_match_from_ripgrep(
                relative_path=relative_path,
                line_number=int(parsed.group(1)),
                column=int(parsed.group(2)),
                content=parsed.group(3).removesuffix(b"\r"),
            )
        )
        return True

    argv = [
        str(_resolve_ripgrep_executable()),
        *RIPGREP_COMMON_ARGS,
        "--no-heading",
        "--with-filename",
        "--null",
        "--line-number",
        "--column",
        "--max-columns",
        str(RIPGREP_MAX_COLUMNS),
        "--max-columns-preview",
        "--binary",
    ]
    if follow_symlinks:
        argv.append("--follow")
    if include_glob is not None:
        argv.extend(("--glob", include_glob))
    if excluded_relative_path is not None:
        argv.extend(("--glob", _literal_exclusion_glob(excluded_relative_path)))
    argv.extend(_pruning_args(pruned_relative_paths))
    argv.extend(("--", pattern, ".", *extra_search_paths))
    result = _run_ripgrep_lines(
        argv=tuple(argv),
        cwd=cwd,
        sandbox_profile=sandbox_profile,
        handle_line=handle_line,
    )
    _raise_if_ripgrep_grep_failed(result=result)
    return RipgrepGrepResult(
        matches=tuple(matches),
        truncated=truncated or result.stdout_truncated or result.timed_out,
        truncation_reason=_ripgrep_truncation_reason(
            limit_reached=truncated,
            result=result,
        ),
        timed_out=result.timed_out,
        skipped_files=result.stderr_lines,
        first_skip_error=_first_skip_error(result.stderr),
        binary_match_paths=tuple(binary_match_paths),
    )


def _first_skip_error(stderr: str) -> str | None:
    first_line = stderr.partition("\n")[0].strip()
    return first_line.removeprefix(RIPGREP_ERROR_PREFIX) or None


def _is_excluded_relative_path(path: str, excluded: str | None) -> bool:
    if excluded is None:
        return False
    candidate = PurePosixPath(path)
    private = PurePosixPath(excluded)
    return (
        candidate.parent == private.parent
        and candidate.name.casefold() == private.name.casefold()
    )


def _pruning_args(relative_paths: tuple[str, ...]) -> tuple[str, ...]:
    # An excluded directory is not descended, so nothing under it is read or
    # charged to the output budget; an explicit search path still is.
    return tuple(
        argument
        for relative_path in relative_paths
        for argument in ("--glob", _literal_exclusion_glob(relative_path))
    )


def _literal_exclusion_glob(relative_path: str) -> str:
    # Every component matches case-insensitively, like the APFS volume it names.
    escaped = "/".join(
        "".join(
            f"[{character.lower()}{character.upper()}]"
            if character.isascii() and character.isalpha()
            else f"\\{character}"
            if character in r"\*?[]{}"
            else character
            for character in component
        )
        for component in PurePosixPath(relative_path).parts
    )
    return f"!/{escaped}"


def _resolve_ripgrep_executable() -> Path:
    resolved = shutil.which(RIPGREP_COMMAND, path=RIPGREP_TRUSTED_PATH)
    if resolved is None:
        raise BrokerPolicyError(
            "ripgrep backend is unavailable",
            code="DISCOVERY_BACKEND_UNAVAILABLE",
            fix_hint="Install ripgrep in a trusted system location before using glob or grep.",
        )
    return Path(resolved).resolve()


def _run_ripgrep_lines(
    *,
    argv: tuple[str, ...],
    cwd: Path,
    sandbox_profile: str,
    handle_line: LineHandler,
) -> RipgrepRunResult:
    process = subprocess.Popen(
        _sandboxed_argv(argv, sandbox_profile),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timed_out = False
    stdout_truncated = False
    stopped_early = False
    if process.stdout is None:
        raise BrokerPolicyError(
            "ripgrep backend did not expose stdout",
            code="DISCOVERY_BACKEND_FAILED",
        )
    if process.stderr is None:
        raise BrokerPolicyError(
            "ripgrep backend did not expose stderr",
            code="DISCOVERY_BACKEND_FAILED",
        )
    stderr_drain = _StderrDrain(
        io.TextIOWrapper(process.stderr, encoding="utf-8", errors="replace")
    )
    stderr_thread = threading.Thread(target=stderr_drain.run, daemon=True)
    stderr_thread.start()

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        process.kill()

    timer = threading.Timer(RIPGREP_TIMEOUT_SECONDS, kill_on_timeout)
    timer.start()
    stdout_bytes = 0
    try:
        while True:
            # Reading at most the rest of the budget keeps one endless line
            # from being held in memory whole.
            raw_line = process.stdout.readline(
                RIPGREP_MAX_STDOUT_BYTES - stdout_bytes + 1
            )
            if not raw_line:
                break
            stdout_bytes += len(raw_line)
            if stdout_bytes > RIPGREP_MAX_STDOUT_BYTES:
                stdout_truncated = True
                stopped_early = True
                process.kill()
                break
            if not handle_line(raw_line.removesuffix(b"\n")):
                stopped_early = True
                process.kill()
                break
        exit_code = process.wait()
    finally:
        timer.cancel()
    stderr_thread.join()
    drained_stderr = stderr_drain.result()
    return RipgrepRunResult(
        exit_code=exit_code,
        stderr=drained_stderr.stderr,
        stderr_truncated=drained_stderr.truncated,
        stderr_lines=drained_stderr.lines,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stopped_early=stopped_early,
    )


def _sandboxed_argv(argv: tuple[str, ...], sandbox_profile: str) -> tuple[str, ...]:
    """``argv`` under the seatbelt profile that bounds what ripgrep may read.

    Only macOS has sandbox-exec, and it is the only platform the app ships on.
    The unit tests also run on Linux, where this returns the command unchanged.
    """

    # Keep both paths type-checked on Linux CI; mypy folds a direct sys.platform guard.
    on_macos = sys.platform == "darwin"
    if not on_macos:
        return argv
    return (SANDBOX_EXEC, "-p", sandbox_profile, *argv)


class _StderrDrain:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._chunks: list[str] = []
        self._stored_chars = 0
        self._truncated = False
        self._lines = 0

    def run(self) -> None:
        while True:
            chunk = self._stream.read(4096)
            if not chunk:
                return
            self._lines += chunk.count("\n")
            remaining = RIPGREP_MAX_STDERR_CHARS - self._stored_chars
            if remaining <= 0:
                self._truncated = True
                continue
            self._chunks.append(chunk[:remaining])
            self._stored_chars += min(len(chunk), remaining)
            if len(chunk) > remaining:
                self._truncated = True

    def result(self) -> StderrDrainResult:
        return StderrDrainResult(
            stderr="".join(self._chunks),
            truncated=self._truncated,
            lines=self._lines,
        )


def _raise_if_ripgrep_glob_failed(*, result: RipgrepRunResult) -> None:
    if result.exit_code in (0, 1) or result.stopped_early or result.timed_out:
        return
    stderr = result.stderr.lower()
    if _contains_any(stderr, RIPGREP_GLOB_ERROR_MARKERS):
        raise BrokerPolicyError(
            "glob.pattern must be a valid ripgrep glob pattern",
            code="GLOB_PATTERN_INVALID",
            fix_hint=GLOB_PATTERN_FIX_HINT,
            examples=GLOB_PATTERN_EXAMPLES,
        )
    if _only_unreadable_paths(result.stderr):
        return
    raise BrokerPolicyError(
        "ripgrep discovery backend failed",
        code="GLOB_BACKEND_FAILED",
        fix_hint=_stderr_hint(result.stderr),
    )


def _raise_if_ripgrep_grep_failed(*, result: RipgrepRunResult) -> None:
    if result.exit_code in (0, 1) or result.stopped_early or result.timed_out:
        return
    stderr = result.stderr.lower()
    if _contains_any(stderr, RIPGREP_REGEX_ERROR_MARKERS):
        raise BrokerPolicyError(
            "grep.pattern must be a valid ripgrep regular expression",
            code="GREP_PATTERN_INVALID",
            fix_hint=GREP_PATTERN_FIX_HINT,
            examples=GREP_PATTERN_EXAMPLES,
        )
    if _contains_any(stderr, RIPGREP_GLOB_ERROR_MARKERS):
        raise BrokerPolicyError(
            "grep.include_glob must be a valid ripgrep glob pattern",
            code="GREP_INCLUDE_GLOB_INVALID",
            fix_hint=GREP_INCLUDE_GLOB_FIX_HINT,
            examples=GREP_INCLUDE_GLOB_EXAMPLES,
        )
    if _only_unreadable_paths(result.stderr):
        return
    raise BrokerPolicyError(
        "ripgrep grep backend failed",
        code="GREP_BACKEND_FAILED",
        fix_hint=_stderr_hint(result.stderr),
    )


def _only_unreadable_paths(stderr: str) -> bool:
    # ripgrep reports a path it cannot read on its own line and exits 2 once the
    # rest is searched; those paths are reported as skipped, not as a failure.
    error_lines = [line for line in stderr.splitlines() if line.strip()]
    return bool(error_lines) and all(
        line.startswith(RIPGREP_ERROR_PREFIX) for line in error_lines
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _ripgrep_truncation_reason(
    *,
    limit_reached: bool,
    result: RipgrepRunResult,
) -> RipgrepTruncationReason | None:
    if result.stdout_truncated:
        return "output_bytes"
    if result.timed_out:
        return "timeout"
    if limit_reached:
        return "limit"
    return None


def _stderr_hint(stderr: str) -> str | None:
    stripped = stderr.strip()
    if not stripped:
        return None
    return stripped[:RIPGREP_MAX_STDERR_CHARS]

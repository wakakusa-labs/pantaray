#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SHELL_PATH = "/bin/zsh"


@dataclass(frozen=True)
class CommandSpec:
    label: str
    cwd: Path
    command: str


@dataclass(frozen=True)
class TargetSpec:
    name: str
    trigger_rules: tuple[str, ...]
    commands: tuple[CommandSpec, ...]


@dataclass(frozen=True)
class CommandResult:
    spec: CommandSpec
    return_code: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0


WEB_TARGET = TargetSpec(
    name="web",
    trigger_rules=("frontend/", ".github/workflows/ci-web.yml"),
    commands=(
        CommandSpec(
            label="Icon policy (lucide-only)",
            cwd=Path("frontend"),
            command="node scripts/check-icon-policy.js",
        ),
        CommandSpec(label="Lint", cwd=Path("frontend"), command="pnpm run lint"),
        CommandSpec(
            label="Typecheck (renderer and tests)",
            cwd=Path("frontend"),
            command="pnpm run typecheck",
        ),
        CommandSpec(
            label="Test (web)",
            cwd=Path("frontend"),
            command="pnpm run test:vitest",
        ),
        CommandSpec(
            label="Test (renderer build policy)",
            cwd=Path("frontend"),
            command="node --test ./tests/vite_renderer_build.test.js",
        ),
        CommandSpec(
            label="Build (web)",
            cwd=Path("frontend"),
            command="pnpm run build:web",
        ),
    ),
)

DESKTOP_TARGET = TargetSpec(
    name="desktop",
    trigger_rules=(
        "frontend/electron/",
        "frontend/scripts/",
        "frontend/tests/",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/shared/",
        ".github/workflows/ci-desktop.yml",
    ),
    commands=(
        CommandSpec(
            label="Test (electron/node)",
            cwd=Path("frontend"),
            command="pnpm run test:node",
        ),
    ),
)

BACKEND_TARGET = TargetSpec(
    name="backend",
    trigger_rules=(
        "agents/",
        # The TODO list's switch has a twin in agents/; a backend test checks they agree.
        "frontend/src/todos/todoUxFeature.ts",
        ".github/workflows/ci-backend.yml",
        ".github/workflows/backend-checks.yml",
    ),
    commands=(
        CommandSpec(label="Run ruff (lint)", cwd=Path("agents"), command="uv run ruff check ."),
        CommandSpec(
            label="Run ruff (format check)",
            cwd=Path("agents"),
            command="uv run ruff format --check .",
        ),
        CommandSpec(label="Run mypy", cwd=Path("agents"), command="uv run mypy"),
        # tests/integration/local_runtime は backend-checks.yml と同じく除外する
        # （macOS の seatbelt 前提。E2E (desktop) の macOS ジョブが回す）。
        CommandSpec(
            label="Run pytest (unit, ws, integration)",
            cwd=Path("agents"),
            command=(
                "uv run pytest -q -n auto --dist loadfile"
                " tests/unit tests/ws tests/integration"
                " --ignore=tests/integration/local_runtime"
            ),
        ),
    ),
)

TARGETS: tuple[TargetSpec, ...] = (
    WEB_TARGET,
    DESKTOP_TARGET,
    BACKEND_TARGET,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local CI commands that mirror the current GitHub workflows.",
    )
    parser.add_argument(
        "--mode",
        choices=("staged", "worktree", "all"),
        default="staged",
        help="Choose staged files, the full worktree, or all targets.",
    )
    return parser.parse_args()


def run_git(repo_root: Path, args: Sequence[str]) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def list_changed_files(repo_root: Path, mode: str) -> list[str]:
    if mode == "all":
        return []

    if mode == "staged":
        return run_git(
            repo_root,
            ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        )

    unstaged = run_git(repo_root, ["diff", "--name-only", "--diff-filter=ACMR"])
    staged = run_git(
        repo_root,
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
    )
    untracked = run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    return sorted(set((*unstaged, *staged, *untracked)))


def path_matches_rule(path: str, rule: str) -> bool:
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule


def select_targets(changed_files: Sequence[str], mode: str) -> list[TargetSpec]:
    if mode == "all":
        return list(TARGETS)

    matched_targets: list[TargetSpec] = []
    for target in TARGETS:
        if any(
            path_matches_rule(changed_file, rule)
            for changed_file in changed_files
            for rule in target.trigger_rules
        ):
            matched_targets.append(target)
    return matched_targets


def run_command(repo_root: Path, spec: CommandSpec) -> CommandResult:
    completed = subprocess.run(
        [SHELL_PATH, "-lc", spec.command],
        cwd=repo_root / spec.cwd,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        spec=spec,
        return_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def render_output_block(title: str, content: str) -> str:
    if not content:
        return f"#### {title}\n\n_(empty)_"
    return f"#### {title}\n\n```text\n{content}\n```"


def build_report(
    changed_files: Sequence[str],
    selected_targets: Sequence[TargetSpec],
    results: Sequence[CommandResult],
    mode: str,
) -> str:
    lines: list[str] = ["# Local CI Report", ""]
    lines.append(f"- Mode: `{mode}`")
    if changed_files:
        lines.append("- Changed files:")
        lines.extend(f"  - `{path}`" for path in changed_files)
    else:
        lines.append("- Changed files: _(not used)_")

    if not selected_targets:
        lines.extend(("", "No local CI target matched the current file set."))
        return "\n".join(lines) + "\n"

    target_names = ", ".join(f"`{target.name}`" for target in selected_targets)
    lines.append(f"- Selected targets: {target_names}")
    lines.append("")

    for result in results:
        status = "PASS" if result.succeeded else "FAIL"
        lines.append(f"## {status} {result.spec.label}")
        lines.append(f"- Directory: `{result.spec.cwd}`")
        lines.append(f"- Command: `{result.spec.command}`")
        lines.append(render_output_block("stdout", result.stdout))
        lines.append(render_output_block("stderr", result.stderr))
        lines.append("")

    return "\n".join(lines)


def iter_commands(targets: Sequence[TargetSpec]) -> Iterable[CommandSpec]:
    for target in targets:
        yield from target.commands


def main() -> int:
    args = parse_args()
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    changed_files = list_changed_files(repo_root, args.mode)
    selected_targets = select_targets(changed_files, args.mode)
    results: list[CommandResult] = []

    for spec in iter_commands(selected_targets):
        result = run_command(repo_root, spec)
        results.append(result)
        if not result.succeeded:
            break

    report = build_report(changed_files, selected_targets, results, args.mode)
    print(report, end="")

    if not selected_targets:
        return 0
    return 0 if all(result.succeeded for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())

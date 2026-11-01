"""The desktop app's page renderer withholds the files the file tools withhold.

``frontend/shared/secret_paths.fixtures.json`` holds the rules sent for one home
folder, folded names and verdicts; the renderer's test reads the same file. When
the rules change, regenerate it from ``secret_path_patterns``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantaray_agents.tools.files import secret_paths

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[4]
        / "frontend/shared/secret_paths.fixtures.json"
    ).read_text()
)


@pytest.fixture(autouse=True)
def fixture_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_paths, "_home", lambda: Path(FIXTURES["home"]))


def test_the_fixture_holds_the_rules_the_render_tools_send() -> None:
    assert FIXTURES["rules"] == [
        {"withheld": rule.withheld, "excepted": rule.excepted}
        for tier in ("app_secret", "cli_credential")
        for rule in secret_paths.secret_path_patterns(tier)
    ]


@pytest.mark.parametrize(("text", "folded"), FIXTURES["folds"])
def test_the_fixture_folds_names_as_the_file_tools_do(text: str, folded: str) -> None:
    assert secret_paths._folded(text) == folded


@pytest.mark.parametrize(("path", "withheld"), FIXTURES["cases"])
def test_the_fixture_withholds_what_the_file_tools_withhold(
    path: str, withheld: bool
) -> None:
    reason = secret_paths.withheld_reason(Path(path), credentials_readable=False)
    assert (reason is not None) is withheld

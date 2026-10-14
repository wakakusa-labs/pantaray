"""glob and grep results read back as absolute paths, as a model joins them."""

from __future__ import annotations

from pathlib import Path

from pantaray_agents.schema.agent.base import JSONValue


def glob_paths(output: dict[str, JSONValue]) -> list[str]:
    base = Path(str(output["base_path"]))
    matches = output["matches"]
    assert isinstance(matches, list)
    return [str(base / str(path)) for path in matches]


def grep_matches(output: dict[str, JSONValue]) -> list[dict[str, JSONValue]]:
    """Each matching line with its file's absolute path, in result order."""
    base = Path(str(output["base_path"]))
    files = output["files"]
    assert isinstance(files, list)
    found: list[dict[str, JSONValue]] = []
    for entry in files:
        assert isinstance(entry, dict) and isinstance(entry["matches"], list)
        for match in entry["matches"]:
            assert isinstance(match, str)
            number, line = match.split(":", 1)
            found.append(
                {
                    "path": str(base / str(entry["path"])),
                    "line_number": int(number),
                    "line": line,
                }
            )
    return found

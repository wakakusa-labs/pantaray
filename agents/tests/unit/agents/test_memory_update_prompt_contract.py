"""The unified Memory prompt must describe the entry shape the host parses."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pantaray_agents.agents.memory_agent import (
    MEMORY_CONSOLIDATION_PROMPT_NAME,
    MemoryConsolidationRequest,
    MemoryUpdateAgent,
)
from pantaray_agents.agents.memory_agent import agent as memory_agent_module
from pantaray_agents.agents.memory_file_editor.runner import MemoryFileEditorRunInput
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    parse_agent_experience_markdown,
)
from pantaray_agents.utils.prompt_loader import PromptLoader, load_config, prompt_loader

_ENTRY_HEADER = "# Agent Experience"
_EXPERIENCE_ID = "11111111-2222-3333-4444-555555555555"
# One JSON value per canonical field line, in the order the prompt lists them.
_FIELD_VALUES = (
    f'"{_EXPERIENCE_ID}"',
    '{"kind":"user","key":null}',
    '"the repository pins its toolchain with uv"',
    '"ran pytest through the venv instead of uv run"',
    '"ineffective"',
    '"run uv run pytest so the pinned interpreter is used"',
    '"the suite failed on a missing dependency until uv run was used"',
    "[]",
)


def _system_instruction() -> str:
    return load_config(MemoryUpdateAgent.PROMPT_NAME).system_instruction or ""


def _canonical_field_lines() -> tuple[str, ...]:
    lines = _system_instruction().splitlines()
    start = lines.index(_ENTRY_HEADER) + 1
    return tuple(lines[start : start + len(_FIELD_VALUES)])


def test_prompt_entry_shape_is_the_shape_the_parser_accepts() -> None:
    field_lines = _canonical_field_lines()
    assert all(line.startswith("- ") for line in field_lines)

    entry = (
        "\n".join(
            (
                _ENTRY_HEADER,
                *(
                    f"{line.split(': ', 1)[0]}: {value}"
                    for line, value in zip(field_lines, _FIELD_VALUES, strict=True)
                ),
            )
        )
        + "\n"
    )

    content = parse_agent_experience_markdown(
        entry, expected_experience_id=_EXPERIENCE_ID
    )

    assert content.outcome == "ineffective"
    assert content.supersedes_experience_ids == ()


def test_prompt_reserves_evidence_and_the_index_for_the_host() -> None:
    normalized = " ".join(_system_instruction().split())

    assert "agent_experience/entries/<experience_id>.md" in normalized
    assert (
        "Write no Evidence line and never edit `agent_experience/index.md`"
        in normalized
    )
    assert "Agent Experience evidence refs are host-owned" in normalized


def test_prompt_names_the_zone_of_its_local_times() -> None:
    assert "{local_time_note}" in load_config(MemoryUpdateAgent.PROMPT_NAME).prompt


async def _consolidation_system_instruction(
    monkeypatch: pytest.MonkeyPatch, loader: PromptLoader
) -> str:
    sent: list[str] = []

    async def capture(run_input: MemoryFileEditorRunInput) -> tuple[str, ...]:
        sent.append(run_input.system_instruction)
        return ()

    monkeypatch.setattr(memory_agent_module, "prompt_loader", loader)
    monkeypatch.setattr(memory_agent_module, "run_memory_file_editor", capture)
    await MemoryUpdateAgent(client=object(), llm_config={}).consolidate(
        MemoryConsolidationRequest(
            run_id="run",
            prompt_name=MEMORY_CONSOLIDATION_PROMPT_NAME,
            path="facts/project.md",
            content="# Project",
            limit_chars=10_000,
            today="2026-10-12",
        ),
        tool_definitions=(),
        tool_result_directory_fd=0,
    )
    return sent[0]


async def test_consolidation_uses_the_memory_update_facts_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = memory_agent_module.what_facts_hold(_system_instruction())
    sent = await _consolidation_system_instruction(monkeypatch, prompt_loader)

    assert section.startswith("## What facts hold\n")
    assert "Details that have a home of their own" in section
    assert section in sent
    assert "{what_facts_hold}" not in sent


async def test_editing_the_facts_definition_changes_the_consolidation_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (MemoryUpdateAgent.PROMPT_NAME, MEMORY_CONSOLIDATION_PROMPT_NAME):
        shutil.copy(prompt_loader.get_full_path(name), tmp_path / f"{name}.yaml")
    update_file = tmp_path / f"{MemoryUpdateAgent.PROMPT_NAME}.yaml"
    update_file.write_text(
        update_file.read_text("utf-8").replace(
            "## What facts hold\n", "## What facts hold\n  Edited definition.\n"
        ),
        "utf-8",
    )

    sent = await _consolidation_system_instruction(monkeypatch, PromptLoader(tmp_path))

    assert "## What facts hold\nEdited definition.\n" in sent

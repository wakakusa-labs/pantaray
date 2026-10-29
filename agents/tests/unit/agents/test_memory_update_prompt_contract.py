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
from pantaray_agents.agents.memory_file_editor.runner import (
    MemoryFileEditorRunInput,
    run_memory_file_editor,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    parse_agent_experience_markdown,
)
from pantaray_agents.local_runtime.runtime.help_outcomes import DueOutcome
from pantaray_agents.tools.memory.sql_tool import MemorySqlSession
from pantaray_agents.utils.prompt_loader import PromptLoader, load_config, prompt_loader
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock

from .test_memory_file_editor_runner import _read_tool, _reply, _run_input, _scripted

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
    monkeypatch: pytest.MonkeyPatch, loader: PromptLoader, tool_results_path: Path
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
        tool_results_path=tool_results_path,
        db_path=tool_results_path / "runtime.sqlite3",
    )
    return sent[0]


async def test_consolidation_uses_the_memory_update_facts_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    section = memory_agent_module.what_facts_hold(_system_instruction())
    sent = await _consolidation_system_instruction(monkeypatch, prompt_loader, tmp_path)

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

    sent = await _consolidation_system_instruction(
        monkeypatch, PromptLoader(tmp_path), tmp_path
    )

    assert "## What facts hold\nEdited definition.\n" in sent


async def test_the_look_back_goes_on_behind_the_update_to_another_completed(
    tmp_path: Path,
) -> None:
    send, requests = _scripted(
        _reply(("read_memory", {"path": "facts.md"})),
        _reply(("completed", {})),
        _reply(("memory_sql", {"sql": "SELECT 1"})),
        _reply(("completed", {})),
    )
    conversation = await run_memory_file_editor(
        _run_input(tmp_path, send, tools=(_read_tool(),))
    )
    due = DueOutcome(
        kind="action",
        item_id="action-1",
        checkpoint_days=7,
        delivered_at="2026-02-20T09:00:00.000Z",
        expectation="Draft the review checklist.",
        source_suggestion_id="s-1",
    )

    await MemoryUpdateAgent(client=object(), llm_config={}).look_back_on_outcomes(
        conversation,
        (due,),
        tool_definitions=(
            MemorySqlSession(
                db_path=tmp_path / "runtime.sqlite3",
                busy_timeout_ms=1_000,
                user_id="user-1",
            ).definition(),
        ),
        now="2026-03-01 12:00 +00:00",
    )

    update, look_back = requests[1], requests[2]
    assert (look_back.prompt, look_back.system_instruction) == (
        update.prompt,
        update.system_instruction,
    )
    assert look_back.conversation[: len(update.conversation)] == update.conversation
    completed, answer, instruction = look_back.conversation[len(update.conversation) :]
    assert isinstance(completed, LlmTurnAssistantItem)
    assert isinstance(answer, LlmTurnToolResultItem)
    assert answer.call_id == completed.calls[0].call_id
    assert answer.output == {"status": "accepted"}
    assert isinstance(instruction, LlmTurnUserItem)
    (block,) = instruction.content
    assert isinstance(block, LlmInputTextBlock)
    text = " ".join(block.text.split())
    assert "What Pantaray's help changed" in text
    assert '"action_id": "action-1"' in text
    assert '"source_suggestion_id": "s-1"' in text
    assert {tool.name for tool in look_back.tools} >= {"memory_sql", "completed"}
    # It ended on the second `completed`, with no turn left unsent.
    assert len(requests) == 4

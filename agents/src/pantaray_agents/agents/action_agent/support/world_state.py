"""The parts of the Executing head that can change while an Action lasts.

The head is the first item of every Executing request, so rewriting it re-bills
the whole conversation behind it: the first call after each new message read
only the fixed prefix from the prompt cache. Every run re-reads the workspace
and the Pantaray-wide AGENTS.md, and every turn re-reads the linkable memory and
the time, so the head instead shows them as they were on the Action's first turn
and is never rewritten. (Memory and its source coverage are read once per Action
and are not sections here.) A turn that
reads a different version appends it to its turn context, saying it replaces the
earlier one, and a turn that reads the same version appends nothing -- the
world-state pattern Codex uses for its own instructions and environment.

What the conversation shows is read off the rows that sent it: the head's
values, then each update a still-replayed turn context carried, later ones
replacing earlier ones. So an update is sent once, and sent again only when a
rebuilt window drops the turn that carried it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pantaray_agents.agents.action_agent.runtime.state import HistoryEntry
from pantaray_agents.agents.action_agent.support.conversation_projection import (
    replays_turn_context,
)

# Each section is replaced whole, in the order the head shows them.
WORLD_STATE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspace", ("workspace_path_contract", "workspace_context_prompt")),
    ("agents_md", ("agents_md_instructions",)),
    ("linkable_persisted_memory", ("linkable_persisted_memory",)),
    ("current_time", ("current_time",)),
)


@dataclass(frozen=True, slots=True)
class WorldStateUpdate:
    # What this turn's context carries after its heading.
    text: str
    # The field values it sends, recorded on the THINK row that sends them.
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class WorldState:
    """The sections the head shows, at the head's values and at this run's."""

    head: Mapping[str, str]
    current: Mapping[str, str]
    # Looks up an update template by section, or ``<section>_removed`` when
    # the section is now empty.
    template: Callable[[str], str]

    def update_since(
        self, entries: Sequence[HistoryEntry], *, omit_before_step_number: int
    ) -> WorldStateUpdate | None:
        """What this turn has to append so the conversation shows ``current``."""

        shown = dict(self.head)
        for entry in entries:
            if replays_turn_context(entry, omit_before_step_number):
                shown.update(entry.get("world_state", {}))
        texts: list[str] = []
        values: dict[str, str] = {}
        for section, fields in WORLD_STATE_SECTIONS:
            if not all(field in self.head for field in fields):
                continue  # the head does not show this section
            current = {field: self.current[field] for field in fields}
            if all(shown[field] == current[field] for field in fields):
                continue
            key = section if any(current.values()) else f"{section}_removed"
            texts.append(self.template(key).format(**current))
            values |= current
        if not texts:
            return None
        return WorldStateUpdate(text="\n\n".join(texts), values=values)


def world_state_fields(head_fields: Mapping[str, str]) -> tuple[str, ...]:
    """The world-state fields a head with these fields shows."""

    return tuple(
        field
        for _, fields in WORLD_STATE_SECTIONS
        if all(field in head_fields for field in fields)
        for field in fields
    )


__all__ = [
    "WORLD_STATE_SECTIONS",
    "WorldState",
    "WorldStateUpdate",
    "world_state_fields",
]

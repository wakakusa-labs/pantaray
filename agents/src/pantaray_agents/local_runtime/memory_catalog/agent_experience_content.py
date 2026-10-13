from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
)

from .errors import MemoryCatalogIntegrityError

AgentExperienceScopeKind = Literal["user", "workspace", "repository"]
AgentExperienceOutcome = Literal["effective", "ineffective"]

AGENT_EXPERIENCE_ROOT = "agent_experience"
AGENT_EXPERIENCE_INDEX_PATH = f"{AGENT_EXPERIENCE_ROOT}/index.md"
AGENT_EXPERIENCE_ENTRIES_ROOT = f"{AGENT_EXPERIENCE_ROOT}/entries"
_HEADER = "# Agent Experience"
_INDEX_HEADER = "# Agent Experience Index"
_FIELD_PREFIXES = {
    "experience_id": "- Experience ID: ",
    "scope": "- Scope: ",
    "applies_when": "- Applies when: ",
    "observed_approach": "- Observed approach: ",
    "outcome": "- Outcome: ",
    "next_time_rule": "- Next-time rule: ",
    "observed_result": "- Observed result: ",
    "supersedes_experience_ids": "- Supersedes experience: ",
}
_EVIDENCE_PREFIX = "- Evidence: Action "
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EXPERIENCE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,127}$")
_MARKDOWN_INLINE_SPECIALS = frozenset(r"\`*_{}[]<>()#+-.!|")


@dataclass(frozen=True, slots=True)
class AgentExperienceEvidence:
    action_id: str
    local_ref_id: str


class AgentExperienceScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: AgentExperienceScopeKind
    key: str | None

    @model_validator(mode="after")
    def validate_scope_key(self) -> AgentExperienceScope:
        if self.kind == "user" and self.key is not None:
            raise ValueError("user-scoped experience must not have a scope key")
        if self.kind != "user" and not _is_nonblank(self.key):
            raise ValueError("workspace/repository experience requires a scope key")
        return self


class AgentExperienceContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    experience_id: str = Field(min_length=8, max_length=128)
    scope: AgentExperienceScope
    applies_when: str = Field(min_length=1, max_length=1_000)
    observed_approach: str = Field(min_length=1, max_length=4_000)
    outcome: AgentExperienceOutcome
    next_time_rule: str = Field(min_length=1, max_length=4_000)
    observed_result: str = Field(min_length=1, max_length=2_000)
    # The entries this one replaces; each left the tree when this one was written.
    supersedes_experience_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_content(self) -> AgentExperienceContent:
        _require_experience_id(self.experience_id)
        for field_name in (
            "applies_when",
            "observed_approach",
            "next_time_rule",
            "observed_result",
        ):
            _require_nonblank(getattr(self, field_name), field=field_name)
        if self.applies_when.splitlines() != [self.applies_when]:
            raise ValueError("applies_when must be exactly one line")
        for superseded in self.supersedes_experience_ids:
            _require_experience_id(superseded)
        if self.experience_id in self.supersedes_experience_ids:
            raise ValueError("an experience cannot supersede itself")
        if len(set(self.supersedes_experience_ids)) != len(
            self.supersedes_experience_ids
        ):
            raise ValueError("an experience names a superseded ID more than once")
        return self


def experience_entry_path(experience_id: str) -> str:
    _require_experience_id(experience_id)
    return f"{AGENT_EXPERIENCE_ENTRIES_ROOT}/{experience_id}.md"


def experience_id_from_path(source_path: str) -> str:
    path = PurePosixPath(source_path)
    if path.parent.as_posix() != AGENT_EXPERIENCE_ENTRIES_ROOT or path.suffix != ".md":
        raise MemoryCatalogIntegrityError("Agent Experience entry path is invalid")
    experience_id = path.stem
    try:
        _require_experience_id(experience_id)
    except ValueError as exc:
        raise MemoryCatalogIntegrityError(
            "Agent Experience entry ID is invalid"
        ) from exc
    return experience_id


def render_agent_experience_markdown(content: AgentExperienceContent) -> str:
    values: dict[str, object] = content.model_dump(mode="json")
    lines = [_HEADER]
    lines.extend(
        prefix + _json_value(values[field_name])
        for field_name, prefix in _FIELD_PREFIXES.items()
    )
    return "\n".join(lines) + "\n"


def parse_agent_experience_markdown(
    markdown: str,
    *,
    expected_experience_id: str,
) -> AgentExperienceContent:
    """Parse one entry file.

    Action evidence lines are optional: a lesson drawn from a short-term
    Insight or an Activity Summary has no Action to cite, and its provenance is
    the Memory run that published it.
    """

    lines = markdown.splitlines()
    field_count = len(_FIELD_PREFIXES)
    if not lines or lines[0] != _HEADER or len(lines) < field_count + 1:
        raise MemoryCatalogIntegrityError("Agent Experience entry is incomplete")
    values: dict[str, object] = {}
    for index, (field_name, prefix) in enumerate(_FIELD_PREFIXES.items(), start=1):
        line = lines[index]
        if not line.startswith(prefix):
            raise MemoryCatalogIntegrityError(
                f"Agent Experience field order is invalid: {field_name}"
            )
        values[field_name] = _parse_json_value(
            line.removeprefix(prefix), field_name=field_name
        )
    supersedes = values["supersedes_experience_ids"]
    legacy_line: str | None = None
    if supersedes is None or isinstance(supersedes, str):
        # Entries written before the field became a list hold `null` or one ID,
        # and a published revision is never rewritten.
        legacy_line = _FIELD_PREFIXES["supersedes_experience_ids"] + _json_value(
            supersedes
        )
        supersedes = [] if supersedes is None else [supersedes]
    if isinstance(supersedes, list):
        # A strict model takes a tuple field only from a tuple.
        values["supersedes_experience_ids"] = tuple(supersedes)
    try:
        content = AgentExperienceContent.model_validate(values)
    except ValidationError as exc:
        # The writing agent fixes the entry from this message, so name each field.
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'entry'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise MemoryCatalogIntegrityError(
            f"Agent Experience content is invalid: {problems}"
        ) from exc
    if content.experience_id != expected_experience_id:
        raise MemoryCatalogIntegrityError("Agent Experience path and ID differ")
    for line in lines[field_count + 1 :]:
        _validate_evidence_line(line)
    canonical_prefix = render_agent_experience_markdown(content).splitlines()
    for field_name, line, canonical in zip(
        _FIELD_PREFIXES, lines[1 : field_count + 1], canonical_prefix[1:], strict=True
    ):
        if field_name == "supersedes_experience_ids" and legacy_line is not None:
            canonical = legacy_line
        if line != canonical:
            raise MemoryCatalogIntegrityError(
                "Agent Experience field must be compact JSON in the documented "
                f"key order: {field_name}"
            )
    return content


def render_agent_experience_index(
    entries: tuple[AgentExperienceContent, ...],
) -> str:
    lines = [
        _INDEX_HEADER,
        "",
        "This index is generated from the active entry files.",
    ]
    for entry in sorted(entries, key=lambda item: item.experience_id):
        path = experience_entry_path(entry.experience_id).removeprefix(
            f"{AGENT_EXPERIENCE_ROOT}/"
        )
        lines.append(
            f"- [{entry.experience_id}]({path}) — "
            f"{_escape_markdown_inline(entry.applies_when)}"
        )
    return "\n".join(lines) + "\n"


def render_action_evidence_anchor(action_id: str) -> str:
    _require_nonblank(action_id, field="action_id")
    return f"{_EVIDENCE_PREFIX}{action_id}"


def is_action_evidence_line(line: str) -> bool:
    return line.startswith(_EVIDENCE_PREFIX)


def initial_agent_experience_documents() -> tuple[tuple[str, str], ...]:
    return ((AGENT_EXPERIENCE_INDEX_PATH, render_agent_experience_index(())),)


def agent_experience_evidence_text(markdown: str) -> str:
    """Return the host-owned suffix verbatim, including its line endings."""
    return "".join(markdown.splitlines(keepends=True)[len(_FIELD_PREFIXES) + 1 :])


def parse_agent_experience_evidence(
    markdown: str,
) -> tuple[AgentExperienceEvidence, ...]:
    return tuple(
        _parse_evidence_line(line)
        for line in agent_experience_evidence_text(markdown).splitlines()
    )


def _validate_evidence_line(line: str) -> None:
    _parse_evidence_line(line)


def _parse_evidence_line(line: str) -> AgentExperienceEvidence:
    if not line.startswith(_EVIDENCE_PREFIX):
        raise MemoryCatalogIntegrityError("Agent Experience evidence is invalid")
    occurrences = extract_markdown_references(line)
    if (
        len(occurrences) != 1
        or occurrences[0].note != "source action"
        or not occurrences[0].local_ref_id.startswith("ref_")
    ):
        raise MemoryCatalogIntegrityError("Agent Experience evidence ref is invalid")
    action_id = occurrences[0].anchor_text.removeprefix(_EVIDENCE_PREFIX).strip()
    if not action_id or occurrences[0].anchor_text != f"{_EVIDENCE_PREFIX}{action_id}":
        raise MemoryCatalogIntegrityError("Agent Experience evidence Action is invalid")
    return AgentExperienceEvidence(
        action_id=action_id,
        local_ref_id=occurrences[0].local_ref_id,
    )


def _escape_markdown_inline(value: str) -> str:
    return "".join(
        f"\\{character}" if character in _MARKDOWN_INLINE_SPECIALS else character
        for character in value
    )


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json_value(raw_value: str, *, field_name: str) -> object:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise MemoryCatalogIntegrityError(
            f"Agent Experience field is not valid JSON: {field_name}"
        ) from exc


def _is_nonblank(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_experience_id(value: str) -> None:
    if not _EXPERIENCE_ID_RE.fullmatch(value):
        raise ValueError("experience_id contains unsupported characters")


def _require_nonblank(value: str, *, field: str) -> None:
    if not value.strip() or _CONTROL_CHAR_RE.search(value):
        raise ValueError(f"{field} must be nonblank text without control characters")


__all__ = [
    "AGENT_EXPERIENCE_ENTRIES_ROOT",
    "AGENT_EXPERIENCE_INDEX_PATH",
    "AGENT_EXPERIENCE_ROOT",
    "AgentExperienceContent",
    "AgentExperienceEvidence",
    "AgentExperienceScope",
    "agent_experience_evidence_text",
    "experience_entry_path",
    "experience_id_from_path",
    "initial_agent_experience_documents",
    "is_action_evidence_line",
    "parse_agent_experience_markdown",
    "parse_agent_experience_evidence",
    "render_action_evidence_anchor",
    "render_agent_experience_index",
    "render_agent_experience_markdown",
]

from __future__ import annotations

import json
from typing import Literal

import pytest

from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    AgentExperienceContent,
    AgentExperienceScope,
    experience_entry_path,
    parse_agent_experience_markdown,
    render_agent_experience_index,
    render_agent_experience_markdown,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_delta import (
    rebuild_agent_experience_index,
    validate_agent_experience_tree_change,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument

NEW_ID = "3f861ab4-7b3c-5d90-b520-3424da5bca75"
SECOND_ID = "9a9b9c9d-1111-5222-8333-123456789abc"
OLD_ID = "1a1b1c1d-1111-5222-8333-123456789abc"
SECOND_OLD_ID = "2a2b2c2d-1111-5222-8333-123456789abc"
ALLOWED_NEW_IDS = (NEW_ID, SECOND_ID)


def _content(
    experience_id: str,
    *,
    outcome: Literal["effective", "ineffective"] = "effective",
    supersedes: tuple[str, ...] = (),
) -> AgentExperienceContent:
    observed_approach = "Changed the shared boundary after tracing every caller."
    observed_result = "One root fix restored every caller."
    if outcome == "ineffective":
        observed_approach = "Changed the shared boundary before tracing every caller."
        observed_result = "The change repeated the failure in an untraced caller."
    return AgentExperienceContent(
        experience_id=experience_id,
        scope=AgentExperienceScope(kind="user", key=None),
        applies_when="A shared terminal boundary fails.",
        observed_approach=observed_approach,
        outcome=outcome,
        next_time_rule="Trace every caller before changing the shared boundary.",
        observed_result=observed_result,
        supersedes_experience_ids=supersedes,
    )


def _tree(*entries: MemoryDocument) -> tuple[MemoryDocument, ...]:
    index = MemoryDocument(AGENT_EXPERIENCE_INDEX_PATH, "")
    return rebuild_agent_experience_index((index, *entries))


def _entry(content: AgentExperienceContent, *, evidence: bool) -> MemoryDocument:
    markdown = render_agent_experience_markdown(content)
    if evidence:
        markdown += (
            "- Evidence: Action action-old "
            '[[ref:ref_action_old note:"source action"]]\n'
        )
    return MemoryDocument(experience_entry_path(content.experience_id), markdown)


@pytest.mark.parametrize("outcome", ("effective", "ineffective"))
def test_canonical_markdown_round_trips_both_outcomes(
    outcome: Literal["effective", "ineffective"],
) -> None:
    content = _content(NEW_ID, outcome=outcome)

    markdown = render_agent_experience_markdown(content)

    assert markdown == (
        "# Agent Experience\n"
        f'- Experience ID: "{NEW_ID}"\n'
        '- Scope: {"kind":"user","key":null}\n'
        '- Applies when: "A shared terminal boundary fails."\n'
        f"- Observed approach: {json.dumps(content.observed_approach)}\n"
        f'- Outcome: "{outcome}"\n'
        '- Next-time rule: "Trace every caller before changing the shared boundary."\n'
        f"- Observed result: {json.dumps(content.observed_result)}\n"
        "- Supersedes experience: []\n"
    )
    assert (
        parse_agent_experience_markdown(
            markdown,
            expected_experience_id=NEW_ID,
        )
        == content
    )


def test_parser_rejects_legacy_decision_and_verified_shape() -> None:
    legacy_markdown = (
        "# Agent Experience\n"
        f'- Experience ID: "{NEW_ID}"\n'
        '- Scope: {"kind":"user","key":null}\n'
        '- Applies when: "A shared terminal boundary fails."\n'
        '- Decision: "Trace every caller before changing the shared boundary."\n'
        '- Outcome: "verified"\n'
        '- Observed result: "One root fix restored every caller."\n'
        "- Supersedes experience: null\n"
    )

    with pytest.raises(MemoryCatalogIntegrityError, match="entry is incomplete"):
        parse_agent_experience_markdown(
            legacy_markdown,
            expected_experience_id=NEW_ID,
        )


@pytest.mark.parametrize(
    "field_name",
    ("observed_approach", "next_time_rule", "observed_result"),
)
def test_reusable_lesson_fields_must_be_nonblank(field_name: str) -> None:
    with pytest.raises(ValueError, match=f"{field_name} must be nonblank"):
        AgentExperienceContent.model_validate(
            {**_content(NEW_ID).model_dump(), field_name: "   "}
        )


def test_tree_change_records_several_lessons_of_one_run() -> None:
    base = _tree()
    published = _tree(
        _entry(_content(NEW_ID), evidence=False),
        _entry(_content(SECOND_ID), evidence=False),
    )

    change = validate_agent_experience_tree_change(
        base_documents=base,
        published_documents=published,
        allowed_new_experience_ids=ALLOWED_NEW_IDS,
    )

    assert change.added == tuple(sorted(ALLOWED_NEW_IDS))
    assert change.modified == change.removed == ()


def test_tree_change_rejects_an_entry_id_the_run_does_not_own() -> None:
    published = _tree(_entry(_content(OLD_ID), evidence=False))

    with pytest.raises(MemoryCatalogIntegrityError, match="does not own"):
        validate_agent_experience_tree_change(
            base_documents=_tree(),
            published_documents=published,
            allowed_new_experience_ids=ALLOWED_NEW_IDS,
        )


def test_tree_change_supersede_must_remove_the_entry_it_replaces() -> None:
    old = _entry(_content(OLD_ID), evidence=True)
    base = _tree(old)
    replacement = _entry(_content(NEW_ID, supersedes=(OLD_ID,)), evidence=False)

    change = validate_agent_experience_tree_change(
        base_documents=base,
        published_documents=_tree(replacement),
        allowed_new_experience_ids=ALLOWED_NEW_IDS,
    )

    assert (change.added, change.removed) == ((NEW_ID,), (OLD_ID,))
    with pytest.raises(MemoryCatalogIntegrityError, match="still active"):
        validate_agent_experience_tree_change(
            base_documents=base,
            published_documents=_tree(old, replacement),
            allowed_new_experience_ids=ALLOWED_NEW_IDS,
        )


@pytest.mark.parametrize(
    ("line", "superseded"),
    (("null", ()), (f'"{OLD_ID}"', (OLD_ID,)), (f'["{OLD_ID}"]', (OLD_ID,))),
    ids=("legacy_null", "legacy_one_id", "list"),
)
def test_an_entry_written_before_supersedes_became_a_list_still_parses(
    line: str, superseded: tuple[str, ...]
) -> None:
    lines = render_agent_experience_markdown(_content(NEW_ID)).splitlines()
    markdown = "\n".join((*lines[:-1], f"- Supersedes experience: {line}")) + "\n"

    content = parse_agent_experience_markdown(markdown, expected_experience_id=NEW_ID)

    assert content.supersedes_experience_ids == superseded


def test_one_entry_may_replace_several_that_the_change_removes() -> None:
    base = _tree(
        _entry(_content(OLD_ID), evidence=False),
        _entry(_content(SECOND_OLD_ID), evidence=False),
    )
    merged = _entry(
        _content(NEW_ID, supersedes=(OLD_ID, SECOND_OLD_ID)), evidence=False
    )

    change = validate_agent_experience_tree_change(
        base_documents=base,
        published_documents=_tree(merged),
        allowed_new_experience_ids=ALLOWED_NEW_IDS,
    )

    assert (change.added, change.removed) == ((NEW_ID,), (OLD_ID, SECOND_OLD_ID))


def test_an_entry_cannot_claim_to_replace_an_id_that_was_not_in_the_tree() -> None:
    base = _tree(_entry(_content(OLD_ID), evidence=False))
    merged = _entry(
        _content(NEW_ID, supersedes=(OLD_ID, SECOND_OLD_ID)), evidence=False
    )

    with pytest.raises(MemoryCatalogIntegrityError, match=SECOND_OLD_ID):
        validate_agent_experience_tree_change(
            base_documents=base,
            published_documents=_tree(merged),
            allowed_new_experience_ids=ALLOWED_NEW_IDS,
        )


@pytest.mark.parametrize(
    ("supersedes", "message"),
    (((NEW_ID,), "cannot supersede itself"), ((OLD_ID, OLD_ID), "more than once")),
    ids=("itself", "repeated"),
)
def test_supersedes_names_other_entries_once_each(
    supersedes: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _content(NEW_ID, supersedes=supersedes)


def test_tree_change_rejects_deleting_an_entry_nothing_replaces() -> None:
    base = _tree(_entry(_content(OLD_ID), evidence=True))

    with pytest.raises(MemoryCatalogIntegrityError, match="without a superseding"):
        validate_agent_experience_tree_change(
            base_documents=base,
            published_documents=_tree(),
            allowed_new_experience_ids=ALLOWED_NEW_IDS,
        )


def test_applies_when_must_be_one_line() -> None:
    with pytest.raises(ValueError, match="exactly one line"):
        AgentExperienceContent.model_validate(
            {
                **_content(NEW_ID).model_dump(),
                "applies_when": "First line.\nSecond line.",
            }
        )


def test_index_escapes_untrusted_applies_when_as_markdown_text() -> None:
    entry = AgentExperienceContent.model_validate(
        {
            **_content(NEW_ID).model_dump(),
            "applies_when": "[label](https://invalid.example) <tag> *emphasis*",
        }
    )

    index = render_agent_experience_index((entry,))

    assert r"\[label\]\(https://invalid\.example\)" in index
    assert r"\<tag\>" in index
    assert r"\*emphasis\*" in index

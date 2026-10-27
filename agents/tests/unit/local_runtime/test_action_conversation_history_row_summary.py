import pytest

from pantaray_agents.local_runtime.action_conversation.history_row_summary import (
    answer_status_line,
    artifact_names,
    first_line,
    linked_document_names,
)
from pantaray_agents.schema.conversation_history import (
    CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\n  \n# **Done** with `it`\nmore", "Done with it"),
        ("> quoted", "quoted"),
        ("- item", "item"),
        ("12. step", "step"),
        ("__bold__ and [the report](pantaray-file:///a/b.md)", "bold and the report"),
        ("See pantaray-file:///a/%E8%A8%88%E7%94%BB.md.", "See 計画.md."),
        ("   ", ""),
    ],
)
def test_first_line_is_plain_text(text: str, expected: str) -> None:
    assert first_line(text) == expected


def test_status_line_fits_one_row_and_omits_an_empty_answer() -> None:
    line = answer_status_line("x" * 500)
    assert line is not None
    assert len(line) == CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS
    assert line.endswith("…")
    assert answer_status_line(
        "y" * CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS
    ) == ("y" * CONVERSATION_HISTORY_STATUS_LINE_MAX_CODEPOINTS)
    assert answer_status_line("**\n\n") is None


def test_linked_documents_follow_the_renderer_file_chips() -> None:
    answers = (
        "See [the notes](pantaray-file:///Users/me/notes.md), "
        "pantaray-file:///Users/me/My%20Deck.pdf and pantaray-file:///Users/me/app.py.",
        "「pantaray-file:///Users/me/表.xlsx」と pantaray-file:///Users/me/(draft).docx) "
        "pantaray-file:///Users/me/.env pantaray-file:///Users/me/Makefile "
        "pantaray-file:///Users/me/notes.md",
    )
    assert linked_document_names(answers) == (
        "notes.md",
        "My Deck.pdf",
        "表.xlsx",
        "(draft).docx",
    )
    assert linked_document_names(("No files here.",)) == ()


def test_suggestion_artifact_names_are_their_last_path_part() -> None:
    assert artifact_names(["drafts/outline.md", "notes.txt", "dir/ "]) == (
        "outline.md",
        "notes.txt",
    )

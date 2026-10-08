from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import zip_longest

from .fragment_visibility import (
    MEMORY_FRAGMENT_COLUMNS,
    VISIBLE_FRAGMENT_JOINS,
    VISIBLE_FRAGMENTS_FROM_NODES,
    visible_fragment_predicate,
)
from .models import MemorySource

# A word, or a "#" issue number. The "#" is kept only on numbers of one or two
# digits, which are otherwise too short to tell from any other number; those are
# matched as whole words so #7 finds neither #77 nor #7a.
_QUERY_TOKEN_RE = re.compile(r"#?[^\W_]+", re.UNICODE)
_SHORT_ISSUE_NUMBER_RE = re.compile(r"#[0-9]{1,2}")
# memory_fragments_fts is tokenized with trigram, so a MATCH term is a substring
# of three characters or more and nothing shorter resolves through the index.
TRIGRAM_LENGTH = 3
# One OR-ed window costs about 0.8 ms on a 24k-fragment store holding 15 MB of
# text (sqlite 3.50, measured: 16 windows 20 ms, 48 windows 38 ms, 128 windows
# 138 ms), so this holds the index lane under ~40 ms there. It covers a query of
# 50 characters whole; a longer one is a paragraph rather than a recall phrase,
# and its words are still searched verbatim, so the cap bounds only how much
# partial recall a single query buys.
MAX_QUERY_TRIGRAM_WINDOWS = 48


def search_lexical_fragments(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    query: str,
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
    candidate_limit: int,
) -> tuple[sqlite3.Row, ...]:
    terms = _lexical_terms(query)
    visibility, visibility_parameters = visible_fragment_predicate(
        user_id=user_id,
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    return _interleave_rows(
        _indexed_lexical_rows(
            connection=connection,
            terms=terms.indexed,
            visibility=visibility,
            visibility_parameters=visibility_parameters,
            candidate_limit=candidate_limit,
        ),
        _scanned_lexical_rows(
            connection=connection,
            substrings=terms.scanned,
            whole_words=terms.whole_words,
            visibility=visibility,
            visibility_parameters=visibility_parameters,
            candidate_limit=candidate_limit,
        ),
        limit=candidate_limit,
    )


def lexical_query_notes(query: str) -> tuple[str, ...]:
    """What the word matchers left out of the query, for the caller to read."""

    terms = _lexical_terms(query)
    notes: list[str] = []
    if terms.skipped:
        notes.append(
            f"Not matched by words: {', '.join(terms.skipped)}. Single characters "
            "are never matched by words, and two-letter ASCII words only when "
            "written in capitals (PR, UI) or with a digit (#7, v2)."
        )
    if terms.windows_cut:
        notes.append(
            "Partial matching inside words written without spaces stopped after "
            f"the first {MAX_QUERY_TRIGRAM_WINDOWS} three-character pieces; those "
            "words were still matched whole. Search the rest in a shorter query."
        )
    return tuple(notes)


@dataclass(frozen=True, slots=True)
class _LexicalTerms:
    indexed: tuple[str, ...]
    scanned: tuple[str, ...]
    whole_words: tuple[str, ...]
    skipped: tuple[str, ...]
    windows_cut: bool


def _lexical_terms(query: str) -> _LexicalTerms:
    """Sort the query's words into the matcher each one can reach.

    The index resolves a substring of three characters or more, so a word that
    long is matched through it. Japanese is written without spaces, so such a
    word is often a whole clause that no fragment holds verbatim; a word holding
    a non-ASCII character is therefore also expanded into its three-character
    windows, and bm25 orders a fragment by how many of them it holds.

    Below three characters the index is blind, and a full scan is worth its cost
    only where words are written that short: 申請 and 見積 are words. ASCII "ai"
    or "db" as a substring is a piece of mail, detail and training, and "is" or
    "of" is in every English sentence, so a two-character ASCII word is matched
    only whole, and only when it is written as an acronym (PR, UI, matched as
    written) or holds a digit (#7, v2). A single character of any script
    matches nearly every fragment, so it reaches no matcher; those words are
    reported as skipped.
    """
    indexed: list[str] = []
    windows: list[str] = []
    scanned: list[str] = []
    whole_words: list[str] = []
    skipped: list[str] = []
    for token in dict.fromkeys(_QUERY_TOKEN_RE.findall(query)):
        if _SHORT_ISSUE_NUMBER_RE.fullmatch(token):
            whole_words.append(token)
            continue
        if token.startswith("#"):
            token = token[1:]
        word = token.casefold()
        if len(word) >= TRIGRAM_LENGTH:
            indexed.append(word)
            if not word.isascii():
                windows.extend(
                    word[start : start + TRIGRAM_LENGTH]
                    for start in range(len(word) - TRIGRAM_LENGTH + 1)
                )
        elif len(word) > 1 and not word.isascii():
            scanned.append(word)
        elif len(word) > 1 and (token.isupper() or any(c.isdigit() for c in token)):
            whole_words.append(token)
        else:
            skipped.append(token)
    return _LexicalTerms(
        indexed=tuple(dict.fromkeys((*indexed, *windows[:MAX_QUERY_TRIGRAM_WINDOWS]))),
        scanned=tuple(dict.fromkeys(scanned)),
        whole_words=tuple(dict.fromkeys(whole_words)),
        skipped=tuple(dict.fromkeys(skipped)),
        windows_cut=len(windows) > MAX_QUERY_TRIGRAM_WINDOWS,
    )


def _whole_word_glob(word: str) -> str:
    """A GLOB pattern matching `word` with no ASCII letter or digit beside it.

    An acronym is matched as written; letters next to a digit match either case.
    The scan pads the content with a space, so a word at either end still has
    a neighbor to test, and a leading "#" is its own boundary.
    """
    letters = (
        "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in word)
        if any(c.isdigit() for c in word)
        else word
    )
    before = "" if word.startswith("#") else "[^0-9A-Za-z]"
    return f"*{before}{letters}[^0-9A-Za-z]*"


def _indexed_lexical_rows(
    *,
    connection: sqlite3.Connection,
    terms: tuple[str, ...],
    visibility: str,
    visibility_parameters: tuple[object, ...],
    candidate_limit: int,
) -> tuple[sqlite3.Row, ...]:
    if not terms:
        return ()
    # _QUERY_TOKEN_RE keeps word characters only here, so a term holds no FTS5
    # operator and quoting it is enough to keep it a literal phrase.
    return tuple(
        connection.execute(
            f"""
            SELECT {MEMORY_FRAGMENT_COLUMNS},
                   bm25(memory_fragments_fts) AS relevance
            FROM memory_fragments_fts
            JOIN memory_fragments AS fragments
              ON fragments.rowid = memory_fragments_fts.rowid
            {VISIBLE_FRAGMENT_JOINS}
            WHERE memory_fragments_fts MATCH ?
              AND {visibility}
            ORDER BY relevance, nodes.updated_at DESC, fragments.fragment_id
            LIMIT ?
            """,
            (
                " OR ".join(f'"{term}"' for term in terms),
                *visibility_parameters,
                candidate_limit,
            ),
        ).fetchall()
    )


def _scanned_lexical_rows(
    *,
    connection: sqlite3.Connection,
    substrings: tuple[str, ...],
    whole_words: tuple[str, ...],
    visibility: str,
    visibility_parameters: tuple[object, ...],
    candidate_limit: int,
) -> tuple[sqlite3.Row, ...]:
    """Match the words the trigram index is too coarse to resolve.

    Design limit: this reads the content of the fragments the owner can see,
    newest first, until the pool fills: ~50 ms for a term found nowhere among
    20k visible fragments holding 7 MB of text, out of 71k fragments and 52 MB
    with past revisions. Give two-character words an index of their own once
    that scan exceeds 100 ms.
    """
    if not substrings and not whole_words:
        return ()
    # _QUERY_TOKEN_RE keeps word characters and a leading "#" only, so a term
    # carries no LIKE or GLOB wildcard.
    term_predicate = " OR ".join(
        (
            *("fragments.content_text LIKE '%' || ? || '%'" for _ in substrings),
            # LIKE finds an ASCII word cheaply in any case; the padded GLOB,
            # which copies the text, then runs only on what it found. LIKE folds
            # ASCII only, so a word like the Kelvin sign's "K2" keeps GLOB alone.
            *(
                "(fragments.content_text LIKE '%' || ? || '%' "
                "AND (' ' || fragments.content_text || ' ') GLOB ?)"
                if word.isascii()
                else "(' ' || fragments.content_text || ' ') GLOB ?"
                for word in whole_words
            ),
        )
    )
    return tuple(
        connection.execute(
            f"""
            SELECT {MEMORY_FRAGMENT_COLUMNS}
            FROM {VISIBLE_FRAGMENTS_FROM_NODES}
            WHERE ({term_predicate})
              AND {visibility}
            ORDER BY nodes.updated_at DESC, fragments.fragment_id
            LIMIT ?
            """,
            (
                *substrings,
                *(
                    part
                    for word in whole_words
                    for part in (
                        (word, _whole_word_glob(word))
                        if word.isascii()
                        else (_whole_word_glob(word),)
                    )
                ),
                *visibility_parameters,
                candidate_limit,
            ),
        ).fetchall()
    )


def _interleave_rows(
    indexed: tuple[sqlite3.Row, ...],
    scanned: tuple[sqlite3.Row, ...],
    *,
    limit: int,
) -> tuple[sqlite3.Row, ...]:
    """Leave both matchers a share of the candidate pool."""
    merged: dict[str, sqlite3.Row] = {}
    for pair in zip_longest(indexed, scanned):
        for row in pair:
            if row is not None:
                merged.setdefault(str(row["fragment_id"]), row)
    return tuple(merged.values())[:limit]

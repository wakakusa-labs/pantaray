from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.repositories.action_support.memory_search_helpers import (
    MEMORY_SEARCH_PHRASE_FALLBACK_MIN_CANDIDATES,
    MEMORY_SEARCH_SNIPPET_CONTEXT_CHARS,
    MEMORY_SEARCH_SNIPPET_MAX_CHARS,
)
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_agents.utils.timestamps import parse_iso8601_utc

BUSY_TIMEOUT_PRAGMA_TEMPLATE = "PRAGMA busy_timeout = {timeout_ms};"
FOREIGN_KEYS_ON_PRAGMA = "PRAGMA foreign_keys = ON;"
TECHNICAL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./:@+-]{2,}")
MAX_EXPANDED_TERMS = 8
EXACT_PHRASE_SCORE = 8.0
TERM_MATCH_SCORE = 1.5
FTS_MATCH_SCORE = 4.0
TIME_HINT_MAX_SCORE = 2.0
TIME_HINT_BOUNDARY_SCORE = 0.25
TIME_HINT_OUTER_RADIUS_MULTIPLIER = 4
BM25_RANK_MAX_SCORE = 2.0
BM25_RANK_DECAY = 4.0
SQL_LIKE_ESCAPE_CHAR = "\\"


@dataclass(frozen=True, slots=True)
class ExpandedMemoryQuery:
    phrase: str
    terms: tuple[str, ...]


def configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    connection.execute(BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms))
    connection.execute(FOREIGN_KEYS_ON_PRAGMA)
    connection.row_factory = sqlite3.Row


def normalize_keywords(keywords: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in keywords or []:
        candidate = str(keyword or "").strip()
        if not candidate:
            continue
        folded = candidate.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        normalized.append(candidate)
    return normalized


def expand_memory_query(query: str) -> ExpandedMemoryQuery:
    phrase = " ".join(str(query or "").strip().split())
    seen: set[str] = set()
    terms: list[str] = []
    for token in TECHNICAL_TOKEN_PATTERN.findall(phrase):
        normalized = token.strip()
        folded = normalized.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        terms.append(normalized)
        if len(terms) >= MAX_EXPANDED_TERMS:
            break
    if not terms and phrase:
        terms.append(phrase)
    return ExpandedMemoryQuery(phrase=phrase, terms=tuple(terms))


def broad_like_keywords(expanded_query: ExpandedMemoryQuery) -> list[str]:
    return normalize_keywords([expanded_query.phrase, *expanded_query.terms])


def phrase_fallback_keywords(expanded_query: ExpandedMemoryQuery) -> list[str]:
    if not should_run_phrase_fallback(expanded_query):
        return []
    return [expanded_query.phrase]


def phrase_fallback_limit(limit: int) -> int:
    return max(limit, MEMORY_SEARCH_PHRASE_FALLBACK_MIN_CANDIDATES)


def should_run_phrase_fallback(expanded_query: ExpandedMemoryQuery) -> bool:
    phrase = expanded_query.phrase.strip()
    if not phrase:
        return False
    folded_terms = {term.casefold() for term in expanded_query.terms}
    return phrase.casefold() not in folded_terms


def first_matching_keyword(text: str, keywords: list[str]) -> str | None:
    haystack = text.casefold()
    for keyword in keywords:
        if keyword.casefold() in haystack:
            return keyword
    return None


def score_text_match(
    *,
    text: str,
    expanded_query: ExpandedMemoryQuery,
    fts_hit: bool,
) -> float:
    folded_text = text.casefold()
    score = FTS_MATCH_SCORE if fts_hit else 0.0
    if expanded_query.phrase and expanded_query.phrase.casefold() in folded_text:
        score += EXACT_PHRASE_SCORE
    for term in expanded_query.terms:
        if term.casefold() in folded_text:
            score += TERM_MATCH_SCORE
    return score


def score_time_hint(
    *,
    value: object,
    center: datetime | None,
    radius_hours: int | None,
) -> float:
    if center is None or radius_hours is None or radius_hours <= 0:
        return 0.0
    target = _parse_sort_datetime(value)
    if target is None:
        return 0.0
    radius_seconds = radius_hours * 3600
    delta_seconds = abs((target - center).total_seconds())
    if delta_seconds <= radius_seconds:
        inner_decay = delta_seconds / radius_seconds
        return TIME_HINT_MAX_SCORE - (
            (TIME_HINT_MAX_SCORE - TIME_HINT_BOUNDARY_SCORE) * inner_decay
        )
    outer_radius = radius_seconds * TIME_HINT_OUTER_RADIUS_MULTIPLIER
    if delta_seconds > outer_radius:
        return 0.0
    outer_span = outer_radius - radius_seconds
    outer_decay = (delta_seconds - radius_seconds) / outer_span
    return TIME_HINT_BOUNDARY_SCORE * (1.0 - outer_decay)


def score_bm25_rank(rank: int | None) -> float:
    if rank is None or rank < 0:
        return 0.0
    return BM25_RANK_MAX_SCORE / (1.0 + (rank / BM25_RANK_DECAY))


def memory_search_constraint_error(message: str) -> RepositoryResult[list[DBRow]]:
    return RepositoryResult(
        error=message,
        error_kind=RepositoryErrorKind.CONSTRAINT,
        retryable=False,
    )


def memory_search_validation_error(message: str) -> RepositoryResult[list[DBRow]]:
    return RepositoryResult(
        error=message,
        error_kind=RepositoryErrorKind.VALIDATION,
        retryable=False,
    )


def make_snippet(text: str, *, needle: str | None) -> str:
    if not text:
        return ""
    if not needle:
        return text[:MEMORY_SEARCH_SNIPPET_MAX_CHARS].strip()

    haystack = text.casefold()
    target = needle.casefold()
    index = haystack.find(target)
    if index == -1:
        return ""

    start = max(index - MEMORY_SEARCH_SNIPPET_CONTEXT_CHARS, 0)
    end = min(index + len(target) + MEMORY_SEARCH_SNIPPET_CONTEXT_CHARS, len(text))
    snippet = text[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    if len(snippet) > MEMORY_SEARCH_SNIPPET_MAX_CHARS:
        return snippet[:MEMORY_SEARCH_SNIPPET_MAX_CHARS].rstrip() + "..."
    return snippet


def parse_time_hint_center(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_sort_datetime(value)


def _parse_sort_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            return parse_iso8601_utc(value)
        except ValueError:
            return None
    return None


def dedupe_rows_by_record_id(rows: list[DBRow]) -> list[DBRow]:
    deduped: list[DBRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        dedupe_key = (str(row.get("source") or ""), str(row.get("record_id") or ""))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(row)
    return deduped


def build_keyword_like_clause(
    *,
    column: str,
    keywords: list[str],
    params: list[object],
) -> str:
    if not keywords:
        return ""
    parts: list[str] = []
    for keyword in keywords:
        parts.append(f"LOWER({column}) LIKE ? ESCAPE '{SQL_LIKE_ESCAPE_CHAR}'")
        params.append(f"%{escape_like_keyword(keyword.casefold())}%")
    return " AND (" + " OR ".join(parts) + ")"


def escape_like_keyword(keyword: str) -> str:
    escaped = keyword.replace(SQL_LIKE_ESCAPE_CHAR, SQL_LIKE_ESCAPE_CHAR * 2)
    escaped = escaped.replace("%", f"{SQL_LIKE_ESCAPE_CHAR}%")
    return escaped.replace("_", f"{SQL_LIKE_ESCAPE_CHAR}_")


def build_fts_match_query(keywords: list[str]) -> str:
    escaped = [keyword.replace('"', '""') for keyword in keywords if keyword.strip()]
    return " OR ".join(f'"{keyword}"' for keyword in escaped)

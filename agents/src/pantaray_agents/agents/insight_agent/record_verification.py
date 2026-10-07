"""Keep only the claimed records this run's own reads can prove."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.tools.zanei import ReadEvent

# Bound secondary output without failing the run and stranding its read cursor.
MAX_RECORDS_PER_RUN = 40
MAX_QUOTE_CHARACTERS = 400

# Text the user typed carries no on-screen speaker label, so the prompt reserves
# this one value for it and the speaker check skips it.
USER_SPEAKER = "user"

_WHITESPACE = re.compile(r"\s+")

type RecordRejection = Literal[
    "over_run_limit",
    "duplicate",
    "event_not_read",
    "quote_not_found",
    "source_not_found",
    "speaker_not_found",
    "time_not_found",
    "foreign_header",
]


class SourceRecordClaim(BaseModel):
    """One thing the model says it read verbatim on screen."""

    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(description="The zanei_timeline event this was read from.")
    source: str = Field(
        description="Visible document, sheet, file, page, window or section title, or conversation header, exactly as shown."
    )
    speaker: str = Field(
        description='Displayed author/speaker, or empty when none is shown; "user" only for text demonstrably typed by the user.'
    )
    shown_time: str = Field(
        description="The time exactly as shown, or an empty string."
    )
    quote: str = Field(
        description=f"Verbatim work-relevant text (including labels/units for values), at most {MAX_QUOTE_CHARACTERS} characters."
    )


@dataclass(frozen=True, slots=True)
class VerifiedRecord:
    """A claim the fetched text proved, with the event metadata it belongs to."""

    event_id: str
    observed_at: str
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    source: str
    speaker: str
    shown_time: str
    quote: str


@dataclass(frozen=True, slots=True)
class RecordVerification:
    accepted: tuple[VerifiedRecord, ...]
    rejected: Counter[RecordRejection]


def verify_source_records(
    *,
    claims: Sequence[SourceRecordClaim],
    events: Mapping[str, ReadEvent],
) -> RecordVerification:
    """Accept fields present in the fetched text and its claimed source section.

    The model writes these records from text it read through zanei_query, so the
    same text decides. Whitespace is collapsed on both sides, because a screen
    wraps and indents what a person reads as one line; nothing else is
    normalized, so a paraphrase, a translation or a repaired typo fails.
    """
    rejected: Counter[RecordRejection] = Counter()
    considered = list(claims[:MAX_RECORDS_PER_RUN])
    if len(claims) > len(considered):
        rejected["over_run_limit"] += len(claims) - len(considered)
    evidence = _evidence(considered, events)
    accepted: list[VerifiedRecord] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for claim in considered:
        quote = claim.quote[:MAX_QUOTE_CHARACTERS]
        reason = _rejection(
            claim=claim, quote=quote, evidence=evidence.get(claim.event_id)
        )
        if reason is not None:
            rejected[reason] += 1
            continue
        # The record id is derived from these same fields, so a claim repeated
        # within one run would collide on insert rather than append twice.
        identity = (
            claim.event_id,
            claim.source,
            claim.speaker,
            claim.shown_time,
            quote,
        )
        if identity in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(identity)
        event = events[claim.event_id]
        accepted.append(
            VerifiedRecord(
                event_id=claim.event_id,
                observed_at=event.observed_at,
                app_name=event.app_name,
                bundle_id=event.bundle_id,
                window_title=event.window_title,
                source=claim.source,
                speaker=claim.speaker,
                shown_time=claim.shown_time,
                quote=quote,
            )
        )
    return RecordVerification(accepted=tuple(accepted), rejected=rejected)


@dataclass(frozen=True, slots=True)
class _Evidence:
    """One event's fetched text, collapsed once for every claim against it."""

    texts: tuple[str, ...]
    window_title: str
    headers: frozenset[str]


def _evidence(
    claims: Sequence[SourceRecordClaim], events: Mapping[str, ReadEvent]
) -> dict[str, _Evidence]:
    evidence = {}
    for event_id, event in events.items():
        texts = tuple(_collapse(text) for text in event.texts)
        headers = set()
        for claim in claims:
            source = _collapse(claim.source)
            if (
                claim.event_id == event_id
                and source
                and any(source in text for text in texts)
            ):
                headers.add(source)
        evidence[event_id] = _Evidence(
            texts, _collapse(event.window_title or ""), frozenset(headers)
        )
    return evidence


def _rejection(
    *, claim: SourceRecordClaim, quote: str, evidence: _Evidence | None
) -> RecordRejection | None:
    if evidence is None or not any(evidence.texts):
        return "event_not_read"
    body = _collapse(quote)
    matched = [text for text in evidence.texts if body and body in text]
    if not matched:
        return "quote_not_found"
    source, speaker, shown_time = map(
        _collapse, (claim.source, claim.speaker, claim.shown_time)
    )
    if not source or not (
        source in evidence.window_title
        or any(source in text for text in evidence.texts)
    ):
        return "source_not_found"
    if (
        speaker
        and speaker != USER_SPEAKER
        and not any(speaker in text for text in matched)
    ):
        return "speaker_not_found"
    reason: RecordRejection = "foreign_header"
    for text in matched:
        for position in _positions(text, body):
            before = [
                (index, header)
                for header in evidence.headers
                if (index := text.rfind(header, 0, position)) >= 0
            ]
            preceding = max(before) if before else None
            if preceding is not None:
                if preceding[1] != source:
                    continue
                start = preceding[0] + len(source)
            elif source in evidence.window_title:
                start = 0
            elif text.startswith(source, position):
                start = position
            else:
                continue
            # Evidence from a neighbouring conversation cannot supply the time
            # or speaker. Only the actual window title may label a headerless body.
            # A quote may include its own source label; another source still
            # ends the section even when it occurs inside the quote.
            end = min(
                (
                    index
                    for header in evidence.headers
                    if (
                        index := text.find(
                            header,
                            position + len(body) if header == source else position,
                        )
                    )
                    >= 0
                ),
                default=len(text),
            )
            if position + len(body) > end:
                continue
            section = text[start:end]
            if speaker and speaker != USER_SPEAKER and speaker not in section:
                reason = "speaker_not_found"
            elif shown_time and shown_time not in section:
                reason = "time_not_found"
            else:
                return None
    return reason


def _positions(text: str, value: str) -> Iterator[int]:
    position = text.find(value)
    while position >= 0:
        yield position
        position = text.find(value, position + 1)


def _collapse(value: str) -> str:
    """Ignore only how the screen wrapped and indented the text."""
    return _WHITESPACE.sub("", value)

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pantaray_agents.agents.insight_agent.agent import ShortInsightOutput
from pantaray_agents.agents.insight_agent.record_verification import (
    MAX_QUOTE_CHARACTERS,
    MAX_RECORDS_PER_RUN,
    SourceRecordClaim,
    verify_source_records,
)
from pantaray_agents.tools.zanei import ReadEvent

EVENT, TEAM, DRAFT = "event-1", "チームの連絡", "資料のたたき台"
QUOTE = "明日の打ち合わせは 14 時からに変わりました"
OTHER_QUOTE = "図を差し替えたので確認をお願いします"
SCREEN = (
    f"{TEAM}\nささき ゆい 10:12\n{QUOTE}\n{DRAFT}\nたなべ そう 10:20\n{OTHER_QUOTE}"
)


def _event(*texts: str, title: str = "打ち合わせ準備") -> ReadEvent:
    return ReadEvent(
        "2026-09-07T10:20:00Z",
        "メッセージ",
        "com.example.messages",
        title,
        texts or (SCREEN,),
    )


def _claim(**changes: str) -> SourceRecordClaim:
    return SourceRecordClaim.model_validate(
        {
            "event_id": EVENT,
            "source": TEAM,
            "speaker": "ささき ゆい",
            "shown_time": "10:12",
            "quote": QUOTE,
            **changes,
        }
    )


def test_verbatim_whitespace_matching_preserves_event_metadata() -> None:
    claim = _claim(quote="明日の打ち合わせは\n  14 時からに変わりました")
    result = verify_source_records(claims=[claim], events={EVENT: _event()})
    assert result.rejected == {}
    (record,) = result.accepted
    assert (record.quote, record.source) == (claim.quote, TEAM)
    assert (
        record.observed_at,
        record.app_name,
        record.bundle_id,
        record.window_title,
    ) == (
        "2026-09-07T10:20:00Z",
        "メッセージ",
        "com.example.messages",
        "打ち合わせ準備",
    )


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"quote": "打ち合わせが14時に変更された"}, "quote_not_found"),
        ({"event_id": "unread"}, "event_not_read"),
        ({"source": "別の部屋"}, "source_not_found"),
        ({"source": " "}, "source_not_found"),
        ({"speaker": "いのうえ かえで"}, "speaker_not_found"),
        ({"speaker": "たなべ そう"}, "speaker_not_found"),
        ({"shown_time": "23:59"}, "time_not_found"),
        ({"shown_time": "10:20"}, "time_not_found"),
        (
            {"quote": OTHER_QUOTE, "speaker": "たなべ そう", "shown_time": "10:20"},
            "foreign_header",
        ),
    ],
)
def test_unproven_fields_are_rejected(changes: dict[str, str], reason: str) -> None:
    other = _claim(
        source=DRAFT, speaker="たなべ そう", shown_time="10:20", quote=OTHER_QUOTE
    )
    result = verify_source_records(
        claims=[_claim(**changes), other], events={EVENT: _event()}
    )
    assert result.rejected == {reason: 1}
    assert [r.source for r in result.accepted] == [DRAFT]


@pytest.mark.parametrize(
    "texts, title, sources",
    [
        ((TEAM + " はい " + DRAFT + " はい",), "", [TEAM, DRAFT]),
        ((TEAM + " はい", DRAFT + " はい"), "", [TEAM, DRAFT]),
        ((TEAM, DRAFT + " はい"), "", [DRAFT]),
        ((TEAM, "はい"), "", []),
        (("はい",), TEAM, [TEAM]),
        ((DRAFT + " はい",), TEAM, [DRAFT]),
    ],
)
def test_each_occurrence_uses_its_own_header(
    texts: tuple[str, ...], title: str, sources: list[str]
) -> None:
    claims = [
        _claim(source=s, speaker="user", shown_time="", quote="はい")
        for s in (TEAM, DRAFT)
    ]
    result = verify_source_records(
        claims=claims, events={EVENT: _event(*texts, title=title)}
    )
    assert [r.source for r in result.accepted] == sources


def test_a_quote_never_spans_fields() -> None:
    result = verify_source_records(
        claims=[
            _claim(source="打ち合わせ準備", speaker="user", shown_time="", quote=q)
            for q in ("後半の文章です", "文章です後半の")
        ],
        events={EVENT: _event("前半の文章です", "後半の文章です")},
    )
    assert result.rejected == {"quote_not_found": 1}
    assert [r.quote for r in result.accepted] == ["後半の文章です"]


@pytest.mark.parametrize(
    "title, screen, quote",
    [
        ("Status", "Status: Failed", "Status: Failed"),
        ("配信管理", "Status: Failed", "Status: Failed"),
        ("配信管理", "Status\nCurrent Status: Failed", "Current Status: Failed"),
    ],
)
def test_a_quote_can_include_its_own_source(
    title: str, screen: str, quote: str
) -> None:
    claim = _claim(source="Status", speaker="", shown_time="", quote=quote)
    result = verify_source_records(
        claims=[claim], events={EVENT: _event(screen, title=title)}
    )
    assert result.rejected == {}
    assert [(r.source, r.quote) for r in result.accepted] == [("Status", quote)]


@pytest.mark.parametrize(
    "speaker, shown_time", [("Alice", ""), ("", "10:00"), ("Alice", "10:00")]
)
@pytest.mark.parametrize(
    "title, screen",
    [
        ("配信管理", "Status\nAlice 10:00\nStatus: Failed"),
        ("Status", "Alice 10:00\nStatus: Failed"),
    ],
)
def test_source_prefixed_quote_keeps_preceding_attribution(
    speaker: str, shown_time: str, title: str, screen: str
) -> None:
    claim = _claim(
        source="Status", speaker=speaker, shown_time=shown_time, quote="Status: Failed"
    )
    result = verify_source_records(
        claims=[claim],
        events={EVENT: _event(screen, title=title)},
    )
    assert result.rejected == {}
    assert [(r.speaker, r.shown_time, r.quote) for r in result.accepted] == [
        (speaker, shown_time, claim.quote)
    ]


def test_source_prefix_does_not_override_a_foreign_header() -> None:
    claims = [
        _claim(source="Status", speaker="", shown_time="", quote="Status: Failed"),
        _claim(
            source="History", speaker="", shown_time="", quote="Previous run: Passed"
        ),
    ]
    result = verify_source_records(
        claims=claims,
        events={
            EVENT: _event(
                "History\nPrevious run: Passed\nStatus: Failed", title="Status"
            )
        },
    )
    assert result.rejected == {"foreign_header": 1}
    assert [r.source for r in result.accepted] == ["History"]


def test_a_quote_with_its_source_still_cannot_cross_another_header() -> None:
    claims = [
        _claim(
            source="Status",
            speaker="",
            shown_time="",
            quote="Status: Failed\nHistory\nPrevious run: Passed",
        ),
        _claim(
            source="History", speaker="", shown_time="", quote="Previous run: Passed"
        ),
    ]
    result = verify_source_records(
        claims=claims,
        events={
            EVENT: _event(
                "Status: Failed\nHistory\nPrevious run: Passed", title="Status"
            )
        },
    )
    assert result.rejected == {"foreign_header": 1}
    assert [(r.source, r.quote) for r in result.accepted] == [
        ("History", "Previous run: Passed")
    ]


def test_the_run_limit_duplicate_filter_and_quote_limit() -> None:
    quote = "あ" * (MAX_QUOTE_CHARACTERS + 50)
    result = verify_source_records(
        claims=[_claim(source=TEAM, speaker="user", shown_time="", quote=quote)]
        * (MAX_RECORDS_PER_RUN + 3),
        events={EVENT: _event(TEAM + quote)},
    )
    assert result.rejected == {
        "over_run_limit": 3,
        "duplicate": MAX_RECORDS_PER_RUN - 1,
    }
    assert [r.quote for r in result.accepted] == ["あ" * MAX_QUOTE_CHARACTERS]


@pytest.mark.parametrize(
    "records, kept", [(None, 0), ({}, 0), ([{}, 4, _claim().model_dump()], 1)]
)
def test_malformed_records_are_discarded_individually(
    records: object, kept: int, caplog: pytest.LogCaptureFixture
) -> None:
    payload = {
        "activity": "activity",
        "insight": "insight",
        "reconsideration_reason": None,
        "records": records,
    }
    output = ShortInsightOutput.model_validate(payload)
    assert len(output.records) == kept
    assert "Malformed source records discarded" in caplog.text
    assert QUOTE not in caplog.text
    with pytest.raises(ValidationError):
        ShortInsightOutput.model_validate({**payload, "activity": ""})


@pytest.mark.parametrize(
    "app, source, quote",
    [
        ("文書", "設計仕様.md", "受け入れ条件: オフラインでも保存できること"),
        ("表計算", "予算表.xlsx", "項目 予算（円） 実績（円）\n開発費 500000 620000"),
        ("エディタ", "validation.py", "def validate(value):\n    return value > 0"),
        ("ターミナル", "pytest — project", "FAILED test_save: database is locked"),
        ("タスク管理", "配信設定", "状態: 配信失敗\n理由: 認証が期限切れです"),
    ],
)
def test_work_evidence_without_a_speaker_is_verified(
    app: str, source: str, quote: str
) -> None:
    output = ShortInsightOutput.model_validate(
        {
            "activity": "作業内容を確認",
            "insight": "状況を確認中",
            "reconsideration_reason": None,
            "records": [
                _claim(
                    source=source, speaker="", shown_time="", quote=quote
                ).model_dump()
            ],
        }
    )
    event = ReadEvent("2026-09-19T00:00:00Z", app, None, source, (quote,))
    result = verify_source_records(claims=output.records, events={EVENT: event})
    assert result.rejected == {}
    (record,) = result.accepted
    assert (record.source, record.quote, record.speaker, record.app_name) == (
        source,
        quote,
        "",
        app,
    )


def test_unattributed_evidence_still_requires_the_right_section_and_exact_values() -> (
    None
):
    screen = "予算\n開発費 500000 円\n実績\n開発費 620000 円"
    claims = [
        _claim(source="予算", speaker="", shown_time="", quote="開発費 620000 円"),
        _claim(source="予算", speaker="", shown_time="", quote="開発費 600000 円"),
        _claim(source="実績", speaker="", shown_time="", quote="開発費 620000 円"),
    ]
    result = verify_source_records(
        claims=claims, events={EVENT: _event(screen, title="費用.xlsx")}
    )
    assert result.rejected == {"foreign_header": 1, "quote_not_found": 1}
    assert [(record.source, record.quote) for record in result.accepted] == [
        ("実績", "開発費 620000 円")
    ]

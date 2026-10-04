import pytest

from pantaray_agents.utils.timestamps import (
    normalize_iso8601_utc_z_microseconds,
    parse_iso8601_utc,
)


def test_normalize_iso8601_utc_z_microseconds_preserves_microseconds_and_utc() -> None:
    assert (
        normalize_iso8601_utc_z_microseconds("2026-03-14T15:48:13.126491+00:00")
        == "2026-03-14T15:48:13.126491Z"
    )


def test_normalize_iso8601_utc_z_microseconds_normalizes_offset() -> None:
    assert (
        normalize_iso8601_utc_z_microseconds("2026-03-15T00:48:13.126491+09:00")
        == "2026-03-14T15:48:13.126491Z"
    )


def test_normalize_iso8601_utc_z_microseconds_zero_pads_year() -> None:
    assert normalize_iso8601_utc_z_microseconds("0002-01-01T00:00:00Z") == (
        "0002-01-01T00:00:00.000000Z"
    )


def test_parse_iso8601_utc_rejects_timezone_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone information is required"):
        parse_iso8601_utc("2026-03-14T15:48:13")


@pytest.mark.parametrize(
    "timestamp",
    ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"],
)
def test_parse_iso8601_utc_rejects_utc_range_overflow(timestamp: str) -> None:
    with pytest.raises(ValueError, match="supported UTC range"):
        parse_iso8601_utc(timestamp)


def test_parse_iso8601_utc_normalizes_offset_to_utc() -> None:
    assert parse_iso8601_utc("2026-03-15T00:48:13+09:00").isoformat() == (
        "2026-03-14T15:48:13+00:00"
    )

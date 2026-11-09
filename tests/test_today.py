from datetime import date

from bot import (
    ParseFailure,
    ParsedDay,
    build_predict_button_pages,
    parse_day_argument,
)
from data_fetcher import (
    TODAY_MATCH_COMPETITIONS,
    format_match_list,
    format_todays_matches,
    fixtures_from_payload,
    parse_fixture,
)
from timeutil import format_kickoff_cairo, format_kickoff_short_cairo


def test_today_competitions_are_the_six_requested_leagues() -> None:
    assert TODAY_MATCH_COMPETITIONS == ("PL", "CL", "PD", "SA", "BL1", "FL1")


def test_format_todays_matches_shows_unique_ids_without_predictions() -> None:
    payload = {
        "matches": [
            {
                "id": 101,
                "status": "TIMED",
                "utcDate": "2026-11-08T15:00:00Z",
                "competition": {"code": "PL", "name": "Premier League"},
                "homeTeam": {"id": 1, "name": "Arsenal"},
                "awayTeam": {"id": 2, "name": "Chelsea"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
            {
                "id": 202,
                "status": "SCHEDULED",
                "utcDate": "2026-11-08T20:00:00Z",
                "competition": {"code": "SA", "name": "Serie A"},
                "homeTeam": {"id": 3, "name": "Inter"},
                "awayTeam": {"id": 4, "name": "Milan"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
            {
                "id": 999,
                "status": "TIMED",
                "utcDate": "2026-11-08T12:00:00Z",
                "competition": {"code": "PPL", "name": "Primeira Liga"},
                "homeTeam": {"id": 5, "name": "Benfica"},
                "awayTeam": {"id": 6, "name": "Porto"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
        ]
    }
    fixtures = fixtures_from_payload(payload)
    assert [fixture.id for fixture in fixtures] == [101, 202]
    pages = format_todays_matches(fixtures)
    text = "\n".join(pages)
    assert "#101" in text
    assert "#202" in text
    assert "999" not in text
    assert "Cairo" in text
    assert "no predictions" in text.casefold() or "No predictions" in text
    assert "/predict #101" in text or "Tap a match button" in text
    assert "Premier League" in text
    assert "Serie A" in text


def test_predict_buttons_use_match_ids() -> None:
    fixtures = fixtures_from_payload(
        {
            "matches": [
                {
                    "id": 101,
                    "status": "TIMED",
                    "utcDate": "2026-11-08T15:00:00Z",
                    "competition": {"code": "PL", "name": "Premier League"},
                    "homeTeam": {"id": 1, "name": "Arsenal"},
                    "awayTeam": {"id": 2, "name": "Chelsea"},
                    "score": {"fullTime": {}, "halfTime": {}},
                },
                {
                    "id": 202,
                    "status": "SCHEDULED",
                    "utcDate": "2026-11-08T20:00:00Z",
                    "competition": {"code": "SA", "name": "Serie A"},
                    "homeTeam": {"id": 3, "name": "Inter"},
                    "awayTeam": {"id": 4, "name": "Milan"},
                    "score": {"fullTime": {}, "halfTime": {}},
                },
            ]
        }
    )
    pages = build_predict_button_pages(fixtures, buttons_per_message=1)
    assert len(pages) == 2
    first = pages[0].inline_keyboard[0][0]
    second = pages[1].inline_keyboard[0][0]
    assert first.callback_data == "p:101"
    assert second.callback_data == "p:202"
    assert "Arsenal" in first.text
    assert "Inter" in second.text


def test_kickoffs_are_shown_in_cairo_time() -> None:
    # 15:00 UTC -> 17:00 Cairo (Africa/Cairo is UTC+2 outside DST quirks handled by zoneinfo)
    assert format_kickoff_short_cairo("2026-11-08T15:00:00Z") == "17:00 Cairo"
    assert format_kickoff_cairo("2026-11-08T15:00:00Z") == "2026-11-08 17:00 Cairo"


def test_api_date_window_widens_exclusive_date_to() -> None:
    from timeutil import api_date_window_for_cairo_day

    # Cairo day 8 Nov spans 7 Nov 22:00 UTC .. 8 Nov 22:00 UTC.
    # API dateTo is exclusive, so the request must end on 9 Nov.
    assert api_date_window_for_cairo_day(date(2026, 11, 8)) == ("2026-11-07", "2026-11-09")


def test_fixtures_can_be_filtered_to_a_cairo_calendar_day() -> None:
    # November: Africa/Cairo is UTC+2, so 21:30 UTC is still 24 Nov and 22:30 UTC is 25 Nov.
    payload = {
        "matches": [
            {
                "id": 1,
                "status": "TIMED",
                "utcDate": "2026-11-24T21:30:00Z",
                "competition": {"code": "PL", "name": "Premier League"},
                "homeTeam": {"id": 1, "name": "A"},
                "awayTeam": {"id": 2, "name": "B"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
            {
                "id": 2,
                "status": "TIMED",
                "utcDate": "2026-11-24T22:30:00Z",
                "competition": {"code": "PL", "name": "Premier League"},
                "homeTeam": {"id": 3, "name": "C"},
                "awayTeam": {"id": 4, "name": "D"},
                "score": {"fullTime": {}, "halfTime": {}},
            },
        ]
    }
    fixtures = fixtures_from_payload(payload, cairo_day=date(2026, 11, 24))
    assert [fixture.id for fixture in fixtures] == [1]


def test_empty_today_list_explains_the_coverage() -> None:
    pages = format_todays_matches([])
    text = pages[0]
    assert "Premier League" in text
    assert "UEFA Champions League" in text
    assert "display-only" in text


def test_custom_day_list_uses_dd_mm_yyyy_label() -> None:
    pages = format_match_list([], day_label="24-05-2026")
    assert "24-05-2026" in pages[0]


def test_parse_day_argument_accepts_dd_mm_yyyy() -> None:
    parsed = parse_day_argument("24-05-2026")
    assert isinstance(parsed, ParsedDay)
    assert parsed.display == "24-05-2026"
    assert parsed.api_date == "2026-05-24"
    bad = parse_day_argument("2026-05-24")
    assert isinstance(bad, ParseFailure)
    invalid = parse_day_argument("32-05-2026")
    assert isinstance(invalid, ParseFailure)


def test_parse_fixture_keeps_the_provider_match_id() -> None:
    fixture = parse_fixture(
        {
            "id": 560638,
            "status": "TIMED",
            "utcDate": "2026-11-08T15:00:00Z",
            "competition": {"code": "PL", "name": "Premier League"},
            "homeTeam": {"id": 397, "name": "Brighton"},
            "awayTeam": {"id": 402, "name": "Brentford"},
            "score": {"fullTime": {}, "halfTime": {}},
        }
    )
    assert fixture.id == 560638

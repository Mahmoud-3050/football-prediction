from live.bot import ParseFailure, ParsedCommand, ParsedMatchId, parse_predict_command


def test_command_with_spaces_and_competition() -> None:
    parsed = parse_predict_command("/predict Manchester City vs West Ham | Premier League")
    assert isinstance(parsed, ParsedCommand)
    assert parsed.home == "Manchester City"
    assert parsed.away == "West Ham"
    assert parsed.competition == "Premier League"


def test_command_accepts_a_today_match_id() -> None:
    hashed = parse_predict_command("/predict #560638")
    plain = parse_predict_command("/predict 560638")
    assert isinstance(hashed, ParsedMatchId)
    assert isinstance(plain, ParsedMatchId)
    assert hashed.match_id == 560638
    assert plain.match_id == 560638


def test_command_addressed_to_a_bot_is_accepted() -> None:
    parsed = parse_predict_command("/predict@FootyBot Arsenal vs Chelsea | Premier League")
    assert isinstance(parsed, ParsedCommand)
    assert (parsed.home, parsed.away, parsed.competition) == (
        "Arsenal",
        "Chelsea",
        "Premier League",
    )


def test_missing_competition_is_rejected() -> None:
    parsed = parse_predict_command("/predict Arsenal vs Chelsea")
    assert isinstance(parsed, ParseFailure)
    assert "Competition is required" in parsed.reason


def test_missing_vs_is_rejected() -> None:
    parsed = parse_predict_command("/predict Arsenal Chelsea | Premier League")
    assert isinstance(parsed, ParseFailure)
    assert "vs" in parsed.reason


def test_empty_side_is_rejected() -> None:
    parsed = parse_predict_command("/predict vs Chelsea | Premier League")
    assert isinstance(parsed, ParseFailure)
    assert "required" in parsed.reason


def test_same_club_string_is_rejected() -> None:
    parsed = parse_predict_command("/predict Arsenal vs arsenal | Premier League")
    assert isinstance(parsed, ParseFailure)
    assert "same club" in parsed.reason


def test_bare_command_explains_the_shape() -> None:
    parsed = parse_predict_command("/predict")
    assert isinstance(parsed, ParseFailure)
    assert "/predict Home vs Away | Competition" in parsed.reason
    assert "/predict <id>" in parsed.reason

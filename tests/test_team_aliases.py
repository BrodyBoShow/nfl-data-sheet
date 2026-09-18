from pipeline.core.team_aliases import TEAM_ABBR_ALIASES, normalize_team_abbr


def test_normalize_team_abbr_covers_all_aliases():
    for old_code, current_code in TEAM_ABBR_ALIASES.items():
        assert normalize_team_abbr(old_code) == current_code


def test_normalize_team_abbr_passes_through_unknown_codes():
    assert normalize_team_abbr("KC") == "KC"


def test_normalize_team_abbr_passes_through_none():
    assert normalize_team_abbr(None) is None

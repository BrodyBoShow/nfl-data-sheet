"""
Job: Map retired franchise codes and provider-specific team-abbreviation quirks to
     canonical teams.team_abbr, in one place shared by every collector that needs it.
Reads: nothing
Writes: nothing
Tier: n/a
Phase: P3
"""

from __future__ import annotations

# snap_counts/pfr_advstats (both PFR-sourced) still carry retired franchise codes for
# historical seasons that pbp/player_stats/nextgen_stats already normalize -- verified
# live (docs/sources.md): 2019 snap_counts/pfr_advstats show 'OAK', 2016 snap_counts
# shows 'SD', 2015 snap_counts shows 'STL', while pbp/player_stats/nextgen_stats already
# show the current code for the same seasons/games.
#
# ESPN's injuries feed uses 'WSH' for Washington where nflverse and Sleeper both use
# 'WAS' -- verified live (docs/sources.md "Availability"), not a retired code, just a
# different current-team abbreviation convention.
TEAM_ABBR_ALIASES: dict[str, str] = {
    "OAK": "LV",  # retired Raiders code (PFR-era data)
    "SD": "LAC",  # retired Chargers code (PFR-era data)
    "STL": "LA",  # retired Rams code (PFR-era data)
    "WSH": "WAS",  # ESPN's Washington code vs. nflverse's WAS
}


def normalize_team_abbr(abbr: str | None) -> str | None:
    if abbr is None:
        return None
    return TEAM_ABBR_ALIASES.get(abbr, abbr)

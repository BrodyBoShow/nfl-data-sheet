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
#
# 'LAR' is the Rams in ESPN's and Sleeper's feeds (injuries.team, Sleeper's team-defense
# IDs) and in NGS, where nflverse's canonical code is 'LA' -- verified live 2026-10-15
# (docs/sources.md, Sleeper trending, "Team-defense IDs"; docs/phases/P8.md D19). Before
# this alias, every Rams injury row said 'LAR', so Availability impact found no Rams
# player's position and wrote no Rams redistribution or cluster counts (D19's trace).
TEAM_ABBR_ALIASES: dict[str, str] = {
    "OAK": "LV",  # retired Raiders code (PFR-era data)
    "SD": "LAC",  # retired Chargers code (PFR-era data)
    "STL": "LA",  # retired Rams code (PFR-era data)
    "WSH": "WAS",  # ESPN's Washington code vs. nflverse's WAS
    "LAR": "LA",  # ESPN's, Sleeper's and NGS's Rams code vs. nflverse's LA (P8 D19)
}


def normalize_team_abbr(abbr: str | None) -> str | None:
    if abbr is None:
        return None
    return TEAM_ABBR_ALIASES.get(abbr, abbr)

"""
Job: Decide whether a game's stadiums row really describes where it's played (the name
     guard) and whether weather applies there (the roof rule). Pure, shared by the weather
     collector (whether to fetch) and the Environment analyst (weather_status, venue
     signals) so the two can never disagree about a venue.
Reads: nothing (callers pass games/stadiums values in)
Writes: nothing
Tier: n/a
Phase: P4
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SkipReason = Literal[
    "fixed_roof", "roof_closed", "name_mismatch", "unknown_stadium", "no_forecast_data"
]


@dataclass(frozen=True)
class GameVenue:
    game_id: str
    season: int
    week: int
    stadium_id: str | None
    stadium_name: str | None  # games.stadium, the string the name guard checks
    roof: str | None  # games.roof for this game (null pre-game at retractable venues)


@dataclass(frozen=True)
class Stadium:
    stadium_id: str
    known_names: tuple[str, ...]
    lat: float
    lon: float
    roof_type: str


@dataclass(frozen=True)
class VenueDecision:
    fetch: bool
    skip_reason: SkipReason | None = None
    roof_conflict: bool = False  # open venue, but games.roof claims dome/closed


def resolve_venue(game: GameVenue, stadium: Stadium | None) -> VenueDecision:
    """Pure. The name guard runs before the roof rule: a game whose stadium name doesn't
    match its stadium_id's known names (e.g. 2026_05_PHI_JAX, JAX00 but "Tottenham
    Hotspur Stadium") is never fetched with that row's coords, whatever its roof."""
    if stadium is None:
        return VenueDecision(False, "unknown_stadium")
    if game.stadium_name is None or game.stadium_name not in stadium.known_names:
        return VenueDecision(False, "name_mismatch")
    if stadium.roof_type == "fixed":
        return VenueDecision(False, "fixed_roof")
    if stadium.roof_type == "retractable" and game.roof == "closed":
        return VenueDecision(False, "roof_closed")
    # Retractable with roof null/open is fetched -- nflverse leaves games.roof null
    # pre-game, so the Environment analyst labels these "if roof open". An open venue
    # nflverse calls dome/closed (MCG, Stade de France, Munich) is fetched: the structural
    # roof_type wins, and the conflict is surfaced for the auditor.
    conflict = stadium.roof_type == "open" and game.roof in ("dome", "closed")
    return VenueDecision(True, None, conflict)

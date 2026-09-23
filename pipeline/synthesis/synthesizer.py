"""
Job: Project each window game's spread and total from efficiency signals as a matchup
     card, locking the projection into projection_log at the pre-kickoff lock.
Reads: signals (efficiency, market, environment, availability), games (identity and
       schedule columns only: GAMES_COLUMNS), projection_log (existing locks),
       pipeline/synthesis/model_coefficients.json
Writes: matchup_cards, projection_log
Tier: T1
Phase: P5

**Window.** Same as Environment/Market: `now - 24h < kickoff <= now + 7d`. Each card
carries its game's own season/week, never the dispatcher's.

**Writes.** Only for games with `kickoff > now`. At or after kickoff, a game is never
written again to either table, so its card freezes.

**Lock.**
- A game locks on the first run with `kickoff - _LOCK_LEAD <= now < kickoff` where the
  projection is computable (projection_status 1).
- The insert is `INSERT ... ON CONFLICT (game_id) DO NOTHING`, and migration 0023's
  trigger makes the row immutable.
- After the lock, the card keeps the locked projection. Only its market block and its
  edge vs. the current line refresh, and they're recomputed from the locked projection,
  never re-projected.
- A game that kicks off without a lock is listed in `agent_runs.meta.lock_missed`.

**projection_status.**

| Code | Meaning | Precedence |
|---|---|---|
| 4 | model stale: live efficiency fingerprint doesn't match the file | checked first |
| 5 | in-sample season: the season is in the file's `fit_seasons` | second |
| 2 | awaiting efficiency: no efficiency rows at the game's (season, week) | third |
| 3 | an input is null (a feature, a stability, or the game's location) | fourth |
| 1 | projected | otherwise |

There is never a fallback to an older week's signals, because that would be the wrong
point in time.

**Edges.**
- Edges are computed vs. Market's current consensus.
- The edge is never adjusted. Book disagreement and lookahead-only markets are flags.
- `edge_validated` comes from the coefficients file. The P5 backtest set it false in
  every bucket, and the card says so.

**Card shape.** Opponent pairings are built by `pair_unit_signals`, a generic
`_off`/`_def` suffix match on a subject key (team now, player+team in P7). No
team-vs-team join is hardcoded.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
import psycopg

from pipeline.analysts.market import (
    STATUS_LOOKAHEAD_ONLY,
    STATUS_MOVEMENT,
    STATUS_SINGLE_CAPTURE,
)
from pipeline.core.base import RunContext, Synthesizer, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.hashing import hash_row
from pipeline.core.schedule import kickoff_utc
from pipeline.core.team_aliases import normalize_team_abbr
from pipeline.synthesis.model import (
    COEFFICIENTS_PATH,
    PRIMARY_SPEC,
    ModelFile,
    build_game_frame,
    efficiency_fingerprint,
    load_model_file,
    predict,
    stability_bucket,
)

CARD_VERSION = 1

# Lock lead. Observed dispatcher ticks land 4.5-5.5h apart, so a narrower window could
# miss the lock entirely (docs/phases/P5.md).
_LOCK_LEAD = dt.timedelta(hours=6)
_LOOKBACK = dt.timedelta(hours=24)
_LOOKAHEAD = dt.timedelta(days=7)

# L3 may read exactly these games columns (CLAUDE.md layer rules). Scores, lines,
# results and season_type are off limits at runtime.
GAMES_COLUMNS = (
    "game_id", "season", "week", "home_team", "away_team", "gameday", "gametime", "location",
)
_GAMES_SQL = (
    f"SELECT {', '.join(GAMES_COLUMNS)} FROM games "
    "WHERE gameday BETWEEN %s AND %s AND gametime IS NOT NULL ORDER BY game_id"
)

# The fit and backtest cover REG games only. season_type isn't an allowed column, so a
# postseason game is recognized by its week: REG is 18 weeks since 2021, and nflverse
# numbers POST weeks after REG (docs/sources.md). Such games are still projected and
# locked, but flagged.
_LAST_REG_WEEK = 18

STATUS_PROJECTED = 1
STATUS_AWAITING_EFFICIENCY = 2
STATUS_INPUT_NULL = 3
STATUS_MODEL_STALE = 4
STATUS_IN_SAMPLE = 5
_STATUS_LABELS = {
    STATUS_PROJECTED: "projected",
    STATUS_AWAITING_EFFICIENCY: "awaiting efficiency signals for this week",
    STATUS_INPUT_NULL: "a model input is missing",
    STATUS_MODEL_STALE: "model stale: efficiency config changed since the fit",
    STATUS_IN_SAMPLE: "in-sample season: not projected",
}

_MARKET_EDGE_STATUSES = {
    int(STATUS_MOVEMENT), int(STATUS_SINGLE_CAPTURE), int(STATUS_LOOKAHEAD_ONLY)
}

_AVAILABILITY_CONTEXT = ("ol_cluster_count", "secondary_cluster_count")

EDGE_NOT_VALIDATED_NOTE = (
    "Difference vs. market, not shown to beat the closing line historically."
)
# Out-of-sample 2020-2025 (docs/backtest_report.md): margin RMS 13.30 for the model,
# 12.66 for the closing line; 13.17-13.48 across stability buckets.
OUTCOME_NOISE_NOTE = (
    "NFL results typically land about 13 points from any pregame projection, the model's "
    "or the market's. This is game-outcome noise, not this projection's uncertainty, and "
    "it barely changes with input stability."
)
# docs/signals.md: spread_key_straddle fired on 37% of 2026 captures through week 3.
STRADDLE_COMMON_NOTE = (
    "Common: books split by a half point around a key number on roughly a third to a "
    "half of games. Context, not an alert."
)


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowGame:
    game_id: str
    season: int
    week: int
    home_team: str
    away_team: str
    kickoff: dt.datetime  # UTC
    location: str | None


def in_window(kickoff: dt.datetime, now: dt.datetime) -> bool:
    return now - _LOOKBACK < kickoff <= now + _LOOKAHEAD


def in_lock_window(kickoff: dt.datetime, now: dt.datetime) -> bool:
    return kickoff - _LOCK_LEAD <= now < kickoff


def _iso(t: dt.datetime | None) -> str | None:
    return None if t is None else t.astimezone(dt.UTC).isoformat()


def _brief(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "signal": row["signal"],
        "team": row.get("team"),
        "player_id": row.get("player_id"),
        "value": row.get("value"),
        "stability": row.get("stability"),
        "sample_n": row.get("sample_n"),
    }


def pair_unit_signals(
    rows: Iterable[Mapping[str, Any]],
    subject: Mapping[str, Any],
    opponent: Mapping[str, Any],
    *,
    own_suffix: str = "_off",
    opp_suffix: str = "_def",
    exclude_bases: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Pair every subject signal named `<base><own_suffix>` with the opponent's
    `<base><opp_suffix>`. Rows are matched to a side by the key/value pairs in
    `subject`/`opponent`, e.g. `{"team": "KC", "player_id": None}` for a team unit, or
    `{"team": "KC", "player_id": "00-0036355"}` for a player (P7). The opponent side is
    None when it has no counterpart row. It's never filled."""
    rows = list(rows)
    excluded = set(exclude_bases)

    def matches(row: Mapping[str, Any], key: Mapping[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in key.items())

    subject_rows = {
        r["signal"]: r for r in rows if matches(r, subject) and r["signal"].endswith(own_suffix)
    }
    opponent_rows = {r["signal"]: r for r in rows if matches(r, opponent)}
    out = []
    for signal in sorted(subject_rows):
        base = signal[: -len(own_suffix)]
        if base in excluded:
            continue
        out.append({
            "base": base,
            "subject": _brief(subject_rows[signal]),
            "opponent": _brief(opponent_rows.get(base + opp_suffix)),
        })
    return out


@dataclass(frozen=True)
class MarketView:
    """One game's Market-sector signals: game scope by signal, team scope by team."""

    status: int | None
    game: dict[str, dict[str, Any]] = field(default_factory=dict)
    teams: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    inputs_version: str | None = None

    def value(self, signal: str) -> float | None:
        row = self.game.get(signal)
        return None if row is None else row["value"]


def market_view(rows: Iterable[Mapping[str, Any]]) -> MarketView:
    game: dict[str, dict[str, Any]] = {}
    teams: dict[str, dict[str, dict[str, Any]]] = {}
    version = None
    for r in rows:
        entry = {"value": r["value"], "sample_n": r.get("sample_n")}
        if r.get("team") is None:
            game[r["signal"]] = entry
            if r["signal"] == "market_status":
                version = r.get("inputs_version")
        else:
            teams.setdefault(r["team"], {})[r["signal"]] = entry
    status_row = game.get("market_status")
    status = None if status_row is None or status_row["value"] is None else int(status_row["value"])
    return MarketView(status, game, teams, version)


def _edge_summary(edge_spread: float | None, home: str, away: str) -> str | None:
    if edge_spread is None:
        return None
    if edge_spread < 0:
        return f"model favors {home} by {-edge_spread:.1f} more than market"
    if edge_spread > 0:
        return f"model favors {away} by {edge_spread:.1f} more than market"
    return "model matches market"


def compute_edge(
    projected_spread: float | None,
    projected_total: float | None,
    market: MarketView,
    home: str,
    away: str,
) -> dict[str, Any]:
    """Edge vs. the current consensus. Never adjusted -- book disagreement and
    lookahead-only markets become flags. No edge at market status 4/5 or without market
    rows."""
    usable = market.status in _MARKET_EDGE_STATUSES
    spread_now = market.value("spread_home_current") if usable else None
    total_now = market.value("total_current") if usable else None
    edge_spread = (
        None if projected_spread is None or spread_now is None else projected_spread - spread_now
    )
    edge_total = (
        None if projected_total is None or total_now is None else projected_total - total_now
    )
    spread_range = market.value("spread_book_range")
    total_range = market.value("total_book_range")
    flags = {
        "market_lookahead_only": market.status == int(STATUS_LOOKAHEAD_ONLY),
        "single_book_market": usable and spread_range is None,
        "spread_key_straddle": market.value("spread_key_straddle") == 1.0,
        "spread_within_book_range": (
            edge_spread is not None and spread_range is not None
            and abs(edge_spread) <= spread_range / 2
        ),
        "total_within_book_range": (
            edge_total is not None and total_range is not None
            and abs(edge_total) <= total_range / 2
        ),
    }
    return {
        "spread": edge_spread,
        "total": edge_total,
        "market_spread": spread_now,
        "market_total": total_now,
        "flags": flags,
        "flag_notes": {
            "spread_key_straddle": STRADDLE_COMMON_NOTE if flags["spread_key_straddle"] else None,
        },
        "summary": _edge_summary(edge_spread, home, away),
    }


def projection_status(
    game: WindowGame,
    *,
    model_stale: bool,
    fit_seasons: Sequence[int],
    efficiency_weeks: set[tuple[int, int]],
    pred: Mapping[str, Any] | None,
) -> int:
    if model_stale:
        return STATUS_MODEL_STALE
    if game.season in fit_seasons:
        return STATUS_IN_SAMPLE
    if (game.season, game.week) not in efficiency_weeks:
        return STATUS_AWAITING_EFFICIENCY
    if game.location is None or pred is None or not pred["features_complete"]:
        return STATUS_INPUT_NULL
    return STATUS_PROJECTED


_EffKey = tuple[int, int, str | None, str]  # season, week, team, signal


def _projection_block(
    game: WindowGame, pred: Mapping[str, Any], model: ModelFile, eff: Mapping[_EffKey, Any]
) -> dict[str, Any]:
    """The projection plus its decomposition: each side's points = alpha + HFA + one
    term per model input (beta x centered value), with the raw signal behind it."""

    def side(own: str, opp: str, own_team: str, opp_team: str, sign: float) -> dict[str, Any]:
        terms = []
        for b in PRIMARY_SPEC.bases:
            for unit, prefix, team, coef in (
                ("off", own, own_team, f"beta_off:{b}"),
                ("def", opp, opp_team, f"beta_def:{b}"),
            ):
                centered = pred[f"{prefix}_{unit}__{b}"]
                row = eff.get((game.season, game.week, normalize_team_abbr(team), f"{b}_{unit}"))
                terms.append({
                    "role": "offense" if unit == "off" else "opponent_defense",
                    "team": team,
                    "signal": f"{b}_{unit}",
                    "value": pred[f"{prefix}_{unit}_raw__{b}"],
                    "week_mean": pred[f"{prefix}_{unit}_raw__{b}"] - centered,
                    "centered": centered,
                    "stability": pred[f"{prefix}_{unit}_stab__{b}"],
                    "sample_n": None if row is None else row.get("sample_n"),
                    "beta": model.coef[coef],
                    "contribution": model.coef[coef] * centered,
                })
        return {
            "alpha": model.coef["alpha"],
            "hfa": model.coef["gamma"] * sign * pred["h_home"],
            "terms": terms,
            "points": pred["pts_home"] if sign > 0 else pred["pts_away"],
        }

    return {
        "model_version": model.model_version,
        "efficiency_fingerprint": model.efficiency_fingerprint,
        "spread_home": pred["projected_spread_home"],
        "total": pred["projected_total"],
        "pts_home": pred["pts_home"],
        "pts_away": pred["pts_away"],
        "margin_home": pred["margin_home"],
        "decomposition": {
            "home": side("home", "away", game.home_team, game.away_team, 1.0),
            "away": side("away", "home", game.away_team, game.home_team, -1.0),
        },
    }


def _uncertainty_block(pred: Mapping[str, Any], model: ModelFile) -> dict[str, Any]:
    """Input stability, the edge-validation flags, and outcome noise.

    There is deliberately no per-game +/- band. The bucket RMS residuals in the
    coefficients file barely differ (margin 13.17-13.48 across the three stability
    buckets), because stability changes the projection's inputs, not how widely NFL
    results scatter around a projection. So the card states outcome noise once, as a
    property of NFL games, not as this game's model uncertainty."""
    s = pred["stability_min"]
    bucket = stability_bucket(s, model.stability_cutpoints)
    cal = model.buckets[bucket]
    validated = {"spread": cal.edge_validated_spread, "total": cal.edge_validated_total}
    return {
        "stabilities": {
            f"{side}_{unit}": pred[f"{side}_{unit}_stab__{b}"]
            for b in PRIMARY_SPEC.bases
            for side in ("home", "away")
            for unit in ("off", "def")
        },
        "stability_min": s,
        "stability_bucket": bucket,
        "low_stability": bucket == "low",
        "outcome_noise": {
            "margin_rms": cal.margin_sd,
            "total_rms": cal.total_sd,
            "note": OUTCOME_NOISE_NOTE,
        },
        "edge_validated": validated,
        "edge_note": {m: (None if v else EDGE_NOT_VALIDATED_NOTE) for m, v in validated.items()},
    }


def _market_block(market: MarketView) -> dict[str, Any]:
    return {
        "status": market.status,
        "signals": market.game,
        "teams": market.teams,
        "in_model": False,
    }


def _context_block(
    game: WindowGame,
    week_efficiency: Sequence[Mapping[str, Any]],
    environment: Sequence[Mapping[str, Any]],
    availability: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    home, away = normalize_team_abbr(game.home_team), normalize_team_abbr(game.away_team)
    env_game: dict[str, Any] = {}
    env_teams: dict[str, dict[str, Any]] = {}
    for r in environment:
        if r.get("team") is None:
            env_game[r["signal"]] = r["value"]
        else:
            env_teams.setdefault(r["team"], {})[r["signal"]] = r["value"]
    avail: dict[str, dict[str, Any]] = {}
    for r in availability:
        if r["team"] in (home, away):
            avail.setdefault(r["team"], {})[r["signal"]] = r["value"]
    return {
        "in_model": False,
        "efficiency_pairings": {
            "home_offense": pair_unit_signals(
                week_efficiency, {"team": home, "player_id": None},
                {"team": away, "player_id": None}, exclude_bases=PRIMARY_SPEC.bases,
            ),
            "away_offense": pair_unit_signals(
                week_efficiency, {"team": away, "player_id": None},
                {"team": home, "player_id": None}, exclude_bases=PRIMARY_SPEC.bases,
            ),
        },
        "environment": {"game": env_game, "teams": env_teams},
        "availability": avail,
    }


def _join_versions(parts: Iterable[str | None]) -> str:
    return ";".join(sorted({p for p in parts if p}))


@dataclass
class SynthesisResult:
    cards: list[dict[str, Any]]
    locks: list[dict[str, Any]]
    meta: dict[str, Any]


_EFFICIENCY_FRAME_SCHEMA: dict[str, Any] = {
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "stability": pl.Float64,
}


def _predictions(
    games: Sequence[WindowGame], efficiency_rows: Sequence[Mapping[str, Any]], model: ModelFile
) -> dict[str, dict[str, Any]]:
    located = [g for g in games if g.location is not None]
    if not located:
        return {}
    games_df = pl.DataFrame(
        [
            {"game_id": g.game_id, "season": g.season, "week": g.week,
             "home_team": g.home_team, "away_team": g.away_team, "location": g.location}
            for g in located
        ],
        schema={"game_id": pl.Utf8, "season": pl.Int64, "week": pl.Int64,
                "home_team": pl.Utf8, "away_team": pl.Utf8, "location": pl.Utf8},
    )
    signals_df = pl.DataFrame(
        [{k: r.get(k) for k in _EFFICIENCY_FRAME_SCHEMA} for r in efficiency_rows],
        schema=_EFFICIENCY_FRAME_SCHEMA,
    )
    frame = build_game_frame(games_df, signals_df, PRIMARY_SPEC)
    return {r["game_id"]: r for r in predict(model.coef, frame, PRIMARY_SPEC).to_dicts()}


def build_cards(
    *,
    games: Sequence[WindowGame],
    efficiency_rows: Sequence[Mapping[str, Any]],
    market_rows: Sequence[Mapping[str, Any]],
    environment_rows: Sequence[Mapping[str, Any]],
    availability_rows: Sequence[Mapping[str, Any]],
    model: ModelFile,
    live_fingerprint: str,
    locked: Mapping[str, Mapping[str, Any]],
    now: dt.datetime,
) -> SynthesisResult:
    """Everything the run writes, from already-loaded rows. No DB access."""
    model_stale = (
        model.efficiency_fingerprint != live_fingerprint
        or model.spec_name != PRIMARY_SPEC.name
        or model.bases != PRIMARY_SPEC.bases
    )
    eff: dict[_EffKey, Mapping[str, Any]] = {
        (r["season"], r["week"], r["team"], r["signal"]): r for r in efficiency_rows
    }
    eff_by_week: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for r in efficiency_rows:
        eff_by_week.setdefault((r["season"], r["week"]), []).append(r)
    avail_by_week: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for r in availability_rows:
        avail_by_week.setdefault((r["season"], r["week"]), []).append(r)
    by_game: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for sector, rows in (("market", market_rows), ("environment", environment_rows)):
        for r in rows:
            by_game.setdefault(r["game_id"], {}).setdefault(sector, []).append(r)

    upcoming = [g for g in games if g.kickoff > now]
    preds = _predictions(upcoming, efficiency_rows, model) if not model_stale else {}
    model_tag = f"model@{model.model_version}#{model.efficiency_fingerprint}"

    cards: list[dict[str, Any]] = []
    locks: list[dict[str, Any]] = []
    status_counts: dict[int, int] = {}
    lock_missed: list[str] = []
    frozen = 0

    for g in sorted(games, key=lambda x: x.game_id):
        if g.kickoff <= now:
            frozen += 1
            if g.game_id not in locked:
                lock_missed.append(g.game_id)
            continue

        game_rows = by_game.get(g.game_id, {})
        market = market_view(game_rows.get("market", []))
        pred = preds.get(g.game_id)
        lock = locked.get(g.game_id)
        status = projection_status(
            g, model_stale=model_stale, fit_seasons=model.fit_seasons,
            efficiency_weeks=set(eff_by_week), pred=pred,
        )

        projection: dict[str, Any] | None = None
        uncertainty: dict[str, Any] | None = None
        projection_version: str | None = None
        if lock is not None:
            # Locked: show the locked claim, never re-project.
            status = lock["card"]["projection_status"]
            projection = lock["card"]["projection"]
            uncertainty = lock["card"]["uncertainty"]
            projection_version = lock["inputs_version"]
        elif status == STATUS_PROJECTED and pred is not None:
            projection = _projection_block(g, pred, model, eff)
            uncertainty = _uncertainty_block(pred, model)
            used = [
                eff.get((g.season, g.week, normalize_team_abbr(t), f"{b}_{u}"))
                for b in PRIMARY_SPEC.bases
                for t in (g.home_team, g.away_team)
                for u in ("off", "def")
            ]
            projection_version = _join_versions(
                [model_tag] + [None if r is None else f"efficiency:{r['inputs_version']}"
                               for r in used]
            )
        status_counts[status] = status_counts.get(status, 0) + 1

        spread = None if projection is None else projection["spread_home"]
        total = None if projection is None else projection["total"]
        edge_now = compute_edge(spread, total, market, g.home_team, g.away_team)

        new_lock = lock is None and status == STATUS_PROJECTED and in_lock_window(g.kickoff, now)
        if lock is not None:
            lock_block: dict[str, Any] = {
                "locked": True,
                "locked_at": _iso(lock["locked_at"]),
                "kickoff_at_lock": _iso(lock["kickoff"]),
                "lock_lead_hours": lock["lock_lead_hours"],
            }
            edge_at_lock: dict[str, Any] | None = {
                "spread": lock["edge_spread"], "total": lock["edge_total"],
                "market_spread": lock["market_spread"], "market_total": lock["market_total"],
            }
        elif new_lock:
            lead = (g.kickoff - now).total_seconds() / 3600
            lock_block = {"locked": True, "locked_at": _iso(now),
                          "kickoff_at_lock": _iso(g.kickoff), "lock_lead_hours": lead}
            edge_at_lock = {k: edge_now[k] for k in ("spread", "total", "market_spread",
                                                     "market_total")}
        else:
            lock_block = {"locked": False, "locked_at": None, "kickoff_at_lock": None,
                          "lock_lead_hours": None,
                          "locks_from": _iso(g.kickoff - _LOCK_LEAD)}
            edge_at_lock = None

        card = {
            "card_version": CARD_VERSION,
            "identity": {
                "game_id": g.game_id, "season": g.season, "week": g.week,
                "home_team": g.home_team, "away_team": g.away_team,
                "kickoff": _iso(g.kickoff), "location": g.location,
                "neutral": g.location == "Neutral",
                "outside_fit_scope": g.week > _LAST_REG_WEEK,
            },
            "projection_status": status,
            "projection_status_label": _STATUS_LABELS[status],
            "projection": projection,
            "uncertainty": uncertainty,
            "market": _market_block(market),
            "edge": {"vs_current": edge_now, "at_lock": edge_at_lock},
            "lock": lock_block,
            "context": _context_block(
                g,
                eff_by_week.get((g.season, g.week), []),
                game_rows.get("environment", []),
                avail_by_week.get((g.season, g.week), []),
            ),
        }
        inputs_version = _join_versions(
            [projection_version or model_tag,
             None if market.inputs_version is None else f"market:{market.inputs_version}"]
        )
        card_json = json.dumps(card, sort_keys=True)

        row: dict[str, Any] = {
            "game_id": g.game_id,
            "season": g.season,
            "week": g.week,
            "kickoff": g.kickoff,
            "projection_status": status,
            "projected_spread": spread,
            "projected_total": total,
            "edge_spread": edge_now["spread"],
            "edge_total": edge_now["total"],
            "locked": bool(lock_block["locked"]),
            "card": card_json,
            "inputs_version": inputs_version,
        }
        row["content_hash"] = hash_row(row)
        row["as_of"] = now
        row["updated_at"] = now
        cards.append(row)

        if new_lock:
            assert uncertainty is not None and projection is not None
            locks.append({
                "game_id": g.game_id,
                "season": g.season,
                "week": g.week,
                "locked_at": now,
                "market_spread": edge_now["market_spread"],
                "market_total": edge_now["market_total"],
                "projected_spread": spread,
                "projected_total": total,
                "edge_spread": edge_now["spread"],
                "edge_total": edge_now["total"],
                "inputs_version": inputs_version,
                "card": card_json,
                "kickoff": g.kickoff,
                "lock_lead_hours": lock_block["lock_lead_hours"],
                "model_version": model.model_version,
                "stability_min": uncertainty["stability_min"],
                "stability_bucket": uncertainty["stability_bucket"],
                # In-sample by construction: set from the same walk-forward residuals the
                # backtest's coverage was measured on. The grader measures real coverage.
                "spread_sd": uncertainty["outcome_noise"]["margin_rms"],
                "total_sd": uncertainty["outcome_noise"]["total_rms"],
                "market_status": market.status,
            })

    meta = {
        "games_in_window": len(games),
        "cards_built": len(cards),
        "frozen_past_kickoff": frozen,
        "lock_missed": lock_missed,
        "locking": [lk["game_id"] for lk in locks],
        "projection_status_counts": {str(k): v for k, v in sorted(status_counts.items())},
        "model_stale": model_stale,
        "model_version": model.model_version,
    }
    return SynthesisResult(cards, locks, meta)


# --------------------------------------------------------------------------------------
# DB I/O (thin -- feeds build_cards)
# --------------------------------------------------------------------------------------

_SIGNAL_COLS = (
    "game_id", "season", "week", "team", "player_id", "sector", "signal", "value",
    "sample_n", "stability", "inputs_version",
)


def load_window_games(conn: psycopg.Connection, now: dt.datetime) -> list[WindowGame]:
    """Narrows by ET gameday with a day of slack each side, then filters exactly on
    kickoff_utc (the Environment/Market approach)."""
    start_day = (now - _LOOKBACK - dt.timedelta(days=1)).date()
    end_day = (now + _LOOKAHEAD + dt.timedelta(days=1)).date()
    with conn.cursor() as cur:
        cur.execute(_GAMES_SQL, (start_day, end_day))
        rows = cur.fetchall()
    games = []
    for game_id, season, week, home, away, gameday, gametime, location in rows:
        kickoff = kickoff_utc(gameday, gametime)
        if in_window(kickoff, now):
            games.append(WindowGame(game_id, season, week, home, away, kickoff, location))
    return games


def _fetch_signals(conn: psycopg.Connection, where: str, params: Sequence[Any]) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_SIGNAL_COLS)} FROM signals WHERE {where}", params)
        return [dict(zip(_SIGNAL_COLS, r, strict=True)) for r in cur.fetchall()]


def _load_team_week_signals(
    conn: psycopg.Connection,
    sector: str,
    weeks: Iterable[tuple[int, int]],
    signals: Sequence[str] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for season, week in sorted(set(weeks)):
        where = ("sector = %s AND game_id IS NULL AND player_id IS NULL "
                 "AND season = %s AND week = %s")
        params: list[Any] = [sector, season, week]
        if signals is not None:
            where += " AND signal = ANY(%s)"
            params.append(list(signals))
        rows += _fetch_signals(conn, where, params)
    return rows


def _load_locks(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    cols = ("game_id", "locked_at", "kickoff", "lock_lead_hours", "card", "inputs_version",
            "edge_spread", "edge_total", "market_spread", "market_total")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM projection_log WHERE game_id = ANY(%s)", (game_ids,)
        )
        return {r[0]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}


_LOCK_COLS = (
    "game_id", "season", "week", "locked_at", "market_spread", "market_total",
    "projected_spread", "projected_total", "edge_spread", "edge_total", "inputs_version",
    "card", "kickoff", "lock_lead_hours", "model_version", "stability_min",
    "stability_bucket", "spread_sd", "total_sd", "market_status",
)
_INSERT_LOCK_SQL = (
    f"INSERT INTO projection_log ({', '.join(_LOCK_COLS)}) "
    f"VALUES ({', '.join(f'%({c})s' for c in _LOCK_COLS)}) "
    "ON CONFLICT (game_id) DO NOTHING"
)


class MatchupSynthesizer(Synthesizer):
    name = "synthesizer"

    def __init__(self, model_path: Path = COEFFICIENTS_PATH) -> None:
        self._model_path = model_path

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        # No game in the window is a routine "nothing to do" (skipped_fresh).
        return bool(load_window_games(ctx.conn, ctx.now))

    def compute(self, ctx: RunContext) -> SynthesisResult:
        conn = ctx.conn
        games = load_window_games(conn, ctx.now)
        model = load_model_file(self._model_path)
        game_ids = [g.game_id for g in games]
        weeks = {(g.season, g.week) for g in games}
        return build_cards(
            games=games,
            efficiency_rows=_load_team_week_signals(conn, "efficiency", weeks),
            market_rows=_fetch_signals(
                conn, "sector = 'market' AND game_id = ANY(%s)", [game_ids]
            ),
            environment_rows=_fetch_signals(
                conn, "sector = 'environment' AND game_id = ANY(%s)", [game_ids]
            ),
            availability_rows=_load_team_week_signals(
                conn, "availability", weeks, _AVAILABILITY_CONTEXT
            ),
            model=model,
            live_fingerprint=efficiency_fingerprint(PRIMARY_SPEC),
            locked=_load_locks(conn, game_ids),
            now=ctx.now,
        )

    def write(self, ctx: RunContext, computed: SynthesisResult) -> WorkResult:
        conn = ctx.conn
        inserted: list[str] = []
        lost: set[str] = set()
        with conn.cursor() as cur:
            for lock in computed.locks:
                cur.execute(_INSERT_LOCK_SQL, lock)
                if cur.rowcount == 1:
                    inserted.append(lock["game_id"])
                else:
                    # Another run locked it first. This card claims our lock, so skip it;
                    # the next run rebuilds the card from the stored lock.
                    lost.add(lock["game_id"])
        cards = [c for c in computed.cards if c["game_id"] not in lost]
        changed = filter_changed(conn, "matchup_cards", "game_id", cards)
        update_cols = [c for c in (changed[0] if changed else {}) if c != "game_id"]
        written = upsert_rows(
            conn, "matchup_cards", changed, conflict_cols=["game_id"], update_cols=update_cols
        )
        meta = {
            **computed.meta,
            "locks_inserted": inserted,
            "locks_lost_to_concurrent_run": sorted(lost),
            "cards_written": written,
        }
        return WorkResult(written + len(inserted), meta)

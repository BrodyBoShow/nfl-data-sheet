"""
Job: Grade every locked projection against its game's result and lines, and rebuild the
     grade summary that reports those grades with sample sizes and intervals.
Reads: projection_log (never modified), games (scores and nflverse lines -- L0 may read
       them; L3 may not), matchup_cards (the frozen card of a game that never locked),
       odds_consensus/odds_snapshots (through the Market analyst's own loaders)
Writes: projection_grades, grade_summary
Tier: T2
Phase: P5

**Scope.** Every game from `GRADING_START` on that has kicked off. A game that never
locked still gets a `no_lock` row, so the lock rate is visible next to the grades.

**Lines.** All spreads are home-negative market convention; nflverse's home-positive
`spread_line` is negated first (docs/sources.md).
- *Lock line*: `projection_log.market_spread/market_total`, our consensus at lock. The
  headline for win/loss, because it's the line the claim was made against.
- *Own close*: our last capture before kickoff, selected by the Market analyst's own
  `pre_kickoff_captures`/`latest_capture`. Same source as the lock line. It's often
  the lock line itself (odds targets are sparse), so `clv_own_*` is null unless a
  capture landed after the lock.
- *nflverse close*: `games.spread_line/total_line` once the game has a final score, so
  the value came from a post-game schedule file. nflverse documents neither the book nor
  the timing, and its line is a different source from our 8-book median. CLV against it
  mixes real movement with that basis difference (`clv_basis_*` in the summary measures
  the bias part), so it's labeled mixed-source everywhere.

**CLV.** `sign(edge at lock) * (close - lock line)`, in points: positive means the line
moved toward the model's side. Zero edge is no side, so no CLV.

**Summary.** One `grade_summary` row per (slice, metric) with n, a 95% CI and a verdict.
- `insufficient_n` below the metric's minimum n.
- Only the pre-registered primaries (`_PRIMARY`) can be `supported`/`against`. Every other
  tested slice is `exploratory`, whatever its CI says.
- CLV and win rate converge at very different speeds: a 50-game CLV slice can be
  informative while a 50-game ATS slice is nowhere close. `CLV_MIN_N` starts at the same
  50 as a placeholder, to be reset from the measured per-game SD of line movement
  (`line_move_sd_*`), not left at 50.

Reporting only: nothing in the pipeline reads these tables yet (docs/phases/P5.md).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import psycopg

from pipeline.analysts.market import (
    Capture,
    Game,
    latest_capture,
    load_captures,
    pre_kickoff_captures,
)
from pipeline.core.base import Grader, RunContext, WorkResult
from pipeline.core.db import filter_changed
from pipeline.core.hashing import hash_row
from pipeline.core.schedule import kickoff_utc, to_gameday
from pipeline.core.stats import bootstrap_corr, bootstrap_mean_ci, wilson_ci

GRADER_VERSION = "g1"

# The first week the synthesizer ran live (docs/phases/P5.md). Earlier games could never
# have locked, so they'd only dilute the lock rate.
GRADING_START = (2026, 3)

# Minimum n before a slice's number can be read at all. 50 is a judgment call. CLV has
# its own constant because it converges far faster than a win rate: replace the
# placeholder once line_move_sd_* has been measured on live games.
WIN_RATE_MIN_N = 50
CLV_MIN_N = 50
DEFAULT_MIN_N = 50
# Lock rate is pipeline health, not a skill claim: 0 of 16 locked in a week is a finding
# at n = 16. The Wilson interval still shows the uncertainty.
LOCK_RATE_MIN_N = 1

# Own close counts as a real close for the basis check only this close to kickoff.
_BASIS_MAX_LEAD_HOURS = 4.0

# Same ranges as the backtest (scripts/backtest.py), so live and backtest slices line up.
WEEK_RANGES = ((1, 4), (5, 8), (9, 12), (13, 99))
EDGE_BUCKETS: tuple[tuple[float, float | None], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4), (4, None)
)
_BUCKETS = ("low", "mid", "high")

# Pre-registered primary tests: (slice, metric) -> the favorable direction vs. the null.
_PRIMARY: dict[tuple[str, str], int] = {
    ("all", "clv_nflv_spread_mean"): 1,
    ("all", "clv_nflv_total_mean"): 1,
    ("all", "clv_own_spread_mean"): 1,
    ("all", "clv_own_total_mean"): 1,
    ("all", "margin_mae_diff_lock"): -1,
}

_PARITY_TOLERANCE = 1e-3  # projection_log stores real (float4)


# --------------------------------------------------------------------------------------
# Pure grading
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GradeGame:
    game_id: str
    season: int
    week: int
    season_type: str
    kickoff: dt.datetime  # UTC, from the current schedule
    home_team: str
    away_team: str
    location: str | None
    div_game: bool | None
    home_score: int | None
    away_score: int | None
    spread_line: float | None  # nflverse, home-positive
    total_line: float | None
    updated_at: dt.datetime


def _sign(x: float) -> int:
    return 1 if x > 0 else -1


def pick_result(
    edge: float | None, line: float | None, actual: float | None, market: str
) -> str | None:
    """The model's pick against `line`. `actual` is the home margin (spread) or the game
    total. None when there's no edge, line or result; `no_pick` at zero edge. A negative
    spread edge means the model's side is home, which covers when margin + line > 0."""
    if edge is None or line is None or actual is None:
        return None
    if edge == 0:
        return "no_pick"
    if market == "spread":
        outcome = -_sign(edge) * (actual + line)
    else:
        outcome = _sign(edge) * (actual - line)
    if outcome > 0:
        return "win"
    if outcome < 0:
        return "loss"
    return "push"


def clv(edge: float | None, lock_line: float | None, close: float | None) -> float | None:
    """Closing-line value in points: sign(edge) * (close - lock_line). Positive = the line
    moved toward the model's side (e.g. model likes home, line -3 -> -4: +1)."""
    if edge is None or lock_line is None or close is None or edge == 0:
        return None
    return _sign(edge) * (close - lock_line)


def _as_dict(card: Any) -> dict[str, Any] | None:
    if card is None:
        return None
    return json.loads(card) if isinstance(card, str) else dict(card)


def _hours(later: dt.datetime, earlier: dt.datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= _PARITY_TOLERANCE


def grade_game(
    game: GradeGame,
    *,
    lock: Mapping[str, Any] | None,
    card_status: int | None,
    card: Any,
    captures: Sequence[Capture],
    now: dt.datetime,
) -> dict[str, Any]:
    """One projection_grades row. `lock` is the game's projection_log row (or None);
    `card_status`/`card` are its matchup_cards row, used only when it never locked;
    `captures` are its raw odds captures (load_captures)."""
    home, away = game.home_score, game.away_score
    scored = home is not None and away is not None
    margin = float(home - away) if home is not None and away is not None else None
    total = float(home + away) if home is not None and away is not None else None
    # nflverse keeps updating spread_line until the game; only a scored row is final.
    nflv_spread = None if not scored or game.spread_line is None else -game.spread_line
    nflv_total = None if not scored else game.total_line

    row: dict[str, Any] = {
        "game_id": game.game_id,
        "season": game.season,
        "week": game.week,
        "kickoff": game.kickoff,
        "outside_fit_scope": game.season_type == "POST",
        "neutral": None if game.location is None else game.location == "Neutral",
        "div_game": game.div_game,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "games_updated_at": game.updated_at,
        "grader_version": GRADER_VERSION,
    }
    lock_cols = (
        "projection_log_id", "kickoff_at_lock", "last_card_status", "model_version",
        "efficiency_fingerprint", "stability_min", "stability_bucket", "lock_lead_hours",
        "market_status_at_lock", "lock_line_lead_hours", "flag_spread_key_straddle",
        "flag_spread_within_book_range", "flag_total_within_book_range",
        "flag_single_book_market", "flag_market_lookahead_only", "projected_spread",
        "projected_total", "spread_sd", "total_sd", "margin_error", "total_error",
        "in_band_spread", "in_band_total", "lock_spread", "lock_total", "edge_spread_lock",
        "edge_total_lock", "ats_lock", "ou_lock", "lock_line_parity", "own_close_spread",
        "own_close_total", "own_close_as_of", "own_close_lead_hours", "own_close_after_lock",
        "clv_own_spread", "clv_own_total", "nflv_close_spread", "nflv_close_total",
        "edge_spread_nflv", "edge_total_nflv", "ats_nflv", "ou_nflv", "clv_nflv_spread",
        "clv_nflv_total",
    )
    row.update(dict.fromkeys(lock_cols))

    if lock is None:
        frozen = _as_dict(card)
        uncertainty = None if frozen is None else frozen.get("uncertainty")
        row.update({
            "grade_status": "no_lock",
            "kickoff_moved": False,
            "last_card_status": card_status,
            "stability_min": None if uncertainty is None else uncertainty.get("stability_min"),
            "stability_bucket": (
                None if uncertainty is None else uncertainty.get("stability_bucket")
            ),
            "nflv_close_spread": nflv_spread,
            "nflv_close_total": nflv_total,
        })
        return _finish(row, now)

    locked_card = _as_dict(lock["card"]) or {}
    flags = ((locked_card.get("edge") or {}).get("vs_current") or {}).get("flags") or {}
    lead_signal = ((locked_card.get("market") or {}).get("signals") or {}).get(
        "market_current_lead_hours"
    )
    projection = locked_card.get("projection") or {}

    proj_spread = lock["projected_spread"]
    proj_total = lock["projected_total"]
    margin_error = None if margin is None else -proj_spread - margin
    total_error = None if total is None else proj_total - total
    lock_spread, lock_total = lock["market_spread"], lock["market_total"]
    edge_spread, edge_total = lock["edge_spread"], lock["edge_total"]

    # Own close and the lock-time line, both through the Market analyst's own selection.
    market_game = Game(game.game_id, game.season, game.week, game.kickoff, game.home_team,
                       game.away_team)
    pre_kickoff, _ = pre_kickoff_captures(captures, market_game)
    own = latest_capture(pre_kickoff)
    at_lock = latest_capture([c for c in pre_kickoff if c.as_of < lock["locked_at"]])
    parity = _same(None if at_lock is None else at_lock.spread_home, lock_spread) and _same(
        None if at_lock is None else at_lock.total, lock_total
    )
    after_lock = own is not None and (at_lock is None or own.as_of > at_lock.as_of)

    row.update({
        "grade_status": "graded" if scored else "awaiting_result",
        "projection_log_id": lock["id"],
        "kickoff_at_lock": lock["kickoff"],
        "kickoff_moved": lock["kickoff"] != game.kickoff,
        "model_version": lock["model_version"],
        "efficiency_fingerprint": projection.get("efficiency_fingerprint"),
        "stability_min": lock["stability_min"],
        "stability_bucket": lock["stability_bucket"],
        "lock_lead_hours": lock["lock_lead_hours"],
        "market_status_at_lock": lock["market_status"],
        "lock_line_lead_hours": None if lead_signal is None else lead_signal.get("value"),
        "flag_spread_key_straddle": flags.get("spread_key_straddle"),
        "flag_spread_within_book_range": flags.get("spread_within_book_range"),
        "flag_total_within_book_range": flags.get("total_within_book_range"),
        "flag_single_book_market": flags.get("single_book_market"),
        "flag_market_lookahead_only": flags.get("market_lookahead_only"),
        "projected_spread": proj_spread,
        "projected_total": proj_total,
        "spread_sd": lock["spread_sd"],
        "total_sd": lock["total_sd"],
        "margin_error": margin_error,
        "total_error": total_error,
        "in_band_spread": (
            None if margin_error is None or lock["spread_sd"] is None
            else abs(margin_error) <= lock["spread_sd"]
        ),
        "in_band_total": (
            None if total_error is None or lock["total_sd"] is None
            else abs(total_error) <= lock["total_sd"]
        ),
        "lock_spread": lock_spread,
        "lock_total": lock_total,
        "edge_spread_lock": edge_spread,
        "edge_total_lock": edge_total,
        "ats_lock": pick_result(edge_spread, lock_spread, margin, "spread"),
        "ou_lock": pick_result(edge_total, lock_total, total, "total"),
        "lock_line_parity": parity,
        "own_close_spread": None if own is None else own.spread_home,
        "own_close_total": None if own is None else own.total,
        "own_close_as_of": None if own is None else own.as_of,
        "own_close_lead_hours": None if own is None else _hours(game.kickoff, own.as_of),
        "own_close_after_lock": after_lock,
        "clv_own_spread": (
            clv(edge_spread, lock_spread, own.spread_home) if after_lock and own else None
        ),
        "clv_own_total": clv(edge_total, lock_total, own.total) if after_lock and own else None,
        "nflv_close_spread": nflv_spread,
        "nflv_close_total": nflv_total,
        "edge_spread_nflv": None if nflv_spread is None else proj_spread - nflv_spread,
        "edge_total_nflv": None if nflv_total is None else proj_total - nflv_total,
        "ats_nflv": pick_result(
            None if nflv_spread is None else proj_spread - nflv_spread, nflv_spread, margin,
            "spread",
        ),
        "ou_nflv": pick_result(
            None if nflv_total is None else proj_total - nflv_total, nflv_total, total, "total"
        ),
        "clv_nflv_spread": clv(edge_spread, lock_spread, nflv_spread),
        "clv_nflv_total": clv(edge_total, lock_total, nflv_total),
    })
    return _finish(row, now)


def _finish(row: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    row["content_hash"] = hash_row(row)
    row["result_first_seen_at"] = now if row["home_score"] is not None else None
    row["updated_at"] = now
    return row


# --------------------------------------------------------------------------------------
# Pure summary
# --------------------------------------------------------------------------------------


def verdict(
    *,
    kind: str,
    n: int,
    min_n: int,
    ci_low: float | None,
    ci_high: float | None,
    null_value: float | None,
    favorable: int | None,
) -> str:
    """`kind` is 'descriptive' (no null tested), 'exploratory' (tested, not
    pre-registered) or 'primary'. Below min_n nothing else matters."""
    if n < min_n:
        return "insufficient_n"
    if kind != "primary":
        return kind
    if ci_low is None or ci_high is None or null_value is None or favorable is None:
        return "ci_spans_null"
    if ci_low > null_value:
        return "supported" if favorable > 0 else "against"
    if ci_high < null_value:
        return "against" if favorable > 0 else "supported"
    return "ci_spans_null"


Row = Mapping[str, Any]


def _array(values: Iterable[float | None]) -> np.ndarray:
    return np.array([v for v in values if v is not None], dtype=float)


def _vals(rows: Iterable[Row], f: Callable[[Row], float | None]) -> np.ndarray:
    return _array(f(r) for r in rows)


def _margin(r: Row) -> float | None:
    if r["home_score"] is None or r["away_score"] is None:
        return None
    return float(r["home_score"] - r["away_score"])


def _total(r: Row) -> float | None:
    if r["home_score"] is None or r["away_score"] is None:
        return None
    return float(r["home_score"] + r["away_score"])


def _diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b


def _abs(x: float | None) -> float | None:
    return None if x is None else abs(x)


def _paired_abs_diff(r: Row, model_err: str, line: str, actual: Callable[[Row], float | None],
                     negate_line: bool) -> float | None:
    """|model error| - |line error| on one game (negative = model closer)."""
    a = actual(r)
    if r[model_err] is None or r[line] is None or a is None:
        return None
    line_pred = -r[line] if negate_line else r[line]
    return abs(r[model_err]) - abs(line_pred - a)


def _line_error(r: Row, line: str, actual: Callable[[Row], float | None],
                negate_line: bool) -> float | None:
    a = actual(r)
    if r[line] is None or a is None:
        return None
    return abs((-r[line] if negate_line else r[line]) - a)


def _basis(r: Row, market: str) -> float | None:
    """sign(edge at lock) * (nflverse close - own close), on games whose own close was
    taken near kickoff: the part of clv_nflv that is source basis, not movement."""
    edge, nflv, own = (
        (r["edge_spread_lock"], r["nflv_close_spread"], r["own_close_spread"])
        if market == "spread"
        else (r["edge_total_lock"], r["nflv_close_total"], r["own_close_total"])
    )
    lead = r["own_close_lead_hours"]
    if lead is None or lead > _BASIS_MAX_LEAD_HOURS:
        return None
    return clv(edge, own, nflv)


class _Summary:
    def __init__(self, computed_at: dt.datetime) -> None:
        self.rows: list[dict[str, Any]] = []
        self.computed_at = computed_at

    def add(self, slice_: str, metric: str, n: int, estimate: float | None,
            ci: tuple[float | None, float | None], *, null_value: float | None, min_n: int,
            tested: bool) -> None:
        favorable = _PRIMARY.get((slice_, metric))
        kind = "primary" if favorable is not None else ("exploratory" if tested else
                                                        "descriptive")
        self.rows.append({
            "grader_version": GRADER_VERSION,
            "slice": slice_,
            "metric": metric,
            "n": n,
            "estimate": estimate,
            "ci_low": ci[0],
            "ci_high": ci[1],
            "null_value": null_value,
            "min_n": min_n,
            "verdict": verdict(kind=kind, n=n, min_n=min_n, ci_low=ci[0], ci_high=ci[1],
                               null_value=null_value, favorable=favorable),
            "computed_at": self.computed_at,
        })

    def mean(self, slice_: str, metric: str, values: np.ndarray, *, min_n: int,
             null_value: float | None) -> None:
        s = bootstrap_mean_ci(values)
        self.add(slice_, metric, int(s["n"] or 0), s["mean"], (s["ci_low"], s["ci_high"]),
                 null_value=null_value, min_n=min_n, tested=null_value is not None)

    def rate(self, slice_: str, metric: str, hits: int, n: int, *, min_n: int,
             null_value: float | None) -> None:
        self.add(slice_, metric, n, hits / n if n else None, wilson_ci(hits, n),
                 null_value=null_value, min_n=min_n, tested=null_value is not None)

    def win_rate(self, slice_: str, metric: str, results: Iterable[str | None]) -> None:
        decided = [r for r in results if r in ("win", "loss")]
        wins = sum(r == "win" for r in decided)
        self.rate(slice_, metric, wins, len(decided), min_n=WIN_RATE_MIN_N, null_value=0.5)

    def sd(self, slice_: str, metric: str, values: np.ndarray) -> None:
        n = int(values.size)
        est = float(values.std(ddof=1)) if n >= 2 else None
        self.add(slice_, metric, n, est, (None, None), null_value=None, min_n=DEFAULT_MIN_N,
                 tested=False)

    def corr(self, slice_: str, metric: str, x: np.ndarray, y: np.ndarray) -> None:
        s = bootstrap_corr(x, y)
        self.add(slice_, metric, int(s["n"] or 0), s["corr"], (s["ci_low"], s["ci_high"]),
                 null_value=0.0, min_n=DEFAULT_MIN_N, tested=True)


def _pairs(rows: Sequence[Row], fx: Callable[[Row], float | None],
           fy: Callable[[Row], float | None]) -> tuple[np.ndarray, np.ndarray]:
    xy = [(fx(r), fy(r)) for r in rows]
    kept = [(x, y) for x, y in xy if x is not None and y is not None]
    return (np.array([x for x, _ in kept], dtype=float),
            np.array([y for _, y in kept], dtype=float))


def _accuracy(s: _Summary, name: str, rows: Sequence[Row]) -> None:
    for prefix, err, line, nflv, actual, neg, band in (
        ("margin", "margin_error", "lock_spread", "nflv_close_spread", _margin, True,
         "in_band_spread"),
        ("total", "total_error", "lock_total", "nflv_close_total", _total, False,
         "in_band_total"),
    ):
        model_abs = [_abs(r[err]) for r in rows]
        lock_abs = [_line_error(r, line, actual, neg) for r in rows]
        nflv_abs = [_line_error(r, nflv, actual, neg) for r in rows]
        paired = [_paired_abs_diff(r, err, line, actual, neg) for r in rows]
        for metric, values, null_value in (
            (f"{prefix}_mae_model", model_abs, None),
            (f"{prefix}_mae_lock_line", lock_abs, None),
            (f"{prefix}_mae_nflv", nflv_abs, None),
            (f"{prefix}_mae_diff_lock", paired, 0.0),
        ):
            s.mean(name, metric, _array(values), min_n=DEFAULT_MIN_N, null_value=null_value)
        in_band = [r[band] for r in rows if r[band] is not None]
        s.rate(name, f"coverage_{prefix}", sum(in_band), len(in_band), min_n=DEFAULT_MIN_N,
               null_value=None)
    for market, lock_col, nflv_col, own_col in (
        ("spread", "lock_spread", "nflv_close_spread", "own_close_spread"),
        ("total", "lock_total", "nflv_close_total", "own_close_total"),
    ):
        s.sd(name, f"line_move_sd_{market}_nflv",
             _array([_diff(r[nflv_col], r[lock_col]) for r in rows]))
        s.sd(name, f"line_move_sd_{market}_own",
             _array([_diff(r[own_col], r[lock_col]) for r in rows
                     if r["own_close_after_lock"]]))


def _neg(x: float | None) -> float | None:
    return None if x is None else -x


def _ats_margin_nflv(r: Row) -> float | None:
    """Home margin + the nflverse close (market convention): positive = home covered."""
    return _diff(_margin(r), _neg(r["nflv_close_spread"]))


def _market_family(s: _Summary, name: str, rows: Sequence[Row], market: str) -> None:
    # The edge correlation is the backtest's edge-validation test, against the nflverse
    # close, so live and backtest numbers are the same statistic.
    if market == "spread":
        pick_lock, pick_nflv = "ats_lock", "ats_nflv"
        clv_nflv, clv_own = "clv_nflv_spread", "clv_own_spread"
        x, y = _pairs(rows, lambda r: _neg(r["edge_spread_nflv"]), _ats_margin_nflv)
    else:
        pick_lock, pick_nflv = "ou_lock", "ou_nflv"
        clv_nflv, clv_own = "clv_nflv_total", "clv_own_total"
        x, y = _pairs(rows, lambda r: r["edge_total_nflv"],
                      lambda r: _diff(_total(r), r["nflv_close_total"]))
    s.win_rate(name, f"{pick_lock}_win_rate", (r[pick_lock] for r in rows))
    s.win_rate(name, f"{pick_nflv}_win_rate", (r[pick_nflv] for r in rows))
    s.mean(name, f"{clv_nflv}_mean", _vals(rows, lambda r: r[clv_nflv]), min_n=CLV_MIN_N,
           null_value=0.0)
    s.mean(name, f"{clv_own}_mean", _vals(rows, lambda r: r[clv_own]), min_n=CLV_MIN_N,
           null_value=0.0)
    s.mean(name, f"clv_basis_{market}_mean", _vals(rows, lambda r: _basis(r, market)),
           min_n=CLV_MIN_N, null_value=0.0)
    s.corr(name, f"edge_corr_{market}_nflv", x, y)


def _edge_slices(rows: Sequence[Row], edge_col: str) -> list[tuple[str, list[Row]]]:
    out = []
    for lo, hi in EDGE_BUCKETS:
        label = f"{edge_col}={lo}-{hi}" if hi is not None else f"{edge_col}={lo}+"
        out.append((label, [
            r for r in rows
            if r[edge_col] is not None and abs(r[edge_col]) >= lo
            and (hi is None or abs(r[edge_col]) < hi)
        ]))
    return out


def summarize(rows: Sequence[Row], computed_at: dt.datetime) -> list[dict[str, Any]]:
    """Every grade_summary row from every projection_grades row in scope. Grades exclude
    postseason games (outside the fit) and games whose kickoff moved after the lock."""
    s = _Summary(computed_at)
    in_fit = [r for r in rows if not r["outside_fit_scope"]]
    graded = [r for r in in_fit if r["grade_status"] == "graded" and not r["kickoff_moved"]]

    general: list[tuple[str, list[Row]]] = [("all", list(graded))]
    general += [(f"stability_bucket={b}", [r for r in graded if r["stability_bucket"] == b])
                for b in _BUCKETS]
    general += [(f"weeks={lo}-{hi}" if hi < 99 else f"weeks={lo}+",
                 [r for r in graded if lo <= r["week"] <= hi]) for lo, hi in WEEK_RANGES]
    general += [(f"season={season}", [r for r in graded if r["season"] == season])
                for season in sorted({r["season"] for r in graded})]
    for name, part in general:
        _accuracy(s, name, part)
        _market_family(s, name, part, "spread")
        _market_family(s, name, part, "total")

    spread_slices = _edge_slices(graded, "edge_spread_lock") + [
        ("flag=spread_key_straddle", [r for r in graded if r["flag_spread_key_straddle"]]),
        ("flag=spread_within_book_range",
         [r for r in graded if r["flag_spread_within_book_range"]]),
    ]
    for name, part in spread_slices:
        _market_family(s, name, part, "spread")
    total_slices = _edge_slices(graded, "edge_total_lock") + [
        ("flag=total_within_book_range",
         [r for r in graded if r["flag_total_within_book_range"]]),
    ]
    for name, part in total_slices:
        _market_family(s, name, part, "total")

    # Lock rate over every kicked-off game in scope, locked or not. A no-lock game's
    # bucket comes from its frozen card, so games that were never projected are 'unknown'.
    lock_slices: list[tuple[str, list[Row]]] = [("all", in_fit)]
    lock_slices += [(f"season={se},week={wk}",
                     [r for r in in_fit if (r["season"], r["week"]) == (se, wk)])
                    for se, wk in sorted({(r["season"], r["week"]) for r in in_fit})]
    lock_slices += [(f"stability_bucket={b}",
                     [r for r in in_fit if (r["stability_bucket"] or "unknown") == b])
                    for b in (*_BUCKETS, "unknown")]
    for name, part in lock_slices:
        locked = sum(r["grade_status"] != "no_lock" for r in part)
        s.rate(name, "lock_rate", locked, len(part), min_n=LOCK_RATE_MIN_N, null_value=None)

    tests = [r for r in s.rows if r["verdict"] not in ("insufficient_n", "descriptive")
             and r["ci_low"] is not None]
    s.add("meta", "tests_reported", len(tests), float(len(tests)), (None, None),
          null_value=None, min_n=0, tested=False)
    s.add("meta", "expected_false_positives_95", len(tests), 0.05 * len(tests),
          (None, None), null_value=None, min_n=0, tested=False)
    return s.rows


# --------------------------------------------------------------------------------------
# DB I/O (thin -- feeds the pure functions above)
# --------------------------------------------------------------------------------------

_GAMES_COLS = (
    "game_id", "season", "week", "season_type", "gameday", "gametime", "home_team",
    "away_team", "location", "div_game", "home_score", "away_score", "spread_line",
    "total_line", "updated_at",
)
_LOCK_COLS = (
    "id", "game_id", "locked_at", "kickoff", "lock_lead_hours", "model_version",
    "stability_min", "stability_bucket", "spread_sd", "total_sd", "market_status",
    "market_spread", "market_total", "projected_spread", "projected_total", "edge_spread",
    "edge_total", "card",
)


def load_scope_games(conn: psycopg.Connection, now: dt.datetime) -> list[GradeGame]:
    """Games from GRADING_START on that have kicked off by `now`."""
    season0, week0 = GRADING_START
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_GAMES_COLS)} FROM games "
            "WHERE (season > %s OR (season = %s AND week >= %s)) "
            "AND gameday <= %s AND gametime IS NOT NULL ORDER BY game_id",
            (season0, season0, week0, to_gameday(now)),
        )
        rows = cur.fetchall()
    games = []
    for r in rows:
        d = dict(zip(_GAMES_COLS, r, strict=True))
        kickoff = kickoff_utc(d.pop("gameday"), d.pop("gametime"))
        if kickoff <= now:
            games.append(GradeGame(kickoff=kickoff, **d))
    return games


def _load_existing(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, games_updated_at, grader_version FROM projection_grades "
            "WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def _load_locks(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_LOCK_COLS)} FROM projection_log WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        return {r[1]: dict(zip(_LOCK_COLS, r, strict=True)) for r in cur.fetchall()}


def _load_cards(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, tuple[int, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, projection_status, card FROM matchup_cards "
            "WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


@dataclass
class GradeResult:
    grades: list[dict[str, Any]]
    summary: list[dict[str, Any]]
    meta: dict[str, Any]


def build_grades(
    games: Sequence[GradeGame],
    locks: Mapping[str, Mapping[str, Any]],
    cards: Mapping[str, tuple[int, Any]],
    captures: Mapping[str, Sequence[Capture]],
    now: dt.datetime,
) -> GradeResult:
    """Everything the run writes, from already-loaded rows. No DB access."""
    grades = []
    for g in sorted(games, key=lambda x: x.game_id):
        card_status, card = cards.get(g.game_id, (None, None))
        grades.append(grade_game(
            g, lock=locks.get(g.game_id), card_status=card_status, card=card,
            captures=captures.get(g.game_id, []), now=now,
        ))
    status_counts: dict[str, int] = {}
    for r in grades:
        status_counts[r["grade_status"]] = status_counts.get(r["grade_status"], 0) + 1
    meta = {
        "games_in_scope": len(grades),
        "grade_status_counts": status_counts,
        "lock_line_parity_failures": sorted(
            r["game_id"] for r in grades if r["lock_line_parity"] is False
        ),
        "kickoff_moved": sorted(r["game_id"] for r in grades if r["kickoff_moved"]),
        "own_close_after_lock": sum(bool(r["own_close_after_lock"]) for r in grades),
        "grader_version": GRADER_VERSION,
    }
    return GradeResult(grades, summarize(grades, now), meta)


def _upsert_grades(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    """Upsert keyed on game_id. result_first_seen_at keeps its first non-null value, so
    result lag stays measurable across re-grades."""
    if not rows:
        return 0
    cols = list(rows[0])
    updates = [
        "result_first_seen_at = COALESCE(projection_grades.result_first_seen_at, "
        "EXCLUDED.result_first_seen_at)"
        if c == "result_first_seen_at" else f"{c} = EXCLUDED.{c}"
        for c in cols if c != "game_id"
    ]
    query = (
        f"INSERT INTO projection_grades ({', '.join(cols)}) "
        f"VALUES ({', '.join(f'%({c})s' for c in cols)}) "
        f"ON CONFLICT (game_id) DO UPDATE SET {', '.join(updates)}"
    )
    with conn.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def _replace_summary(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM grade_summary WHERE grader_version = %s", (GRADER_VERSION,))
        if rows:
            cols = list(rows[0])
            cur.executemany(
                f"INSERT INTO grade_summary ({', '.join(cols)}) "
                f"VALUES ({', '.join(f'%({c})s' for c in cols)})",
                rows,
            )
    return len(rows)


class ProjectionGrader(Grader):
    name = "grader"

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """Something to (re)grade: a kicked-off game with no grade row, one whose games row
        changed since it was graded (a final score, a corrected line), or a grade from an
        older GRADER_VERSION."""
        games = load_scope_games(ctx.conn, ctx.now)
        existing = _load_existing(ctx.conn, [g.game_id for g in games])
        for g in games:
            seen = existing.get(g.game_id)
            if seen is None or g.updated_at > seen[0] or seen[1] != GRADER_VERSION:
                return True
        return False

    def compute(self, ctx: RunContext) -> GradeResult:
        conn = ctx.conn
        games = load_scope_games(conn, ctx.now)
        game_ids = [g.game_id for g in games]
        locks = _load_locks(conn, game_ids)
        return build_grades(
            games,
            locks,
            _load_cards(conn, [gid for gid in game_ids if gid not in locks]),
            load_captures(conn, list(locks)),
            ctx.now,
        )

    def write(self, ctx: RunContext, computed: GradeResult) -> WorkResult:
        changed = filter_changed(ctx.conn, "projection_grades", "game_id", computed.grades)
        written = _upsert_grades(ctx.conn, changed)
        summary = _replace_summary(ctx.conn, computed.summary)
        meta = {
            **computed.meta,
            "grades_written": written,
            "summary_rows": summary,
            "graded_now": sorted(r["game_id"] for r in changed
                                 if r["grade_status"] == "graded"),
        }
        return WorkResult(written + summary, meta)

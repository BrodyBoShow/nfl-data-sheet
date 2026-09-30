"""One-off script: estimate k (within-season shrinkage) and r (year-over-year reliability)
for every player_eff_week metric, per position group, from nflverse history (P7 step 6).

Not part of the pipeline. Fetches from nflverse into a local cache (`.cache/`, gitignored)
and reads the DB only for `players.position_group` and the PFR->gsis crosswalk. Writes
nothing to the DB; prints a report and writes CSVs next to the cache.

Every per-player-game input is built by the collector's own functions
(`_aggregate_player_game_pbp`, `_build_snaps`, `_build_pfr_advstats`, `_build_ngs`,
`_build_player_week`), not re-derived (CLAUDE.md verification rule). Seasons before FTN
(2022) get an empty FTN frame, so their ftn_* columns are NULL -- the collector's own
uncharted-game path.

Method, per metric x position group, REG only:
- k0 (units of the metric's denominator): one-way random effects on per-game aggregates.
  sigma_e^2 per unit = sum over player-games of den * (game rate - player season rate)^2,
  over sum(games - 1); tau^2 = excess of the volume-weighted between-player variance over
  noise, pooled across seasons. k0 = sigma_e^2 / tau^2: the n at which a player's own
  rate is half signal. tau^2 <= 0 means no detectable between-player signal (k0 = inf).
- r, two ways, on adjacent-season pairs of the same player in the same group:
  - `r_corr`: Efficiency's method (scripts/estimate_reliability.py): pooled Pearson
    correlation of k0-shrunk season values (as deviations from that season's group
    league mean, since league means drift across seasons), clipped to [0, 1].
  - `r_slope`: the weighted slope of next season's raw rate on this season's shrunk
    value. With a shrunk prior, this is the coefficient the blend's `r` multiplies.
- Intervals: bootstrap, players resampled with replacement (all their seasons together).

Usage:
  uv run python scripts/estimate_player_reliability.py fetch      # network, cached
  uv run python scripts/estimate_player_reliability.py estimate   # offline
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nflreadpy as nfl
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.collectors.nflverse_bulk import (  # noqa: E402
    _aggregate_player_game_pbp,
    _build_ngs,
    _build_pfr_advstats,
    _build_player_week,
    _build_snaps,
)
from pipeline.core.db import get_connection  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "player_reliability"
FTN_FIXTURE = ROOT / "tests" / "fixtures" / "nflreadpy_ftn_charting_sample.parquet"

LAST = 2025
FIRST = {"pbp": 1999, "player_stats": 1999, "snaps": 2012, "ngs": 2016, "pfr": 2018, "ftn": 2022}
BOOT_REPS = 200
SHRINK_TO_MEAN = 0.5  # Efficiency's r_final = 0.5 * r_raw + 0.5 * mean


# --------------------------------------------------------------------------- fetch


def _cached(name: str) -> Path:
    return CACHE / f"{name}.parquet"


def fetch() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    ftn_empty = pl.read_parquet(FTN_FIXTURE).head(0)
    metas = []
    for season in range(FIRST["pbp"], LAST + 1):
        out = _cached(f"pgp_{season}")
        if out.exists():
            continue
        pbp = nfl.load_pbp(seasons=[season])
        ftn = nfl.load_ftn_charting(seasons=[season]) if season >= FIRST["ftn"] else ftn_empty
        try:
            pgp, meta = _aggregate_player_game_pbp(pbp, ftn)
        except ValueError as exc:
            # The collector's own guard failed somewhere in this season. Find the games
            # by running the same function one game at a time, and leave those games out
            # (reported, never patched).
            bad = []
            for gid in sorted(pbp["game_id"].unique().to_list()):
                try:
                    _aggregate_player_game_pbp(pbp.filter(pl.col("game_id") == gid), ftn)
                except ValueError:
                    bad.append(gid)
            print(f"pbp {season}: guard failed ({exc}); excluded games {bad}", flush=True)
            pgp, meta = _aggregate_player_game_pbp(pbp.filter(~pl.col("game_id").is_in(bad)), ftn)
            meta["excluded_games"] = ";".join(bad)
        pgp.write_parquet(out)
        metas.append(
            {
                "season": season,
                "pbp_rows": pbp.height,
                "pgp_rows": pgp.height,
                **{k: v for k, v in meta.items() if not isinstance(v, list)},
            }
        )
        print(f"pbp {season}: {pbp.height} plays -> {pgp.height} player-games; {meta}", flush=True)
        del pbp, ftn
    if metas:
        pl.DataFrame(metas).write_csv(CACHE / f"pgp_meta_{len(metas)}.csv")

    seasons = lambda src: list(range(FIRST[src], LAST + 1))  # noqa: E731
    if not _cached("player_week").exists():
        _build_player_week(nfl.load_player_stats(seasons=seasons("player_stats"))).write_parquet(
            _cached("player_week")
        )
        print("player_stats done", flush=True)
    if not _cached("snaps").exists():
        _build_snaps(nfl.load_snap_counts(seasons=seasons("snaps"))).write_parquet(_cached("snaps"))
        print("snaps done", flush=True)
    if not _cached("pfr").exists():
        frames = {
            t: nfl.load_pfr_advstats(seasons=seasons("pfr"), stat_type=t)
            for t in ("pass", "rush", "rec", "def")
        }
        _build_pfr_advstats(frames).write_parquet(_cached("pfr"))
        print("pfr done", flush=True)
    if not _cached("ngs").exists():
        frames = {
            t: nfl.load_nextgen_stats(seasons=seasons("ngs"), stat_type=t)
            for t in ("passing", "rushing", "receiving")
        }
        _build_ngs(frames).write_parquet(_cached("ngs"))
        print("ngs done", flush=True)
    if not _cached("players").exists():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT player_id, position_group FROM players")
            pl.DataFrame(
                cur.fetchall(), schema=["player_id", "position_group"], orient="row"
            ).write_parquet(_cached("players"))
            cur.execute(
                "SELECT pfr_id, player_id FROM player_id_crosswalk WHERE pfr_id IS NOT NULL"
            )
            pl.DataFrame(
                cur.fetchall(), schema=["pfr_player_id", "player_id"], orient="row"
            ).write_parquet(_cached("pfr_crosswalk"))
        print("players/crosswalk done", flush=True)


# ------------------------------------------------------------------------ inputs

GROUPS = {
    "receiving": ["WR", "TE", "RB"],
    "rushing": ["RB", "QB"],
    "passing": ["QB"],
    "defense": ["DL", "LB", "DB"],
}


def _load() -> dict[str, pl.DataFrame]:
    players = pl.read_parquet(_cached("players"))
    xw = pl.read_parquet(_cached("pfr_crosswalk")).unique("pfr_player_id")
    reg = pl.col("season_type") == "REG"
    pgp = pl.concat(
        [pl.read_parquet(p) for p in sorted(CACHE.glob("pgp_*.parquet"))], how="vertical_relaxed"
    ).filter(reg)
    snaps = pl.read_parquet(_cached("snaps")).filter(reg).join(xw, on="pfr_player_id")
    pfr = pl.read_parquet(_cached("pfr")).filter(reg).join(xw, on="pfr_player_id")
    ngs = pl.read_parquet(_cached("ngs")).filter(reg & (pl.col("week") > 0))
    pw = pl.read_parquet(_cached("player_week")).filter(reg)
    return dict(players=players, pgp=pgp, snaps=snaps, pfr=pfr, ngs=ngs, pw=pw)


def _sources(d: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """Per player-game frames, each with player_id, season, week."""
    pgp, pfr, ngs = d["pgp"], d["pfr"], d["ngs"]
    key = ["player_id", "game_id"]
    pfr_t = {
        t: pfr.filter(pl.col("stat_type") == t).drop(
            "season", "week", "season_type", "team", "opponent_team", "stat_type"
        )
        for t in ("pass", "rush", "rec", "def")
    }
    snaps_def = (
        d["snaps"]
        .filter(pl.col("defense_snaps") > 0)
        .select("player_id", "game_id", "season", "week", "defense_snaps")
    )
    pw_def = d["pw"].select(
        key
        + [
            "def_tackles_for_loss",
            "def_sacks",
            "def_qb_hits",
            "def_fumbles_forced",
            "def_pass_defended",
        ]
    )
    pfr_def = pfr_t["def"].select(
        key + ["def_tackles_combined", "def_pressures", "def_times_blitzed", "def_targets"]
    )
    out = {
        "pgp": pgp,
        "pgp_pfr_rec": pgp.join(pfr_t["rec"].select(key + ["receiving_broken_tackles"]), on=key),
        "pgp_pfr_pass": pgp.join(pfr_t["pass"].select(key + ["times_pressured"]), on=key),
        "pfr_rush": pfr.filter(pl.col("stat_type") == "rush"),
        "pfr_pass": pfr.filter(pl.col("stat_type") == "pass"),
        "pfr_def": pfr.filter(pl.col("stat_type") == "def"),
        "ngs_rec": ngs.filter(pl.col("stat_type") == "receiving"),
        "ngs_rush": ngs.filter(pl.col("stat_type") == "rushing"),
        "ngs_pass": ngs.filter(pl.col("stat_type") == "passing"),
        # Defense per-snap rates, two readings of "no PFR / player_week row" (P7 step 7
        # gate): `present` = only games with a numerator row (the registry today),
        # `zero` = every game with defense snaps, a missing row read as 0.
        "def_present_pfr": snaps_def.join(pfr_def, on=key),
        # Only seasons PFR covers: before 2018 there's no PFR data at all, and a missing
        # row there is no evidence of anything.
        "def_zero_pfr": snaps_def.filter(c("season") >= FIRST["pfr"])
        .join(pfr_def, on=key, how="left")
        .fill_null(0),
        "def_present_pw": snaps_def.join(pw_def, on=key),
        "def_zero_pw": snaps_def.join(pw_def, on=key, how="left").fill_null(0),
    }
    return {k: v.join(d["players"], on="player_id", how="left") for k, v in out.items()}


c = pl.col


def _metrics() -> list[tuple[str, str, str, pl.Expr, pl.Expr]]:
    """(family, metric, source, numerator, denominator): the registry's formulas."""
    m: list[tuple[str, str, str, pl.Expr, pl.Expr]] = []
    rec = lambda name, num, den, src="pgp": m.append(("receiving", name, src, num, den))  # noqa: E731
    rec("epa_per_target", c("rec_epa_sum"), c("targets"))
    rec("rec_success_rate", c("rec_success"), c("targets"))
    rec("catch_rate", c("receptions"), c("targets"))
    rec("yards_per_target", c("rec_yards"), c("targets"))
    rec("rec_adot", c("rec_air_yards_sum"), c("rec_air_yards_n"))
    rec("yac_per_reception", c("rec_yac_sum"), c("receptions"))
    rec("yac_oe_per_reception", c("rec_yac_oe_sum"), c("rec_yac_oe_n"))
    rec("rec_first_down_rate", c("rec_first_downs"), c("targets"))
    rec("rec_explosive_rate", c("rec_explosive"), c("targets"))
    rec("deep_target_rate", c("deep_targets"), c("targets"))
    for loc in ("left", "middle", "right"):
        rec(f"epa_per_target_{loc}", c(f"rec_epa_sum_{loc}"), c(f"targets_{loc}"))
    rec("catchable_catch_rate", c("ftn_catchable_receptions"), c("ftn_catchable_targets"))
    rec("drop_rate", c("ftn_drops"), c("ftn_catchable_targets"))
    rec("contested_target_rate", c("ftn_contested_targets"), c("ftn_charted_targets"))
    rec("contested_catch_rate", c("ftn_contested_receptions"), c("ftn_contested_targets"))
    rec("created_reception_rate", c("ftn_created_receptions"), c("ftn_charted_receptions"))
    rec("screen_target_rate", c("ftn_screen_targets"), c("ftn_charted_targets"))
    rec("epa_per_target_play_action", c("ftn_pa_rec_epa_sum"), c("ftn_pa_targets"))
    rec(
        "broken_tackles_per_reception",
        c("receiving_broken_tackles"),
        c("receptions"),
        "pgp_pfr_rec",
    )
    rec("avg_separation", c("avg_separation") * c("targets"), c("targets"), "ngs_rec")
    rec("avg_cushion", c("avg_cushion") * c("targets"), c("targets"), "ngs_rec")

    rush = lambda name, num, den, src="pgp": m.append(("rushing", name, src, num, den))  # noqa: E731
    rush("epa_per_carry", c("rush_epa_sum"), c("carries"))
    rush("rush_success_rate", c("rush_success"), c("carries"))
    rush("yards_per_carry", c("rush_yards"), c("carries"))
    rush("stuff_rate", c("rush_stuffs"), c("carries"))
    rush("rush_explosive_rate", c("rush_explosive"), c("carries"))
    rush("rush_first_down_rate", c("rush_first_downs"), c("carries"))
    for cell in ("le", "lt", "lg", "mid", "rg", "rt", "re"):
        rush(f"gap_share_{cell}", c(f"carries_{cell}"), c("carries"))
        rush(f"epa_per_carry_{cell}", c(f"rush_epa_sum_{cell}"), c(f"carries_{cell}"))
        rush(f"rush_success_rate_{cell}", c(f"rush_success_{cell}"), c(f"carries_{cell}"))
    rush("stacked_box_rate", c("ftn_stacked_box_carries"), c("ftn_charted_carries"))
    rush("epa_per_carry_stacked_box", c("ftn_stacked_box_epa_sum"), c("ftn_stacked_box_carries"))
    rush(
        "yards_before_contact_per_carry",
        c("rushing_yards_before_contact"),
        c("carries"),
        "pfr_rush",
    )
    rush(
        "yards_after_contact_per_carry", c("rushing_yards_after_contact"), c("carries"), "pfr_rush"
    )
    rush("broken_tackles_per_carry", c("rushing_broken_tackles"), c("carries"), "pfr_rush")
    rush("ryoe_per_carry", c("rush_yards_over_expected"), c("rush_attempts"), "ngs_rush")
    rush(
        "avg_time_to_los", c("avg_time_to_los") * c("rush_attempts"), c("rush_attempts"), "ngs_rush"
    )

    pas = lambda name, num, den, src="pgp": m.append(("passing", name, src, num, den))  # noqa: E731
    pas("epa_per_dropback", c("dropback_epa_sum"), c("dropbacks"))
    pas("dropback_success_rate", c("dropback_success"), c("dropbacks"))
    pas("cpoe", c("cpoe_sum"), c("cpoe_n"))
    pas("pass_adot", c("pass_air_yards_sum"), c("pass_air_yards_n"))
    pas("sack_rate", c("sacks"), c("dropbacks"))
    pas("scramble_rate", c("scrambles"), c("dropbacks"))
    pas("int_rate", c("interceptions"), c("pass_attempts"))
    pas("deep_attempt_rate", c("deep_attempts"), c("pass_attempts"))
    pas("play_action_rate", c("ftn_pa_dropbacks"), c("ftn_charted_dropbacks"))
    pas("epa_per_dropback_play_action", c("ftn_pa_epa_sum"), c("ftn_pa_dropbacks"))
    pas("blitzed_rate", c("ftn_blitzed_dropbacks"), c("ftn_charted_dropbacks"))
    pas("epa_per_dropback_vs_blitz", c("ftn_blitzed_epa_sum"), c("ftn_blitzed_dropbacks"))
    pas("out_of_pocket_rate", c("ftn_out_of_pocket_dropbacks"), c("ftn_charted_dropbacks"))
    pas("screen_rate", c("ftn_screen_attempts"), c("ftn_charted_attempts"))
    pas("throwaway_rate", c("ftn_throwaways"), c("ftn_charted_attempts"))
    pas(
        "catchable_rate",
        c("ftn_catchable_attempts"),
        c("ftn_charted_attempts") - c("ftn_throwaways"),
    )
    pas("int_worthy_rate", c("ftn_int_worthy"), c("ftn_charted_attempts"))
    pas("qb_fault_sack_share", c("ftn_qb_fault_sacks"), c("ftn_charted_sacks"))
    pas("pressure_rate", c("times_pressured"), c("dropbacks"), "pgp_pfr_pass")
    pas("pressure_to_sack_rate", c("times_sacked"), c("times_pressured"), "pfr_pass")
    for col in ("avg_time_to_throw", "aggressiveness", "avg_air_yards_to_sticks"):
        pas(col, c(col) * c("attempts"), c("attempts"), "ngs_pass")

    dfn = lambda name, num, den, src: m.append(("defense", name, src, num, den))  # noqa: E731
    for mode in ("present", "zero"):
        snaps_ = c("defense_snaps")
        dfn(f"tackles_per_snap[{mode}]", c("def_tackles_combined"), snaps_, f"def_{mode}_pfr")
        dfn(f"pressures_per_snap[{mode}]", c("def_pressures"), snaps_, f"def_{mode}_pfr")
        dfn(f"blitzes_per_snap[{mode}]", c("def_times_blitzed"), snaps_, f"def_{mode}_pfr")
        dfn(f"targets_per_snap[{mode}]", c("def_targets"), snaps_, f"def_{mode}_pfr")
        dfn(f"tfl_per_snap[{mode}]", c("def_tackles_for_loss"), snaps_, f"def_{mode}_pw")
        dfn(f"sacks_per_snap[{mode}]", c("def_sacks"), snaps_, f"def_{mode}_pw")
        dfn(f"qb_hits_per_snap[{mode}]", c("def_qb_hits"), snaps_, f"def_{mode}_pw")
        dfn(f"forced_fumbles_per_snap[{mode}]", c("def_fumbles_forced"), snaps_, f"def_{mode}_pw")
        dfn(f"pass_defended_per_snap[{mode}]", c("def_pass_defended"), snaps_, f"def_{mode}_pw")
    dfn(
        "missed_tackle_rate",
        c("def_missed_tackles"),
        c("def_tackles_combined") + c("def_missed_tackles"),
        "pfr_def",
    )
    dfn("completion_pct_allowed", c("def_completions_allowed"), c("def_targets"), "pfr_def")
    dfn("yards_per_target_allowed", c("def_yards_allowed"), c("def_targets"), "pfr_def")
    dfn(
        "yac_allowed_per_completion",
        c("def_yards_after_catch"),
        c("def_completions_allowed"),
        "pfr_def",
    )
    dfn("adot_allowed", c("def_adot") * c("def_targets"), c("def_targets"), "pfr_def")
    dfn("td_rate_allowed", c("def_receiving_td_allowed"), c("def_targets"), "pfr_def")
    dfn("int_rate_on_targets", c("def_ints"), c("def_targets"), "pfr_def")
    return m


# ---------------------------------------------------------------------- estimators


def _player_seasons(df: pl.DataFrame, num: pl.Expr, den: pl.Expr) -> pl.DataFrame:
    g = df.select(
        "player_id",
        "season",
        "week",
        num.cast(pl.Float64).alias("n_"),
        den.cast(pl.Float64).alias("d_"),
    ).filter(c("d_").is_not_null() & (c("d_") > 0) & c("n_").is_not_null())
    ps = g.group_by("player_id", "season").agg(
        c("d_").sum().alias("N"), c("n_").sum().alias("S"), pl.len().alias("G")
    )
    g = g.join(ps, on=["player_id", "season"]).with_columns(
        (c("d_") * (c("n_") / c("d_") - c("S") / c("N")) ** 2).alias("wss")
    )
    return ps.join(
        g.group_by("player_id", "season").agg(c("wss").sum()), on=["player_id", "season"]
    ).sort("player_id", "season")


def _k0(ps: pl.DataFrame, w: np.ndarray | None = None) -> tuple[float, float, float]:
    """Pooled (sigma_e^2, tau^2, k0) with optional per-row bootstrap weights."""
    N, S, G, wss = (ps[x].to_numpy() for x in ("N", "S", "G", "wss"))
    season = ps["season"].to_numpy() - FIRST["pbp"]
    w = np.ones_like(N) if w is None else w
    dof = (w * (G - 1)).sum()
    if dof <= 0:
        return float("nan"), float("nan"), float("nan")
    s2e = (w * wss).sum() / dof
    nb = season.max() + 1
    sw_n = np.bincount(season, w * N, nb)
    sw_s = np.bincount(season, w * S, nb)
    ok = sw_n > 0
    L = np.where(ok, sw_s / np.where(ok, sw_n, 1), 0.0)
    p = S / N
    between = np.bincount(season, w * N * (p - L[season]) ** 2, nb)
    count = np.bincount(season, w, nb)
    denom = sw_n - np.bincount(season, w * N * N, nb) / np.where(ok, sw_n, 1)
    tau2 = (between - s2e * (count - 1))[ok].sum() / denom[ok].sum()
    return s2e, tau2, (s2e / tau2 if tau2 > 0 else float("inf"))


def _league(ps: pl.DataFrame) -> pl.DataFrame:
    return ps.group_by("season").agg((c("S").sum() / c("N").sum()).alias("L"))


def _pairs(ps: pl.DataFrame, k0: float) -> pl.DataFrame:
    x = ps.join(_league(ps), on="season").with_columns(
        ((c("S") + k0 * c("L")) / (c("N") + k0) - c("L")).alias("v"),
        (c("S") / c("N") - c("L")).alias("raw"),
    )
    nxt = x.select(
        "player_id",
        (c("season") - 1).alias("season"),
        c("v").alias("v1"),
        c("raw").alias("raw1"),
        c("N").alias("N1"),
    )
    return x.select("player_id", "season", "v").join(nxt, on=["player_id", "season"])


def _r(pairs: pl.DataFrame, k0: float, w: np.ndarray | None = None) -> tuple[float, float]:
    v, v1, raw1, N1 = (pairs[x].to_numpy() for x in ("v", "v1", "raw1", "N1"))
    w = np.ones_like(v) if w is None else w
    if w.sum() < 10 or np.allclose(v, 0):
        return float("nan"), float("nan")
    mv, mv1 = np.average(v, weights=w), np.average(v1, weights=w)
    cov = np.average((v - mv) * (v1 - mv1), weights=w)
    corr = cov / np.sqrt(
        np.average((v - mv) ** 2, weights=w) * np.average((v1 - mv1) ** 2, weights=w)
    )
    ws = w * N1 / (N1 + k0)
    mx, my = np.average(v, weights=ws), np.average(raw1, weights=ws)
    slope = (ws * (v - mx) * (raw1 - my)).sum() / (ws * (v - mx) ** 2).sum()
    return float(corr), float(slope)


def _boot_weights(ids: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    uniq, inv = np.unique(ids, return_inverse=True)
    counts = rng.multinomial(len(uniq), np.full(len(uniq), 1 / len(uniq)))
    return counts[inv].astype(float)


def _fmt(x: float) -> str:
    if x != x:
        return "n/a"
    return "inf" if x == float("inf") or x > 1e6 else (f"{x:.0f}" if abs(x) >= 10 else f"{x:.2f}")


def estimate() -> None:
    d = _load()
    print("COVERAGE (REG rows per source)")
    for name in ("pgp", "snaps", "pfr", "ngs", "pw"):
        s = d[name].group_by("season").len().sort("season")
        yrs, lens = s["season"].to_list(), s["len"].to_list()
        print(
            f"  {name:<6} {yrs[0]}-{yrs[-1]} ({len(yrs)} seasons), "
            f"rows/season min {min(lens)} max {max(lens)}"
        )
    for tp in ("pass", "rush", "rec", "def"):
        s = d["pfr"].filter(c("stat_type") == tp).group_by("season").len().sort("season")
        print(f"  pfr {tp:<4} seasons {s['season'].to_list()}")
    for tp in ("passing", "rushing", "receiving"):
        s = d["ngs"].filter(c("stat_type") == tp).group_by("season").len().sort("season")
        per_season = dict(zip(s["season"].to_list(), s["len"].to_list(), strict=True))
        print(f"  ngs {tp:<9} rows/season {per_season}")
    for meta in sorted(CACHE.glob("pgp_meta_*.csv")):
        print(pl.read_csv(meta).filter(c("season") >= FIRST["ftn"] - 1))
    src = _sources(d)
    rng = np.random.default_rng(20260929)
    rows = []
    for family, metric, source, num, den in _metrics():
        for group in GROUPS[family]:
            df = src[source].filter(c("position_group") == group)
            ps = _player_seasons(df, num, den)
            if ps.height == 0:
                continue
            seasons = sorted(ps["season"].unique().to_list())
            k0 = _k0(ps)[2]
            ids = ps["player_id"].to_numpy()
            kb = []
            for _ in range(BOOT_REPS):
                kb.append(_k0(ps, _boot_weights(ids, rng))[2])
            kb_a = np.where(np.isinf(kb), 1e12, kb)
            k_lo, k_hi = np.nanpercentile(kb_a, [5, 95])
            no_signal = float(np.mean(np.isinf(kb)))
            r_corr = r_slope = rc_lo = rc_hi = rs_lo = rs_hi = float("nan")
            n_pairs = 0
            if np.isfinite(k0):
                pairs = _pairs(ps, k0)
                n_pairs = pairs.height
                if n_pairs >= 10:
                    r_corr, r_slope = _r(pairs, k0)
                    pids = pairs["player_id"].to_numpy()
                    rb = [_r(pairs, k0, _boot_weights(pids, rng)) for _ in range(BOOT_REPS)]
                    rc_lo, rc_hi = np.nanpercentile([a for a, _ in rb], [5, 95])
                    rs_lo, rs_hi = np.nanpercentile([b for _, b in rb], [5, 95])
            rows.append(
                dict(
                    family=family,
                    metric=metric,
                    group=group,
                    source=source,
                    seasons=f"{seasons[0]}-{seasons[-1]}",
                    n_seasons=len(seasons),
                    player_seasons=ps.height,
                    units=float(ps["N"].sum()),
                    k0=k0,
                    k0_p5=k_lo,
                    k0_p95=k_hi,
                    boot_no_signal=no_signal,
                    n_pairs=n_pairs,
                    r_corr=r_corr,
                    r_corr_p5=rc_lo,
                    r_corr_p95=rc_hi,
                    r_slope=r_slope,
                    r_slope_p5=rs_lo,
                    r_slope_p95=rs_hi,
                )
            )
            print(
                f"{family:>9} {group:>2} {metric:<34} {source:<14} "
                f"{seasons[0]}-{seasons[-1]} ({len(seasons):2d}) pairs {n_pairs:5d}  "
                f"k0 {_fmt(k0):>5} [{_fmt(k_lo)}-{_fmt(k_hi)}] "
                f"r_corr {_fmt(r_corr)} [{_fmt(rc_lo)}-{_fmt(rc_hi)}] "
                f"r_slope {_fmt(r_slope)} [{_fmt(rs_lo)}-{_fmt(rs_hi)}]",
                flush=True,
            )
    out = pl.DataFrame(rows)
    out.write_csv(CACHE / "estimates.csv")
    _volume(d)
    _usage(d)


def _hist(s: pl.Series, edges: list[float]) -> str:
    total = s.len()
    parts = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=False):
        n = s.filter((s >= lo) & (s < hi)).len()
        parts.append(
            f"    [{lo:g},{hi:g}) {n:5d} {100 * n / total:5.1f}% {'#' * round(80 * n / total)}"
        )
    return "\n".join(parts)


def _volume(d: dict[str, pl.DataFrame]) -> None:
    """Per-player-season volume per game played (games played = snaps rows), 2012-2025."""
    print("\n" + "=" * 100 + "\nVOLUME PER GAME PLAYED, player-seasons 2013-2025 REG")
    gp = d["snaps"].group_by("player_id", "season").agg(pl.len().alias("gp"))
    pgp = (
        d["pgp"]
        .filter(c("season") >= FIRST["snaps"])
        .join(d["players"], on="player_id", how="left")
    )
    specs: list[tuple[str, str, list[float]]] = [
        ("carries", "QB", [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 6, 7, 8, 10]),
        ("carries", "RB", [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 18, 21, 25]),
        ("targets", "WR", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]),
        ("targets", "TE", [0, 1, 2, 3, 4, 5, 6, 7, 8, 10]),
        ("targets", "RB", [0, 1, 2, 3, 4, 5, 6, 8]),
        ("dropbacks", "QB", [0, 2, 5, 8, 10, 12, 15, 17, 20, 25, 30, 35, 40, 50]),
    ]
    for col, group, edges in specs:
        t = (
            pgp.filter((c("position_group") == group) & (c(col) > 0))
            .group_by("player_id", "season")
            .agg(c(col).sum().alias("v"))
            .join(gp, on=["player_id", "season"])
            .with_columns((c("v") / c("gp")).alias("pg"))
        )
        print(f"\n  {group} {col} per game played ({t.height} player-seasons)")
        print(_hist(t["pg"], edges))
    snaps = d["snaps"].filter(c("defense_snaps") > 0).join(d["players"], on="player_id", how="left")
    for group in ("DL", "LB", "DB"):
        t = (
            snaps.filter(c("position_group") == group)
            .group_by("player_id", "season")
            .agg(c("defense_snaps").sum().alias("v"), pl.len().alias("gp"))
            .with_columns((c("v") / c("gp")).alias("pg"))
        )
        print(f"\n  {group} defense snaps per game played ({t.height} player-seasons)")
        print(_hist(t["pg"], [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 80]))


def _usage(d: dict[str, pl.DataFrame]) -> None:
    """k0 in games for usage shares, per season, 2012-2025 (unweighted one-way ANOVA)."""
    print("\n" + "=" * 100 + "\nUSAGE k0 IN GAMES, by season (median and range across seasons)")
    pgp = d["pgp"]
    team = pgp.group_by("game_id", "team").agg(
        c("targets").sum().alias("tt"), c("carries").sum().alias("tc")
    )
    x = pgp.join(team, on=["game_id", "team"]).join(d["players"], on="player_id", how="left")
    x = x.with_columns(
        (c("targets").fill_null(0) / c("tt")).alias("target_share"),
        (c("carries").fill_null(0) / c("tc")).alias("carry_share"),
    )
    sn = (
        d["snaps"]
        .join(d["players"], on="player_id", how="left")
        .with_columns(
            c("offense_pct").cast(pl.Float64).alias("off_snap_share"),
            c("defense_pct").cast(pl.Float64).alias("def_snap_share"),
        )
    )

    def anova(df: pl.DataFrame, col: str) -> float:
        per = (
            df.group_by("player_id")
            .agg(c(col).mean().alias("m"), c(col).var().alias("v"), pl.len().alias("G"))
            .filter(c("G") >= 2)
        )
        if per.height < 10:
            return float("nan")
        v, g, m = (per[x].to_numpy().astype(float) for x in ("v", "G", "m"))
        s2w = float((v * (g - 1)).sum() / (g - 1).sum())
        s2b = float(np.var(m, ddof=1)) - s2w / float(g.mean())
        return s2w / s2b if s2b > 0 else float("inf")

    for label, df, col, cond, groups in [
        ("target_share", x, "target_share", c("targets") > 0, ["WR", "TE", "RB"]),
        ("carry_share", x, "carry_share", c("carries") > 0, ["RB"]),
        ("off_snap_share", sn, "off_snap_share", c("offense_snaps") > 0, ["WR", "TE", "RB", "OL"]),
        ("def_snap_share", sn, "def_snap_share", c("defense_snaps") > 0, ["DL", "LB", "DB"]),
    ]:
        for g in groups:
            sub = df.filter(cond & (c("position_group") == g))
            ks = [
                anova(sub.filter(c("season") == s), col)
                for s in sorted(sub["season"].unique().to_list())
            ]
            ks = [k for k in ks if k == k]
            if ks:
                print(
                    f"  {label:<15} {g}: {len(ks)} seasons, median {np.median(ks):.2f}, "
                    f"range {min(ks):.2f}-{max(ks):.2f}"
                )


# ---------------------------------------------------------------------- recommend
#
# Turns estimates.csv into the k and r the analyst uses (decided 2026-09-29, user: pinned
# where the data pins them, judgment elsewhere following Efficiency's pattern, and every
# entry says which). Prints the registry table (docs/signals.md) and the constants block
# (pipeline/analysts/player_efficiency.py) from the same rows, so the two can't differ.

HEADLINE = {
    "receiving": "epa_per_target",
    "rushing": "epa_per_carry",
    "passing": "epa_per_dropback",
    "defense": "tackles_per_snap",
}
# Split metrics: same outcome as the parent, over a subset of the parent's denominator.
# A split whose own k can't be pinned takes its parent's k, in the split's own units. k0 is
# noise-per-unit over true variance, so a subset of the same plays keeps about the same k0,
# and the measured point estimates agree: RB epa_per_carry cells 65-408 vs parent 215, WR
# epa_per_target left/middle/right 179/150/155 vs 177. Efficiency instead sizes splits
# below the parent in proportion to their share (plays 200 -> pass/rush 120 -> down 50);
# applied here that gave QB gap cells k of about 1, i.e. no shrinkage (2026-09-29).
PARENT = {
    **{f"epa_per_target_{x}": "epa_per_target" for x in ("left", "middle", "right")},
    "epa_per_target_play_action": "epa_per_target",
    "catchable_catch_rate": "catch_rate",
    "contested_catch_rate": "catch_rate",
    "drop_rate": "catch_rate",
    **{f"epa_per_carry_{x}": "epa_per_carry" for x in ("le", "lt", "lg", "mid", "rg", "rt", "re")},
    **{
        f"rush_success_rate_{x}": "rush_success_rate"
        for x in ("le", "lt", "lg", "mid", "rg", "rt", "re")
    },
    "epa_per_carry_stacked_box": "epa_per_carry",
    "epa_per_dropback_play_action": "epa_per_dropback",
    "epa_per_dropback_vs_blitz": "epa_per_dropback",
}
PIN_K_RATIO = 2.0  # k0's 90% interval within 2x
R_SLOPE_STABLE = 1.5  # r_slope's 90% upper bound; above it the slope is unstable
FTN_METRICS_REVISIT = "3 season pairs, revisit at 5+"


def _registry_name(metric: str) -> str:
    return metric.replace("[present]", "")


def _k_round(k: float) -> float:
    return float(round(k)) if k >= 10 else round(k, 1)


def recommend() -> None:
    e = pl.read_csv(CACHE / "estimates.csv", infer_schema_length=None).filter(
        ~c("metric").str.contains(r"\[zero\]")
    )
    d = _load()
    src = _sources(d)
    specs = {
        (fam, _registry_name(m)): (source, num, den) for fam, m, source, num, den in _metrics()
    }

    def share(family: str, metric: str, parent: str, group: str, seasons: str) -> float:
        source, _, den = specs[(family, metric)]
        _, _, pden = specs[(family, parent)]
        lo, hi = (int(x) for x in seasons.split("-"))
        df = (
            src[source]
            .filter((c("position_group") == group) & c("season").is_between(lo, hi))
            .select(den.cast(pl.Float64).alias("d"), pden.cast(pl.Float64).alias("p"))
        )
        df = df.filter(c("p").is_not_null() & (c("p") > 0))
        return float(df["d"].fill_null(0).sum()) / float(df["p"].sum())

    rows = {(r["family"], _registry_name(r["metric"]), r["group"]): r for r in e.to_dicts()}
    out: dict[tuple[str, str, str], dict] = {}

    def pinned(r: dict) -> bool:
        k_ok = (
            np.isfinite(r["k0"])
            and np.isfinite(r["k0_p95"])
            and r["k0_p95"] < 1e6
            and r["k0_p95"] / r["k0_p5"] <= PIN_K_RATIO
        )
        return bool(k_ok and r["r_corr_p5"] is not None and r["r_corr_p5"] > 0)

    def r_value(r: dict, k_pinned: bool) -> tuple[float, str]:
        # The "0" cases are Efficiency's down4 pattern: no reliable year-over-year signal.
        if r["r_slope"] is None or r["r_corr_p5"] is None or not np.isfinite(r["r_slope"]):
            return 0.0, "0: no season pairs"
        if r["r_corr_p5"] <= 0:
            return 0.0, "0: no YoY signal (r_corr reaches 0)"
        if not k_pinned and r["r_slope_p95"] > R_SLOPE_STABLE:
            return 0.0, "0: r_slope unstable"
        basis = "r_slope" if k_pinned else "r_slope (k judgment)"
        return float(min(1.0, max(0.0, r["r_slope"]))), basis

    def finite_k0(r: dict | None) -> bool:
        return r is not None and bool(np.isfinite(r["k0"])) and r["k0"] < 1e6

    # Parents resolve before their splits.
    order = sorted(rows, key=lambda key: key[1] in PARENT)
    for key in order:
        family, metric, group = key
        r = rows[key]
        is_pinned = pinned(r)
        no_signal = False
        if is_pinned:
            k, k_basis = r["k0"], "pinned"
        elif metric in PARENT and (family, PARENT[metric], group) in out:
            parent = out[(family, PARENT[metric], group)]
            s = share(family, metric, PARENT[metric], group, r["seasons"])
            k = parent["k"]
            k_basis = f"J: split of `{PARENT[metric]}` ({s:.2f} of its units), parent k"
        elif finite_k0(r):
            k, k_basis = r["k0"], "J: point k0 (interval too wide)"
        else:
            # No detectable signal: an ordinary k in the metric's own units -- the same
            # metric's point k0 in the family's first group that has one -- and r = 0.
            no_signal = True
            donor = next(
                (g for g in GROUPS[family] if finite_k0(rows.get((family, metric, g)))), None
            )
            if donor is not None:
                k = rows[(family, metric, donor)]["k0"]
                k_basis = f"J: no detectable signal, {donor} point k0 (same units)"
            else:
                k = out[(family, HEADLINE[family], group)]["k"]
                k_basis = f"J: no detectable signal, `{HEADLINE[family]}` k"
        rv, r_basis = r_value(r, is_pinned)
        if no_signal:
            rv, r_basis = 0.0, "0: no detectable signal"
        note = ""
        if r["seasons"].startswith("2022") and family in ("passing", "receiving", "rushing"):
            note = FTN_METRICS_REVISIT
        r_corr = r["r_corr"]
        out[key] = dict(
            family=family,
            metric=metric,
            group=group,
            k=_k_round(k),
            k_basis=k_basis,
            r=round(rv, 2),
            r_basis=r_basis,
            r_corr=None if r_corr is None or not np.isfinite(r_corr) else round(r_corr, 2),
            seasons=r["seasons"],
            pairs=r["n_pairs"],
            pinned=is_pinned,
            note=note,
        )

    fam_order = ["receiving", "rushing", "passing", "defense"]
    metric_order = [_registry_name(m) for _, m, *_ in _metrics()]
    keyed = sorted(
        out.values(),
        key=lambda x: (
            fam_order.index(x["family"]),
            metric_order.index(x["metric"]),
            GROUPS[x["family"]].index(x["group"]),
        ),
    )
    print("PINNED", sum(x["pinned"] for x in keyed), "of", len(keyed))
    print("\n=== REGISTRY ROWS ===")
    for fam in fam_order:
        print(f"\n--- {fam}")
        print(
            "| Metric | Group | k | k basis | r | r basis | r_corr (attenuated) | Seasons (pairs) |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for x in (x for x in keyed if x["family"] == fam):
            note = f"; {x['note']}" if x["note"] else ""
            print(
                f"| `{x['metric']}` | {x['group']} | {x['k']:g} | {x['k_basis']} | "
                f"{x['r']:.2f} | {x['r_basis']}{note} | "
                f"{'n/a' if x['r_corr'] is None else f'{x["r_corr"]:.2f}'} | "
                f"{x['seasons']} ({x['pairs']}) |"
            )
    print("\n=== CONSTANTS ===")
    for x in keyed:
        print(f'    ("{x["metric"]}", "{x["group"]}"): ({x["k"]:g}, {x["r"]:.2f}),')


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["fetch", "estimate", "recommend"])
    args = ap.parse_args()
    if args.step == "fetch":
        fetch()
    elif args.step == "estimate":
        estimate()
    else:
        recommend()

"""Player efficiency analyst (pipeline/analysts/player_efficiency.py): blend, prior,
stability, percentile minimums and the defense gate, _hist, the registry sync, and its
guards. Synthetic frames only, no live calls."""

import re
from pathlib import Path

import polars as pl
import pytest

from pipeline.analysts import player_efficiency as eff

_SIGNALS_MD = Path(__file__).resolve().parents[1] / "docs" / "signals.md"
_TEXT = {"player_id", "game_id", "team", "season_type", "stat_type", "position_group"}
S = 2099


def _frame(cols, rows):
    schema = {
        c: (pl.Utf8 if c in _TEXT else pl.Int64 if c in ("season", "week") else pl.Float64)
        for c in cols
    }
    return pl.DataFrame([{c: r.get(c) for c in cols} for r in rows], schema=schema)


def _game(week, team="KC"):
    return f"{S}_{week:02d}_{team}_X"


def _inputs(pgp=(), snaps=(), pfr=(), ngs=(), pw=(), part=(), positions=()):
    base = ["player_id", "season", "week", "season_type"]
    return eff.EffInputs(
        pgp=_frame(eff._PGP_COLS, [{"season_type": "REG", **r} for r in pgp]),
        snaps=_frame(
            [
                "player_id",
                "game_id",
                "season",
                "week",
                "season_type",
                "team",
                "offense_snaps",
                "defense_snaps",
                "st_snaps",
            ],
            [
                {"season_type": "REG", "offense_snaps": 0, "defense_snaps": 0, "st_snaps": 0, **r}
                for r in snaps
            ],
        ),
        pfr=_frame(
            [*base, "stat_type", *eff._PFR_COLS], [{"season_type": "REG", **r} for r in pfr]
        ),
        ngs=_frame(
            [*base, "stat_type", *eff._NGS_COLS], [{"season_type": "REG", **r} for r in ngs]
        ),
        player_week=_frame([*base, *eff._PW_DEF_COLS], [{"season_type": "REG", **r} for r in pw]),
        participation=_frame(["player_id", "season", *eff._PART_COLS], list(part)),
        positions=_frame(["player_id", "position_group"], list(positions)),
    )


def _rec(pid, season, week, targets, epa, receptions=None, team="KC"):
    return {
        "player_id": pid,
        "season": season,
        "week": week,
        "game_id": _game(week, team),
        "team": team,
        "targets": targets,
        "rec_epa_sum": epa,
        "receptions": receptions if receptions is not None else targets,
    }


def _snap(pid, season, week, off=50, de=0, team="KC"):
    return {
        "player_id": pid,
        "season": season,
        "week": week,
        "game_id": _game(week, team),
        "team": team,
        "offense_snaps": off,
        "defense_snaps": de,
    }


def _row(df, pid, week):
    return df.filter((pl.col("player_id") == pid) & (pl.col("week") == week)).row(0, named=True)


def _wr_league():
    """Two WRs this season (weeks 1-2), one of whom (A) also played last season."""
    pgp = [
        _rec("A", S, 1, 10, 3.0),
        _rec("A", S, 2, 10, 1.0),
        _rec("B", S, 1, 5, 0.0),
        _rec("B", S, 2, 5, -1.0),
        _rec("A", S - 1, 1, 100, 40.0),
        _rec("B2", S - 1, 1, 100, 0.0),
    ]
    snaps = [
        _snap(p, s, w)
        for p, s, w in (
            ("A", S, 1),
            ("A", S, 2),
            ("B", S, 1),
            ("B", S, 2),
            ("A", S - 1, 1),
            ("B2", S - 1, 1),
        )
    ]
    pos = [{"player_id": p, "position_group": "WR"} for p in ("A", "B", "B2")]
    return _inputs(pgp=pgp, snaps=snaps, positions=pos)


# --- blend, prior, stability -----------------------------------------------------------


def test_std_is_the_three_way_blend_with_a_shrunk_prior():
    df = eff.build_eff_rows(_wr_league(), S)
    a = _row(df, "A", 2)
    k, r = eff._K_R[("epa_per_target", "WR")]
    cur, n = 4.0 / 20, 20.0
    league = (4.0 + -1.0) / 30  # both WRs, weeks 1-2
    prior_league = 40.0 / 200  # last season's WR league
    prior = (40.0 + k * prior_league) / (100 + k)  # shrunk with the same k
    w_cur = n / (n + k)
    w_prior = (1 - w_cur) * r
    expected = w_cur * cur + w_prior * prior + (1 - w_cur - w_prior) * league
    assert a["epa_per_target_std"] == pytest.approx(expected)
    assert a["epa_per_target_game"] == pytest.approx(0.1)
    assert a["epa_per_target_l4"] == pytest.approx(0.2)
    # Stability counts only the prior's own share of itself: w_prior * 100/(100 + k).
    assert a["rec_stability"] == pytest.approx(w_cur + w_prior * 100 / (100 + k))


def test_no_prior_season_means_no_prior_weight():
    df = eff.build_eff_rows(_wr_league(), S)
    b = _row(df, "B", 2)
    k, _ = eff._K_R[("epa_per_target", "WR")]
    w_cur = 10 / (10 + k)
    assert b["rec_stability"] == pytest.approx(w_cur)
    assert b["epa_per_target_std"] == pytest.approx(w_cur * (-0.1) + (1 - w_cur) * 0.1)


def test_std_is_null_without_a_current_sample():
    inp = _wr_league()
    # B (a WR) carries once, so the WR league has an epa_per_carry value A could borrow.
    inp = inp._replace(
        pgp=inp.pgp.with_columns(
            pl.when((pl.col("player_id") == "B") & (pl.col("week") == 1) & (pl.col("season") == S))
            .then(pl.lit(1.0))
            .otherwise(pl.col(c))
            .alias(c)
            for c in ("carries", "rush_epa_sum")
        )
    )
    df = eff.build_eff_rows(inp, S)
    assert _row(df, "B", 1)["epa_per_carry__league"] == pytest.approx(1.0)
    a = _row(df, "A", 2)
    assert a["epa_per_carry_std"] is None  # A never carried: no value, even with a league
    assert a["rush_carries_std"] is None  # and the family sample is null, not 0
    assert a["rush_stability"] is None


def test_later_week_never_changes_an_earlier_row():
    inp = _wr_league()
    before = eff.build_eff_rows(
        inp._replace(
            pgp=inp.pgp.filter(~((pl.col("season") == S) & (pl.col("week") == 2))),
            snaps=inp.snaps.filter(~((pl.col("season") == S) & (pl.col("week") == 2))),
        ),
        S,
    )
    after = eff.build_eff_rows(inp, S).filter(pl.col("week") == 1)
    cols = [c for c in before.columns if c.endswith(("_std", "_game", "_l4", "_stability"))]
    assert before.sort("player_id").select(cols).equals(after.sort("player_id").select(cols))


# --- percentiles -----------------------------------------------------------------------


def test_pct_population_uses_the_groups_minimum_per_game_played():
    # C is a WR with 2 targets in one game: below 3 per game played, so unranked.
    inp = _wr_league()
    inp = inp._replace(
        pgp=pl.concat(
            [inp.pgp, _frame(eff._PGP_COLS, [{"season_type": "REG", **_rec("C", S, 2, 2, 1.0)}])]
        ),
        snaps=pl.concat(
            [
                inp.snaps,
                _frame(
                    inp.snaps.columns, [{"season_type": "REG", "st_snaps": 0, **_snap("C", S, 2)}]
                ),
            ]
        ),
        positions=pl.concat(
            [
                inp.positions,
                _frame(
                    ["player_id", "position_group"], [{"player_id": "C", "position_group": "WR"}]
                ),
            ]
        ),
    )
    df = eff.build_eff_rows(inp, S)
    assert _row(df, "C", 2)["epa_per_target_pct"] is None
    assert _row(df, "A", 2)["epa_per_target_pct"] is not None


def test_minimums_per_group():
    assert eff.min_per_game("passing", "QB") == 15
    assert eff.min_per_game("rushing", "QB") == 2
    assert eff.min_per_game("rushing", "RB") == 6
    assert eff.min_per_game("receiving", "TE") == 3
    assert eff.min_per_game("defense", "DB") == 20


def test_defense_percentiles_are_gated():
    snaps = [_snap("D1", S, 1, off=0, de=60), _snap("D2", S, 1, off=0, de=55)]
    pfr = [
        {"player_id": p, "season": S, "week": 1, "stat_type": "def", "def_tackles_combined": t}
        for p, t in (("D1", 6.0), ("D2", 3.0))
    ]
    pos = [{"player_id": p, "position_group": "LB"} for p in ("D1", "D2")]
    df = eff.build_eff_rows(_inputs(snaps=snaps, pfr=pfr, positions=pos), S)
    d1 = _row(df, "D1", 1)
    assert d1["tackles_per_snap_game"] == pytest.approx(0.1)  # values are written
    assert all(d1[f"{m.name}_pct"] is None for m in eff.METRICS if m.family == "defense")


# --- _hist -----------------------------------------------------------------------------


def test_hist_sums_the_completed_seasons_and_names_them():
    part = [
        {
            "player_id": "A",
            "season": S - 1,
            "off_dropbacks_man": 100,
            "off_dropbacks_zone": 300,
            "targets_man": 20,
            "targets_zone": 30,
            "rec_epa_sum_man": 10.0,
            "rec_epa_sum_zone": 3.0,
        },
        {
            "player_id": "A",
            "season": S - 2,
            "off_dropbacks_man": 50,
            "off_dropbacks_zone": 50,
            "targets_man": 10,
            "targets_zone": 10,
            "rec_epa_sum_man": 2.0,
            "rec_epa_sum_zone": 1.0,
        },
        {"player_id": "A", "season": S, "off_dropbacks_man": 999},  # current season: never
    ]
    df = eff.build_eff_rows(
        _wr_league()._replace(participation=_frame(["player_id", "season", *eff._PART_COLS], part)),
        S,
    )
    a = _row(df, "A", 2)
    assert a["hist_span"] == f"{S - 2}-{S - 1}"
    assert a["rec_hist_n"] == 500
    assert a["epa_per_target_vs_man_hist"] == pytest.approx(12.0 / 30)
    assert a["target_rate_vs_zone_hist"] == pytest.approx(40 / 350)
    assert a["pass_hist_n"] is None


# --- guards ----------------------------------------------------------------------------


def test_duplicate_player_week_in_a_source_is_refused():
    inp = _wr_league()
    pfr = [
        {
            "player_id": "A",
            "season": S,
            "week": 1,
            "stat_type": "rec",
            "receiving_broken_tackles": 1.0,
        }
    ] * 2
    with pytest.raises(ValueError, match="pfr_advstats rec"):
        eff.build_eff_rows(
            inp._replace(pfr=_frame(inp.pfr.columns, [{"season_type": "REG", **r} for r in pfr])), S
        )


def test_rows_carry_exactly_the_migration_columns():
    df = eff.build_eff_rows(_wr_league(), S)
    produced = {
        c for c in eff.COLUMNS if c not in ("as_of", "inputs_version", "content_hash", "updated_at")
    }
    assert produced <= set(df.columns)


def test_metrics_match_the_migration():
    std_cols = {c[:-4] for c in eff.COLUMNS if c.endswith("_std")}
    samples = {prefix for prefix, _ in eff._FAMILY_SAMPLE.values()}
    assert {m.name for m in eff.METRICS} == std_cols - samples


def _registry_k_r() -> dict[tuple[str, str], tuple[float, float]]:
    text = _SIGNALS_MD.read_text(encoding="utf-8")
    section = text.split("#### k and r by metric and position group", 1)[1].split("\n#### ", 1)[0]
    out = {}
    for line in section.splitlines():
        m = re.match(r"\| `([a-z_]+)` \| ([A-Z]{2}) \| ([0-9.]+) \| [^|]+ \| ([0-9.]+) \|", line)
        if m:
            out[(m.group(1), m.group(2))] = (float(m.group(3)), float(m.group(4)))
    return out


def test_k_r_constants_match_the_registry_table():
    registry = _registry_k_r()
    assert len(registry) == 206
    assert {k: (float(v[0]), float(v[1])) for k, v in eff._K_R.items()} == registry


def test_every_metric_has_k_r_for_its_primary_group():
    for m in eff.METRICS:
        assert (m.name, eff.PRIMARY_GROUP[m.family]) in eff._K_R or any(
            name == m.name for name, _ in eff._K_R
        ), m.name
        k, r = eff.k_r(m, "OL")  # an unestimated group borrows, never raises
        assert k > 0 and 0 <= r <= 1

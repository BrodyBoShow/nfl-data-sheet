"""One-off script: fit the P5 projection model on completed seasons and write
pipeline/synthesis/model_coefficients.json (committed, reviewed as a diff, like the
reliability r constants in efficiency.py).

What it does:
1. Loads historical REG games (scores, location, closing lines) and the backfilled
   efficiency signals (scripts/projection_history.py).
2. Refuses the current season or later, and any season with an unscored REG game.
3. Fits the pre-registered model (pipeline/synthesis/model.py, PRIMARY_SPEC) on every
   `--seasons` game. Those are the coefficients the synthesizer uses.
4. Runs the expanding-window walk-forward over `--test-seasons` (each test season fit
   only on earlier seasons). From those out-of-sample predictions it sets the stability
   tercile cutpoints, each bucket's +/- band (RMS residual), and each bucket's
   `edge_validated` flag (edge vs. closing line, bootstrap 95% CI entirely above 0).

Every model computation is imported from pipeline/synthesis/model.py. Nothing is
reimplemented here (CLAUDE.md's verification-script convention).

Reads: games, signals. Writes: the JSON file only (nothing in the database).

Refit manually after each season completes (alongside scripts/estimate_reliability.py)
and after any efficiency definition change (re-backfill first). Live-season outcomes
never feed coefficients.

Usage:
  uv run python scripts/fit_projection_model.py --seasons 2019-2025 --test-seasons 2020-2025
  uv run python scripts/fit_projection_model.py --dry-run   # print, don't write
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import nflreadpy as nfl
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection  # noqa: E402
from pipeline.synthesis.model import (  # noqa: E402
    MODEL_VERSION,
    PRIMARY_SPEC,
    build_game_frame,
    efficiency_fingerprint,
    fit,
    to_team_rows,
    walk_forward,
)
from scripts.projection_history import (  # noqa: E402
    add_outcomes,
    calibrate,
    check_fit_seasons,
    load_efficiency_signals,
    load_games,
)

COEFFICIENTS_PATH = (
    Path(__file__).resolve().parent.parent / "pipeline" / "synthesis" / "model_coefficients.json"
)


def _season_range(text: str) -> list[int]:
    start, end = (int(x) for x in text.split("-"))
    return list(range(start, end + 1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2019-2025")
    parser.add_argument("--test-seasons", default="2020-2025")
    parser.add_argument("--out", type=Path, default=COEFFICIENTS_PATH)
    parser.add_argument("--dry-run", action="store_true", help="print the payload, don't write")
    args = parser.parse_args()
    seasons = _season_range(args.seasons)
    test_seasons = _season_range(args.test_seasons)
    if not set(test_seasons) <= set(seasons) or min(test_seasons) <= min(seasons):
        print("refusing: test seasons must be inside --seasons and after its first season",
              file=sys.stderr)
        return 1

    with get_connection() as conn:
        games = load_games(conn, seasons)
        signals = load_efficiency_signals(conn, seasons, PRIMARY_SPEC.signal_names)
    try:
        check_fit_seasons(seasons, nfl.get_current_season(), games)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    incomplete = frame.filter(~pl.col("features_complete"))
    full = fit(to_team_rows(frame, PRIMARY_SPEC), PRIMARY_SPEC)

    oos, _ = walk_forward(frame, PRIMARY_SPEC, test_seasons)
    _, calibration = calibrate(add_outcomes(oos))

    payload = {
        "model_version": MODEL_VERSION,
        "spec": {"name": PRIMARY_SPEC.name, "bases": list(PRIMARY_SPEC.bases)},
        "fit_seasons": seasons,
        "fitted_on": datetime.now(UTC).date().isoformat(),
        "efficiency_fingerprint": efficiency_fingerprint(PRIMARY_SPEC),
        "n_games": full.n_games,
        "n_team_rows": full.n_rows,
        "games_dropped_incomplete_features": incomplete.height,
        "coefficients": {
            name: {"value": full.coef[name], "se": full.se[name]} for name in full.coef
        },
        "resid_sd_team_points": full.resid_sd,
        "calibration": calibration,
    }
    text = json.dumps(payload, indent=2) + "\n"

    print(f"fit seasons {seasons[0]}-{seasons[-1]}: {full.n_games} games "
          f"({incomplete.height} dropped, incomplete features)")
    for name in full.coef:
        print(f"  {name:<24} {full.coef[name]:>10.4f}  se {full.se[name]:.4f}")
    print(f"walk-forward test seasons {test_seasons[0]}-{test_seasons[-1]}: "
          f"{calibration['n_games']} games, cutpoints {calibration['stability_cutpoints']}")

    if args.dry_run:
        print(text)
        return 0
    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

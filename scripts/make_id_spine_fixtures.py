"""One-off script: pull each ID-spine source live and save a trimmed fixture.

Not part of the pipeline — run manually when a source's shape needs re-verifying.
Usage: uv run python scripts/make_id_spine_fixtures.py
"""

import json
from pathlib import Path

import httpx
import nflreadpy as nfl
import polars as pl

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    schedules = nfl.load_schedules(seasons=[2025])
    schedules.head(8).write_parquet(FIXTURES / "nflreadpy_schedules_sample.parquet")

    teams = nfl.load_teams()
    teams.write_parquet(FIXTURES / "nflreadpy_teams_sample.parquet")

    ff_ids = nfl.load_ff_playerids()
    ff_sample = ff_ids.filter(pl.col("gsis_id").is_not_null()).head(15)
    ff_sample.write_parquet(FIXTURES / "nflreadpy_ff_playerids_sample.parquet")

    players = nfl.load_players()
    gsis_ids = ff_sample["gsis_id"].to_list()
    players_sample = players.filter(pl.col("gsis_id").is_in(gsis_ids))
    players_sample.write_parquet(FIXTURES / "nflreadpy_players_sample.parquet")

    resp = httpx.get(
        "https://github.com/nflverse/nflverse-data/releases/download/players/timestamp.json",
        follow_redirects=True,
        timeout=30,
    )
    resp.raise_for_status()
    (FIXTURES / "nflverse_timestamp_sample.json").write_text(json.dumps(resp.json(), indent=2))

    print("schedules:", schedules.shape, "-> sample", schedules.head(8).shape)
    print("teams:", teams.shape)
    print("ff_playerids sample:", ff_sample.shape)
    print("players sample:", players_sample.shape)


if __name__ == "__main__":
    main()

"""One-off, read-only: sample nflverse's schedule file across a live slate to test
whether a score ever appears in it before the game is final.

Why: `games` has no game-status column. The grader treats "row carries a score" as
"final" (docs/phases/P5.md, "Grader finality assumption"). This script collects the
evidence for or against that.

What it does:
- Every --poll-minutes, reads nflverse's `schedules` timestamp.json (the same gate
  id_spine uses).
- When it changes, re-downloads the schedule with nflreadpy's cache OFF. nflreadpy
  caches in memory for 24h by default, so a long-running process would otherwise
  never see an update.
- For every game with kickoff in [now - 8h, now + 2h], it logs a line on first sight and
  whenever (away_score, home_score, result, total, overtime) changes.
- Kickoff comes from pipeline.core.schedule.kickoff_utc, the production function.

Reads: nflverse-data release `schedules` (timestamp.json, load_schedules)
Writes: data/finality_samples/<UTC start>.jsonl (gitignored). Nothing in the database.

Usage:
  uv run python scripts/sample_schedule_finality.py                   # 14h, poll every 2 min
  uv run python scripts/sample_schedule_finality.py --hours 8 --poll-minutes 1
  uv run python scripts/sample_schedule_finality.py --summarize data/finality_samples/<file>.jsonl

Reading the summary, per game:
  first_scored_min    minutes from kickoff to the first sample with a score. The error is
                      at most one poll interval, plus nflverse's own publish delay.
  changes_after_score how many times the score tuple changed after it first appeared.
                      Anything above 0 means a partial score was published (or a
                      correction). That's what would break the grader's assumption.
  overtime_null       a score was present while `overtime` was still null. A candidate
                      finality marker; finals so far carry 0/1.
  early               first scored under --early-minutes after kickoff. Informational
                      only: a game can't plausibly have ended that soon.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.schedule import kickoff_utc  # noqa: E402

_TIMESTAMP_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/timestamp.json"
_FIELDS = ("away_score", "home_score", "result", "total", "overtime")
_WINDOW_BEFORE = dt.timedelta(hours=8)  # games that kicked off up to 8h ago
_WINDOW_AFTER = dt.timedelta(hours=2)  # and games kicking off in the next 2h
_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "finality_samples"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="seconds")


def fetch_timestamp(client: httpx.Client) -> str:
    res = client.get(_TIMESTAMP_URL, follow_redirects=True, timeout=30)
    res.raise_for_status()
    return str(res.json()["last_updated"])


def load_window_games(season: int, now: dt.datetime) -> list[dict[str, Any]]:
    import nflreadpy as nfl
    from nflreadpy.config import update_config

    update_config(cache_mode="off", verbose=False)
    df = nfl.load_schedules(seasons=[season])
    out = []
    for r in df.select(["game_id", "gameday", "gametime", *_FIELDS]).iter_rows(named=True):
        if not r["gameday"] or not r["gametime"]:
            continue
        kickoff = kickoff_utc(dt.date.fromisoformat(r["gameday"]), r["gametime"])
        if now - _WINDOW_BEFORE <= kickoff <= now + _WINDOW_AFTER:
            out.append({**r, "kickoff": kickoff})
    return out


def sample(season: int, hours: float, poll_minutes: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    last_ts: str | None = None
    last_tuple: dict[str, tuple[Any, ...]] = {}
    deadline = _now() + dt.timedelta(hours=hours)
    print(f"sampling season {season} until {_iso(deadline)}; log: {out}")
    with httpx.Client() as client, out.open("a", encoding="utf-8") as log:
        while _now() < deadline:
            polled = _now()
            try:
                ts = fetch_timestamp(client)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                print(f"{_iso(polled)} timestamp fetch failed: {e}")
                time.sleep(poll_minutes * 60)
                continue
            if ts != last_ts:
                try:
                    games = load_window_games(season, polled)
                except Exception as e:  # noqa: BLE001 -- keep sampling through a bad fetch
                    print(f"{_iso(polled)} schedule fetch failed: {e}")
                    time.sleep(poll_minutes * 60)
                    continue
                scored = sum(g["home_score"] is not None for g in games)
                log.write(json.dumps({"type": "release", "polled_at": _iso(polled),
                                      "nflverse_ts": ts, "in_window": len(games),
                                      "scored": scored}) + "\n")
                for g in games:
                    tup = tuple(g[f] for f in _FIELDS)
                    if last_tuple.get(g["game_id"]) != tup:
                        last_tuple[g["game_id"]] = tup
                        minutes = (polled - g["kickoff"]).total_seconds() / 60
                        log.write(json.dumps({
                            "type": "game", "polled_at": _iso(polled), "nflverse_ts": ts,
                            "game_id": g["game_id"], "kickoff": _iso(g["kickoff"]),
                            "minutes_since_kickoff": round(minutes, 1),
                            **{f: g[f] for f in _FIELDS},
                        }) + "\n")
                log.flush()
                print(f"{_iso(polled)} nflverse {ts}: "
                      f"{len(games)} games in window, {scored} scored")
                last_ts = ts
            time.sleep(poll_minutes * 60)


def summarize(events: Iterable[dict[str, Any]], early_minutes: float = 150) -> list[dict[str, Any]]:
    """Per game: when a score first appeared, and whether it changed afterward."""
    games: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("type") != "game":
            continue
        g = games.setdefault(e["game_id"], {
            "game_id": e["game_id"], "kickoff": e["kickoff"], "first_scored_min": None,
            "first_score": None, "last_score": None, "changes_after_score": 0,
            "overtime_null": False,
        })
        score = (e["away_score"], e["home_score"], e["result"], e["total"], e["overtime"])
        if e["home_score"] is None and e["away_score"] is None:
            if g["first_score"] is not None:
                g["changes_after_score"] += 1  # a score disappeared: also a change
                g["last_score"] = score
            continue
        if g["first_score"] is None:
            g["first_scored_min"] = e["minutes_since_kickoff"]
            g["first_score"] = score
        elif score != g["last_score"]:
            g["changes_after_score"] += 1
        g["last_score"] = score
        if e["overtime"] is None:
            g["overtime_null"] = True
    for g in games.values():
        m = g["first_scored_min"]
        g["early"] = m is not None and m < early_minutes
    return sorted(games.values(), key=lambda g: (g["kickoff"], g["game_id"]))


def print_summary(rows: list[dict[str, Any]]) -> int:
    print(f"{'game_id':<18} {'kickoff (UTC)':<26} {'first_scored_min':>16} "
          f"{'changes_after_score':>19}  overtime_null  early  final")
    suspect = 0
    for g in rows:
        final = g["last_score"]
        shown = "-" if final is None else f"{final[0]}-{final[1]} ot={final[4]}"
        m = "-" if g["first_scored_min"] is None else f"{g['first_scored_min']:.1f}"
        flag = g["changes_after_score"] > 0 or g["overtime_null"] or g["early"]
        suspect += flag
        print(f"{g['game_id']:<18} {g['kickoff']:<26} {m:>16} {g['changes_after_score']:>19}  "
              f"{str(g['overtime_null']):<13}  {str(g['early']):<5}  {shown}"
              f"{'   <-- CHECK' if flag else ''}")
    scored = [g for g in rows if g["first_scored_min"] is not None]
    print(f"\n{len(rows)} games observed, {len(scored)} scored, {suspect} flagged.")
    if scored:
        mins = sorted(g["first_scored_min"] for g in scored)
        print(f"first_scored_min: min {mins[0]:.1f}, median {mins[len(mins) // 2]:.1f}, "
              f"max {mins[-1]:.1f}")
    return 1 if suspect else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Sample nflverse's schedule file for score finality.")
    p.add_argument("--season", type=int, default=None, help="default: nflreadpy current season")
    p.add_argument("--hours", type=float, default=14.0)
    p.add_argument("--poll-minutes", type=float, default=2.0)
    p.add_argument("--early-minutes", type=float, default=150.0)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--summarize", type=Path, default=None, help="summarize an existing log")
    args = p.parse_args()

    if args.summarize is None:
        import nflreadpy as nfl

        season = args.season or nfl.get_current_season()
        out = args.out or _OUT_DIR / f"{_now().strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        try:
            sample(season, args.hours, args.poll_minutes, out)
        except KeyboardInterrupt:
            print("\nstopped; summarizing what was collected")
        path = out
    else:
        path = args.summarize
    with path.open(encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    return print_summary(summarize(events, args.early_minutes))


if __name__ == "__main__":
    sys.exit(main())

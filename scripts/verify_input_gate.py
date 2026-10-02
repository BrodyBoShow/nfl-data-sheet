"""One-off script: verify the input gate (pipeline/core/input_gate.py) on the real Postgres
engine. Tests never open a live connection, so what Postgres does with the gate's
statements is checked here.

Run it after the egress calibration window, and after migration 0034 is applied (it stops
at check 5 until then). Written 2026-10-02, not yet run.

Real tables are only read. Everything the checks write goes to TEMP tables that shadow the
real names (snaps, player_game_pbp, players, agent_runs), because the production code
under test uses unqualified names, and Postgres searches the session's temp schema first
when search_path doesn't list it. Each shadow is asserted to resolve to pg_temp before
anything is written, and every write names `pg_temp.` explicitly. It's all one
transaction, rolled back: nothing is committed and no real table is touched. The one
exception is check 1, which runs first and lets the gate open and commit its own
read-only transaction, as it does on a forced run.

Every check calls the production code (the gate's own `_digests`/`_markers`/
`_last_success`/`check`, the auditor's `check_gate_inputs`), never a re-written query.
Check 9 sets `GATE_KEY_SOURCE` in this process only, never in the file.

Checks (each prints PASS/FAIL):
  1. With no transaction open, the gate's read opens and commits its own, and the
     connection is idle and usable after.
  2. Markers: every declared marker of both analysts is in source_freshness, including
     nflverse:players. None unknown.
  3. Digests on the real tables: every input of both analysts digests to an md5 or
     'empty', none unknown, the same on a second call. Prints time and result bytes. The
     auditor's check runs on the real tables without error.
  4. An empty window (season 2099) digests every input to 'empty'.
  5. Migration 0034: the status CHECK allows skipped_unchanged, and still rejects a bogus
     status. The script stops here if it fails.
  6. Usage's inputs copied to TEMP tables digest the same as the real ones. An update,
     insert or delete inside the window moves only that input's digest; reverting
     restores it. A row outside the window, and a players name change, move nothing.
  7. players: a null position_group and a player missing from players digest differently.
  8. A failed digest read leaves the transaction usable: every part unknown, then
     SELECT 1 still works (the savepoint).
  9. End to end, each branch, on a TEMP agent_runs: the first check runs
     (no_prior_success); with that stored as a success, the next skips and names it. On
     the content branch, a moved input then runs (changed: snaps).
 10. The auditor on TEMP tables: quiet with no change; quiet after a change with no tick;
     alerts on a skipped_unchanged after the change, and on a synthesizer run with no
     analyst run; quiet once the analyst has run.
Then three break demonstrations, which must each show the break:
 11. Digesting content_hash alone (the spec's literal text): a key-only change is
     invisible. Why the digest includes the key columns.
 12. players as `player_id || ':' || position_group` (the spec's literal text): a null
     group digests the same as a missing player. Why it uses quote_nullable.
 13. The digest read without a savepoint: the failed read aborts the transaction.

Usage:
  uv run python scripts/verify_input_gate.py --season 2026 --week 5
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg import pq  # noqa: E402

import pipeline.core.input_gate as ig  # noqa: E402
from pipeline.analysts.player_efficiency import INPUT_GATE as EFF  # noqa: E402
from pipeline.analysts.usage import INPUT_GATE as USAGE  # noqa: E402
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.orchestration.auditor import check_gate_inputs  # noqa: E402

_MD5 = re.compile(r"[0-9a-f]{32}")
_EXPECTED = 13
# Explicit ids for TEMP agent_runs rows, so no real sequence value is used up.
_RUN_ID = 990_000
_SAME_ROW = "game_id = %s AND pfr_player_id = %s"


class _Stop(Exception):
    """A check whose failure makes the rest meaningless."""


def _ctx(conn: psycopg.Connection, season: int, week: int) -> RunContext:
    return RunContext(season, week, "REG", datetime.now(UTC), None, conn)  # type: ignore[arg-type]


def _input(gate: ig.InputGate, table: str) -> ig.GateInput:
    return next(i for i in gate.inputs if i.table == table)


def _with_input(gate: ig.InputGate, table: str, **changes: Any) -> ig.InputGate:
    """`gate` with one input altered, for the break demonstrations."""
    inputs = tuple(
        dataclasses.replace(i, **changes) if i.table == table else i for i in gate.inputs
    )
    return dataclasses.replace(gate, inputs=inputs)


def _digests(
    conn: psycopg.Connection, gate: ig.InputGate, season: int, week: int
) -> dict[str, str]:
    d, err = gate._digests(conn, season, week)
    if err:
        raise RuntimeError(f"{gate.agent} digest failed: {err}")
    return d


def _shadow(cur: Any, table: str, ddl: str) -> None:
    """Create TEMP `table` and confirm the unqualified name now resolves to it."""
    cur.execute(ddl)
    cur.execute(
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s)",
        (table,),
    )
    row = cur.fetchone()
    if row is None or not row[0].startswith("pg_temp"):
        raise _Stop(f"{table} resolves to {row and row[0]}, not the TEMP shadow; stopping")


def _add_run(
    cur: Any, run_id: int, agent: str, status: str, started: datetime, gate: dict | None
) -> None:
    cur.execute(
        "INSERT INTO pg_temp.agent_runs "
        "(id, agent, started_at, finished_at, status, rows_written, meta) "
        "VALUES (%s, %s, %s, %s, %s, 0, %s::jsonb)",
        (run_id, agent, started, started, status, json.dumps({"gate": gate} if gate else {})),
    )


def _report(results: list[tuple[str, bool, str]]) -> int:
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    print("Rolled back; nothing committed (check 1's own read-only transaction aside).")
    return 0 if all(ok for _, ok, _ in results) and len(results) == _EXPECTED else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    args = ap.parse_args()
    season, week = args.season, args.week
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    with get_connection() as conn:
        # 1. no transaction open: the gate's own transaction() begins and commits
        before = conn.info.transaction_status
        markers_eff, err_eff = EFF._markers(conn)
        after = conn.info.transaction_status
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            usable = cur.fetchone() == (1,)
        conn.rollback()
        check(
            "1 no open transaction: the gate's read commits its own, connection usable",
            before == after == pq.TransactionStatus.IDLE and usable and err_eff is None,
            f"before={before.name}, after={after.name}, usable={usable}, error={err_eff}",
        )

        try:
            with conn.cursor() as cur:
                # Open the one transaction everything below runs in, so each gate read
                # from here on is a savepoint inside it.
                cur.execute("SELECT 1")

                # 2. markers
                markers_usage, err_usage = USAGE._markers(conn)
                unknown = sorted(
                    f"{g}:{t}"
                    for g, m in (("usage", markers_usage), ("player_efficiency", markers_eff))
                    for t, v in m.items()
                    if v == ig.UNKNOWN
                )
                check(
                    "2 every declared marker exists, nflverse:players included",
                    not unknown and err_usage is None and "players" in markers_eff,
                    f"unknown={unknown}, usage={markers_usage}, eff={markers_eff}",
                )

                # 3. digests on the real tables, twice
                real: dict[str, dict[str, str]] = {}
                detail3 = []
                ok3 = True
                for gate in (USAGE, EFF):
                    t0 = time.monotonic()
                    first = _digests(conn, gate, season, week)
                    elapsed = time.monotonic() - t0
                    again = _digests(conn, gate, season, week)
                    real[gate.agent] = first
                    formed = all(_MD5.fullmatch(v) or v == ig.EMPTY for v in first.values())
                    ok3 = ok3 and formed and first == again
                    size = sum(len(v) for v in first.values())
                    detail3.append(
                        f"{gate.agent}: {elapsed:.2f}s, {size} bytes of digests, "
                        f"stable={first == again}, {first}"
                    )
                audit_real = {
                    g.agent: check_gate_inputs(conn, g, season, week) for g in (USAGE, EFF)
                }
                detail3.append(f"auditor on real tables: {audit_real}")
                check("3 real digests: well-formed and stable", ok3, "; ".join(detail3))

                # 4. empty window
                empty = {g.agent: _digests(conn, g, 2099, 1) for g in (USAGE, EFF)}
                check(
                    "4 empty window digests to 'empty'",
                    all(v == ig.EMPTY for d in empty.values() for v in d.values()),
                    f"{empty}",
                )

                # 5. migration 0034
                cur.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'agent_runs_status_check' "
                    "AND conrelid = 'public.agent_runs'::regclass"
                )
                row = cur.fetchone()
                constraint = row[0] if row else ""
                _shadow(
                    cur,
                    "agent_runs",
                    "CREATE TEMP TABLE agent_runs (LIKE public.agent_runs INCLUDING CONSTRAINTS)",
                )
                cur.execute("SAVEPOINT bogus_status")
                try:
                    _add_run(cur, _RUN_ID, "usage", "not_a_status", datetime.now(UTC), None)
                    rejected = False
                except psycopg.errors.CheckViolation:
                    rejected = True
                cur.execute("ROLLBACK TO SAVEPOINT bogus_status")
                ok5 = "'skipped_unchanged'" in constraint and rejected
                check(
                    "5 migration 0034: skipped_unchanged allowed, bogus status rejected",
                    ok5,
                    f"constraint={constraint}, bogus rejected={rejected}",
                )
                if not ok5:
                    raise _Stop("migration 0034 isn't applied; apply it, then re-run")

                # 6. Usage's inputs as TEMP copies, selected by the gate's own filters.
                # players goes last: its filter reads the TEMP snaps copy.
                for table in ("snaps", "player_game_pbp", "players"):
                    _shadow(
                        cur,
                        table,
                        f"CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA",
                    )
                    where, params = _input(USAGE, table).digest_filter(season, week)
                    cur.execute(
                        f"INSERT INTO pg_temp.{table} SELECT * FROM public.{table} WHERE {where}",
                        params,
                    )
                base = _digests(conn, USAGE, season, week)
                # A player with two or more games in the window, so deleting one of his
                # rows leaves the players digest's id set unchanged.
                cur.execute(
                    "SELECT game_id, pfr_player_id, player_id FROM pg_temp.snaps s "
                    "WHERE (SELECT count(*) FROM pg_temp.snaps t "
                    "WHERE t.player_id = s.player_id) > 1 "
                    "ORDER BY game_id, pfr_player_id LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    raise _Stop("no player with two snaps rows in the window")
                g_id, pfr_id, pid = row
                steps: dict[str, dict[str, str]] = {}
                cur.execute(
                    "UPDATE pg_temp.snaps SET content_hash = content_hash || 'x' "
                    f"WHERE {_SAME_ROW}",
                    (g_id, pfr_id),
                )
                steps["update"] = _digests(conn, USAGE, season, week)
                cur.execute(
                    "UPDATE pg_temp.snaps SET content_hash = left(content_hash, -1) "
                    f"WHERE {_SAME_ROW}",
                    (g_id, pfr_id),
                )
                steps["reverted"] = _digests(conn, USAGE, season, week)
                cur.execute(
                    "INSERT INTO pg_temp.snaps (game_id, pfr_player_id, player_id, season, "
                    "week, season_type, content_hash) VALUES ('verify_g', 'verify_p', "
                    "'verify_pid', %s, 1, 'REG', 'h')",
                    (season - 1,),
                )
                steps["outside_insert"] = _digests(conn, USAGE, season, week)
                cur.execute(
                    "UPDATE pg_temp.snaps SET season = %s WHERE game_id = 'verify_g'", (season,)
                )
                steps["inside_insert"] = _digests(conn, USAGE, season, week)
                cur.execute("DELETE FROM pg_temp.snaps WHERE game_id = 'verify_g'")
                cur.execute("SAVEPOINT del")
                cur.execute(f"DELETE FROM pg_temp.snaps WHERE {_SAME_ROW}", (g_id, pfr_id))
                steps["delete"] = _digests(conn, USAGE, season, week)
                cur.execute("ROLLBACK TO SAVEPOINT del")
                cur.execute("SAVEPOINT name")
                cur.execute(
                    "UPDATE pg_temp.players SET display_name = display_name || 'x', "
                    "content_hash = content_hash || 'x' WHERE player_id = %s",
                    (pid,),
                )
                steps["players_name"] = _digests(conn, USAGE, season, week)
                cur.execute("ROLLBACK TO SAVEPOINT name")

                def moved(step: str) -> list[str]:
                    return sorted(t for t in base if steps[step][t] != base[t])

                check(
                    "6 content changes move exactly their input's digest",
                    base == real["usage"]
                    and moved("update") == ["snaps"]
                    and steps["reverted"] == base
                    and moved("outside_insert") == []
                    and moved("inside_insert") == ["snaps"]
                    and moved("delete") == ["snaps"]
                    and moved("players_name") == [],
                    f"copy==real {base == real['usage']}, "
                    + ", ".join(f"{s}: moved {moved(s)}" for s in steps),
                )

                # 7 (and 12's data). null group vs missing player
                literal_players = _with_input(
                    USAGE, "players", row="player_id || ':' || position_group"
                )
                cur.execute("SAVEPOINT groups")
                cur.execute(
                    "UPDATE pg_temp.players SET position_group = NULL WHERE player_id = %s",
                    (pid,),
                )
                null_group = _digests(conn, USAGE, season, week)["players"]
                null_literal = _digests(conn, literal_players, season, week)["players"]
                cur.execute("DELETE FROM pg_temp.players WHERE player_id = %s", (pid,))
                missing = _digests(conn, USAGE, season, week)["players"]
                missing_literal = _digests(conn, literal_players, season, week)["players"]
                cur.execute("ROLLBACK TO SAVEPOINT groups")
                check(
                    "7 players: null group and missing player digest differently",
                    null_group != missing,
                    f"null={null_group}, missing={missing}",
                )

                # 8. a failed digest read leaves the transaction usable
                broken = ig.InputGate(
                    "verify",
                    "pipeline.core.input_gate",
                    (
                        ig.GateInput(
                            "no_such_table_gate_verify", "x", ("id",), "true", lambda s, w: ()
                        ),
                    ),
                )
                d8, err8 = broken._digests(conn, season, week)
                cur.execute("SELECT 1")
                usable8 = cur.fetchone() == (1,)
                check(
                    "8 failed digest: all unknown, transaction still usable",
                    d8 == {"no_such_table_gate_verify": ig.UNKNOWN} and bool(err8) and usable8,
                    f"parts={d8}, error={err8}, usable={usable8}",
                )

                # 9. end to end, each branch, on the TEMP agent_runs
                detail9 = []
                ok9 = True
                original = ig.GATE_KEY_SOURCE
                try:
                    for n, branch in enumerate((ig.MARKER, ig.CONTENT)):
                        ig.GATE_KEY_SOURCE = branch
                        first_v = USAGE.check(_ctx(conn, season, week), force=False)
                        g1 = first_v.meta["gate"]
                        run_id = _RUN_ID + 10 * (n + 1)
                        _add_run(cur, run_id, "usage", "success", datetime.now(UTC), g1)
                        second = USAGE.check(_ctx(conn, season, week), force=False)
                        g2 = second.meta["gate"]
                        ok = (
                            g1["decision"] == "run:no_prior_success"
                            and second.status == "skipped_unchanged"
                            and g2.get("matched_run_id") == run_id
                            and g2["key"] == g1["key"]
                        )
                        after_move = "n/a"
                        if branch == ig.CONTENT:
                            cur.execute("SAVEPOINT moved")
                            cur.execute(
                                "UPDATE pg_temp.snaps SET content_hash = 'moved' "
                                f"WHERE {_SAME_ROW}",
                                (g_id, pfr_id),
                            )
                            third = USAGE.check(_ctx(conn, season, week), force=False)
                            cur.execute("ROLLBACK TO SAVEPOINT moved")
                            g3 = third.meta["gate"]
                            after_move = f"{g3['decision']} {g3.get('changed')}"
                            ok = ok and third.status is None and g3.get("changed") == ["snaps"]
                        ok9 = ok9 and ok
                        detail9.append(
                            f"{branch}: first={g1['decision']}, second={second.status} "
                            f"matched={g2.get('matched_run_id')}, after a move={after_move}"
                        )
                        cur.execute("DELETE FROM pg_temp.agent_runs")
                finally:
                    ig.GATE_KEY_SOURCE = original
                check(
                    "9 end to end: run, then skip naming the run, then run on a move",
                    ok9,
                    "; ".join(detail9),
                )

                # 10. the auditor on the TEMP tables
                now = datetime.now(UTC)
                for table in ("snaps", "player_game_pbp"):
                    cur.execute(
                        f"UPDATE pg_temp.{table} SET updated_at = %s", (now - timedelta(days=3),)
                    )
                gate_meta = {"season": season, "week": week, "key": "k"}
                _add_run(
                    cur, _RUN_ID + 100, "usage", "success", now - timedelta(hours=2), gate_meta
                )
                a_quiet = check_gate_inputs(conn, USAGE, season, week)
                cur.execute(
                    f"UPDATE pg_temp.snaps SET updated_at = %s WHERE {_SAME_ROW}",
                    (now - timedelta(hours=1), g_id, pfr_id),
                )
                a_no_tick = check_gate_inputs(conn, USAGE, season, week)
                cur.execute("SAVEPOINT skip")
                _add_run(
                    cur,
                    _RUN_ID + 101,
                    "usage",
                    "skipped_unchanged",
                    now - timedelta(minutes=30),
                    gate_meta,
                )
                a_skip = check_gate_inputs(conn, USAGE, season, week)
                cur.execute("ROLLBACK TO SAVEPOINT skip")
                _add_run(
                    cur, _RUN_ID + 102, "synthesizer", "success", now - timedelta(minutes=20), None
                )
                a_hole = check_gate_inputs(conn, USAGE, season, week)
                _add_run(cur, _RUN_ID + 103, "usage", "failed", now - timedelta(minutes=10), None)
                a_ran = check_gate_inputs(conn, USAGE, season, week)
                check(
                    "10 auditor: alerts on a wrong skip and an unserved tick only",
                    a_quiet == []
                    and a_no_tick == []
                    and len(a_skip) == 1
                    and "skipped_unchanged after the change" in a_skip[0]
                    and len(a_hole) == 1
                    and "tick ran without it" in a_hole[0]
                    and a_ran == [],
                    f"quiet={a_quiet}, no tick={a_no_tick}, skip={a_skip}, hole={a_hole}, "
                    f"ran={a_ran}",
                )

                # 11. break: content_hash alone misses a key-only change
                literal_rows = _with_input(USAGE, "snaps", row="content_hash")
                cur.execute(
                    "SELECT game_id, pfr_player_id FROM pg_temp.snaps "
                    "ORDER BY game_id DESC, pfr_player_id DESC LIMIT 1"
                )
                last = cur.fetchone()
                assert last is not None
                before_lit = _digests(conn, literal_rows, season, week)["snaps"]
                before_ours = _digests(conn, USAGE, season, week)["snaps"]
                cur.execute("SAVEPOINT rekey")
                # The last row in key order, re-keyed so it stays last: same content, same
                # order, only the key differs.
                cur.execute(
                    f"UPDATE pg_temp.snaps SET pfr_player_id = pfr_player_id || 'z' "
                    f"WHERE {_SAME_ROW}",
                    last,
                )
                after_lit = _digests(conn, literal_rows, season, week)["snaps"]
                after_ours = _digests(conn, USAGE, season, week)["snaps"]
                cur.execute("ROLLBACK TO SAVEPOINT rekey")
                check(
                    "11 break: content_hash alone misses a key-only change",
                    before_lit == after_lit and before_ours != after_ours,
                    f"literal moved={before_lit != after_lit}, "
                    f"ours moved={before_ours != after_ours}",
                )

                # 12. break: players without quote_nullable
                check(
                    "12 break: without quote_nullable, null group == missing player",
                    null_literal == missing_literal and null_group != missing,
                    f"literal: null={null_literal}, missing={missing_literal}",
                )

                # 13. break: the same failed read without a savepoint aborts the transaction
                cur.execute("SAVEPOINT no_savepoint")
                sql, params = broken.digest_sql(season, week)
                try:
                    cur.execute(sql, params)
                except psycopg.errors.UndefinedTable:
                    pass
                try:
                    cur.execute("SELECT 1")
                    aborted = False
                except psycopg.errors.InFailedSqlTransaction:
                    aborted = True
                cur.execute("ROLLBACK TO SAVEPOINT no_savepoint")
                check(
                    "13 break: no savepoint -> the run's transaction is aborted",
                    aborted,
                    f"next statement failed with InFailedSqlTransaction: {aborted}",
                )
        except _Stop as stop:
            print(f"STOPPED: {stop}")
        except Exception as exc:
            check("aborted", False, f"{type(exc).__name__}: {exc}")
        finally:
            conn.rollback()

    return _report(results)


if __name__ == "__main__":
    sys.exit(main())

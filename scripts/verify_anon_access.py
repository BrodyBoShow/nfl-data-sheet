"""One-off script: verify migration 0026's anon surface (docs/phases/P6.md §2), live.

Two halves:
- catalog: the pipeline's own DB connection reads what `anon` and `authenticated` may
  do according to pg_catalog, and checks it exactly against §2a: which columns, which
  policies, RLS on every table, invoker views, closed default privileges.
- api: PostgREST with SUPABASE_ANON_KEY, as an outsider holding the key would use it.
  Reads each `web` view, attempts writes, and probes the `public` schema.

Read-only by construction. Writes carry an empty body or a filter that matches no row
(`season = -1`), so even a wrongly granted privilege couldn't change data. A 2xx
response still fails the check, because it proves the privilege exists.

Order (the `web` schema must exist before it's exposed):
  1. uv run python db/migrate.py
  2. uv run python scripts/verify_anon_access.py --catalog-only
  3. Dashboard -> Data API -> Exposed schemas = web only
  4. uv run python scripts/verify_anon_access.py
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402

ANON_COLUMNS: dict[str, set[str]] = {
    "games": {
        "game_id", "season", "week", "home_team", "away_team", "gameday", "gametime",
        "location",
    },
    "matchup_cards": {
        "game_id", "season", "week", "kickoff", "projection_status", "projected_spread",
        "projected_total", "edge_spread", "edge_total", "locked", "card", "as_of",
        "inputs_version",
    },
    "signals": {
        "season", "week", "game_id", "team", "player_id", "sector", "signal", "value",
        "league_pct", "sample_n", "stability", "as_of", "inputs_version",
    },
}
ANON_POLICIES = {
    ("games", "games_anon_read"),
    ("matchup_cards", "matchup_cards_anon_read"),
    ("signals", "signals_anon_read"),
}
WEB_VIEWS = ("games", "weeks", "week_cards", "cards", "signals")
_TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_ROLES = ("anon", "authenticated")

Result = tuple[str, bool, str]


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


def catalog_checks() -> list[Result]:
    out: list[Result] = []
    with get_connection() as conn:
        for role in _ROLES:
            rels = conn.execute(
                """
                SELECT n.nspname, c.relname, p.priv
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN unnest(%s::text[]) AS p(priv)
                WHERE n.nspname IN ('public', 'web') AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
                  AND has_table_privilege(%s, c.oid, p.priv)
                ORDER BY 1, 2, 3
                """,
                (list(_TABLE_PRIVS), role),
            ).fetchall()
            got = {(s, t, p) for s, t, p in rels}
            want = {("web", v, "SELECT") for v in WEB_VIEWS} if role == "anon" else set()
            out.append((
                f"catalog: {role} table-level privileges == "
                + ("SELECT on the 5 web views" if role == "anon" else "none"),
                got == want,
                _diff(got, want),
            ))

            cols = conn.execute(
                """
                SELECT c.relname, a.attname, p.priv
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']) AS p(priv)
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND a.attnum > 0 AND NOT a.attisdropped
                  AND has_column_privilege(%s, c.oid, a.attnum, p.priv)
                """,
                (role,),
            ).fetchall()
            got_cols = {(t, a, p) for t, a, p in cols}
            want_cols = (
                {(t, a, "SELECT") for t, attrs in ANON_COLUMNS.items() for a in attrs}
                if role == "anon" else set()
            )
            out.append((
                f"catalog: {role} column privileges on public == "
                + ("SELECT on games/matchup_cards/signals allow-list" if role == "anon"
                   else "none"),
                got_cols == want_cols,
                _diff(got_cols, want_cols),
            ))

            funcs = conn.execute(
                """
                SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname IN ('public', 'web') AND p.prorettype <> 'trigger'::regtype
                  AND has_function_privilege(%s, p.oid, 'EXECUTE')
                """,
                (role,),
            ).fetchall()
            out.append((
                f"catalog: {role} can execute no non-trigger function",
                not funcs,
                ", ".join(r[0] for r in funcs),
            ))

        no_rls = conn.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
            ORDER BY 1
            """
        ).fetchall()
        out.append((
            "catalog: RLS enabled on every public table",
            not no_rls,
            ", ".join(r[0] for r in no_rls),
        ))

        # A policy whose roles include PUBLIC applies to anon too.
        policies = conn.execute(
            """
            SELECT tablename, policyname, cmd FROM pg_policies
            WHERE schemaname = 'public'
              AND roles && ARRAY['anon', 'authenticated', 'public']::name[]
            """
        ).fetchall()
        got_pol = {(t, p) for t, p, _ in policies}
        non_select = [f"{t}.{p} ({c})" for t, p, c in policies if c != "SELECT"]
        out.append((
            "catalog: anon-reachable policies == the 3 SELECT policies",
            got_pol == ANON_POLICIES and not non_select,
            "; ".join(filter(None, [_diff(got_pol, ANON_POLICIES), ", ".join(non_select)])),
        ))

        views = conn.execute(
            """
            SELECT c.relname, coalesce(c.reloptions, '{}') FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'web' AND c.relkind = 'v'
            """
        ).fetchall()
        not_invoker = sorted(v for v, opts in views if "security_invoker=true" not in opts)
        out.append((
            "catalog: web has the 5 views, all security_invoker",
            {v for v, _ in views} == set(WEB_VIEWS) and not not_invoker,
            f"views={sorted(v for v, _ in views)} not_invoker={not_invoker}",
        ))

        acl = conn.execute(
            """
            SELECT d.defaclobjtype, d.defaclacl::text FROM pg_default_acl d
            JOIN pg_namespace n ON n.oid = d.defaclnamespace
            WHERE pg_get_userbyid(d.defaclrole) = 'postgres' AND n.nspname = 'public'
            """
        ).fetchall()
        leaky = [f"{t}:{a}" for t, a in acl if "anon=" in a or "authenticated=" in a]
        out.append((
            "catalog: postgres default privileges in public grant anon/authenticated nothing",
            not leaky,
            "; ".join(leaky),
        ))
        conn.rollback()
    return out


def _diff(got: set[Any], want: set[Any]) -> str:
    extra, missing = sorted(got - want), sorted(want - got)
    parts = []
    if extra:
        parts.append(f"unexpected={extra}")
    if missing:
        parts.append(f"missing={missing}")
    return " ".join(parts)


def player_signal_rows() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT count(*) FROM signals WHERE player_id IS NOT NULL").fetchone()
        conn.rollback()
    return int(row[0]) if row else 0


def public_tables() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm') ORDER BY 1
            """
        ).fetchall()
        conn.rollback()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------------------


def key_role(key: str) -> str | None:
    """The role a key acts as. The api half is meaningless with a key that bypasses RLS."""
    if key.startswith("sb_publishable_"):
        return "anon"
    if key.startswith("sb_secret_"):
        return "service_role"
    parts = key.split(".")
    if len(parts) == 3:
        try:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        except (binascii.Error, ValueError):
            return None
        role = payload.get("role") if isinstance(payload, dict) else None
        return role if isinstance(role, str) else None
    return None


def _code(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return ""
    return str(body.get("code", "")) if isinstance(body, dict) else ""


def _rejected(r: httpx.Response) -> bool:
    # A 23xxx (integrity) error means the statement got past the privilege check.
    return r.status_code >= 400 and not _code(r).startswith("23")


def api_checks(client: httpx.Client, n_player_rows: int) -> list[Result]:
    out: list[Result] = []
    web = {"Accept-Profile": "web"}

    for view in WEB_VIEWS:
        r = client.get(f"/{view}", params={"limit": "1"}, headers=web)
        rows = r.json() if r.status_code == 200 else None
        out.append((
            f"api: GET web.{view} returns rows",
            isinstance(rows, list) and len(rows) == 1,
            f"{r.status_code} {_code(r)}",
        ))
        if view == "games" and isinstance(rows, list) and rows:
            keys = set(rows[0])
            out.append((
                "api: web.games exposes exactly the 8 identity columns",
                keys == ANON_COLUMNS["games"],
                _diff(keys, ANON_COLUMNS["games"]),
            ))

    r = client.get("/games", params={"select": "home_score", "limit": "1"}, headers=web)
    out.append(("api: web.games?select=home_score is an error", r.status_code >= 400,
                f"{r.status_code} {_code(r)}"))

    r = client.get(
        "/signals", params={"player_id": "not.is.null", "limit": "1"}, headers=web
    )
    leaked = r.json() if r.status_code == 200 else None
    out.append((
        f"api: web.signals returns 0 player rows ({n_player_rows} exist in the table)",
        n_player_rows > 0 and leaked == [],
        f"{r.status_code} rows={leaked if leaked is None else len(leaked)}"
        + ("" if n_player_rows > 0 else " (vacuous: no player rows to hide)"),
    ))

    writes = {**web, "Content-Profile": "web", "Prefer": "return=minimal"}
    for view in WEB_VIEWS:
        attempts = {
            "POST": client.post(f"/{view}", json={}, headers=writes),
            "PATCH": client.patch(
                f"/{view}", params={"season": "eq.-1"}, json={"season": -1}, headers=writes
            ),
            "DELETE": client.delete(f"/{view}", params={"season": "eq.-1"}, headers=writes),
        }
        for method, resp in attempts.items():
            out.append((
                f"api: {method} web.{view} rejected",
                _rejected(resp),
                f"{resp.status_code} {_code(resp)}",
            ))

    public = {"Accept-Profile": "public"}
    exposed, readable = [], []
    for table in public_tables():
        resp = client.get(f"/{table}", params={"limit": "1"}, headers=public)
        if resp.status_code < 400:
            readable.append(table)
        elif _code(resp) != "PGRST106":
            exposed.append(f"{table}:{resp.status_code}/{_code(resp)}")
    out.append(("api: no public table readable", not readable, ", ".join(readable)))
    out.append((
        "api: public schema not exposed (PGRST106; dashboard step done)",
        not exposed and not readable,
        "; ".join(exposed[:5]) + (" ..." if len(exposed) > 5 else ""),
    ))

    resp = client.post(
        "/rpc/projection_log_immutable", json={}, headers={"Content-Profile": "public"}
    )
    out.append(("api: RPC projection_log_immutable unreachable", resp.status_code >= 400,
                f"{resp.status_code} {_code(resp)}"))
    return out


# --------------------------------------------------------------------------------------


def _print(results: list[Result]) -> bool:
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail if not ok else ''}".rstrip())
    return all(ok for _, ok, _ in results)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify migration 0026's anon surface.")
    parser.add_argument(
        "--catalog-only", action="store_true",
        help="check grants/policies only (run after migrate, before the dashboard change)",
    )
    args = parser.parse_args()

    results = catalog_checks()
    if not args.catalog_only:
        settings = get_settings()
        if not settings.supabase_url or not settings.supabase_anon_key:
            print("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env for the api half.")
            return 2
        key = settings.supabase_anon_key
        role = key_role(key)
        if role != "anon":
            print(f"SUPABASE_ANON_KEY acts as role {role!r}, not 'anon'. Refusing to run the "
                  "api half with it: a key that bypasses RLS would make every check "
                  "meaningless.")
            return 2
        headers = {"apikey": key}
        if key.count(".") == 2:  # legacy JWT; a publishable key must not go in Authorization
            headers["Authorization"] = f"Bearer {key}"
        base = settings.supabase_url.rstrip("/") + "/rest/v1"
        with httpx.Client(base_url=base, headers=headers, timeout=20.0) as client:
            results += api_checks(client, player_signal_rows())

    ok = _print(results)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

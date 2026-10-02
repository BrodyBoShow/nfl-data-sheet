"""
Job: Decide whether a gated analyst's declared inputs or code changed since its last
     successful run for the same season/week, and record the key it decided on.
Reads: source_freshness (marker branch) or the analyst's declared input tables (content
       branch), agent_runs (the last success's key), and the analyst's own source files
Writes: nothing (its meta reaches agent_runs.meta through base._execute)
Tier: n/a
Phase: P7
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from .base import Readiness, RunContext

# Bumped whenever the key or the decision changes. A stored key from another version
# never matches (docs/phases/P7.md, step 9, the gate spec, item 3).
GATE_VERSION = 1

MARKER = "marker"
CONTENT = "content"

# Which key the gate builds: MARKER (spec item 2, branch A) or CONTENT (branch B). The
# logger's week picks it on 2026-10-07 ("Picking on 10-07"). Until then it's None, and
# None is a hard error when a gated analyst calls the gate (GateNotSelected), so a deploy
# before the pick fails loudly instead of running on a default branch.
GATE_KEY_SOURCE: str | None = None

# A part the gate couldn't read. Never equal to anything, not even another UNKNOWN: a
# key with an UNKNOWN part always runs, and is never compared.
UNKNOWN = "unknown"
# An input with no rows in its window. string_agg over no rows is NULL, so it's given a
# fixed token: decidable, not unknown.
EMPTY = "empty"

# The last success a key is compared against: this agent, the key's own season and week.
# A backfill (--season 2025 --week 18) never satisfies the live week. The auditor's
# check (pipeline/orchestration/auditor.py) reads the same rows.
LAST_SUCCESS_WHERE = (
    "agent = %s AND status = 'success' "
    "AND meta->'gate'->>'season' = %s AND meta->'gate'->>'week' = %s"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class GateNotSelected(RuntimeError):
    """GATE_KEY_SOURCE is neither MARKER nor CONTENT. The one error the gate doesn't
    catch: the run fails, by design, until the branch is picked."""


def selected_branch() -> str:
    if GATE_KEY_SOURCE not in (MARKER, CONTENT):
        raise GateNotSelected(
            f"input_gate.GATE_KEY_SOURCE is {GATE_KEY_SOURCE!r}: set it to MARKER or CONTENT "
            "(docs/phases/P7.md, step 9, 'Picking on 10-07') before a gated analyst can run"
        )
    return GATE_KEY_SOURCE


@dataclass(frozen=True)
class GateInput:
    """One table the analyst's `_fetch` reads.

    `read_where` is the WHERE `_fetch` issues, by the analyst's own constant, never a
    copy, and `params` builds its parameters from (season, through_week).

    `ids_from` is set when the read is by an id list the other reads returned (`players`,
    read by `player_id = ANY(%s)`). The digest rebuilds that id set server-side, as the
    union of those inputs' own reads.
    """

    table: str
    marker: str
    pk: tuple[str, ...]
    read_where: str
    params: Callable[[int, int], tuple[Any, ...]] | None = None
    # SQL text digested per row. Default: the key columns and content_hash.
    row: str = ""
    ids_from: tuple[GateInput, ...] = ()
    # False: the auditor's updated_at check skips this input (players, see players_input).
    audit: bool = True

    def row_text(self) -> str:
        return self.row or f"concat_ws(':', {', '.join(self.pk)}, content_hash)"

    def digest_filter(self, season: int, week: int) -> tuple[str, tuple[Any, ...]]:
        """The WHERE and parameters that select exactly the rows `_fetch` reads."""
        if self.ids_from:
            union = " UNION ".join(
                f"SELECT player_id FROM {s.table} WHERE {s.read_where}" for s in self.ids_from
            )
            params = tuple(p for s in self.ids_from for p in s.digest_filter(season, week)[1])
            return f"player_id IN ({union})", params
        if self.params is None:
            raise ValueError(f"{self.table}: a gate input needs params or ids_from")
        return self.read_where, self.params(season, week)


def players_input(read_where: str, ids_from: tuple[GateInput, ...]) -> GateInput:
    """`players`, which both player analysts read for `position_group` only (P7 open item
    12). Its `content_hash` also covers names and status, so the digest is over
    `player_id` and `position_group` alone.

    `quote_nullable`, not a bare `||`: `'x' || NULL` is NULL, and string_agg drops it, so
    a player present with a null group would digest the same as a player not in
    `players` at all. The analysts treat those two differently (a missing player is
    `skipped_not_in_players`; a null group is written).

    Excluded from the auditor's updated_at check: its `updated_at` moves on columns the
    analysts don't read, so it would alarm on a correct skip (spec item 5)."""
    return GateInput(
        table="players",
        marker="nflverse:players",
        pk=("player_id",),
        read_where=read_where,
        row="player_id || ':' || quote_nullable(position_group)",
        ids_from=ids_from,
        audit=False,
    )


# --------------------------------------------------------------------------------------
# Code fingerprint
# --------------------------------------------------------------------------------------


def _module_file(module: str, root: Path) -> Path | None:
    base = root.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _import_base(node: ast.ImportFrom, package: str) -> str:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    parts = parts[: len(parts) - (node.level - 1)]
    return ".".join(parts + ([node.module] if node.module else []))


def code_files(module: str, root: Path = _REPO_ROOT) -> list[Path]:
    """Every file in `module`'s `pipeline.*` import closure, parsed from the source
    itself, so no list can fall behind an edit. Transitive: a change to anything the
    analyst runs (`player_tables`, `db`, `hashing`, `base`) is a code change. Package
    `__init__.py` files are included, since importing a module executes them."""
    files: set[Path] = set()
    seen: set[str] = set()
    todo = [module]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_file(mod, root)
        if path is None:
            raise ValueError(f"{mod}: imported, but no file under {root}")
        files.add(path)
        parts = mod.split(".")
        for i in range(1, len(parts)):
            init = root.joinpath(*parts[:i], "__init__.py")
            if init.is_file():
                files.add(init)
        package = mod if path.name == "__init__.py" else mod.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                todo += [a.name for a in node.names if a.name.split(".")[0] == "pipeline"]
            elif isinstance(node, ast.ImportFrom):
                base = _import_base(node, package)
                if base.split(".")[0] != "pipeline":
                    continue
                todo.append(base)
                # `from pipeline.core import db` imports a module; `from x import Name` doesn't.
                todo += [
                    f"{base}.{a.name}" for a in node.names if _module_file(f"{base}.{a.name}", root)
                ]
    return sorted(files)


def code_fingerprint(files: Iterable[Path], root: Path = _REPO_ROOT) -> str:
    """sha256 over each file's repo path and bytes. CRLF is read as LF, so a checkout's
    line endings are never a code change."""
    h = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.resolve().relative_to(root).as_posix()):
        data = path.read_bytes().replace(b"\r\n", b"\n")
        rel = path.resolve().relative_to(root).as_posix()
        h.update(f"{rel}\0{len(data)}\0".encode())
        h.update(data)
    return h.hexdigest()


def key_of(parts: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


# --------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InputGate:
    """One analyst's gate: its inputs, its module (for the code fingerprint) and any file
    the module reads that isn't an import (its migration, which sets `COLUMNS`)."""

    agent: str
    module: str
    inputs: tuple[GateInput, ...]
    extra_files: tuple[Path, ...] = ()

    def code_files(self) -> list[Path]:
        return sorted(set(code_files(self.module)) | {p.resolve() for p in self.extra_files})

    def digest_sql(self, season: int, week: int) -> tuple[str, tuple[Any, ...]]:
        """Every input's digest in one statement. Each is md5 over its rows' text in key
        order, or EMPTY when the window holds no rows."""
        cols: list[str] = []
        params: list[Any] = []
        for i in self.inputs:
            where, p = i.digest_filter(season, week)
            cols.append(
                f"(SELECT coalesce(md5(string_agg({i.row_text()}, '|' ORDER BY "
                f"{', '.join(i.pk)})), '{EMPTY}') FROM {i.table} WHERE {where})"
            )
            params += p
        return "SELECT " + ", ".join(cols), tuple(params)

    def _digests(
        self, conn: psycopg.Connection, season: int, week: int
    ) -> tuple[dict[str, str], str | None]:
        sql, params = self.digest_sql(season, week)
        try:
            # A savepoint: a failed read must leave the run's transaction usable.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        except Exception as exc:
            return {i.table: UNKNOWN for i in self.inputs}, _error(exc)
        if row is None:
            return {i.table: UNKNOWN for i in self.inputs}, "digest returned no row"
        return {i.table: (v or UNKNOWN) for i, v in zip(self.inputs, row, strict=True)}, None

    def _markers(self, conn: psycopg.Connection) -> tuple[dict[str, str], str | None]:
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT source, last_value FROM source_freshness WHERE source = ANY(%s)",
                    (sorted({i.marker for i in self.inputs}),),
                )
                found: dict[str, str | None] = dict(cur.fetchall())
        except Exception as exc:
            return {i.table: UNKNOWN for i in self.inputs}, _error(exc)
        # A missing marker is unknown, not a value.
        return {i.table: (found.get(i.marker) or UNKNOWN) for i in self.inputs}, None

    def _last_success(
        self, conn: psycopg.Connection, season: int, week: int
    ) -> tuple[int, dict[str, Any]] | None:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                f"SELECT id, meta->'gate' FROM agent_runs WHERE {LAST_SUCCESS_WHERE} "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (self.agent, str(season), str(week)),
            )
            row = cur.fetchone()
        return None if row is None else (row[0], row[1] or {})

    def check(self, ctx: RunContext, *, force: bool) -> Readiness:
        """Run (status None) or skip as `skipped_unchanged`, with `meta.gate` either way.

        Raises only GateNotSelected. Anything else is caught and runs, recorded as
        `run:gate_error`: the gate never fails a run and never skips on an error."""
        branch = selected_branch()
        gate: dict[str, Any] = {
            "version": GATE_VERSION,
            "branch": branch,
            "season": ctx.season,
            "week": ctx.week,
        }
        try:
            return self._decide(ctx.conn, gate, ctx.season, ctx.week, force=force)
        except Exception as exc:
            gate["decision"] = "run:gate_error"
            gate["error"] = _error(exc)
            return Readiness(None, {"gate": gate})

    def _decide(
        self,
        conn: psycopg.Connection,
        gate: dict[str, Any],
        season: int,
        week: int,
        *,
        force: bool,
    ) -> Readiness:
        def run(decision: str, **extra: Any) -> Readiness:
            gate.update(decision=decision, **extra)
            return Readiness(None, {"gate": gate})

        # The key is taken before compute() reads. If inputs change in between, the next
        # run sees a new key: one extra run, never a skip (spec item 4).
        parts: dict[str, Any] = {
            "gate_version": GATE_VERSION,
            "branch": gate["branch"],
            "season": season,
            "week": week,
            "code": UNKNOWN,
        }
        errors: dict[str, str] = {}
        try:
            parts["code"] = code_fingerprint(self.code_files())
        except Exception as exc:
            errors["code"] = _error(exc)
        if gate["branch"] == CONTENT:
            inputs, err = self._digests(conn, season, week)
        else:
            inputs, err = self._markers(conn)
        if err:
            errors["inputs"] = err
        parts["inputs"] = inputs
        gate["parts"] = parts
        gate["key"] = key_of(parts)
        if errors:
            gate["errors"] = errors

        if force:
            return run("run:forced")
        named = [("code", parts["code"]), *inputs.items()]
        unknown = sorted(name for name, v in named if v == UNKNOWN)
        if unknown:
            return run("run:unknown_part", unknown=unknown)
        try:
            last = self._last_success(conn, season, week)
        except Exception as exc:
            return run("run:lookup_error", lookup_error=_error(exc))
        if last is None:
            return run("run:no_prior_success")
        run_id, stored = last
        if stored.get("version") != GATE_VERSION:
            return run("run:other_gate_version", compared_run_id=run_id)
        if stored.get("branch") != gate["branch"]:
            return run("run:other_branch", compared_run_id=run_id)
        if not stored.get("key"):
            return run("run:prior_has_no_key", compared_run_id=run_id)
        if stored["key"] != gate["key"]:
            return run("run:changed", compared_run_id=run_id, changed=_changed(stored, parts))
        gate.update(decision="skip:unchanged", matched_run_id=run_id)
        return Readiness("skipped_unchanged", {"gate": gate})


def _changed(stored: dict[str, Any], parts: dict[str, Any]) -> list[str]:
    """Which parts differ from the compared run's, so which input moved can be read back
    from any run."""
    old = stored.get("parts") or {}
    scalars = ("gate_version", "branch", "season", "week", "code")
    out = [k for k in scalars if old.get(k) != parts[k]]
    old_inputs, new_inputs = old.get("inputs") or {}, parts["inputs"]
    tables = sorted(set(old_inputs) | set(new_inputs))
    return out + [t for t in tables if old_inputs.get(t) != new_inputs.get(t)]

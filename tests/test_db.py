"""upsert_changed's client side, against a fake cursor. What Postgres does with the
statement (updated_at, RETURNING, the counts) is checked on the real engine by
scripts/verify_server_diff.py, since tests never open a live connection."""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.core.db import upsert_changed, upsert_changed_sql


class _FakeCopy:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows

    def __enter__(self) -> _FakeCopy:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def write_row(self, row: list[Any]) -> None:
        self.rows.append(row)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)

    def copy(self, sql: str) -> _FakeCopy:
        self.conn.sql.append(sql)
        return _FakeCopy(self.conn.copied)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.conn.returned


class _FakeConn:
    def __init__(self, returned: list[tuple[Any, ...]] | None = None) -> None:
        self.sql: list[str] = []
        self.copied: list[list[Any]] = []
        self.returned = returned or []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def _row(key: str, value: int, h: str) -> dict[str, Any]:
    return {"k": key, "v": value, "content_hash": h, "updated_at": "t"}


def test_sql_keeps_the_prefilter_and_the_conflict_guard():
    sql = upsert_changed_sql("t", "s", ["k", "v", "content_hash"], ["k"], ["v", "content_hash"])
    assert (
        "WHERE NOT EXISTS (SELECT 1 FROM t AS cur WHERE cur.k = src.k "
        "AND cur.content_hash = src.content_hash)"
    ) in sql
    assert "DO UPDATE SET v = EXCLUDED.v, content_hash = EXCLUDED.content_hash" in sql
    assert "WHERE t.content_hash IS DISTINCT FROM EXCLUDED.content_hash" in sql
    assert sql.endswith("RETURNING (t.xmax = 0)")


def test_sql_update_exprs_and_returning():
    sql = upsert_changed_sql(
        "g",
        "s",
        ["id", "a"],
        ["id"],
        ["a"],
        update_exprs={"a": "COALESCE(g.a, EXCLUDED.a)"},
        returning=("id",),
    )
    assert "SET a = COALESCE(g.a, EXCLUDED.a)" in sql
    assert sql.endswith("RETURNING (g.xmax = 0), id")


def test_duplicate_keys_keep_the_last_row_and_are_counted():
    conn = _FakeConn()
    rows = [_row("a", 1, "h1"), _row("b", 2, "h2"), _row("a", 3, "h3"), _row("a", 4, "h4")]
    result = upsert_changed(conn, "t", rows, "k", ["v", "content_hash", "updated_at"])  # type: ignore[arg-type]
    assert result.duplicates_dropped == 2
    assert sorted(conn.copied) == [["a", 4, "h4", "t"], ["b", 2, "h2", "t"]]


def test_counts_come_from_returning():
    conn = _FakeConn(returned=[(True,), (False,), (False,)])
    rows = [_row(k, 1, "h") for k in "abcd"]
    result = upsert_changed(conn, "t", rows, ["k"], ["v", "content_hash", "updated_at"])  # type: ignore[arg-type]
    assert (result.inserted, result.updated, result.rows_changed) == (1, 2, 3)
    assert result.meta() == {"rows_changed": 3, "duplicates_dropped": 0}


def test_returning_columns_are_handed_back_without_the_flag():
    conn = _FakeConn(returned=[(True, "g1", "graded"), (False, "g2", "pending")])
    rows = [_row("a", 1, "h"), _row("b", 1, "h")]
    result = upsert_changed(conn, "t", rows, "k", ["v"], returning=("k", "status"))  # type: ignore[arg-type]
    assert result.returned == [("g1", "graded"), ("g2", "pending")]


def test_rows_without_content_hash_fail_loudly():
    rows = [{"k": "a", "v": 1}]
    with pytest.raises(ValueError, match="no content_hash"):
        upsert_changed(_FakeConn(), "t", rows, "k", ["v"])  # type: ignore[arg-type]


def test_no_rows_sends_nothing():
    conn = _FakeConn()
    result = upsert_changed(conn, "t", [], "k", ["v"])  # type: ignore[arg-type]
    assert result.rows_changed == 0 and result.duplicates_dropped == 0
    assert conn.sql == []


def test_stage_is_created_copied_and_dropped_in_order():
    conn = _FakeConn()
    upsert_changed(conn, "t", [_row("a", 1, "h")], "k", ["v", "content_hash"])  # type: ignore[arg-type]
    kinds = [s.split(" ", 1)[0] for s in conn.sql]
    assert kinds == ["CREATE", "COPY", "INSERT", "DROP"]
    assert "ON COMMIT DROP" in conn.sql[0] and "WITH NO DATA" in conn.sql[0]

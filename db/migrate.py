"""Apply pending SQL migrations in db/migrations/ to Supabase, in filename order.

Tracks applied migrations in a `schema_migrations` table, so re-running is a no-op
once everything is applied. Requires SUPABASE_DB_URL (see .env.example).

Usage: uv run python db/migrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.config import get_settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def main() -> None:
    settings = get_settings()
    conn = psycopg.connect(settings.supabase_db_url)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()

        applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations").fetchall()}
        pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.stem not in applied)

        if not pending:
            print("Nothing to apply -- schema is up to date.")
            return

        for path in pending:
            print(f"Applying {path.name} ...")
            sql_text = path.read_text(encoding="utf-8")
            try:
                conn.execute(sql_text)
                conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (path.stem,))
                conn.commit()
                print("  ok")
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()

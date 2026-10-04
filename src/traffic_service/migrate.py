"""Apply ordered .sql migrations, once each, each in its own transaction."""
from __future__ import annotations

import pathlib
import sys

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"


def discover(migrations_dir: pathlib.Path | None = None) -> list[pathlib.Path]:
    directory = migrations_dir or MIGRATIONS_DIR
    return sorted(directory.glob("*.sql"))


def applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """select table_name from information_schema.tables
               where table_schema = 'public' and table_name = 'schema_migrations'"""
        )
        if cur.fetchone() is None:
            return set()
        cur.execute("select filename from schema_migrations")
        return {row[0] for row in cur.fetchall()}


def migrate(conn, migrations_dir: pathlib.Path | None = None) -> list[str]:
    """Apply pending migrations; returns the filenames applied by this call."""
    done = applied(conn)
    pending = [p for p in discover(migrations_dir) if p.name not in done]
    applied_now: list[str] = []
    for path in pending:
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
        conn.commit()
        applied_now.append(path.name)
        print(f"applied {path.name}", file=sys.stderr)
    return applied_now

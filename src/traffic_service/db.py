"""Database access — one place that knows how to reach Postgres.

Deliberately thin: psycopg 3 with plain SQL. There is one service and one
schema, so an ORM or a migration framework would add a dependency without
removing any work.
"""
from __future__ import annotations

import os

import psycopg

DEFAULT_URL = "postgresql://traffic:traffic@localhost:5433/traffic"


def database_url() -> str:
    """`DATABASE_URL` from the environment, else the local Compose default."""
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection. Autocommit stays off so callers own the transaction."""
    return psycopg.connect(url or database_url())

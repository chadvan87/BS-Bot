"""Database access: SQLite by default, Postgres (e.g. Supabase) when DATABASE_URL is set.

Callers use one interface for both: `with connect(path) as c: c.execute(sql, params)`, with
`?` placeholders, dict-style rows, and `cur.lastrowid` / `cur.rowcount` on the result.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

# Inserts into tables with an `id` column get `RETURNING id` appended so lastrowid works on Postgres.
_INSERT_WITH_ID = re.compile(r"\s*INSERT\s+INTO\s+(signals|posts)\b", re.IGNORECASE)
_pool = None

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL, side TEXT NOT NULL, timeframe TEXT NOT NULL,
  entry_low REAL, entry_high REAL, stop_loss REAL,
  tp1 REAL, tp2 REAL, tp3 REAL, score INTEGER,
  reason TEXT, status TEXT DEFAULT 'NEW', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id INTEGER, title TEXT, content TEXT NOT NULL,
  status TEXT DEFAULT 'DRAFT', scheduled_at TEXT,
  published_at TEXT, publish_result TEXT, created_at TEXT NOT NULL,
  chart_path TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

# DOUBLE PRECISION, not REAL: Postgres REAL is 4-byte and would round small-cap prices.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL, side TEXT NOT NULL, timeframe TEXT NOT NULL,
  entry_low DOUBLE PRECISION, entry_high DOUBLE PRECISION, stop_loss DOUBLE PRECISION,
  tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION, tp3 DOUBLE PRECISION, score INTEGER,
  reason TEXT, status TEXT DEFAULT 'NEW', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id BIGSERIAL PRIMARY KEY,
  signal_id BIGINT, title TEXT, content TEXT NOT NULL,
  status TEXT DEFAULT 'DRAFT', scheduled_at TEXT,
  published_at TEXT, publish_result TEXT, created_at TEXT NOT NULL,
  chart_path TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

SIGNAL_EXTRA_COLUMNS = ("price", "ema20", "ema50", "ema200", "rsi", "atr", "volume_ratio", "support", "resistance")
POST_EXTRA_COLUMNS = (("template_name", "TEXT"), ("delete_after_publish", "INTEGER DEFAULT 0"))
DEFAULT_SETTINGS = (("PUBLISH_MODE", "mock"), ("MIN_SPACING_MINUTES", "20"), ("DAILY_SOFT_LIMIT", "40"))


class _PgCursor:
    def __init__(self, cur: Any, lastrowid: int | None) -> None:
        self._cur = cur
        self.lastrowid = lastrowid
        self.rowcount = cur.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._cur.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cur.fetchall()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._cur.fetchall())


class _PgConn:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        sql = sql.replace("?", "%s")
        returning = bool(_INSERT_WITH_ID.match(sql)) and "RETURNING" not in sql.upper()
        if returning:
            sql += " RETURNING id"
        cur = self._conn.execute(sql, params)
        lastrowid = cur.fetchone()["id"] if returning else None
        return _PgCursor(cur, lastrowid)


def _get_pool() -> Any:
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # prepare_threshold=None keeps it working behind Supabase's transaction pooler too.
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


@contextmanager
def connect(sqlite_path: Path) -> Iterator[Any]:
    """Yield a connection; commits on success, rolls back on error."""
    if USE_POSTGRES:
        with _get_pool().connection() as c:
            yield _PgConn(c)
        return
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(sqlite_path)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init_schema(sqlite_path: Path) -> None:
    if USE_POSTGRES:
        with _get_pool().connection() as c:
            for stmt in filter(str.strip, POSTGRES_SCHEMA.split(";")):
                c.execute(stmt)
            for col in SIGNAL_EXTRA_COLUMNS:
                c.execute(f"ALTER TABLE signals ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION")
            for col, col_type in POST_EXTRA_COLUMNS:
                c.execute(f"ALTER TABLE posts ADD COLUMN IF NOT EXISTS {col} {col_type}")
            # Supabase exposes the public schema over its REST API; RLS with no policies blocks
            # that path, while this app's direct connection (the table owner) is unaffected.
            for table in ("signals", "posts", "settings"):
                c.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            for key, value in DEFAULT_SETTINGS:
                c.execute("INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING", (key, value))
        return
    with connect(sqlite_path) as c:
        c.executescript(SQLITE_SCHEMA)
        signal_cols = {r["name"] for r in c.execute("PRAGMA table_info(signals)")}
        for col in SIGNAL_EXTRA_COLUMNS:
            if col not in signal_cols:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
        post_cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
        for col, col_type in POST_EXTRA_COLUMNS:
            if col not in post_cols:
                c.execute(f"ALTER TABLE posts ADD COLUMN {col} {col_type}")
        for key, value in DEFAULT_SETTINGS:
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

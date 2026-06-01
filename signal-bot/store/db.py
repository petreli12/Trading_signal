"""SQLite persistence layer for signal-bot.

Tables:
    posts        — normalized X posts (one row per tweet)
    pdf_docs     — extracted EOD PDF documents
    ticker_daily — per-day, per-ticker, per-bucket aggregates (the core signal)
    runs         — metadata for each pipeline run

Day-over-day deltas depend on ``ticker_daily`` being persisted every run, so
keep yesterday's rows around rather than overwriting them.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "signal_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,          -- tweet id
    author      TEXT,
    text        TEXT,
    ts          TEXT,                       -- ISO 8601 timestamp of the post
    engagement  INTEGER DEFAULT 0,
    run_id      INTEGER,
    fetched_at  TEXT,                        -- ISO 8601 timestamp of ingestion
    FOREIGN KEY (run_id) REFERENCES runs (id)
);

CREATE TABLE IF NOT EXISTS pdf_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_date    TEXT,                        -- the date the recap covers (YYYY-MM-DD)
    source      TEXT,
    raw_text    TEXT,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS ticker_daily (
    date            TEXT NOT NULL,           -- YYYY-MM-DD
    ticker          TEXT NOT NULL,
    bucket          TEXT NOT NULL,           -- e.g. 'x' (narrative) or 'pdf' (recap)
    mention_count   INTEGER DEFAULT 0,
    net_sentiment   REAL DEFAULT 0.0,
    dispersion      REAL DEFAULT 0.0,
    weighted_score  REAL DEFAULT 0.0,
    PRIMARY KEY (date, ticker, bucket)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT,                        -- 'preopen' | 'postclose'
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,                        -- 'running' | 'ok' | 'error'
    notes       TEXT
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults.

    Rows are returned as ``sqlite3.Row`` so callers can access columns by name.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def connection(db_path: str = DEFAULT_DB_PATH):
    """Context manager yielding a connection that commits on success.

    Rolls back on exception and always closes the connection.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create all tables if they do not already exist.

    Safe to call on every run; uses ``CREATE TABLE IF NOT EXISTS``.
    """
    with connection(db_path) as conn:
        conn.executescript(SCHEMA)


def get_posts_by_run(run_id: int, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return all posts fetched by a given run, newest first."""
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, author, text, ts, engagement FROM posts "
            "WHERE run_id = ? ORDER BY CAST(id AS INTEGER) DESC",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_posts_by_date(day: str, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return all posts whose timestamp falls on ``day`` (YYYY-MM-DD)."""
    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT id, author, text, ts, engagement FROM posts "
            "WHERE substr(ts, 1, 10) = ? ORDER BY CAST(id AS INTEGER) DESC",
            (day,),
        ).fetchall()
        return [dict(row) for row in rows]


# --- pdf_docs helpers ----------------------------------------------------

def insert_pdf_doc(doc: dict[str, Any], db_path: str = DEFAULT_DB_PATH) -> int:
    """Insert an extracted PDF document and return its new row id.

    Args:
        doc: Dict with ``doc_date``, ``source``, and ``raw_text``.
    """
    with connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO pdf_docs (doc_date, source, raw_text, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                doc.get("doc_date", ""),
                doc.get("source", ""),
                doc.get("raw_text", ""),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def get_pdf_doc_by_date(
    doc_date: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Return the most recently fetched PDF doc covering ``doc_date``."""
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, doc_date, source, raw_text, fetched_at FROM pdf_docs "
            "WHERE doc_date = ? ORDER BY id DESC LIMIT 1",
            (doc_date,),
        ).fetchone()
        return dict(row) if row else None


def get_latest_pdf_doc(
    on_or_before: str | None = None, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any] | None:
    """Return the most recent recap available, optionally on/before a date.

    Used by the pre-open run: today's recap does not exist yet, so it must
    fall back to the most recently available recap (the prior trading day's).
    """
    with connection(db_path) as conn:
        if on_or_before is None:
            row = conn.execute(
                "SELECT id, doc_date, source, raw_text, fetched_at FROM pdf_docs "
                "ORDER BY doc_date DESC, id DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, doc_date, source, raw_text, fetched_at FROM pdf_docs "
                "WHERE doc_date <= ? ORDER BY doc_date DESC, id DESC LIMIT 1",
                (on_or_before,),
            ).fetchone()
        return dict(row) if row else None


# --- ticker_daily helpers ------------------------------------------------

def upsert_ticker_daily(row: dict[str, Any], db_path: str = DEFAULT_DB_PATH) -> None:
    """Insert or replace a ticker_daily row (PK = date + ticker + bucket)."""
    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ticker_daily
                (date, ticker, bucket, mention_count, net_sentiment,
                 dispersion, weighted_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["date"],
                row["ticker"],
                row["bucket"],
                int(row.get("mention_count", 0)),
                float(row.get("net_sentiment", 0.0)),
                float(row.get("dispersion", 0.0)),
                float(row.get("weighted_score", 0.0)),
            ),
        )


def prior_mention_counts(
    before_date: str, db_path: str = DEFAULT_DB_PATH
) -> dict[tuple[str, str], int]:
    """Return prior-day mention counts keyed by (ticker, bucket).

    For each (ticker, bucket), uses the most recent ``ticker_daily`` row
    strictly before ``before_date`` — so deltas compare against the last
    trading day with data, skipping weekends/holidays automatically.
    """
    with connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.ticker, t.bucket, t.mention_count
            FROM ticker_daily t
            WHERE t.date = (
                SELECT MAX(d.date) FROM ticker_daily d
                WHERE d.date < ? AND d.ticker = t.ticker AND d.bucket = t.bucket
            )
            """,
            (before_date,),
        ).fetchall()
        return {(row["ticker"], row["bucket"]): row["mention_count"] for row in rows}


# --- runs helpers --------------------------------------------------------

def start_run(mode: str, db_path: str = DEFAULT_DB_PATH) -> int:
    """Insert a new run row with status 'running' and return its id."""
    with connection(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (mode, started_at, status) VALUES (?, ?, 'running')",
            (mode, datetime.now(timezone.utc).isoformat()),
        )
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    status: str = "ok",
    notes: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Mark a run finished with a terminal status ('ok' | 'error') and notes."""
    with connection(db_path) as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, notes = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), status, notes, run_id),
        )


# --- posts helpers -------------------------------------------------------

def latest_post_id(db_path: str = DEFAULT_DB_PATH) -> str | None:
    """Return the highest (newest) stored tweet id, or ``None`` if empty.

    Tweet ids are snowflake values that increase over time, so the max id is
    the most recently seen post. Used as a ``since_id`` watermark to avoid
    re-fetching old posts.
    """
    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM posts ORDER BY CAST(id AS INTEGER) DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None


def existing_post_ids(ids: Iterable[str], db_path: str = DEFAULT_DB_PATH) -> set[str]:
    """Return the subset of ``ids`` already present in the posts table."""
    ids = [str(i) for i in ids]
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    with connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT id FROM posts WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {row["id"] for row in rows}


def insert_posts(
    posts: Iterable[dict[str, Any]],
    run_id: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Insert normalized posts, ignoring any whose id already exists.

    Args:
        posts: Iterable of normalized post dicts
            (``id``, ``author``, ``text``, ``ts``, ``engagement``).
        run_id: The run that fetched these posts, if any.

    Returns:
        The number of rows actually inserted (duplicates are skipped).
    """
    rows = [
        (
            str(p["id"]),
            p.get("author", ""),
            p.get("text", ""),
            p.get("ts", ""),
            int(p.get("engagement", 0) or 0),
            run_id,
            datetime.now(timezone.utc).isoformat(),
        )
        for p in posts
    ]
    if not rows:
        return 0
    with connection(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO posts
                (id, author, text, ts, engagement, run_id, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return conn.total_changes - before

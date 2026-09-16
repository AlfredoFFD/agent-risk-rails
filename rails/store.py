"""The ledger the gates read from.

Four tables, no venue state: trades, performance, api_costs, portfolio_snapshots.
Everything the risk checks need to answer "what is already at risk right now"
and "how much have we already lost today."

Every connection goes through get_connection() so that WAL mode and a busy
timeout are never accidentally omitted. A watchdog reading while the executor
writes is the normal case, not the exception, and the failure mode of getting
that wrong is a gate that throws instead of refusing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open the ledger with the settings every reader and writer must share.

    - journal_mode=WAL: concurrent readers alongside one writer.
    - busy_timeout=5000: wait five seconds for a lock rather than raising.
      A risk gate that crashes on a locked database is a gate that is not
      running, which is worse than one that is slow.
    - Row factory: rows are addressable by column name, so a schema change
      that reorders columns cannot silently swap two values.
    """
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the schema. Safe to call repeatedly — every table is IF NOT EXISTS."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

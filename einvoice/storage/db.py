"""SQLite database: one file, schema upgraded in place with PRAGMA user_version.

Amounts and rates are stored as TEXT so Decimal values round-trip exactly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    # 1: invoices, lines and the control-number counter
    """
    CREATE TABLE invoices (
        id             INTEGER PRIMARY KEY,
        control_no     TEXT NOT NULL UNIQUE,
        issue_date     TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('issued', 'void')),
        buyer_name     TEXT NOT NULL,
        buyer_tin      TEXT NOT NULL,
        buyer_branch   TEXT NOT NULL,
        buyer_address  TEXT NOT NULL,
        business_style TEXT NOT NULL,
        rate           TEXT NOT NULL,
        rate_date      TEXT NOT NULL,
        rate_source    TEXT NOT NULL,
        total_jpy      TEXT NOT NULL,
        total_php      TEXT NOT NULL,
        eis_key        TEXT NOT NULL,
        void_reason    TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        voided_at      TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE invoice_lines (
        invoice_id     INTEGER NOT NULL REFERENCES invoices(id),
        line_no        INTEGER NOT NULL,
        item_name      TEXT NOT NULL,
        description    TEXT NOT NULL,
        qty            TEXT NOT NULL,
        unit           TEXT NOT NULL,
        unit_cost_jpy  TEXT NOT NULL,
        amount_jpy     TEXT NOT NULL,
        unit_cost_php  TEXT NOT NULL,
        sales_php      TEXT NOT NULL,
        PRIMARY KEY (invoice_id, line_no)
    );
    -- One row: the last control number handed out. Numbers only go up.
    CREATE TABLE control_counter (
        id       INTEGER PRIMARY KEY CHECK (id = 1),
        last_no  INTEGER NOT NULL
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

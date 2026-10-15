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
    # 2: real EIS exports. EisUniqueId is unique, so it can never be reused (SYN003).
    """
    CREATE TABLE eis_exports (
        eis_unique_id  TEXT PRIMARY KEY,
        invoice_id     INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
        exported_at    TEXT NOT NULL,
        file_name      TEXT NOT NULL,
        sha256         TEXT NOT NULL
    );
    """,
    # 3: submissions to BIR (phase 3) and the result per e-invoice
    """
    CREATE TABLE eis_submissions (
        submit_id       TEXT PRIMARY KEY,
        sent_at         TEXT NOT NULL,
        base_url        TEXT NOT NULL,
        ack_id          TEXT NOT NULL DEFAULT '',
        response_dtm    TEXT NOT NULL DEFAULT '',
        process_status  TEXT NOT NULL DEFAULT '',
        fail_reason     TEXT NOT NULL DEFAULT '',
        last_checked_at TEXT NOT NULL DEFAULT '',
        error           TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE eis_submission_items (
        submit_id      TEXT NOT NULL REFERENCES eis_submissions(submit_id),
        eis_unique_id  TEXT NOT NULL REFERENCES eis_exports(eis_unique_id),
        result_code    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (submit_id, eis_unique_id)
    );
    """,
    # 4: client e-mail and the e-mails sent with invoices
    """
    ALTER TABLE invoices ADD COLUMN buyer_email TEXT NOT NULL DEFAULT '';
    CREATE TABLE invoice_emails (
        id          INTEGER PRIMARY KEY,
        invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
        sent_at     TEXT NOT NULL,
        to_addr     TEXT NOT NULL,
        cc          TEXT NOT NULL DEFAULT '',
        subject     TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
        error       TEXT NOT NULL DEFAULT '',
        test        INTEGER NOT NULL DEFAULT 0,
        automatic   INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 5: sale type (EIS TransClass). Invoices before this were all zero-rated.
    """
    ALTER TABLE invoices ADD COLUMN trans_class TEXT NOT NULL DEFAULT '02';
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

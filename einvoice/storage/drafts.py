"""Invoices being filled in, kept in the database by draft id.

The review page has its own address (/invoices/review/<draft>) and the form
can be reopened from its draft, so going Back never loses what was typed,
even after the app was closed and opened again. A draft also remembers the
invoice it was issued as, so a double click or a resubmitted page never
issues twice. Drafts older than DAYS_KEPT days are dropped.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from ..models import PH_TZ

DAYS_KEPT = 30


def _now() -> str:
    return datetime.now(PH_TZ).isoformat(timespec="seconds")


def get(conn: sqlite3.Connection, draft_id: str) -> dict | None:
    row = conn.execute("SELECT data FROM invoice_drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None or not row["data"]:
        return None
    return json.loads(row["data"])


def save(conn: sqlite3.Connection, draft_id: str, data: dict) -> None:
    conn.execute(
        """INSERT INTO invoice_drafts (id, data, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
        (draft_id, json.dumps(data, ensure_ascii=False), _now()))
    cutoff = (datetime.now(PH_TZ) - timedelta(days=DAYS_KEPT)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM invoice_drafts WHERE updated_at < ?", (cutoff,))


def issued_as(conn: sqlite3.Connection, draft_id: str) -> str | None:
    row = conn.execute("SELECT issued_no FROM invoice_drafts WHERE id = ?", (draft_id,)).fetchone()
    return row["issued_no"] if row and row["issued_no"] else None


def mark_issued(conn: sqlite3.Connection, draft_id: str, control_no: str) -> None:
    conn.execute(
        """INSERT INTO invoice_drafts (id, data, issued_no, updated_at) VALUES (?, '', ?, ?)
           ON CONFLICT (id) DO UPDATE SET issued_no = excluded.issued_no,
               updated_at = excluded.updated_at""",
        (draft_id, control_no, _now()))

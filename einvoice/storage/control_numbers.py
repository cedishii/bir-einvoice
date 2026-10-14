"""Control numbers: auto-increment, never reused.

The counter row is updated in the same transaction that saves the invoice,
so a number is only consumed by an invoice that was actually saved, two
saves can never get the same number, and a voided invoice keeps its number.
"""
from __future__ import annotations

import sqlite3

from ..config import CONTROL_NO_WIDTH


def format_control_no(n: int) -> str:
    return str(n).zfill(CONTROL_NO_WIDTH)


def peek_next(conn: sqlite3.Connection, start: int) -> str:
    """The number the next issued invoice will get (display only)."""
    row = conn.execute("SELECT last_no FROM control_counter WHERE id = 1").fetchone()
    return format_control_no(row["last_no"] + 1 if row else start)


def take_next(conn: sqlite3.Connection, start: int) -> str:
    """Consume the next number. Call inside an open write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("take_next must run inside the invoice's transaction")
    row = conn.execute("SELECT last_no FROM control_counter WHERE id = 1").fetchone()
    if row is None:
        n = start
        conn.execute("INSERT INTO control_counter (id, last_no) VALUES (1, ?)", (n,))
    else:
        n = row["last_no"] + 1
        conn.execute("UPDATE control_counter SET last_no = ? WHERE id = 1", (n,))
    control_no = format_control_no(n)
    used = conn.execute("SELECT 1 FROM invoices WHERE control_no = ?", (control_no,)).fetchone()
    if used:  # only possible if the database was edited by hand
        raise RuntimeError(f"Control number {control_no} is already used")
    return control_no

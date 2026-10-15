"""Double-entry ledger. Every posting is one balanced journal entry in one book.

Posted entries are final: database triggers refuse any change, so a mistake
is fixed with a reversing entry. Each entry stores the hash of the entry
posted before it, so `verify` can show that no entry was changed, removed or
slipped in afterwards (for example by editing the database file by hand).
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..models import PH_TZ
from ..money import ZERO, decimals_of

BOOKS = {
    "SJ": "Sales Journal",
    "PJ": "Purchase Journal",
    "CRJ": "Cash Receipts Journal",
    "CDJ": "Cash Disbursements Journal",
    "GJ": "General Journal",
}
ACCOUNT_TYPES = ["asset", "liability", "equity", "income", "expense"]
GENESIS = "0" * 64  # prev_hash of the very first entry


class BooksError(ValueError):
    pass


@dataclass
class Line:
    account: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: str = ""


def now() -> str:
    return datetime.now(PH_TZ).isoformat(timespec="seconds")


def current_user() -> str:
    """The Windows user running the app (the app has no logins of its own)."""
    try:
        return os.environ.get("USERNAME") or getpass.getuser()
    except Exception:
        return "unknown"


def audit(conn: sqlite3.Connection, action: str, detail: str = "",
          user: str | None = None) -> None:
    conn.execute("INSERT INTO audit_log (at, user, action, detail) VALUES (?,?,?,?)",
                 (now(), user or current_user(), action, detail))


# --- Accounts ----------------------------------------------------------------

def account_for(conn: sqlite3.Connection, role: str) -> str:
    row = conn.execute("SELECT code FROM accounts WHERE role = ?", (role,)).fetchone()
    if row is None:
        raise BooksError(f"No account is set up for '{role}'.")
    return row["code"]


def accounts(conn: sqlite3.Connection, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM accounts"
    if active_only:
        sql += " WHERE active = 1"
    return conn.execute(sql + " ORDER BY code").fetchall()


def cash_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM accounts WHERE is_cash = 1 AND active = 1 ORDER BY code").fetchall()


# --- Closed periods ----------------------------------------------------------

def closed_through(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'closed_through'").fetchone()
    return date.fromisoformat(row["value"]) if row else None


def close_through(conn: sqlite3.Connection, through: date) -> None:
    """Close the books up to and including `through`. Closing never moves back."""
    current = closed_through(conn)
    if current and through < current:
        raise BooksError(f"The books are already closed through {current}; "
                         "a closed period can't be reopened.")
    conn.execute("INSERT INTO settings (key, value) VALUES ('closed_through', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (through.isoformat(),))
    audit(conn, "close books", f"closed through {through}")


def check_open(conn: sqlite3.Connection, d: date) -> None:
    closed = closed_through(conn)
    if closed and d <= closed:
        raise BooksError(f"The books are closed through {closed}. "
                         f"Nothing can be posted on {d}.")


# --- Posting -----------------------------------------------------------------

def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def _hash(prev_hash: str, entry: dict, lines: list[Line]) -> str:
    payload = json.dumps(
        [prev_hash, entry,
         [[l.account, _amount(l.debit), _amount(l.credit), l.memo] for l in lines]],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_lines(conn: sqlite3.Connection, lines: list[Line]) -> None:
    if len(lines) < 2:
        raise BooksError("A journal entry needs at least two lines.")
    for l in lines:
        for v in (l.debit, l.credit):
            if v < 0 or decimals_of(v) > 2:
                raise BooksError("Amounts must be positive with at most 2 decimals.")
        if (l.debit > 0) == (l.credit > 0):
            raise BooksError("Each line must have either a debit or a credit.")
        row = conn.execute("SELECT active FROM accounts WHERE code = ?",
                           (l.account,)).fetchone()
        if row is None:
            raise BooksError(f"Unknown account {l.account}.")
        if not row["active"]:
            raise BooksError(f"Account {l.account} is inactive.")
    debits = sum((l.debit for l in lines), ZERO)
    credits = sum((l.credit for l in lines), ZERO)
    if debits != credits:
        raise BooksError(f"The entry does not balance: debits {debits:,.2f}, "
                         f"credits {credits:,.2f}.")


def post(conn: sqlite3.Connection, *, book: str, entry_date: date, particulars: str,
         lines: list[Line], reference: str = "", party: tuple[str, int | None] = ("", None),
         source: tuple[str, int | None] = ("manual", None), check_period: bool = True,
         user: str | None = None) -> int:
    """Post one balanced entry. Call inside the transaction that saves its source."""
    if not conn.in_transaction:
        raise RuntimeError("post must run inside the source document's transaction")
    if book not in BOOKS:
        raise BooksError(f"Unknown book {book}.")
    particulars = " ".join(particulars.split())
    if not particulars:
        raise BooksError("Particulars are required.")
    lines = [l for l in lines if l.debit or l.credit]
    _check_lines(conn, lines)
    if check_period:
        check_open(conn, entry_date)

    entry_no = conn.execute("SELECT COALESCE(MAX(entry_no), 0) + 1 FROM journal_entries "
                            "WHERE book = ?", (book,)).fetchone()[0]
    last = conn.execute("SELECT hash FROM journal_entries ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else GENESIS
    entry = {
        "book": book, "entry_no": entry_no, "entry_date": entry_date.isoformat(),
        "reference": reference, "particulars": particulars,
        "party_type": party[0], "party_id": party[1],
        "source_type": source[0], "source_id": source[1],
        "created_at": now(), "created_by": user or current_user(),
    }
    digest = _hash(prev_hash, entry, lines)
    cur = conn.execute(
        """INSERT INTO journal_entries (book, entry_no, entry_date, reference, particulars,
               party_type, party_id, source_type, source_id, created_at, created_by,
               prev_hash, hash)
           VALUES (:book, :entry_no, :entry_date, :reference, :particulars, :party_type,
               :party_id, :source_type, :source_id, :created_at, :created_by,
               :prev_hash, :hash)""",
        entry | {"prev_hash": prev_hash, "hash": digest})
    entry_id = cur.lastrowid
    for no, l in enumerate(lines, start=1):
        conn.execute("INSERT INTO journal_lines VALUES (?,?,?,?,?,?)",
                     (entry_id, no, l.account, _amount(l.debit), _amount(l.credit), l.memo))
    audit(conn, "post", f"{book} {entry_no} ({entry_date}): {particulars}", user)
    return entry_id


def entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()


def entry_lines(conn: sqlite3.Connection, entry_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT l.*, a.name AS account_name FROM journal_lines l
           JOIN accounts a ON a.code = l.account_code
           WHERE l.entry_id = ? ORDER BY l.line_no""", (entry_id,)).fetchall()


def entry_for(conn: sqlite3.Connection, source_type: str, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM journal_entries WHERE source_type = ? AND source_id = ?",
                        (source_type, source_id)).fetchone()


def reversal_of(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return entry_for(conn, "reversal", entry_id)


def reverse(conn: sqlite3.Connection, entry_id: int, *, entry_date: date, reason: str,
            check_period: bool = True, user: str | None = None) -> int:
    """Post the mirror image of an entry in the General Journal."""
    original = entry(conn, entry_id)
    if original is None:
        raise BooksError("No such journal entry.")
    if original["source_type"] == "reversal":
        raise BooksError("A reversing entry can't itself be reversed.")
    if reversal_of(conn, entry_id):
        raise BooksError(f"{original['book']} {original['entry_no']} is already reversed.")
    reason = " ".join(reason.split())
    if not reason:
        raise BooksError("A reason is required.")
    lines = [Line(l["account_code"], Decimal(l["credit"]), Decimal(l["debit"]), l["memo"])
             for l in entry_lines(conn, entry_id)]
    return post(conn, book="GJ", entry_date=entry_date,
                particulars=f"Reversal of {original['book']} {original['entry_no']}: {reason}",
                lines=lines, reference=original["reference"],
                party=(original["party_type"], original["party_id"]),
                source=("reversal", entry_id), check_period=check_period, user=user)


# --- Integrity ---------------------------------------------------------------

def verify(conn: sqlite3.Connection) -> list[str]:
    """Problems found in the journal: broken hash chain or unbalanced entries."""
    problems = []
    prev_hash = GENESIS
    for row in conn.execute("SELECT * FROM journal_entries ORDER BY id"):
        name = f"{row['book']} {row['entry_no']}"
        lines = [Line(l["account_code"], Decimal(l["debit"]), Decimal(l["credit"]), l["memo"])
                 for l in conn.execute("SELECT * FROM journal_lines WHERE entry_id = ? "
                                       "ORDER BY line_no", (row["id"],))]
        fields = {k: row[k] for k in ("book", "entry_no", "entry_date", "reference",
                                      "particulars", "party_type", "party_id",
                                      "source_type", "source_id", "created_at",
                                      "created_by")}
        if row["prev_hash"] != prev_hash:
            problems.append(f"{name}: the entry before it was changed or removed.")
        if _hash(row["prev_hash"], fields, lines) != row["hash"]:
            problems.append(f"{name}: its contents were changed after posting.")
        if sum((l.debit for l in lines), ZERO) != sum((l.credit for l in lines), ZERO):
            problems.append(f"{name}: debits and credits do not balance.")
        prev_hash = row["hash"]
    return problems

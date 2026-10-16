"""Save, load, list and void invoices."""
from __future__ import annotations

import secrets
import sqlite3
import string
from datetime import date, datetime
from decimal import Decimal

from ..books import ledger, postings
from ..models import PH_TZ, Invoice, InvoiceLine, today_ph
from . import control_numbers

EIS_KEY_ALPHABET = string.ascii_uppercase + string.digits
EIS_KEY_LENGTH = 8


def new_eis_key() -> str:
    """The 8 random [A-Z0-9] characters that end the EisUniqueId."""
    return "".join(secrets.choice(EIS_KEY_ALPHABET) for _ in range(EIS_KEY_LENGTH))


def _now() -> str:
    return datetime.now(PH_TZ).isoformat(timespec="seconds")


class Repository:
    def __init__(self, conn: sqlite3.Connection, control_no_start: int):
        self.conn = conn
        self.control_no_start = control_no_start

    def next_control_no(self) -> str:
        return control_numbers.peek_next(self.conn, self.control_no_start)

    def issue(self, invoice: Invoice) -> Invoice:
        """Assign the next control number and save. The invoice is final after this."""
        if invoice.status != "draft" or invoice.id is not None:
            raise ValueError("Only a new draft invoice can be issued")
        invoice.compute()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            invoice.control_no = control_numbers.take_next(conn, self.control_no_start)
            invoice.eis_key = self._unique_eis_key(invoice.issue_date)
            invoice.status = "issued"
            invoice.created_at = _now()
            cur = conn.execute(
                """INSERT INTO invoices (control_no, issue_date, status, buyer_name,
                       buyer_tin, buyer_branch, buyer_address, business_style, rate,
                       rate_date, rate_source, total_jpy, total_php, eis_key, created_at,
                       buyer_email, trans_class, withholding_php)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (invoice.control_no, invoice.issue_date.isoformat(), invoice.status,
                 invoice.buyer_name, invoice.buyer_tin, invoice.buyer_branch,
                 invoice.buyer_address, invoice.business_style, str(invoice.rate),
                 invoice.rate_date.isoformat(), invoice.rate_source,
                 str(invoice.total_jpy), str(invoice.total_php), invoice.eis_key,
                 invoice.created_at, invoice.buyer_email, invoice.trans_class,
                 str(invoice.withholding_php)))
            invoice.id = cur.lastrowid
            for no, line in enumerate(invoice.lines, start=1):
                conn.execute(
                    """INSERT INTO invoice_lines VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (invoice.id, no, line.item_name, line.description, _text(line.qty),
                     line.unit, _text(line.unit_cost_jpy), str(line.amount_jpy),
                     str(line.unit_cost_php), str(line.sales_php)))
            invoice.issued_by = ledger.current_user()
            conn.execute("UPDATE invoices SET issued_by = ? WHERE id = ?",
                         (invoice.issued_by, invoice.id))
            ledger.audit(conn, "issue invoice",
                         f"{invoice.buyer_name}, {invoice.sale_type}, "
                         f"PHP {invoice.total_due_php:,.2f} (JPY {invoice.total_due_jpy:,.2f} "
                         f"at {invoice.rate})"
                         + (f", withholding tax PHP {invoice.withholding_php:,.2f}"
                            if invoice.withholding_php else ""), entity="invoice", entity_id=invoice.control_no)
            postings.post_invoice(conn, invoice)  # Sales Journal
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            invoice.control_no, invoice.eis_key, invoice.status, invoice.id = "", "", "draft", None
            raise
        return invoice

    def _unique_eis_key(self, issue_date: date) -> str:
        while True:
            key = new_eis_key()
            clash = self.conn.execute(
                "SELECT 1 FROM invoices WHERE issue_date = ? AND eis_key = ?",
                (issue_date.isoformat(), key)).fetchone()
            if not clash:
                return key

    def void(self, control_no: str, reason: str) -> Invoice:
        reason = " ".join(reason.split())
        if not reason:
            raise ValueError("A reason is required to void an invoice.")
        exported = self.conn.execute(
            """SELECT 1 FROM eis_exports e JOIN invoices i ON i.id = e.invoice_id
               WHERE i.control_no = ?""", (control_no,)).fetchone()
        if exported:
            # Once an e-invoice exists, BIR expects a correction document
            # (CorrYN "Y", e.g. CorrectionCd 04 Cancellation), not a silent void.
            raise ValueError(f"Invoice {control_no} has already been exported to EIS "
                             "JSON and can't simply be voided. Corrections (credit "
                             "memos / cancellations) are not built yet.")
        with postings.transaction(self.conn):
            cur = self.conn.execute(
                """UPDATE invoices SET status = 'void', void_reason = ?, voided_at = ?,
                       voided_by = ?
                   WHERE control_no = ? AND status = 'issued'""",
                (reason, _now(), ledger.current_user(), control_no))
            if cur.rowcount != 1:
                raise ValueError(f"Invoice {control_no} is not an issued invoice.")
            invoice_id = self.conn.execute("SELECT id FROM invoices WHERE control_no = ?",
                                           (control_no,)).fetchone()["id"]
            ledger.audit(self.conn, "void invoice", reason, entity="invoice",
                         entity_id=control_no, changes={"status": ["issued", "void"]})
            postings.post_invoice_void(self.conn, invoice_id, reason, on=today_ph())
        return self.get(control_no)

    def get(self, control_no: str) -> Invoice | None:
        row = self.conn.execute(
            "SELECT * FROM invoices WHERE control_no = ?", (control_no,)).fetchone()
        if row is None:
            return None
        lines = self.conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
            (row["id"],)).fetchall()
        return _invoice_from_rows(row, lines)

    def list(self) -> list[Invoice]:
        """All invoices, newest control number first (lines not loaded)."""
        rows = self.conn.execute(
            "SELECT * FROM invoices ORDER BY control_no DESC").fetchall()
        return [_invoice_from_rows(r, []) for r in rows]


def _text(value: Decimal | None) -> str:
    """Blank qty / unit price are stored as ''."""
    return "" if value is None else str(value)


def _invoice_from_rows(row: sqlite3.Row, line_rows: list[sqlite3.Row]) -> Invoice:
    lines = [
        InvoiceLine(
            item_name=l["item_name"], description=l["description"],
            qty=Decimal(l["qty"]) if l["qty"] else None, unit=l["unit"],
            unit_cost_jpy=Decimal(l["unit_cost_jpy"]) if l["unit_cost_jpy"] else None,
            # No unit price but an amount: the amount was typed directly.
            amount_entered=(Decimal(l["amount_jpy"])
                            if not l["unit_cost_jpy"] and Decimal(l["amount_jpy"]) else None),
            amount_jpy=Decimal(l["amount_jpy"]),
            unit_cost_php=Decimal(l["unit_cost_php"]),
            sales_php=Decimal(l["sales_php"]),
        )
        for l in line_rows
    ]
    return Invoice(
        id=row["id"], control_no=row["control_no"],
        issue_date=date.fromisoformat(row["issue_date"]), status=row["status"],
        buyer_name=row["buyer_name"], buyer_tin=row["buyer_tin"],
        buyer_branch=row["buyer_branch"], buyer_address=row["buyer_address"],
        business_style=row["business_style"], buyer_email=row["buyer_email"],
        trans_class=row["trans_class"],
        withholding_php=Decimal(row["withholding_php"]),
        rate=Decimal(row["rate"]),
        rate_date=date.fromisoformat(row["rate_date"]),
        rate_source=row["rate_source"], lines=lines,
        total_jpy=Decimal(row["total_jpy"]), total_php=Decimal(row["total_php"]),
        eis_key=row["eis_key"], void_reason=row["void_reason"],
        created_at=row["created_at"], voided_at=row["voided_at"],
        issued_by=row["issued_by"], voided_by=row["voided_by"],
    )

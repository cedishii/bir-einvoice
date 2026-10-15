"""Source documents and the journal entries they post automatically.

  Service invoice issued   SJ   Dr Accounts Receivable / Cr Service Income, Output VAT
  Invoice voided           GJ   reversal of the SJ entry
  Collection from client   CRJ  Dr Cash, Creditable Withholding Tax, FX loss
                                / Cr Accounts Receivable, FX gain
  Supplier bill            PJ   Dr expense (or asset), Input VAT / Cr Accounts Payable
  Payment to supplier      CDJ  Dr Accounts Payable / Cr Cash, Withholding Tax Payable
  Manual entry             CRJ / CDJ when it moves cash in / out, otherwise GJ

A cancelled collection, bill or payment keeps its row (status "cancelled")
and its entry is reversed in the General Journal.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from ..models import Invoice, today_ph
from ..money import ZERO
from . import ledger
from .ledger import BooksError, Line

PURCHASE_CATEGORIES = {
    "services": "Services (VATable)",
    "capital": "Capital goods (VATable)",
    "goods": "Goods other than capital goods (VATable)",
    "zero": "Zero-rated",
    "exempt": "VAT-exempt / non-VAT supplier",
}
VATABLE_PURCHASES = ("services", "capital", "goods")


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _not_future(d: date, label: str) -> None:
    if d > today_ph():
        raise BooksError(f"{label} cannot be in the future.")


def _clean(text: str) -> str:
    return " ".join((text or "").split())


# --- Customers and suppliers -------------------------------------------------

def find_or_create_customer(conn: sqlite3.Connection, *, name: str, tin: str, branch: str,
                            address: str, business_style: str = "", email: str = "") -> int:
    """Customers are kept from the invoices: same name + TIN + branch = same customer.
    The address, business style and e-mail follow the latest invoice."""
    row = conn.execute("SELECT * FROM customers WHERE name = ? AND tin = ? AND branch = ?",
                       (name, tin, branch)).fetchone()
    after = {"address": address, "business_style": business_style, "email": email}
    if row:
        changes = ledger.changed(dict(row), after)
        if changes:
            conn.execute("UPDATE customers SET address = ?, business_style = ?, email = ? "
                         "WHERE id = ?", (address, business_style, email, row["id"]))
            ledger.audit(conn, "change client", f"{name} (from an invoice)",
                         entity="customer", entity_id=row["id"], changes=changes)
        return row["id"]
    cid = conn.execute(
        """INSERT INTO customers (name, tin, branch, address, business_style, email, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name, tin, branch, address, business_style, email, ledger.now())).lastrowid
    ledger.audit(conn, "add client", f"{name} (TIN {tin}-{branch})", entity="customer",
                 entity_id=cid)
    return cid


def create_supplier(conn: sqlite3.Connection, *, name: str, tin: str, branch: str,
                    address: str) -> int:
    name, address = _clean(name), _clean(address)
    if not name or not address:
        raise BooksError("Supplier name and address are required.")
    if conn.execute("SELECT 1 FROM suppliers WHERE name = ? AND tin = ? AND branch = ?",
                    (name, tin, branch)).fetchone():
        raise BooksError(f"Supplier {name} with this TIN is already on file.")
    with transaction(conn):
        sid = conn.execute(
            "INSERT INTO suppliers (name, tin, branch, address, created_at, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (name, tin, branch, address, ledger.now(), ledger.current_user())).lastrowid
        ledger.audit(conn, "add supplier", f"{name} (TIN {tin}-{branch})",
                     entity="supplier", entity_id=sid)
    return sid


def update_supplier(conn: sqlite3.Connection, supplier_id: int, *, name: str, tin: str,
                    branch: str, address: str) -> None:
    """Correct a supplier's details. The change (before and after) is audited;
    posted entries keep pointing at the same supplier."""
    s = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if s is None:
        raise BooksError("No such supplier.")
    after = {"name": _clean(name), "tin": tin, "branch": branch, "address": _clean(address)}
    if not after["name"] or not after["address"]:
        raise BooksError("Supplier name and address are required.")
    if conn.execute("SELECT 1 FROM suppliers WHERE name = ? AND tin = ? AND branch = ? AND id != ?",
                    (after["name"], tin, branch, supplier_id)).fetchone():
        raise BooksError("Another supplier with this name and TIN is already on file.")
    changes = ledger.changed(dict(s), after)
    if not changes:
        return
    with transaction(conn):
        conn.execute("UPDATE suppliers SET name = ?, tin = ?, branch = ?, address = ? WHERE id = ?",
                     (after["name"], tin, branch, after["address"], supplier_id))
        ledger.audit(conn, "change supplier", after["name"], entity="supplier",
                     entity_id=supplier_id, changes=changes)


# --- Sales invoices ----------------------------------------------------------

def post_invoice(conn: sqlite3.Connection, inv: Invoice, *, check_period: bool = True,
                 user: str | None = None) -> int:
    """Sales Journal entry for an issued invoice. Runs in the invoice's transaction."""
    cid = find_or_create_customer(
        conn, name=inv.buyer_name, tin=inv.buyer_tin, branch=inv.buyer_branch,
        address=inv.buyer_address, business_style=inv.business_style, email=inv.buyer_email)
    conn.execute("UPDATE invoices SET customer_id = ? WHERE id = ?", (cid, inv.id))
    lines = [
        Line(ledger.account_for(conn, "ar"), debit=inv.total_due_php),
        Line(ledger.account_for(conn, "sales"), credit=inv.total_php, memo=inv.sale_type),
    ]
    if inv.vat_php:
        lines.append(Line(ledger.account_for(conn, "output_vat"), credit=inv.vat_php))
    return ledger.post(conn, book="SJ", entry_date=inv.issue_date,
                       particulars=f"{inv.sale_type} sale to {inv.buyer_name}",
                       lines=lines, reference=f"SI {inv.control_no}",
                       party=("customer", cid), source=("invoice", inv.id),
                       check_period=check_period, user=user)


def post_invoice_void(conn: sqlite3.Connection, invoice_id: int, reason: str, *,
                      on: date | None = None, check_period: bool = True,
                      user: str | None = None) -> int:
    if collections_for_invoice(conn, invoice_id):
        raise BooksError("This invoice has collections recorded against it. "
                         "Cancel those first.")
    e = ledger.entry_for(conn, "invoice", invoice_id)
    return ledger.reverse(conn, e["id"], entry_date=on or today_ph(),
                          reason=f"invoice voided ({reason})",
                          check_period=check_period, user=user)


def sync(conn: sqlite3.Connection) -> None:
    """Post invoices issued before the books existed (once, on upgrade)."""
    todo = conn.execute(
        """SELECT i.control_no FROM invoices i
           LEFT JOIN journal_entries e ON e.source_type = 'invoice' AND e.source_id = i.id
           WHERE e.id IS NULL ORDER BY i.control_no""").fetchall()
    if not todo:
        return
    from ..storage.repository import Repository  # avoid an import cycle
    repo = Repository(conn, 0)
    with transaction(conn):
        for row in todo:
            inv = repo.get(row["control_no"])
            post_invoice(conn, inv, check_period=False, user="system (books upgrade)")
            if inv.status == "void":
                post_invoice_void(conn, inv.id, inv.void_reason or "voided",
                                  on=date.fromisoformat(inv.voided_at[:10]),
                                  check_period=False, user="system (books upgrade)")
        ledger.audit(conn, "books upgrade",
                     f"posted {len(todo)} invoice(s) issued before the books existed",
                     user="system (books upgrade)")


# --- Balances ----------------------------------------------------------------

def _sum(rows, key) -> Decimal:
    return sum((Decimal(r[key]) for r in rows), ZERO)


def collections_for_invoice(conn, invoice_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM collections WHERE invoice_id = ? AND status = 'posted'",
                        (invoice_id,)).fetchall()


def payments_for_bill(conn, bill_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM payments WHERE bill_id = ? AND status = 'posted'",
                        (bill_id,)).fetchall()


def invoice_receivable(conn, invoice_id: int) -> Decimal:
    """What the invoice put in Accounts Receivable (0 if voided)."""
    e = ledger.entry_for(conn, "invoice", invoice_id)
    if e is None or ledger.reversal_of(conn, e["id"]):
        return ZERO
    ar = ledger.account_for(conn, "ar")
    rows = conn.execute("SELECT debit FROM journal_lines WHERE entry_id = ? AND account_code = ?",
                        (e["id"], ar)).fetchall()
    return _sum(rows, "debit")


def invoice_balance(conn, invoice_id: int) -> Decimal:
    return invoice_receivable(conn, invoice_id) - _sum(
        collections_for_invoice(conn, invoice_id), "applied")


def bill_balance(conn, bill_id: int) -> Decimal:
    bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if bill is None or bill["status"] != "posted":
        return ZERO
    return Decimal(bill["total"]) - _sum(payments_for_bill(conn, bill_id), "amount")


def open_invoices(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT i.id, i.control_no, i.issue_date, i.customer_id, i.buyer_name, i.total_php,
                  i.trans_class FROM invoices i
           WHERE i.status = 'issued' ORDER BY i.control_no""").fetchall()
    out = []
    for r in rows:
        bal = invoice_balance(conn, r["id"])
        if bal > 0:
            net = Decimal(r["total_php"])
            out.append(dict(r) | {"balance": bal, "net_php": net})
    return out


def open_bills(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT b.*, s.name AS supplier_name FROM bills b JOIN suppliers s ON s.id = b.supplier_id
           WHERE b.status = 'posted' ORDER BY b.bill_date, b.id""").fetchall()
    out = []
    for r in rows:
        bal = bill_balance(conn, r["id"])
        if bal > 0:
            out.append(dict(r) | {"balance": bal})
    return out


# --- Collections (CRJ) -------------------------------------------------------

def create_collection(conn: sqlite3.Connection, *, customer_id: int, coll_date: date,
                      reference: str, applied: Decimal, cash: Decimal, cash_account: str,
                      invoice_id: int | None = None, cwt: Decimal = ZERO, atc: str = "",
                      cwt_base: Decimal = ZERO, cwt_rate: Decimal = ZERO) -> int:
    """Money received from a client. `applied` is what comes off Accounts
    Receivable (PHP). Any difference from cash + tax withheld is a foreign
    exchange gain or loss (the client pays JPY; the receivable was booked at
    the invoice's rate)."""
    reference, atc = _clean(reference), _clean(atc).upper()
    _not_future(coll_date, "Collection date")
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        raise BooksError("Choose the client.")
    if not reference:
        raise BooksError("Reference (official receipt / bank reference) is required.")
    if applied <= 0:
        raise BooksError("The amount applied to the receivable must be greater than zero.")
    if cash < 0 or cwt < 0:
        raise BooksError("Amounts can't be negative.")
    if cwt > 0 and not atc:
        raise BooksError("Enter the ATC shown on the client's Form 2307.")
    _cash_account(conn, cash_account)
    if invoice_id is not None:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if inv is None or inv["customer_id"] != customer_id or inv["status"] != "issued":
            raise BooksError("That invoice is not an open invoice of this client.")
        balance = invoice_balance(conn, invoice_id)
        if applied > balance:
            raise BooksError(f"Invoice {inv['control_no']} has only {balance:,.2f} left to collect.")
    fx = applied - cash - cwt
    lines = [Line(cash_account, debit=cash, memo="cash received"),
             Line(ledger.account_for(conn, "cwt"), debit=cwt, memo=atc),
             Line(ledger.account_for(conn, "ar"), credit=applied)]
    if fx > 0:
        lines.append(Line(ledger.account_for(conn, "fx_loss"), debit=fx))
    elif fx < 0:
        lines.append(Line(ledger.account_for(conn, "fx_gain"), credit=-fx))
    with transaction(conn):
        cid = conn.execute(
            """INSERT INTO collections (customer_id, invoice_id, coll_date, reference, applied,
                   cash, cwt, atc, cwt_base, cwt_rate, cash_account, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (customer_id, invoice_id, coll_date.isoformat(), reference, str(applied),
             str(cash), str(cwt), atc, str(cwt_base), str(cwt_rate), cash_account,
             ledger.now(), ledger.current_user())).lastrowid
        what = f"SI {inv['control_no']}" if invoice_id else "on account"
        ledger.audit(conn, "add collection",
                     f"{reference}: {customer['name']} ({what}), applied {applied:,.2f}, "
                     f"cash {cash:,.2f}, tax withheld {cwt:,.2f}",
                     entity="collection", entity_id=cid)
        ledger.post(conn, book="CRJ", entry_date=coll_date,
                    particulars=f"Collection from {customer['name']} ({what})",
                    lines=lines, reference=reference, party=("customer", customer_id),
                    source=("collection", cid))
    return cid


# --- Supplier bills (PJ) and payments (CDJ) ---------------------------------

def create_bill(conn: sqlite3.Connection, *, supplier_id: int, bill_date: date,
                supplier_ref: str, description: str, account_code: str, category: str,
                net: Decimal, vat: Decimal) -> int:
    supplier_ref, description = _clean(supplier_ref), _clean(description)
    _not_future(bill_date, "Bill date")
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if supplier is None:
        raise BooksError("Choose the supplier.")
    if not supplier_ref:
        raise BooksError("The supplier's invoice / receipt number is required.")
    if not description:
        raise BooksError("Description is required.")
    if category not in PURCHASE_CATEGORIES:
        raise BooksError("Choose the purchase type.")
    if net <= 0:
        raise BooksError("The amount (net of VAT) must be greater than zero.")
    if vat < 0:
        raise BooksError("VAT can't be negative.")
    if vat and category not in VATABLE_PURCHASES:
        raise BooksError("Zero-rated and VAT-exempt purchases have no input VAT.")
    acct = conn.execute("SELECT * FROM accounts WHERE code = ? AND active = 1",
                        (account_code,)).fetchone()
    if acct is None or acct["role"] or acct["is_cash"] or acct["type"] not in ("asset", "expense"):
        raise BooksError("Choose an expense or asset account to charge.")
    total = net + vat
    with transaction(conn):
        bid = conn.execute(
            """INSERT INTO bills (supplier_id, bill_date, supplier_ref, description,
                   account_code, category, net, vat, total, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_id, bill_date.isoformat(), supplier_ref, description, account_code,
             category, str(net), str(vat), str(total), ledger.now(),
             ledger.current_user())).lastrowid
        ledger.audit(conn, "add bill",
                     f"{supplier['name']} {supplier_ref}: {description}, net {net:,.2f}, "
                     f"VAT {vat:,.2f}", entity="bill", entity_id=bid)
        ledger.post(conn, book="PJ", entry_date=bill_date,
                    particulars=f"{description} - {supplier['name']}",
                    lines=[Line(account_code, debit=net),
                           Line(ledger.account_for(conn, "input_vat"), debit=vat),
                           Line(ledger.account_for(conn, "ap"), credit=total)],
                    reference=supplier_ref, party=("supplier", supplier_id),
                    source=("bill", bid))
    return bid


def create_payment(conn: sqlite3.Connection, *, supplier_id: int, pay_date: date,
                   reference: str, amount: Decimal, cash_account: str,
                   bill_id: int | None = None, ewt: Decimal = ZERO, atc: str = "",
                   ewt_base: Decimal = ZERO, ewt_rate: Decimal = ZERO) -> int:
    """Payment of Accounts Payable. Tax withheld (`ewt`) stays with Micronet
    as Withholding Tax Payable, so the supplier is paid amount - ewt."""
    reference, atc = _clean(reference), _clean(atc).upper()
    _not_future(pay_date, "Payment date")
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if supplier is None:
        raise BooksError("Choose the supplier.")
    if not reference:
        raise BooksError("Reference (check / transfer number) is required.")
    if amount <= 0:
        raise BooksError("The amount paid must be greater than zero.")
    if ewt < 0 or ewt >= amount:
        raise BooksError("Tax withheld must be zero or less than the amount.")
    if ewt > 0 and (not atc or ewt_base <= 0):
        raise BooksError("Enter the ATC and the income payment the tax was withheld on "
                         "(both go on Form 2307).")
    _cash_account(conn, cash_account)
    if bill_id is not None:
        bill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if bill is None or bill["supplier_id"] != supplier_id or bill["status"] != "posted":
            raise BooksError("That bill is not an open bill of this supplier.")
        balance = bill_balance(conn, bill_id)
        if amount > balance:
            raise BooksError(f"Bill {bill['supplier_ref']} has only {balance:,.2f} left to pay.")
    with transaction(conn):
        pid = conn.execute(
            """INSERT INTO payments (supplier_id, bill_id, pay_date, reference, amount, atc,
                   ewt_base, ewt_rate, ewt, cash_account, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (supplier_id, bill_id, pay_date.isoformat(), reference, str(amount), atc,
             str(ewt_base), str(ewt_rate), str(ewt), cash_account, ledger.now(),
             ledger.current_user())).lastrowid
        what = f"bill {bill['supplier_ref']}" if bill_id else "on account"
        ledger.audit(conn, "add payment",
                     f"{reference}: {supplier['name']} ({what}), {amount:,.2f}, "
                     f"tax withheld {ewt:,.2f}", entity="payment", entity_id=pid)
        ledger.post(conn, book="CDJ", entry_date=pay_date,
                    particulars=f"Payment to {supplier['name']} ({what})",
                    lines=[Line(ledger.account_for(conn, "ap"), debit=amount),
                           Line(cash_account, credit=amount - ewt, memo="cash paid"),
                           Line(ledger.account_for(conn, "ewt_payable"), credit=ewt, memo=atc)],
                    reference=reference, party=("supplier", supplier_id),
                    source=("payment", pid))
    return pid


def _cash_account(conn, code: str) -> None:
    if not conn.execute("SELECT 1 FROM accounts WHERE code = ? AND is_cash = 1 AND active = 1",
                        (code,)).fetchone():
        raise BooksError("Choose a cash or bank account.")


# --- Cancelling --------------------------------------------------------------

_DOCS = {"collection": "collections", "bill": "bills", "payment": "payments"}


def cancel(conn: sqlite3.Connection, kind: str, doc_id: int, reason: str) -> int:
    """Cancel a collection, bill or payment: mark it and reverse its entry."""
    table = _DOCS[kind]
    reason = _clean(reason)
    if not reason:
        raise BooksError("A reason is required.")
    doc = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (doc_id,)).fetchone()
    if doc is None or doc["status"] != "posted":
        raise BooksError(f"That {kind} is not posted.")
    if kind == "bill" and payments_for_bill(conn, doc_id):
        raise BooksError("This bill has payments recorded against it. Cancel those first.")
    with transaction(conn):
        conn.execute(f"UPDATE {table} SET status = 'cancelled', cancel_reason = ?, "
                     "cancelled_by = ?, cancelled_at = ? WHERE id = ?",
                     (reason, ledger.current_user(), ledger.now(), doc_id))
        ledger.audit(conn, f"cancel {kind}", reason, entity=kind, entity_id=doc_id,
                     changes={"status": ["posted", "cancelled"]})
        e = ledger.entry_for(conn, kind, doc_id)
        return ledger.reverse(conn, e["id"], entry_date=today_ph(),
                              reason=f"{kind} cancelled ({reason})")


# --- Manual entries ----------------------------------------------------------

def book_for(conn: sqlite3.Connection, lines: list[Line]) -> str:
    """Cash coming in -> Cash Receipts, going out -> Cash Disbursements, else General."""
    cash = {r["code"] for r in conn.execute("SELECT code FROM accounts WHERE is_cash = 1")}
    net = sum((l.debit - l.credit for l in lines if l.account in cash), ZERO)
    return "CRJ" if net > 0 else "CDJ" if net < 0 else "GJ"


def create_manual_entry(conn: sqlite3.Connection, *, entry_date: date, reference: str,
                        particulars: str, lines: list[Line],
                        party: tuple[str, int | None] = ("", None),
                        opening: bool = False) -> int:
    """`opening`: balances carried over from the manual books, always General Journal."""
    _not_future(entry_date, "Entry date")
    with transaction(conn):
        return ledger.post(conn, book="GJ" if opening else book_for(conn, lines),
                           entry_date=entry_date, particulars=particulars, lines=lines,
                           reference=_clean(reference), party=party,
                           source=("opening" if opening else "manual", None))


def reverse_manual(conn: sqlite3.Connection, entry_id: int, reason: str) -> int:
    e = ledger.entry(conn, entry_id)
    if e is None or e["source_type"] not in ("manual", "opening"):
        raise BooksError("Only manual entries are reversed here. Invoices, collections, "
                         "bills and payments are voided or cancelled from their own page.")
    with transaction(conn):
        return ledger.reverse(conn, entry_id, entry_date=today_ph(), reason=reason)

"""Books and BIR reports. Each report is one `Report` (a titled table), shown
on screen, printed to PDF and saved as CSV from the same rows, so the three
always agree.

Amounts are in PHP. Journals list entries in date order; entry numbers run
per book in posting order.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .. import money
from ..models import NO_TIN, VAT_EXEMPT, VATABLE, ZERO_RATED
from ..money import ZERO
from . import ledger
from .postings import PURCHASE_CATEGORIES, VATABLE_PURCHASES

NORMAL_DEBIT = ("asset", "expense")


@dataclass
class Column:
    label: str
    align: str = "left"     # "left" or "num"
    width: float = 1.0      # relative width in the PDF


@dataclass
class Row:
    cells: list             # str, Decimal or None (blank)
    kind: str = ""          # "", "group" (one heading cell), "total", "sub"


@dataclass
class Report:
    key: str
    title: str
    start: date | None
    end: date
    columns: list[Column]
    rows: list[Row] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def period(self) -> str:
        if self.start is None:
            return f"As of {self.end:%B %d, %Y}"
        return f"{self.start:%B %d, %Y} to {self.end:%B %d, %Y}"


def tin_text(tin: str, branch: str) -> str:
    if tin == NO_TIN:
        return ""
    return f"{tin[0:3]}-{tin[3:6]}-{tin[6:9]}-{branch}"


def quarter_of(d: date) -> tuple[date, date]:
    q_start = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    return q_start, _month_end(q_start, 2)


def _month_end(start: date, plus_months: int) -> date:
    m = start.month - 1 + plus_months + 1
    first_next = date(start.year + m // 12, m % 12 + 1, 1)
    return first_next - timedelta(days=1)


def party_names(conn) -> dict[tuple[str, int], str]:
    names = {("customer", r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM customers")}
    names |= {("supplier", r["id"]): r["name"] for r in conn.execute("SELECT id, name FROM suppliers")}
    return names


# --- Journals ----------------------------------------------------------------

def journal(conn: sqlite3.Connection, book: str, start: date, end: date) -> Report:
    if book not in ledger.BOOKS:
        raise ledger.BooksError(f"Unknown book {book}.")
    r = Report(f"journal-{book}", ledger.BOOKS[book], start, end, [
        Column("Date", width=0.9), Column("No.", width=0.5), Column("Reference", width=1.0),
        Column("Particulars", width=2.6), Column("Account", width=2.0),
        Column("Debit", "num", 1.1), Column("Credit", "num", 1.1)])
    names = party_names(conn)
    entries = conn.execute(
        """SELECT * FROM journal_entries WHERE book = ? AND entry_date BETWEEN ? AND ?
           ORDER BY entry_date, entry_no""", (book, start.isoformat(), end.isoformat())).fetchall()
    tot_d = tot_c = ZERO
    for e in entries:
        party = names.get((e["party_type"], e["party_id"]), "")
        particulars = e["particulars"]
        if party and party not in particulars:
            particulars += f" ({party})"
        for i, l in enumerate(ledger.entry_lines(conn, e["id"])):
            d, c = Decimal(l["debit"]), Decimal(l["credit"])
            tot_d, tot_c = tot_d + d, tot_c + c
            account = f"{l['account_code']} {l['account_name']}"
            if l["memo"]:
                account += f" - {l['memo']}"
            first = i == 0
            r.rows.append(Row([e["entry_date"] if first else "",
                               f"{book}-{e['entry_no']}" if first else "",
                               e["reference"] if first else "",
                               particulars if first else "",
                               account, d or None, c or None]))
    r.rows.append(Row(["", "", "", f"Total ({len(entries)} entries)", "", tot_d, tot_c], "total"))
    return r


# --- Ledgers -----------------------------------------------------------------

def _balance_cells(balance: Decimal) -> list:
    return [balance if balance >= 0 else None, -balance if balance < 0 else None]


def general_ledger(conn: sqlite3.Connection, start: date, end: date,
                   account: str | None = None) -> Report:
    r = Report("gl", "General Ledger", start, end, [
        Column("Date", width=0.9), Column("Book / No.", width=0.8), Column("Reference", width=1.0),
        Column("Particulars", width=3.0), Column("Debit", "num", 1.1),
        Column("Credit", "num", 1.1), Column("Balance Dr", "num", 1.1),
        Column("Balance Cr", "num", 1.1)])
    accts = [a for a in ledger.accounts(conn) if account in (None, a["code"])]
    for a in accts:
        opening = _net(conn, a["code"], before=start)
        lines = conn.execute(
            """SELECT e.*, l.debit, l.credit FROM journal_lines l
               JOIN journal_entries e ON e.id = l.entry_id
               WHERE l.account_code = ? AND e.entry_date BETWEEN ? AND ?
               ORDER BY e.entry_date, e.id, l.line_no""",
            (a["code"], start.isoformat(), end.isoformat())).fetchall()
        if not lines and not opening and account is None:
            continue  # no activity: left out of the full ledger
        r.rows.append(Row([f"{a['code']} {a['name']} ({a['type']})"], "group"))
        r.rows.append(Row(["", "", "", "Balance forward", None, None] + _balance_cells(opening), "sub"))
        bal, tot_d, tot_c = opening, ZERO, ZERO
        for l in lines:
            d, c = Decimal(l["debit"]), Decimal(l["credit"])
            bal += d - c
            tot_d, tot_c = tot_d + d, tot_c + c
            r.rows.append(Row([l["entry_date"], f"{l['book']}-{l['entry_no']}", l["reference"],
                               l["particulars"], d or None, c or None] + _balance_cells(bal)))
        r.rows.append(Row(["", "", "", f"Ending balance {a['code']}", tot_d, tot_c]
                          + _balance_cells(bal), "total"))
    return r


def _net(conn, code: str, *, before: date | None = None, through: date | None = None,
         party: tuple[str, int] | None = None) -> Decimal:
    """Debits minus credits on an account."""
    sql = ["SELECT l.debit, l.credit FROM journal_lines l "
           "JOIN journal_entries e ON e.id = l.entry_id WHERE l.account_code = ?"]
    args: list = [code]
    if before:
        sql.append("AND e.entry_date < ?")
        args.append(before.isoformat())
    if through:
        sql.append("AND e.entry_date <= ?")
        args.append(through.isoformat())
    if party:
        sql.append("AND e.party_type = ? AND e.party_id = ?")
        args += list(party)
    rows = conn.execute(" ".join(sql), args).fetchall()
    return sum((Decimal(x["debit"]) - Decimal(x["credit"]) for x in rows), ZERO)


def subsidiary_ledger(conn: sqlite3.Connection, kind: str, start: date, end: date,
                      party_id: int | None = None) -> Report:
    """Accounts Receivable per client (kind "customer") or Accounts Payable per
    supplier (kind "supplier")."""
    role, table, title = {"customer": ("ar", "customers", "Accounts Receivable Subsidiary Ledger"),
                          "supplier": ("ap", "suppliers", "Accounts Payable Subsidiary Ledger")}[kind]
    code = ledger.account_for(conn, role)
    r = Report(f"sl-{kind}", title, start, end, [
        Column("Date", width=0.9), Column("Book / No.", width=0.8), Column("Reference", width=1.1),
        Column("Particulars", width=3.0), Column("Debit", "num", 1.1),
        Column("Credit", "num", 1.1), Column("Balance", "num", 1.2)])
    sign = 1 if role == "ar" else -1  # AR: debit balance; AP: credit balance
    parties = conn.execute(f"SELECT * FROM {table} ORDER BY name").fetchall()
    for p in parties:
        if party_id not in (None, p["id"]):
            continue
        key = (kind, p["id"])
        opening = sign * _net(conn, code, before=start, party=key)
        lines = conn.execute(
            """SELECT e.*, l.debit, l.credit FROM journal_lines l
               JOIN journal_entries e ON e.id = l.entry_id
               WHERE l.account_code = ? AND e.party_type = ? AND e.party_id = ?
                 AND e.entry_date BETWEEN ? AND ?
               ORDER BY e.entry_date, e.id, l.line_no""",
            (code, kind, p["id"], start.isoformat(), end.isoformat())).fetchall()
        if not lines and not opening and party_id is None:
            continue
        tin = tin_text(p["tin"], p["branch"])
        r.rows.append(Row([f"{p['name']}" + (f"  TIN {tin}" if tin else "")], "group"))
        r.rows.append(Row(["", "", "", "Balance forward", None, None, opening], "sub"))
        bal = opening
        for l in lines:
            d, c = Decimal(l["debit"]), Decimal(l["credit"])
            bal += sign * (d - c)
            r.rows.append(Row([l["entry_date"], f"{l['book']}-{l['entry_no']}", l["reference"],
                               l["particulars"], d or None, c or None, bal]))
        r.rows.append(Row(["", "", "", "Ending balance", None, None, bal], "total"))
    return r


def trial_balance(conn: sqlite3.Connection, as_of: date) -> Report:
    r = Report("tb", "Trial Balance", None, as_of, [
        Column("Code", width=0.6), Column("Account", width=3.0), Column("Type", width=0.9),
        Column("Debit", "num", 1.2), Column("Credit", "num", 1.2)])
    tot_d = tot_c = ZERO
    for a in ledger.accounts(conn):
        net = _net(conn, a["code"], through=as_of)
        if not net:
            continue
        d, c = (net, None) if net > 0 else (None, -net)
        tot_d += d or ZERO
        tot_c += c or ZERO
        r.rows.append(Row([a["code"], a["name"], a["type"], d, c]))
    r.rows.append(Row(["", "Total", "", tot_d, tot_c], "total"))
    if tot_d != tot_c:
        r.notes.append("The trial balance does not balance. Run the integrity check.")
    return r


# --- BIR summary lists -------------------------------------------------------

def sls(conn: sqlite3.Connection, start: date, end: date) -> Report:
    """Summary List of Sales: issued (not voided) invoices per client."""
    r = Report("sls", "Summary List of Sales", start, end, [
        Column("TIN", width=1.2), Column("Registered name", width=2.4),
        Column("Address", width=2.4), Column("Exempt sales", "num", 1.1),
        Column("Zero-rated sales", "num", 1.1), Column("Taxable sales", "num", 1.1),
        Column("Output tax", "num", 1.0), Column("Gross taxable sales", "num", 1.1)])
    rows = conn.execute(
        """SELECT c.id, c.name, c.tin, c.branch, c.address, i.trans_class, i.total_php
           FROM invoices i JOIN customers c ON c.id = i.customer_id
           WHERE i.status = 'issued' AND i.issue_date BETWEEN ? AND ?
           ORDER BY c.name""", (start.isoformat(), end.isoformat())).fetchall()
    per: dict[int, dict] = {}
    for x in rows:
        p = per.setdefault(x["id"], {"row": x, VAT_EXEMPT: ZERO, ZERO_RATED: ZERO,
                                     VATABLE: ZERO, "vat": ZERO})
        net = Decimal(x["total_php"])
        p[x["trans_class"]] += net
        if x["trans_class"] == VATABLE:
            p["vat"] += money.vat_of(net)  # VAT is worked out per invoice
    totals = [ZERO] * 5
    for p in per.values():
        x = p["row"]
        nums = [p[VAT_EXEMPT], p[ZERO_RATED], p[VATABLE], p["vat"], p[VATABLE] + p["vat"]]
        totals = [a + b for a, b in zip(totals, nums)]
        r.rows.append(Row([tin_text(x["tin"], x["branch"]), x["name"], x["address"]] + nums))
    r.rows.append(Row(["", "Total", ""] + totals, "total"))
    r.notes.append("Voided invoices are left out. Clients without a Philippine TIN "
                   "(foreign clients) are listed with a blank TIN.")
    return r


def slp(conn: sqlite3.Connection, start: date, end: date) -> Report:
    """Summary List of Purchases: posted supplier bills per supplier."""
    r = Report("slp", "Summary List of Purchases", start, end, [
        Column("TIN", width=1.1), Column("Registered name", width=2.0),
        Column("Address", width=1.8), Column("Gross purchase", "num", 1.0),
        Column("Exempt", "num", 0.9), Column("Zero-rated", "num", 0.9),
        Column("Taxable (net)", "num", 1.0), Column("Services", "num", 0.9),
        Column("Capital goods", "num", 0.9), Column("Other goods", "num", 0.9),
        Column("Input tax", "num", 0.9), Column("Gross taxable", "num", 1.0)])
    rows = conn.execute(
        """SELECT s.id, s.name, s.tin, s.branch, s.address, b.category, b.net, b.vat
           FROM bills b JOIN suppliers s ON s.id = b.supplier_id
           WHERE b.status = 'posted' AND b.bill_date BETWEEN ? AND ?
           ORDER BY s.name""", (start.isoformat(), end.isoformat())).fetchall()
    per: dict[int, dict] = {}
    for x in rows:
        p = per.setdefault(x["id"], {"row": x} | {k: ZERO for k in PURCHASE_CATEGORIES}
                           | {"vat": ZERO})
        p[x["category"]] += Decimal(x["net"])
        p["vat"] += Decimal(x["vat"])
    totals = [ZERO] * 9
    for p in per.values():
        x = p["row"]
        taxable = sum((p[k] for k in VATABLE_PURCHASES), ZERO)
        nums = [p["exempt"] + p["zero"] + taxable + p["vat"], p["exempt"], p["zero"], taxable,
                p["services"], p["capital"], p["goods"], p["vat"], taxable + p["vat"]]
        totals = [a + b for a, b in zip(totals, nums)]
        r.rows.append(Row([tin_text(x["tin"], x["branch"]), x["name"], x["address"]] + nums))
    r.rows.append(Row(["", "Total", ""] + totals, "total"))
    r.notes.append("Cancelled bills are left out.")
    return r


def _quarter_months(start: date, end: date) -> list[tuple[date, date]]:
    q_start, q_end = quarter_of(start)
    if (start, end) != (q_start, q_end):
        raise ledger.BooksError("Choose a calendar quarter for this report.")
    return [(date(start.year, start.month + i, 1), _month_end(start, i)) for i in range(3)]


def withholding(conn: sqlite3.Connection, kind: str, start: date, end: date,
                party_id: int | None = None) -> Report:
    """Tax withheld in a quarter, per payee and ATC, by month: the figures for
    BIR Form 2307 Part II.

    kind "issued": Micronet withheld from its suppliers (Micronet issues the
    2307s). kind "received": clients withheld from Micronet (they issue the
    2307s to Micronet; Micronet claims the credit)."""
    months = _quarter_months(start, end)
    if kind == "issued":
        title = "Expanded Withholding Tax on Payments to Suppliers (Form 2307 data)"
        sql = """SELECT s.id AS pid, s.name, s.tin, s.branch, p.pay_date AS d, p.atc,
                        p.ewt_rate AS rate, p.ewt_base AS base, p.ewt AS tax
                 FROM payments p JOIN suppliers s ON s.id = p.supplier_id
                 WHERE p.status = 'posted' AND CAST(p.ewt AS REAL) > 0
                   AND p.pay_date BETWEEN ? AND ?"""
        who = "Payee"
    else:
        title = "Creditable Tax Withheld by Clients (Form 2307 received)"
        sql = """SELECT c.id AS pid, c.name, c.tin, c.branch, k.coll_date AS d, k.atc,
                        k.cwt_rate AS rate, k.cwt_base AS base, k.cwt AS tax
                 FROM collections k JOIN customers c ON c.id = k.customer_id
                 WHERE k.status = 'posted' AND CAST(k.cwt AS REAL) > 0
                   AND k.coll_date BETWEEN ? AND ?"""
        who = "Withholding agent"
    r = Report(f"wt-{kind}", title, start, end, [
        Column("TIN", width=1.2), Column(who, width=2.4), Column("ATC", width=0.7),
        Column("Rate %", "num", 0.6)]
        + [Column(f"{m:%b} income", "num", 1.1) for m, _ in months]
        + [Column("Total income", "num", 1.1), Column("Tax withheld", "num", 1.1)])
    rows = conn.execute(sql + " ORDER BY name, atc", (start.isoformat(), end.isoformat())).fetchall()
    per: dict[tuple, dict] = {}
    for x in rows:
        if party_id not in (None, x["pid"]):
            continue
        p = per.setdefault((x["name"], x["pid"], x["atc"], x["rate"]),
                           {"row": x, "m": [ZERO] * 3, "tax": ZERO})
        month = date.fromisoformat(x["d"]).month - start.month
        p["m"][month] += Decimal(x["base"])
        p["tax"] += Decimal(x["tax"])
    tot_base = tot_tax = ZERO
    for p in per.values():
        x = p["row"]
        base = sum(p["m"], ZERO)
        tot_base, tot_tax = tot_base + base, tot_tax + p["tax"]
        r.rows.append(Row([tin_text(x["tin"], x["branch"]), x["name"], x["atc"],
                           Decimal(x["rate"])] + p["m"] + [base, p["tax"]]))
    r.rows.append(Row(["", "Total", "", None, None, None, None, tot_base, tot_tax], "total"))
    r.notes.append("These are the figures for BIR Form 2307 Part II. Fill in the official "
                   "form (this app does not print the form itself).")
    return r


# --- Audit log ---------------------------------------------------------------

ENTITY_NAMES = {
    "invoice": "Invoice", "journal": "Journal entry", "collection": "Collection",
    "bill": "Bill", "payment": "Payment", "customer": "Client", "supplier": "Supplier",
    "account": "Account", "user": "User", "settings": "Setting", "report": "Report",
    "database": "Database", "bir": "BIR",
}


def audit_log(conn: sqlite3.Connection, start: date, end: date, *, user: str | None = None,
              action: str | None = None, entity: str | None = None,
              entity_id: str | None = None) -> Report:
    """Everything done in the app: entries, changes (with before and after),
    cancellations, sign-ins, printouts and exports, each with the user and
    the computer it was done from."""
    r = Report("audit", "History (Audit Trail)", start, end, [
        Column("When", width=1.3), Column("User", width=0.9), Column("Computer", width=1.2),
        Column("Action", width=1.0), Column("Record", width=1.1), Column("Details", width=4.2)])
    sql = "SELECT * FROM audit_log WHERE substr(at, 1, 10) BETWEEN ? AND ?"
    args: list = [start.isoformat(), end.isoformat()]
    for column, value in (("user", user), ("action", action), ("entity", entity),
                          ("entity_id", entity_id)):
        if value:
            sql += f" AND {column} = ?"
            args.append(value)
    for x in conn.execute(sql + " ORDER BY id", args):
        details = x["detail"]
        changes = "; ".join(f"{f}: {_blank(b)} -> {_blank(a)}"
                            for f, b, a in ledger.change_list(x["changes"]))
        if changes:
            details = f"{details} [{changes}]" if details else changes
        record = (f"{ENTITY_NAMES.get(x['entity'], x['entity'])} {x['entity_id']}".strip()
                  if x["entity"] else "")
        r.rows.append(Row([x["at"].replace("T", " ")[:19], x["user"], x["workstation"],
                           x["action"], record, details]))
    filters = [f"{k} {v}" for k, v in (("user", user), ("action", action),
                                       ("record type", ENTITY_NAMES.get(entity or "", entity)),
                                       ("record", entity_id))
               if v]
    if filters:
        r.notes.append("Filtered by " + ", ".join(filters) + ".")
    r.notes.append("Nothing can be deleted in this system: invoices are voided, documents "
                   "cancelled, entries reversed, accounts and users deactivated. Each row is "
                   "chained to the one before it, so a changed or removed row shows up in the "
                   "integrity check.")
    return r


def _blank(value) -> str:
    return "(blank)" if value in (None, "") else str(value)

"""Books of accounts pages: collections, supplier bills and payments, manual
journal entries, chart of accounts, suppliers / customers, and the books and
BIR reports (screen, PDF, CSV, year-end archive)."""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, url_for)

from .. import money
from ..books import ledger, postings, printing, reports
from ..books.ledger import BooksError, Line
from ..books.postings import PURCHASE_CATEGORIES
from ..models import parse_tin, today_ph

bp = Blueprint("books", __name__)

REPORTS = {
    "SJ": "Sales Journal", "PJ": "Purchase Journal", "CRJ": "Cash Receipts Journal",
    "CDJ": "Cash Disbursements Journal", "GJ": "General Journal",
    "gl": "General Ledger", "ar": "Accounts Receivable Subsidiary Ledger",
    "ap": "Accounts Payable Subsidiary Ledger", "tb": "Trial Balance",
    "sls": "Summary List of Sales", "slp": "Summary List of Purchases",
    "wt-issued": "Tax withheld from suppliers (Form 2307 to issue)",
    "wt-received": "Tax withheld by clients (Form 2307 received)",
    "audit": "Audit trail",
}
QUARTERLY = ("sls", "slp", "wt-issued", "wt-received")
MAX_ENTRY_LINES = 30


def conn() -> sqlite3.Connection:
    return current_app.extensions["einvoice"]["repo"]().conn


def cfg():
    return current_app.extensions["einvoice"]["cfg"]()


class FormError(Exception):
    pass


def _date(name: str, label: str, errors: dict) -> date | None:
    try:
        return date.fromisoformat(request.form.get(name, "").strip())
    except ValueError:
        errors[name] = f"{label} is required (YYYY-MM-DD)."
        return None


def _amount(name: str, label: str, errors: dict, *, required=True,
            places: int = 2) -> Decimal:
    text = request.form.get(name, "").strip()
    if not text and not required:
        return money.ZERO
    try:
        value = money.parse_decimal(text, field=label, max_decimals=places, positive=False)
    except money.MoneyError as e:
        errors[name] = str(e)
        return money.ZERO
    if value < 0:
        errors[name] = f"{label} can't be negative."
    return value


def _int(name: str) -> int | None:
    text = request.form.get(name, "").strip()
    return int(text) if text.isdigit() else None


def _period(default_quarter: bool = False) -> tuple[date, date]:
    today = today_ph()
    if default_quarter:
        start, end = reports.quarter_of(today)
    else:
        start, end = date(today.year, 1, 1), today
    try:
        start = date.fromisoformat(request.args.get("from", "")) if request.args.get("from") else start
        end = date.fromisoformat(request.args.get("to", "")) if request.args.get("to") else end
    except ValueError:
        abort(400)
    return start, end


def _download(content: bytes, name: str, mimetype: str, inline=False) -> Response:
    disposition = "inline" if inline else "attachment"
    return Response(content, mimetype=mimetype,
                    headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


def _quarters(year: int) -> list[tuple[str, date, date]]:
    return [(f"Q{q + 1}",) + reports.quarter_of(date(year, 3 * q + 1, 1)) for q in range(4)]


# --- Overview ----------------------------------------------------------------

@bp.get("/books")
def overview():
    c = conn()
    today = today_ph()
    start, end = _period()
    counts = {k: c.execute("SELECT COUNT(*) FROM journal_entries WHERE book = ?",
                           (k,)).fetchone()[0] for k in ledger.BOOKS}
    return render_template("books/overview.html", reports=REPORTS, quarterly=QUARTERLY,
                           start=start, end=end, counts=counts,
                           problems=ledger.verify(c), closed=ledger.closed_through(c),
                           quarters=_quarters(today.year), year=today.year,
                           years=_years(c))


def _years(c) -> list[int]:
    rows = c.execute("SELECT DISTINCT substr(entry_date, 1, 4) AS y FROM journal_entries "
                     "ORDER BY y DESC").fetchall()
    return [int(r["y"]) for r in rows] or [today_ph().year]


@bp.get("/books/report/<key>")
def report(key):
    if key not in REPORTS:
        abort(404)
    c = conn()
    start, end = _period(default_quarter=key in QUARTERLY)
    party = request.args.get("party", type=int)
    account = request.args.get("account") or None
    try:
        r = _build(c, key, start, end, party, account)
    except BooksError as e:
        flash(str(e), "error")
        return redirect(url_for("books.overview"))
    fmt = request.args.get("format")
    stem = f"{printing.file_stem(r)}_{start.isoformat() if r.start else ''}_{end.isoformat()}"
    if fmt == "pdf":
        ledger.audit(c, "print", f"{r.title}, {r.period}")
        return _download(printing.report_pdf(r, cfg()), f"{stem}.pdf", "application/pdf",
                         inline=True)
    if fmt == "csv":
        ledger.audit(c, "export", f"{r.title}, {r.period} (CSV)")
        return _download(printing.report_csv(r, cfg()), f"{stem}.csv", "text/csv")
    parties = []
    if key in ("ar", "wt-received"):
        parties = c.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
    elif key in ("ap", "wt-issued"):
        parties = c.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()
    return render_template("books/report.html", r=r, key=key, start=start, end=end,
                           party=party, account=account, parties=parties,
                           accounts=ledger.accounts(c), quarterly=key in QUARTERLY,
                           quarters=_quarters(start.year), cell=printing.cell_text,
                           as_of=key == "tb")


def _build(c, key, start, end, party, account):
    if key in ledger.BOOKS:
        return reports.journal(c, key, start, end)
    if key == "gl":
        return reports.general_ledger(c, start, end, account)
    if key == "ar":
        return reports.subsidiary_ledger(c, "customer", start, end, party)
    if key == "ap":
        return reports.subsidiary_ledger(c, "supplier", start, end, party)
    if key == "tb":
        return reports.trial_balance(c, end)
    if key == "sls":
        return reports.sls(c, start, end)
    if key == "slp":
        return reports.slp(c, start, end)
    if key.startswith("wt-"):
        return reports.withholding(c, key[3:], start, end, party)
    return reports.audit_log(c, start, end)


@bp.get("/books/archive")
def archive():
    year = request.args.get("year", type=int) or today_ph().year
    content = printing.year_archive(conn(), cfg(), year)
    return _download(content, f"Books-{cfg().seller.tin}-{year}.zip", "application/zip")


@bp.post("/books/close")
def close_books():
    c = conn()
    try:
        through = date.fromisoformat(request.form.get("through", ""))
        if through >= today_ph():
            raise BooksError("Only past dates can be closed.")
        with postings.transaction(c):
            ledger.close_through(c, through)
        flash(f"Books closed through {through}. Nothing can be posted on or before that date.")
    except ValueError as e:
        flash(str(e) if isinstance(e, BooksError) else "Enter a date.", "error")
    return redirect(url_for("books.overview"))


# --- Journal entries ---------------------------------------------------------

@bp.get("/books/entries/<int:entry_id>")
def show_entry(entry_id):
    c = conn()
    e = ledger.entry(c, entry_id)
    if e is None:
        abort(404)
    names = reports.party_names(c)
    invoice_no = None
    if e["source_type"] == "invoice":
        invoice_no = c.execute("SELECT control_no FROM invoices WHERE id = ?",
                               (e["source_id"],)).fetchone()["control_no"]
    lines = ledger.entry_lines(c, entry_id)
    return render_template("books/entry.html", e=e, lines=lines, invoice_no=invoice_no,
                           total=sum((Decimal(l["debit"]) for l in lines), money.ZERO),
                           party=names.get((e["party_type"], e["party_id"]), ""),
                           reversal=ledger.reversal_of(c, entry_id),
                           reverses=ledger.entry(c, e["source_id"]) if e["source_type"] == "reversal" else None,
                           books=ledger.BOOKS, D=Decimal)


@bp.post("/books/entries/<int:entry_id>/reverse")
def reverse_entry(entry_id):
    try:
        rid = postings.reverse_manual(conn(), entry_id, request.form.get("reason", ""))
        flash("Reversing entry posted.")
        return redirect(url_for("books.show_entry", entry_id=rid))
    except BooksError as e:
        flash(str(e), "error")
        return redirect(url_for("books.show_entry", entry_id=entry_id))


def _entry_form(data, errors=None, status=200):
    c = conn()
    return render_template("books/entry_form.html", data=data, errors=errors or {},
                           accounts=ledger.accounts(c, active_only=True),
                           customers=c.execute("SELECT id, name FROM customers ORDER BY name").fetchall(),
                           suppliers=c.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()), status


@bp.get("/books/entries/new")
def new_entry():
    return _entry_form({"entry_date": today_ph().isoformat(), "reference": "", "particulars": "",
                        "party": "", "opening": "", "rows": [("", "", "", "")] * 4})


@bp.post("/books/entries/new")
def create_entry():
    f = request.form
    rows = list(zip(f.getlist("account[]"), f.getlist("debit[]"), f.getlist("credit[]"),
                    f.getlist("memo[]")))
    data = {k: f.get(k, "") for k in ("entry_date", "reference", "particulars", "party",
                                      "opening")}
    data["rows"] = rows
    errors: dict[str, str] = {}
    entry_date = _date("entry_date", "Date", errors)
    lines = []
    for i, (acct, dr, cr, memo) in enumerate(rows, start=1):
        if not (acct or dr.strip() or cr.strip()):
            continue
        try:
            d = money.parse_decimal(dr or "0", field="Debit", max_decimals=2, positive=False)
            k = money.parse_decimal(cr or "0", field="Credit", max_decimals=2, positive=False)
        except money.MoneyError as e:
            errors[f"line{i}"] = f"Line {i}: {e}"
            continue
        if not acct:
            errors[f"line{i}"] = f"Line {i}: choose an account."
            continue
        lines.append(Line(acct, d, k, " ".join(memo.split())))
    if len(lines) > MAX_ENTRY_LINES:
        errors["rows"] = f"At most {MAX_ENTRY_LINES} lines."
    party = ("", None)
    if data["party"]:
        kind, _, pid = data["party"].partition(":")
        if kind in ("customer", "supplier") and pid.isdigit():
            party = (kind, int(pid))
    if errors:
        return _entry_form(data, errors, 422)
    try:
        eid = postings.create_manual_entry(conn(), entry_date=entry_date,
                                           reference=data["reference"],
                                           particulars=data["particulars"], lines=lines,
                                           party=party, opening=bool(data["opening"]))
    except BooksError as e:
        return _entry_form(data, {"entry": str(e)}, 422)
    e = ledger.entry(conn(), eid)
    flash(f"Posted to the {ledger.BOOKS[e['book']]} as {e['book']}-{e['entry_no']}.")
    return redirect(url_for("books.show_entry", entry_id=eid))


@bp.get("/books/entries")
def entries():
    c = conn()
    start, end = _period()
    book = request.args.get("book", "")
    sql = "SELECT * FROM journal_entries WHERE entry_date BETWEEN ? AND ?"
    args = [start.isoformat(), end.isoformat()]
    if book in ledger.BOOKS:
        sql += " AND book = ?"
        args.append(book)
    rows = c.execute(sql + " ORDER BY entry_date DESC, id DESC LIMIT 500", args).fetchall()
    totals: dict[int, Decimal] = {}
    for line in c.execute("SELECT entry_id, debit FROM journal_lines"):
        totals[line["entry_id"]] = totals.get(line["entry_id"], money.ZERO) + Decimal(line["debit"])
    return render_template("books/entries.html", rows=rows, totals=totals, start=start, end=end,
                           book=book, books=ledger.BOOKS, names=reports.party_names(c))


# --- Collections -------------------------------------------------------------

@bp.get("/collections")
def collections():
    rows = conn().execute(
        """SELECT k.*, c.name AS customer, i.control_no FROM collections k
           JOIN customers c ON c.id = k.customer_id LEFT JOIN invoices i ON i.id = k.invoice_id
           ORDER BY k.coll_date DESC, k.id DESC""").fetchall()
    return render_template("books/collections.html", rows=rows, D=Decimal,
                           open_invoices=postings.open_invoices(conn()))


def _collection_form(data, errors=None, status=200):
    c = conn()
    return render_template("books/collection_form.html", data=data, errors=errors or {},
                           invoices=postings.open_invoices(c),
                           customers=c.execute("SELECT id, name FROM customers ORDER BY name").fetchall(),
                           cash_accounts=ledger.cash_accounts(c)), status


@bp.get("/collections/new")
def new_collection():
    data = {k: "" for k in ("invoice_id", "customer_id", "reference", "applied", "cash", "cwt",
                            "atc", "cwt_base", "cwt_rate", "cash_account")}
    data["coll_date"] = today_ph().isoformat()
    data["invoice_id"] = request.args.get("invoice", "")
    return _collection_form(data)


@bp.post("/collections/new")
def create_collection():
    data = request.form.to_dict()
    errors: dict[str, str] = {}
    coll_date = _date("coll_date", "Date received", errors)
    applied = _amount("applied", "Amount applied", errors)
    cash = _amount("cash", "Cash received", errors)
    cwt = _amount("cwt", "Tax withheld", errors, required=False)
    cwt_base = _amount("cwt_base", "Income payment", errors, required=False)
    cwt_rate = _amount("cwt_rate", "Rate", errors, required=False, places=4)
    invoice_id = _int("invoice_id")
    customer_id = _int("customer_id")
    if invoice_id:
        row = conn().execute("SELECT customer_id FROM invoices WHERE id = ?",
                             (invoice_id,)).fetchone()
        customer_id = row["customer_id"] if row else None
    if errors:
        return _collection_form(data, errors, 422)
    try:
        cid = postings.create_collection(
            conn(), customer_id=customer_id or 0, invoice_id=invoice_id, coll_date=coll_date,
            reference=data.get("reference", ""), applied=applied, cash=cash,
            cash_account=data.get("cash_account", ""), cwt=cwt, atc=data.get("atc", ""),
            cwt_base=cwt_base, cwt_rate=cwt_rate)
    except BooksError as e:
        return _collection_form(data, {"doc": str(e)}, 422)
    flash("Collection recorded in the Cash Receipts Journal.")
    return redirect(url_for("books.show_entry",
                            entry_id=ledger.entry_for(conn(), "collection", cid)["id"]))


# --- Bills and payments ------------------------------------------------------

@bp.get("/bills")
def bills():
    c = conn()
    rows = c.execute(
        """SELECT b.*, s.name AS supplier FROM bills b JOIN suppliers s ON s.id = b.supplier_id
           ORDER BY b.bill_date DESC, b.id DESC""").fetchall()
    balances = {r["id"]: postings.bill_balance(c, r["id"]) for r in rows}
    return render_template("books/bills.html", rows=rows, balances=balances, D=Decimal,
                           categories=PURCHASE_CATEGORIES)


def _bill_form(data, errors=None, status=200):
    c = conn()
    accts = [a for a in ledger.accounts(c, active_only=True)
             if not a["role"] and not a["is_cash"] and a["type"] in ("asset", "expense")]
    return render_template("books/bill_form.html", data=data, errors=errors or {},
                           accounts=accts, categories=PURCHASE_CATEGORIES,
                           suppliers=c.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()), status


@bp.get("/bills/new")
def new_bill():
    data = {k: "" for k in ("supplier_id", "supplier_ref", "description", "account_code",
                            "category", "net", "vat")}
    data["bill_date"] = today_ph().isoformat()
    data["supplier_id"] = request.args.get("supplier", "")
    return _bill_form(data)


@bp.post("/bills/new")
def create_bill():
    data = request.form.to_dict()
    errors: dict[str, str] = {}
    bill_date = _date("bill_date", "Bill date", errors)
    net = _amount("net", "Amount (net of VAT)", errors)
    vat = _amount("vat", "VAT", errors, required=False)
    if errors:
        return _bill_form(data, errors, 422)
    try:
        bid = postings.create_bill(
            conn(), supplier_id=_int("supplier_id") or 0, bill_date=bill_date,
            supplier_ref=data.get("supplier_ref", ""), description=data.get("description", ""),
            account_code=data.get("account_code", ""), category=data.get("category", ""),
            net=net, vat=vat)
    except BooksError as e:
        return _bill_form(data, {"doc": str(e)}, 422)
    flash("Bill recorded in the Purchase Journal.")
    return redirect(url_for("books.show_entry", entry_id=ledger.entry_for(conn(), "bill", bid)["id"]))


@bp.get("/payments")
def payments():
    rows = conn().execute(
        """SELECT p.*, s.name AS supplier, b.supplier_ref FROM payments p
           JOIN suppliers s ON s.id = p.supplier_id LEFT JOIN bills b ON b.id = p.bill_id
           ORDER BY p.pay_date DESC, p.id DESC""").fetchall()
    return render_template("books/payments.html", rows=rows, D=Decimal)


def _payment_form(data, errors=None, status=200):
    c = conn()
    return render_template("books/payment_form.html", data=data, errors=errors or {},
                           bills=postings.open_bills(c), cash_accounts=ledger.cash_accounts(c),
                           suppliers=c.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()), status


@bp.get("/payments/new")
def new_payment():
    data = {k: "" for k in ("bill_id", "supplier_id", "reference", "amount", "ewt", "atc",
                            "ewt_base", "ewt_rate", "cash_account")}
    data["pay_date"] = today_ph().isoformat()
    data["bill_id"] = request.args.get("bill", "")
    return _payment_form(data)


@bp.post("/payments/new")
def create_payment():
    data = request.form.to_dict()
    errors: dict[str, str] = {}
    pay_date = _date("pay_date", "Payment date", errors)
    amount = _amount("amount", "Amount settled", errors)
    ewt = _amount("ewt", "Tax withheld", errors, required=False)
    ewt_base = _amount("ewt_base", "Income payment", errors, required=False)
    ewt_rate = _amount("ewt_rate", "Rate", errors, required=False, places=4)
    bill_id = _int("bill_id")
    supplier_id = _int("supplier_id")
    if bill_id:
        row = conn().execute("SELECT supplier_id FROM bills WHERE id = ?", (bill_id,)).fetchone()
        supplier_id = row["supplier_id"] if row else None
    if errors:
        return _payment_form(data, errors, 422)
    try:
        pid = postings.create_payment(
            conn(), supplier_id=supplier_id or 0, bill_id=bill_id, pay_date=pay_date,
            reference=data.get("reference", ""), amount=amount,
            cash_account=data.get("cash_account", ""), ewt=ewt, atc=data.get("atc", ""),
            ewt_base=ewt_base, ewt_rate=ewt_rate)
    except BooksError as e:
        return _payment_form(data, {"doc": str(e)}, 422)
    flash("Payment recorded in the Cash Disbursements Journal.")
    return redirect(url_for("books.show_entry", entry_id=ledger.entry_for(conn(), "payment", pid)["id"]))


@bp.post("/<any(collection, bill, payment):kind>s/<int:doc_id>/cancel")
def cancel(kind, doc_id):
    try:
        rid = postings.cancel(conn(), kind, doc_id, request.form.get("reason", ""))
        flash(f"The {kind} is cancelled; its entry was reversed.")
        return redirect(url_for("books.show_entry", entry_id=rid))
    except BooksError as e:
        flash(str(e), "error")
        return redirect(url_for(f"books.{kind}s"))


# --- Accounts, suppliers, customers -------------------------------------------

@bp.get("/accounts")
def accounts():
    c = conn()
    used = {r[0] for r in c.execute("SELECT DISTINCT account_code FROM journal_lines")}
    return render_template("books/accounts.html", accounts=ledger.accounts(c), used=used,
                           types=ledger.ACCOUNT_TYPES)


@bp.post("/accounts")
def add_account():
    c = conn()
    code = request.form.get("code", "").strip()
    name = " ".join(request.form.get("name", "").split())
    kind = request.form.get("type", "")
    is_cash = 1 if request.form.get("is_cash") else 0
    try:
        if not (code.isdigit() and 3 <= len(code) <= 8):
            raise BooksError("The account code must be 3 to 8 digits.")
        if not name:
            raise BooksError("The account name is required.")
        if kind not in ledger.ACCOUNT_TYPES:
            raise BooksError("Choose the account type.")
        if is_cash and kind != "asset":
            raise BooksError("Only an asset account can be a cash / bank account.")
        if c.execute("SELECT 1 FROM accounts WHERE code = ?", (code,)).fetchone():
            raise BooksError(f"Account {code} already exists.")
        with postings.transaction(c):
            c.execute("INSERT INTO accounts (code, name, type, is_cash) VALUES (?,?,?,?)",
                      (code, name, kind, is_cash))
            ledger.audit(c, "add account", f"{code} {name} ({kind})")
        flash(f"Account {code} {name} added.")
    except BooksError as e:
        flash(str(e), "error")
    return redirect(url_for("books.accounts"))


@bp.post("/accounts/<code>")
def update_account(code):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
    if a is None:
        abort(404)
    name = " ".join(request.form.get("name", a["name"]).split()) or a["name"]
    active = 1 if request.form.get("active") else 0
    if a["role"] and not active:
        flash(f"{code} {a['name']} is used by the automatic postings and stays active.", "error")
        return redirect(url_for("books.accounts"))
    with postings.transaction(c):
        c.execute("UPDATE accounts SET name = ?, active = ? WHERE code = ?", (name, active, code))
        ledger.audit(c, "change account", f"{code}: name '{a['name']}' -> '{name}', "
                     f"active {a['active']} -> {active}")
    flash(f"Account {code} saved.")
    return redirect(url_for("books.accounts"))


@bp.get("/suppliers")
def suppliers():
    c = conn()
    rows = c.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    return render_template("books/parties.html", kind="supplier", rows=rows,
                           tin=reports.tin_text, data={}, errors={})


@bp.post("/suppliers")
def add_supplier():
    data = request.form.to_dict()
    try:
        tin, branch = parse_tin(data.get("tin", ""))
        postings.create_supplier(conn(), name=data.get("name", ""), tin=tin, branch=branch,
                                 address=data.get("address", ""))
    except ValueError as e:
        rows = conn().execute("SELECT * FROM suppliers ORDER BY name").fetchall()
        return render_template("books/parties.html", kind="supplier", rows=rows,
                               tin=reports.tin_text, data=data, errors={"doc": str(e)}), 422
    flash(f"Supplier {' '.join(data['name'].split())} added.")
    back = request.form.get("next", "")
    return redirect(back if back.startswith("/") else url_for("books.suppliers"))


@bp.get("/customers")
def customers():
    rows = conn().execute("SELECT * FROM customers ORDER BY name").fetchall()
    return render_template("books/parties.html", kind="customer", rows=rows,
                           tin=reports.tin_text, data={}, errors={})

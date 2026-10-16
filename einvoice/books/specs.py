"""System and Technical Specifications, for registering the app with BIR as a
Computerized Accounting System (CAS).

Covers what BIR asks for in the system description: application name,
software and database; functions, features and data flows; backup, data
security and disaster recovery; the reports with their description, purpose
and sample layout; and facsimiles of the invoices and books.

Facts come from the running app (software versions, database tables, schema
version, settings), so the document stays true as the app changes. The
facsimiles are printed by the app itself from a throwaway database of sample
transactions; the company's own records are never included.
"""
from __future__ import annotations

import platform
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import metadata
from io import BytesIO
from pathlib import Path

import pymupdf
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

from .. import __version__, backup
from ..config import Config
from ..models import PH_TZ, VATABLE, ZERO_RATED, Invoice, InvoiceLine, today_ph
from . import chart, ledger, postings, printing, reports, taxes
from .ledger import Line as JLine

APP_NAME = "Micronet e-Invoice"
TITLE = "System and Technical Specifications"

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#555555")
RED = colors.HexColor("#c8102e")
GRID = colors.HexColor("#b5b5b5")
SHADE = colors.HexColor("#efefef")
MARGIN = 0.8 * inch

S_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK,
                        spaceAfter=5)
S_SMALL = ParagraphStyle("small", parent=S_BODY, fontSize=8, leading=10.5, textColor=MUTED)
S_CELL = ParagraphStyle("cell", parent=S_BODY, fontSize=8.5, leading=11, spaceAfter=0)
S_CELL_B = ParagraphStyle("cellb", parent=S_CELL, fontName="Helvetica-Bold")
S_H1 = ParagraphStyle("h1", parent=S_BODY, fontName="Helvetica-Bold", fontSize=14, leading=18,
                      spaceBefore=6, spaceAfter=8, textColor=RED)
S_H2 = ParagraphStyle("h2", parent=S_BODY, fontName="Helvetica-Bold", fontSize=11, leading=15,
                      spaceBefore=10, spaceAfter=4)
S_TITLE = ParagraphStyle("title", parent=S_BODY, fontName="Helvetica-Bold", fontSize=24,
                         leading=30, alignment=TA_CENTER)
S_SUB = ParagraphStyle("sub", parent=S_BODY, fontSize=12, leading=16, alignment=TA_CENTER,
                       textColor=MUTED)
S_BULLET = ParagraphStyle("bullet", parent=S_BODY, leftIndent=14, bulletIndent=4, spaceAfter=3)

# What each table holds (database architecture section). Tables the app adds
# later and that are not described here are still listed, from the database.
TABLES = {
    "invoices": "Issued Service Invoices: control no., date, client, sale type, rate, totals, "
                "withholding tax, status (issued / void), who issued or voided it",
    "invoice_lines": "Lines of each invoice (item, description, qty, unit, prices in JPY and PHP); "
                     "never changed after issue",
    "control_counter": "Last Service Invoice control number used (numbers are never reused)",
    "invoice_drafts": "Invoices being filled in, before they are issued (no number, not in the books)",
    "invoice_emails": "Each e-mail of an invoice PDF: when, to whom, result",
    "customers": "Clients: name, TIN and branch, address, business style, e-mail",
    "suppliers": "Suppliers: name, TIN and branch, address",
    "accounts": "Chart of accounts: code, name, type, financial statement section, flags",
    "journal_entries": "Books of original entry (SJ, PJ, CRJ, CDJ, GJ): number per book, date, "
                       "reference, particulars, source document, SHA-256 chain",
    "journal_lines": "Debit and credit lines of each journal entry",
    "bills": "Supplier invoices / receipts (purchases): net, VAT, purchase type, expense account",
    "payments": "Payments to suppliers, with expanded withholding tax (ATC, base, rate, amount)",
    "collections": "Money received from clients, with creditable withholding tax (Form 2307)",
    "eis_exports": "EIS e-invoice JSON files produced: unique ID, file, SHA-256",
    "eis_submissions": "Transmissions to the BIR EIS (when enrolled): acknowledgment and status",
    "eis_submission_items": "Result code per e-invoice in each transmission",
    "users": "Sign-in accounts: username, name, password hash, lockout data (never deleted)",
    "audit_log": "History (audit trail): every action, who, when, computer, before / after values, "
                 "SHA-256 chain",
    "settings": "Settings such as the tax profile and the date the books are closed through",
    "signatures": "Each user's saved signature picture (earlier ones kept, inactive)",
    "signed_documents": "PDFs signed in the app: the signed copy, its SHA-256, who and when",
}


@dataclass
class Facsimile:
    annex: str
    title: str
    pdf: bytes


# --- Building blocks -----------------------------------------------------------------

def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(text, style=S_BODY):
    return Paragraph(text, style)


def _bullets(items):
    return [Paragraph(t, S_BULLET, bulletText="•") for t in items]


def _table(rows, widths, header=True):
    data = [[c if not isinstance(c, str) else _p(c, S_CELL_B if header and i == 0 else S_CELL)
             for c in row] for i, row in enumerate(rows)]
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), SHADE))
    t.setStyle(TableStyle(style))
    return t


def _version(dist: str) -> str:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return "-"


# --- Diagrams ------------------------------------------------------------------------

def _box(d, x, y, w, h, text, fill=colors.white, bold=False, size=7.5):
    d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=fill, strokeColor=INK, strokeWidth=0.7))
    lines = text.split("\n")
    top = y + h / 2 + (len(lines) - 1) * (size + 1.5) / 2 - size / 3
    for i, line in enumerate(lines):
        d.add(String(x + w / 2, top - i * (size + 1.5), line, textAnchor="middle",
                     fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
                     fillColor=INK))


def _arrow(d, x1, y1, x2, y2):
    d.add(Line(x1, y1, x2, y2, strokeColor=INK, strokeWidth=0.7))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    s = 4.5
    d.add(Polygon([x2, y2, x2 - s * math.cos(a - 0.4), y2 - s * math.sin(a - 0.4),
                   x2 - s * math.cos(a + 0.4), y2 - s * math.sin(a + 0.4)],
                  fillColor=INK, strokeColor=INK, strokeWidth=0.5))


def data_flow_diagram() -> Drawing:
    """Source documents -> books of original entry -> ledgers -> reports."""
    w, h = 6.9 * inch, 3.6 * inch
    d = Drawing(w, h)
    col = [0, 1.55 * inch, 3.35 * inch, 5.15 * inch]
    bw, bh = 1.45 * inch, 0.42 * inch
    heads = ["Source documents\n(entered in the app)", "Books of original entry",
             "Ledgers", "Reports and returns"]
    for x, t in zip(col, heads):
        d.add(String(x + (1.75 * inch if x == col[3] else bw) / 2, h - 10, t.split("\n")[0],
                     textAnchor="middle", fontName="Helvetica-Bold", fontSize=8, fillColor=RED))
    sources = ["Service Invoice\n(issued)", "Collection from client", "Supplier bill",
               "Payment to supplier", "Manual entry / void /\nreversal"]
    books = ["Sales Journal (SJ)", "Cash Receipts\nJournal (CRJ)", "Purchase Journal (PJ)",
             "Cash Disbursements\nJournal (CDJ)", "General Journal (GJ)"]
    ys = [h - 0.75 * inch - i * 0.58 * inch for i in range(5)]
    for y, s, b in zip(ys, sources, books):
        _box(d, col[0], y, bw, bh, s, fill=colors.HexColor("#fdeaea"))
        _box(d, col[1], y, bw, bh, b)
        _arrow(d, col[0] + bw, y + bh / 2, col[1], y + bh / 2)
    gl_y, sub_y = h - 1.6 * inch, h - 2.75 * inch
    _box(d, col[2], gl_y, bw, 0.6 * inch, "General Ledger\n(all accounts)", bold=True)
    _box(d, col[2], sub_y, bw, 0.6 * inch, "Subsidiary ledgers\nAccounts receivable\nAccounts payable")
    for y in ys:
        _arrow(d, col[1] + bw, y + bh / 2, col[2], gl_y + 0.3 * inch)
    _arrow(d, col[2] + bw / 2, gl_y, col[2] + bw / 2, sub_y + 0.6 * inch)
    outs = ["Trial Balance", "Income Statement, Balance Sheet", "SLS / SLP, 2307 / SAWT / QAP data",
            "Tax return worksheets", "EIS e-invoice JSON (from invoices)"]
    ow = 1.75 * inch
    for i, t in enumerate(outs):
        y = h - 0.75 * inch - i * 0.58 * inch
        _box(d, col[3], y, ow, bh, t, fill=SHADE)
        _arrow(d, col[2] + bw, gl_y + 0.3 * inch, col[3], y + bh / 2)
    d.add(String(0, 4, "Every step is recorded in the History (audit trail) with the user, date and time, "
                       "and computer.", fontName="Helvetica-Oblique", fontSize=7, fillColor=MUTED))
    return d


def invoice_flow_diagram() -> Drawing:
    w, h = 6.9 * inch, 2.15 * inch
    d = Drawing(w, h)
    bw, bh, gap = 1.2 * inch, 0.55 * inch, 0.22 * inch
    steps = ["Enter invoice\n(draft, no number)", "Review page\nEIS v2.1 checks", "Issue: next\ncontrol no.\nassigned",
             "Sales Journal\nentry posted\n(automatic)", "PDF printed /\ne-mailed;\noptional signature"]
    y = h - bh - 6
    for i, t in enumerate(steps):
        x = i * (bw + gap)
        _box(d, x, y, bw, bh, t, fill=colors.HexColor("#fdeaea") if i == 2 else colors.white,
             bold=i == 2)
        if i:
            _arrow(d, x - gap, y + bh / 2, x, y + bh / 2)
    y2 = 0.2 * inch
    lower = [(2, "Mistake found: void\nwith a reason"), (3, "General Journal\nreversing entry"),
             (4, "Reissue under a\nnew control no.")]
    for n, (i, t) in enumerate(lower):
        x = i * (bw + gap)
        _box(d, x, y2, bw, bh, t, fill=SHADE)
        if n:
            _arrow(d, x - gap, y2 + bh / 2, x, y2 + bh / 2)
    _arrow(d, 2 * (bw + gap) + bw / 2, y, 2 * (bw + gap) + bw / 2, y2 + bh)
    d.add(String(0, y2 + bh / 2 - 3, "Issued invoices are never edited or deleted.",
                 fontName="Helvetica-Oblique", fontSize=7, fillColor=MUTED))
    d.add(String(0, y2 + bh / 2 - 12, "EIS JSON export / transmission when enrolled.",
                 fontName="Helvetica-Oblique", fontSize=7, fillColor=MUTED))
    return d


# --- Sample data for the facsimiles ----------------------------------------------------

def _first_account(conn, sql: str, fallback_type: str) -> str:
    row = conn.execute(sql).fetchone() or conn.execute(
        "SELECT code FROM accounts WHERE type = ? AND active = 1 AND role IS NULL ORDER BY code",
        (fallback_type,)).fetchone()
    return row["code"]


def _accrual_accounts(conn) -> tuple[str, str, str] | None:
    """(expense, accrued liability, what) for a sample accrual, matched by name."""
    def find(type_, *words, exclude=""):
        for w in words:
            row = conn.execute(
                "SELECT code FROM accounts WHERE type = ? AND active = 1 AND role IS NULL "
                "AND lower(name) LIKE ? AND (? = '' OR lower(name) NOT LIKE ?) ORDER BY code",
                (type_, f"%{w}%", exclude, f"%{exclude}%")).fetchone()
            if row:
                return row["code"]
        return None
    for liability_words, expense_words, what in [
            (("accrued expenses", "accrued utilit"), ("light", "utilit", "electric"), "electricity"),
            (("accrued%audit",), ("audit", "professional"), "audit fee"),
            (("accrued%prof",), ("professional",), "professional fees"),
            (("accrued",), ("light", "utilit", "electric"), "electricity")]:
        accrued = find("liability", *liability_words, exclude="retire")
        expense = find("expense", *expense_words)
        if accrued and expense:
            return expense, accrued, what
    return None


def sample_facsimiles(cfg: Config) -> list[Facsimile]:
    """Print the invoices, books and reports from a database of sample
    transactions made just for this, in a temporary folder."""
    from ..pdf.service_invoice import render as render_invoice
    from ..storage.db import connect
    from ..storage.repository import Repository

    tmp = Path(tempfile.mkdtemp(prefix="einvoice-specs-"))
    local_chart = cfg.data_dir / chart.LOCAL_FILE
    if local_chart.exists():
        shutil.copy(local_chart, tmp / chart.LOCAL_FILE)  # the company's own accounts
    token = ledger.set_acting_user("sample.user")
    conn = connect(tmp / "einvoice.db")
    try:
        today = today_ph()
        month_end = today.replace(day=1) - timedelta(days=1)
        start = month_end.replace(day=1)
        day = lambda n: start.replace(day=n)  # noqa: E731
        repo = Repository(conn, cfg.control_no_start)
        rate = Decimal("0.3875")

        def invoice(n, **kw):
            fields = dict(issue_date=day(n), buyer_branch="00000", business_style="",
                          rate=rate, rate_date=day(n), rate_source="BSP reference rate (sample)")
            fields.update(kw)
            return repo.issue(Invoice(**fields).compute())

        local = invoice(5, buyer_name="SAMPLE LOCAL CLIENT, INC.", buyer_tin="123456789",
                        buyer_address="Ayala Avenue, Makati City", trans_class=VATABLE,
                        withholding_php=Decimal("54250.00"),
                        lines=[InvoiceLine("IT Services", f"Software maintenance for {start:%B %Y}",
                                           Decimal("1"), "LOT", Decimal("7000000"))])
        abroad = invoice(8, buyer_name="SAMPLE CLIENT CO., LTD.", buyer_tin="000000000",
                         buyer_address="Sapporo, Japan", trans_class=ZERO_RATED,
                         lines=[InvoiceLine("IT Services", f"Software development for {start:%B %Y}",
                                            Decimal("1"), "LOT", Decimal("12000000")),
                                InvoiceLine("IT Services", "System testing and documentation",
                                            Decimal("2"), "LOT", Decimal("1500000"))])
        wrong = invoice(9, buyer_name="SAMPLE CLIENT CO LTD", buyer_tin="000000000",
                        buyer_address="Sapporo, Japan", trans_class=ZERO_RATED,
                        lines=[InvoiceLine("IT Services", "Consulting", Decimal("1"), "LOT",
                                           Decimal("500000"))])

        bank = ledger.account_for(conn, "bank")
        cust = lambda inv: conn.execute("SELECT customer_id FROM invoices WHERE id = ?",  # noqa: E731
                                        (inv.id,)).fetchone()[0]
        postings.create_collection(
            conn, customer_id=cust(abroad), invoice_id=abroad.id, coll_date=day(20),
            reference="Bank credit TT-0001", applied=abroad.total_due_php,
            cash=abroad.total_due_php - Decimal("12500.00"), cash_account=bank)
        postings.create_collection(
            conn, customer_id=cust(local), invoice_id=local.id, coll_date=day(25),
            reference="Check 004512", applied=local.gross_php, cash=local.total_due_php,
            cash_account=bank, cwt=local.withholding_php, atc="WC160",
            cwt_base=local.total_php, cwt_rate=Decimal("2"))

        sid = postings.create_supplier(conn, name="SAMPLE OFFICE LEASING CORP.", tin="987654321",
                                       branch="00000", address="Legaspi Village, Makati City")
        expense = _first_account(conn, "SELECT code FROM accounts WHERE type = 'expense' AND active = 1 "
                                       "AND lower(name) LIKE '%rent%' ORDER BY code", "expense")
        bill = postings.create_bill(conn, supplier_id=sid, bill_date=day(3), supplier_ref="SI 10234",
                                    description=f"Office rent for {start:%B %Y}", account_code=expense,
                                    category="services", net=Decimal("80000.00"),
                                    vat=Decimal("9600.00"))
        postings.create_payment(conn, supplier_id=sid, bill_id=bill, pay_date=day(10),
                                reference="Check 001877", amount=Decimal("89600.00"),
                                cash_account=bank, ewt=Decimal("4000.00"), atc="WC100",
                                ewt_base=Decimal("80000.00"), ewt_rate=Decimal("5"))
        # An adjusting entry, when the chart has accounts for one.
        accrual = _accrual_accounts(conn)
        if accrual:
            expense, accrued, what = accrual
            postings.create_manual_entry(
                conn, entry_date=month_end, reference="ADJ-01",
                particulars=f"Accrual of {start:%B %Y} {what}, bill not yet received",
                lines=[JLine(expense, debit=Decimal("15300.00"), memo="estimate"),
                       JLine(accrued, credit=Decimal("15300.00"))])

        # A mistake: void, General Journal reversal, reissue.
        repo.void(wrong.control_no, "Client name misspelled; reissued under the next number")
        invoice(9, buyer_name="SAMPLE CLIENT CO., LTD.", buyer_tin="000000000",
                buyer_address="Sapporo, Japan", trans_class=ZERO_RATED,
                lines=[InvoiceLine("IT Services", "Consulting", Decimal("1"), "LOT",
                                   Decimal("500000"))])

        qs, qe = reports.quarter_of(month_end)
        out = [Facsimile("A-1", "Service Invoice, VATable, with withholding tax",
                         render_invoice(repo.get(local.control_no), cfg)),
               Facsimile("A-2", "Service Invoice, zero-rated, foreign client, two lines",
                         render_invoice(repo.get(abroad.control_no), cfg)),
               Facsimile("A-3", "Service Invoice, voided (control number kept, marked void)",
                         render_invoice(repo.get(wrong.control_no), cfg))]
        # Through today: a void is posted on the day it is made.
        books = [reports.journal(conn, b, start, today) for b in ledger.BOOKS] + [
            reports.general_ledger(conn, start, today),
            reports.subsidiary_ledger(conn, "customer", start, today),
            reports.subsidiary_ledger(conn, "supplier", start, today)]
        out += [Facsimile(f"B-{i}", r.title, printing.report_pdf(r, cfg))
                for i, r in enumerate(books, start=1)]
        others = [reports.trial_balance(conn, month_end),
                  reports.income_statement(conn, start, month_end),
                  reports.balance_sheet(conn, month_end),
                  reports.sls(conn, qs, qe), reports.slp(conn, qs, qe),
                  reports.withholding(conn, "issued", qs, qe),
                  reports.withholding(conn, "received", qs, qe)]
        form = next(f for f in taxes.FORMS if f.code == "2550Q")
        others.append(taxes.worksheet(conn, form, qe.year, (qe.month - 1) // 3 + 1))
        out += [Facsimile(f"C-{i}", r.title, printing.report_pdf(r, cfg))
                for i, r in enumerate(others, start=1)]
        out.append(Facsimile("D-1", "History (audit trail)",
                             printing.report_pdf(reports.audit_log(conn, start, today), cfg)))
        return out
    finally:
        conn.close()
        ledger.reset_acting_user(token)
        shutil.rmtree(tmp, ignore_errors=True)


# --- The document ----------------------------------------------------------------------

def _software_rows(conn) -> list[list[str]]:
    return [
        ["Component", "Product / version", "Use"],
        ["Application", f"{APP_NAME} {__version__}", "Invoicing, books of accounts, reports"],
        ["Programming language", f"Python {platform.python_version()}", "All program logic"],
        ["Web framework", f"Flask {_version('flask')} (Werkzeug {_version('werkzeug')})",
         "Screens, served only to the same computer (127.0.0.1)"],
        ["Database", f"SQLite {sqlite3.sqlite_version} (built into Python)",
         "One database file; see Database architecture"],
        ["PDF printing", f"ReportLab {_version('reportlab')}",
         "Invoices, books, reports, this document"],
        ["PDF signing / preview", f"PyMuPDF {_version('pymupdf')}, Pillow {_version('pillow')}",
         "Optional signatures on PDFs"],
        ["Cryptography", f"cryptography {_version('cryptography')}",
         "EIS transmission encryption (when enrolled)"],
        ["Desktop window", f"pywebview {_version('pywebview')} with Microsoft Edge WebView2",
         "The app's own window on Windows"],
        ["Operating system", "Microsoft Windows 10 / 11 (64-bit)",
         f"Prepared on {platform.system()} {platform.release()}"],
    ]


def _tables_rows(conn) -> list[list[str]]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [["Table", "Contents"]] + [[n, TABLES.get(n, "")] for n in names]


REPORT_LIST = [
    # (name, description, purpose, frequency, annex)
    ("Service Invoice", "Sequentially numbered invoice: seller and buyer details, items, amounts in "
     "JPY and PHP, VATable / zero-rated / exempt sales, VAT, withholding tax, total amount due",
     "Primary sales document given to the client (RR 7-2024)", "Each sale", "A-1 to A-3"),
    ("Sales Journal", "Issued invoices in date order: invoice no., client, TIN, debit and credit lines",
     "Book of original entry for sales", "Monthly / on demand", "B-1"),
    ("Purchase Journal", "Supplier bills: supplier, TIN, reference, expense, input VAT, payable",
     "Book of original entry for purchases", "Monthly / on demand", "B-2"),
    ("Cash Receipts Journal", "Collections: client, reference, cash, creditable tax withheld, "
     "forex difference, receivable", "Book of original entry for money received",
     "Monthly / on demand", "B-3"),
    ("Cash Disbursements Journal", "Payments: supplier, check / reference, payable, cash, tax withheld",
     "Book of original entry for money paid out", "Monthly / on demand", "B-4"),
    ("General Journal", "Adjustments, accruals, voids (reversing entries), opening balances",
     "Book of original entry for other transactions", "Monthly / on demand", "B-5"),
    ("General Ledger", "Each account's opening balance, postings with their journal reference, "
     "running balance", "Book of final entry", "Monthly / on demand", "B-6"),
    ("Accounts Receivable Subsidiary Ledger", "Per client: invoices, collections, balance",
     "Detail behind Accounts receivable", "Monthly / on demand", "B-7"),
    ("Accounts Payable Subsidiary Ledger", "Per supplier: bills, payments, balance",
     "Detail behind Accounts payable", "Monthly / on demand", "B-8"),
    ("Trial Balance", "Debit and credit balance of every account as of a date",
     "Proves the ledger balances; basis of statements", "Monthly / year-end", "C-1"),
    ("Income Statement", "Revenue, cost, expenses and net income for a period",
     "Financial performance", "Monthly / quarterly / year-end", "C-2"),
    ("Balance Sheet", "Assets, liabilities and equity as of a date", "Financial position",
     "Monthly / quarterly / year-end", "C-3"),
    ("Income vs Expenses by Month", "Monthly totals of income, expenses and net",
     "Management overview", "On demand", "-"),
    ("Summary List of Sales (SLS)", "Per client: TIN, name, address, exempt, zero-rated, taxable "
     "sales and output VAT for the quarter", "Quarterly VAT attachment (SLSP)", "Quarterly", "C-4"),
    ("Summary List of Purchases (SLP)", "Per supplier: purchases by type and input VAT for the quarter",
     "Quarterly VAT attachment", "Quarterly", "C-5"),
    ("Tax withheld from suppliers", "Per payee and ATC: income payments by month and tax withheld",
     "Data for the Forms 2307 Micronet issues and the QAP", "Quarterly", "C-6"),
    ("Tax withheld by clients", "Per client and ATC: income payments and creditable tax withheld",
     "Data for the SAWT; checks the Forms 2307 received", "Quarterly", "C-7"),
    ("Tax return worksheets", "Figures for BIR Forms 2550Q, 0619E, 1601EQ, 1604E, 1702Q, 1702RT "
     "(and others per the tax profile), QAP, SAWT, with due dates",
     "Preparing the returns from the books", "Per filing period", "C-8 (2550Q)"),
    ("History (audit trail)", "Every action: date and time, user, computer, record, before / after values",
     "Accountability for all entries, changes and voids", "On demand", "D-1"),
    ("Year-end archive", "ZIP of every book and report (PDF and CSV) for a year, with SHA-256 checksums "
     "and the journal integrity result", "Annual submission / keeping of computerized books",
     "Yearly", "-"),
    ("EIS e-invoice JSON", "Each issued invoice in the BIR EIS v2.1 format, validated before export",
     "Electronic invoicing (when enrolled in the EIS)", "Each invoice", "-"),
]


def build(conn: sqlite3.Connection, cfg: Config) -> bytes:
    """The whole document: the description, then the facsimiles as annexes."""
    facsimiles = sample_facsimiles(cfg)
    body = _body(conn, cfg, facsimiles)
    return _assemble(body, facsimiles, cfg)


def _body(conn, cfg: Config, facsimiles: list[Facsimile]) -> bytes:
    s = cfg.seller
    now = datetime.now(PH_TZ)
    ptu = "not issued yet" if "PTU_NUM" in cfg.placeholders else cfg.ptu_num
    schema = conn.execute("PRAGMA user_version").fetchone()[0]
    triggers = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0]
    idle = 30
    story = []

    # Cover
    story += [Spacer(0, 1.2 * inch), _p(TITLE, S_TITLE), Spacer(0, 6),
              _p("Computerized Accounting System (CAS)", S_SUB), Spacer(0, 0.5 * inch)]
    story.append(_table([
        ["Taxpayer", _esc(s.reg_nm)],
        ["TIN", s.tin_display],
        ["Registered address", _esc(s.reg_addr)],
        ["Application", f"{APP_NAME} version {__version__}"],
        ["System type", "Computerized Accounting System with electronic invoicing (CAS / EIS-ready)"],
        ["PTU / Acknowledgment Certificate No.", ptu],
        ["Document prepared", f"{now:%B %d, %Y}".replace(" 0", " ")],
    ], [2.3, 4.6], header=False))
    story += [Spacer(0, 0.4 * inch), _p("Contents", S_H2)]
    story += _bullets(["1. General information: application, software and hardware",
                       "2. Database architecture", "3. System functions and features",
                       "4. Process and data flows", "5. Controls and data security",
                       "6. Backup procedures and disaster recovery", "7. Reports generated by the system",
                       "8. Certification",
                       "Annexes A to D: facsimiles of invoices, books of accounts, reports and audit trail"])
    story.append(PageBreak())

    # 1
    story += [_p("1. General information", S_H1),
              _p(f"<b>{APP_NAME}</b> is the accounting and invoicing system of {_esc(s.reg_nm).rstrip('.')}. It issues "
                 "sequentially numbered Service Invoices, keeps the books of accounts automatically by "
                 "double entry, and prints the books, financial statements and BIR reports. It also "
                 "prepares each invoice in the BIR Electronic Invoicing System (EIS) v2.1 format so it can "
                 "be transmitted once the company is enrolled."),
              _p("It is an in-house system that runs on the company's Windows computers. The program and "
                 "its database are on the computer itself; no internet connection is needed except for "
                 "optional e-mailing of invoices and EIS transmission. The screens are served only to the "
                 "same computer (address 127.0.0.1) and open in the app's own window."),
              _p("Software used", S_H2), _table(_software_rows(conn), [1.45, 2.65, 2.8]),
              _p("Hardware (minimum)", S_H2),
              _table([["Item", "Minimum"], ["Computer", "Laptop or desktop able to run Windows 10 / 11, 64-bit"],
                      ["Memory / storage", "4 GB RAM; 1 GB free disk space for the app, its data and backups"],
                      ["Printer", "Any printer for letter-size (8.5 x 11 in) paper"],
                      ["Users", "Each person signs in with their own account (currently 2 users)"]],
                     [1.6, 5.3])]

    # 2
    story += [_p("2. Database architecture", S_H1),
              _p(f"All data is in one SQLite {sqlite3.sqlite_version} database file, <b>data/einvoice.db</b>, "
                 "in the program folder. SQLite is a self-contained relational database built into the "
                 "program; there is no separate database server. The file uses write-ahead logging (WAL), "
                 "foreign keys are enforced, and amounts are stored as exact decimal text (no rounding "
                 f"errors). The schema is upgraded in place by numbered migrations (current version {schema})."),
              _p(f"Integrity is enforced in the database itself by {triggers} triggers: issued invoice lines and "
                 "posted journal entries can't be changed, and no business record (invoices, journal entries, "
                 "bills, payments, collections, clients, suppliers, accounts, users, history) can be deleted. "
                 "Mistakes are corrected by voiding or by reversing entries, which leaves the original visible."),
              _p("Tables", S_H2), _table(_tables_rows(conn), [1.6, 5.3])]

    # 3
    story += [_p("3. System functions and features", S_H1),
              _table([["Module", "Functions"],
                      ["Invoicing", "Invoice entry (JPY with exchange rate, converted to PHP); sale type VATable / "
                       "zero-rated / VAT-exempt; withholding tax; review page with EIS checks; issue with the "
                       "next control number; PDF print; e-mail to the client; void with reason"],
                      ["Electronic invoicing (EIS)", "EIS v2.1 JSON per invoice, validated against BIR's rules; "
                       "signing, encryption and transmission to the EIS (used once enrolled)"],
                      ["Receivables", "Collections against invoices; creditable tax withheld (Form 2307 received); "
                       "foreign exchange gain or loss computed"],
                      ["Purchases and payables", "Supplier bills by purchase type with input VAT; payments with "
                       "expanded withholding tax (ATC, base, rate)"],
                      ["General ledger", "Automatic postings to the five journals; manual journal entries; "
                       "opening balances; reversing entries; closing of periods"],
                      ["Chart of accounts", "Company chart with statement sections; accounts deactivated, never deleted"],
                      ["Reports", "Journals, ledgers, trial balance, financial statements, SLS / SLP, "
                       "withholding tax reports, tax return worksheets, year-end archive (Section 7)"],
                      ["Users and history", "Individual sign-in accounts; History (audit trail) of every action"],
                      ["Signatures", "Optional: a user's saved signature placed on any PDF; signed copies kept"],
                      ["Backups and checks", "Daily checked backups with a second copy; start-up integrity checks"]],
                     [1.6, 5.3])]

    # 4
    story += [_p("4. Process and data flows", S_H1), _p("4.1 Overall data flow", S_H2),
              data_flow_diagram(), Spacer(0, 8), _p("4.2 Service Invoice life cycle", S_H2),
              invoice_flow_diagram(), Spacer(0, 8),
              KeepTogether([_p("4.3 Automatic postings", S_H2),
                            _table([["Transaction", "Book", "Entry"],
                                    ["Invoice issued", "SJ", "Dr Accounts receivable (total incl. VAT) / "
                                     "Cr Service income, Cr Output VAT"],
                                    ["Invoice voided", "GJ", "Reverses the Sales Journal entry"],
                                    ["Collection", "CRJ", "Dr Cash in bank, Dr Creditable withholding tax, "
                                     "Dr/Cr Forex loss/gain / Cr Accounts receivable"],
                                    ["Supplier bill", "PJ", "Dr Expense, Dr Input VAT / Cr Accounts payable"],
                                    ["Payment", "CDJ", "Dr Accounts payable / Cr Cash in bank, "
                                     "Cr Withholding tax payable"],
                                    ["Manual entry", "GJ / CRJ / CDJ", "As entered (must balance); book "
                                     "chosen by the cash movement"]],
                                   [1.3, 0.9, 4.7])]),
              _p("Every journal entry is numbered per book without gaps, carries its source document "
                 "reference, and must balance (total debits = total credits) before it is saved.", S_SMALL)]

    # 5
    story += [_p("5. Controls and data security", S_H1)]
    story += _bullets([
        "<b>User access.</b> Each person has their own account. Passwords are stored only as salted hashes "
        f"(Werkzeug, scrypt). After {5} wrong passwords an account is locked for 15 minutes. A user who is "
        f"idle for {idle} minutes is signed out. Accounts are deactivated, never deleted.",
        "<b>Audit trail.</b> The History records every action (issue, void, posting, collection, bill, "
        "payment, change of a supplier or account, sign-in and sign-out, printing and exporting, signing) "
        "with date and time, user, computer and Windows account, and the values before and after a change. "
        "Each row is chained to the previous one by a SHA-256 hash, so a row changed outside the app is "
        "detected. History rows can't be changed or deleted.",
        "<b>Invoice numbers.</b> Control numbers are assigned by the system in sequence when an invoice "
        "is issued and are never reused. A voided invoice keeps its number and stays on file, marked void, "
        "with the reason, user and time.",
        "<b>Books.</b> Posted journal entries can't be changed or deleted; corrections are reversing "
        "entries. Journal entries are also chained by SHA-256. Once the books are closed through a date, "
        "nothing can be posted on or before it.",
        "<b>Start-up checks.</b> Each time the app starts it runs the database integrity check, verifies "
        "the journal and history chains, and makes the daily backup. Any problem is shown at the top of "
        "every page and written to the error log (data/logs/app.log).",
        "<b>Data location.</b> The database stays on the company computer, protected by the Windows "
        "sign-in. Turning on Windows device encryption (BitLocker) is recommended. E-mail is sent over an "
        "encrypted connection (STARTTLS or SSL); EIS transmissions are encrypted as BIR specifies.",
        "<b>Signatures.</b> A user's saved signature can be used only by that user; each use is in the "
        "History and the signed copy is kept unchanged with its SHA-256 fingerprint. Signing is optional: "
        "any PDF can be printed unsigned for a handwritten signature or notarization.",
    ])

    # 6
    story += [_p("6. Backup procedures and disaster recovery", S_H1), _p("6.1 Backups", S_H2)]
    story += _bullets([
        "<b>Daily, automatic.</b> On the first start of each day the app copies the database with SQLite's "
        "online backup function (a consistent copy even while in use) to data/backups/einvoice-YYYYMMDD.db.",
        "<b>Checked.</b> Each copy is opened and must pass SQLite's integrity check; a bad copy is set aside "
        "and the problem is shown on screen.",
        "<b>Second place.</b> The same copy is also saved to a second folder, by default the company "
        "OneDrive folder \"Micronet e-Invoice backups\", so a lost or broken computer does not lose the books.",
        f"<b>Kept.</b> The last {backup.KEEP_DAILY} daily copies are kept in each place.",
        "<b>On demand.</b> Any user can download a backup at any time (My account &gt; Download a backup).",
        "<b>Year-end.</b> The year-end archive (all books and reports, PDF and CSV, with checksums) is "
        "kept with the company's records.",
    ])
    story += [_p("6.2 Disaster recovery", S_H2),
              _table([["Event", "Recovery"],
                      ["Database damaged (fails the start-up check)",
                       "Restore the latest backup that passed its check (steps below). Re-enter documents "
                       "dated after that backup from the source documents."],
                      ["Computer lost, stolen or broken",
                       "Install the app on another Windows computer and restore the latest backup from the "
                       "OneDrive folder."],
                      ["Wrong entry or invoice", "No restore needed: void the invoice or post a reversing entry."],
                      ["Records changed outside the app",
                       "Detected by the start-up check (hash chains); restore the last good backup and "
                       "investigate using the History."],
                      ["Power failure during work",
                       "SQLite's transactions keep the database consistent: an entry is either saved "
                       "completely or not at all."]],
                     [2.1, 4.8]),
              _p("Restoring a backup", S_H2)]
    story += _bullets([
        "Close the app (all of its windows).",
        "In the program's data folder, rename einvoice.db to einvoice-damaged.db (keep it).",
        "Copy the chosen backup (einvoice-YYYYMMDD.db) from data/backups or the OneDrive folder into the "
        "data folder and rename it einvoice.db.",
        "Start the app. The start-up checks confirm the restored database; the History shows the last "
        "actions it contains.",
    ])
    story.append(_p("At most one day of work is at risk (the time since the last daily backup); a backup "
                    "can also be downloaded at any time, for example after a busy day.", S_SMALL))

    # 7
    story += [_p("7. Reports generated by the system", S_H1),
              _p("Every report can be shown on screen, printed to PDF and, except the invoice, exported to "
                 "CSV. Each printed page carries the company name, TIN, address, report title, period, the "
                 "PTU / Acknowledgment Certificate number, the date and time printed, the user who printed "
                 "it, and page x of y. Sample layouts are in the annexes."),
              _table([["Report", "Description", "Purpose", "When", "Sample"]] +
                     [[n, d, p, w, a] for n, d, p, w, a in REPORT_LIST],
                     [1.25, 2.45, 1.6, 0.85, 0.75])]
    story += [Spacer(0, 6), _p("Official receipts: under RR 7-2024 (Ease of Paying Taxes Act) the invoice is "
                               "the primary document for sales of services, so the system issues Service "
                               "Invoices and records collections in the Cash Receipts Journal against them; "
                               "it does not issue official receipts.", S_SMALL)]

    # 8
    story += [PageBreak(), _p("8. Certification", S_H1),
              _p(f"We certify that the information in this document describes the computerized accounting "
                 f"system used by {_esc(s.reg_nm)}, and that the facsimiles in the annexes were printed by the "
                 "system from sample data."),
              Spacer(0, 0.5 * inch)]
    sig = Table([["Prepared by:", "", "Noted / approved by:"],
                 ["", "", ""],
                 ["Signature over printed name", "", "Signature over printed name"],
                 ["Position:", "", "Position:"],
                 ["Date:", "", "Date:"]],
                colWidths=[3.0 * inch, 0.9 * inch, 3.0 * inch],
                rowHeights=[0.25 * inch, 0.75 * inch, 0.25 * inch, 0.35 * inch, 0.35 * inch])
    sig.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                             ("TEXTCOLOR", (0, 2), (-1, 2), MUTED),
                             ("LINEABOVE", (0, 2), (0, 2), 0.7, INK),
                             ("LINEABOVE", (2, 2), (2, 2), 0.7, INK),
                             ("LINEBELOW", (0, 3), (0, 4), 0.4, GRID),
                             ("LINEBELOW", (2, 3), (2, 4), 0.4, GRID),
                             ("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story += [sig, Spacer(0, 0.5 * inch),
              _p("The signatures may be written by hand on the printed document or placed with the "
                 "system's signature function.", S_SMALL),
              PageBreak(), _p("Annexes: facsimiles", S_H1),
              _p("Printed by the system from a separate database of sample transactions (fictitious "
                 "clients and suppliers); the company's own records are not included. While BIR's "
                 "PTU / Acknowledgment Certificate number is not yet issued, printed pages carry a "
                 "\"PROTOTYPE\" watermark."),
              _table([["Annex", "Document"]] + [[f.annex, _esc(f.title)] for f in facsimiles], [0.8, 6.1])]

    buf = BytesIO()

    def decorate(c, doc):
        c.saveState()
        c.setFont("Helvetica", 7.5)
        c.setFillColor(MUTED)
        if doc.page > 1:
            c.drawString(MARGIN, LETTER[1] - 0.5 * inch, f"{s.reg_nm} · {TITLE}")
            c.drawRightString(LETTER[0] - MARGIN, LETTER[1] - 0.5 * inch, f"{APP_NAME} {__version__}")
        c.restoreState()

    SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=MARGIN, rightMargin=MARGIN,
                      topMargin=MARGIN, bottomMargin=MARGIN, title=TITLE, author=s.reg_nm,
                      subject=f"{APP_NAME} {__version__}").build(
        story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


def _assemble(body: bytes, facsimiles: list[Facsimile], cfg: Config) -> bytes:
    """Body + annex pages, each annex page labelled, all pages numbered."""
    doc = pymupdf.open(stream=body, filetype="pdf")
    body_pages = doc.page_count
    labels: list[str] = []
    for f in facsimiles:
        with pymupdf.open(stream=f.pdf, filetype="pdf") as part:
            doc.insert_pdf(part)
            for i in range(part.page_count):
                labels.append(f"ANNEX {f.annex} · FACSIMILE, SAMPLE DATA"
                              + (f" · page {i + 1} of {part.page_count}" if part.page_count > 1 else ""))
    total = doc.page_count
    for n, page in enumerate(doc):
        r = page.rect
        if n < body_pages:
            text = f"Page {n + 1} of {total}"
            w = pymupdf.get_text_length(text, fontname="helv", fontsize=7.5)
            page.insert_text(((r.width - w) / 2, r.height - 0.45 * 72), text, fontsize=7.5,
                             fontname="helv", color=(0.33, 0.33, 0.33))
        else:
            label = labels[n - body_pages]
            w = pymupdf.get_text_length(label, fontname="hebo", fontsize=7)
            box = pymupdf.Rect(r.width - w - 26, 5, r.width - 10, 19)
            page.draw_rect(box, color=(0.78, 0.06, 0.18), fill=(1, 1, 1), width=0.8)
            page.insert_text((box.x0 + 8, box.y1 - 4.2), label, fontsize=7, fontname="hebo",
                             color=(0.78, 0.06, 0.18))
            text = f"Page {n + 1} of {total}"
            page.insert_text((14, 16), text, fontsize=7, fontname="helv", color=(0.33, 0.33, 0.33))
    doc.set_metadata({"title": f"{TITLE} - {cfg.seller.reg_nm}", "author": cfg.seller.reg_nm,
                      "subject": f"{APP_NAME} {__version__}", "creator": APP_NAME,
                      "producer": APP_NAME, "creationDate": pymupdf.get_pdf_now(),
                      "modDate": pymupdf.get_pdf_now()})
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out

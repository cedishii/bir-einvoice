"""Tax return worksheets, computed from the books, and the filing calendar.

A worksheet gives the figures needed to fill in a BIR return. It is not the
official form: it does not copy the form's layout or line numbers, and it is
never filed from here (this app does not connect to BIR).

Which forms apply depends on the tax settings (corporation or individual,
VAT-registered or not). The others stay available, marked as not applicable,
in case the setup changes. Forms BIR has abolished are not offered: 2550M
(monthly VAT, no longer filed from 2023) and the monthly 1601E (replaced by
0619E and the quarterly 1601EQ).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from .. import money
from ..models import VAT_EXEMPT, VATABLE, ZERO_RATED, today_ph
from ..money import ZERO, round2
from . import ledger
from .reports import Column, Report, Row, _month_end, quarter_of, tin_text

HUNDRED = Decimal(100)

# --- Settings ----------------------------------------------------------------

TAX_DEFAULTS = {
    "taxpayer": "corporation",        # corporation / individual
    "vat_registered": "yes",          # yes / no
    "corp_rate": "25",                # regular corporate income tax, %
    "mcit_rate": "2",                 # minimum corporate income tax, % of gross income
    "mcit_applies": "yes",            # from the 4th taxable year after operations began
    "individual_method": "itemized",  # itemized / osd / eight
    "percentage_rate": "3",           # percentage tax (2551Q), %
}
CHOICES = {
    "taxpayer": {"corporation": "Corporation", "individual": "Individual (sole proprietor / professional)"},
    "vat_registered": {"yes": "VAT-registered", "no": "Non-VAT (percentage tax)"},
    "mcit_applies": {"yes": "Yes (4th taxable year of operations or later)", "no": "No"},
    "individual_method": {
        "itemized": "Graduated rates, itemized deductions",
        "osd": "Graduated rates, optional standard deduction (40% of gross sales)",
        "eight": "8% on gross sales and other income over PHP 250,000",
    },
}
RATE_KEYS = ("corp_rate", "mcit_rate", "percentage_rate")


def tax_settings(conn: sqlite3.Connection) -> dict[str, str]:
    values = dict(TAX_DEFAULTS)
    for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'tax.%'"):
        values[r["key"][4:]] = r["value"]
    return values


def save_tax_settings(conn: sqlite3.Connection, new: dict[str, str]) -> None:
    current = tax_settings(conn)
    changes = {}
    for key in TAX_DEFAULTS:
        value = str(new.get(key, current[key])).strip()
        if key in CHOICES and value not in CHOICES[key]:
            raise ledger.BooksError(f"Choose a value for {key.replace('_', ' ')}.")
        if key in RATE_KEYS:
            try:
                rate = money.parse_decimal(value, field="Rate", max_decimals=2, positive=False)
            except money.MoneyError as e:
                raise ledger.BooksError(str(e)) from None
            if not 0 <= rate <= 100:
                raise ledger.BooksError("Rates are percentages from 0 to 100.")
            value = str(rate)
        if value != current[key]:
            changes[key] = value
    if new.get("individual_method", current["individual_method"]) == "eight" \
            and new.get("vat_registered", current["vat_registered"]) == "yes":
        raise ledger.BooksError("The 8% option is only for non-VAT taxpayers.")
    for key, value in changes.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) "
                     "DO UPDATE SET value = excluded.value", (f"tax.{key}", value))
        ledger.audit(conn, "tax settings", key, entity="settings", entity_id=f"tax.{key}",
                     changes={key: [current[key], value]})


def _pct(settings: dict, key: str) -> Decimal:
    return Decimal(settings[key]) / HUNDRED


# --- Periods and due dates -----------------------------------------------------

def month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    return start, _month_end(start, 0)


def quarter_range(year: int, q: int) -> tuple[date, date]:
    return quarter_of(date(year, 3 * q - 2, 1))


def year_range(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def next_working_day(d: date) -> date:
    """Saturday / Sunday deadlines move to Monday. Holidays are not known here."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _days_after(n: int) -> Callable[[date, date], date]:
    return lambda start, end: end + timedelta(days=n)


def _tenth_of_next_month(start, end):
    return end + timedelta(days=10)


def _end_of_next_month(start, end):
    return _month_end(end + timedelta(days=1), 0)


def _april_15(start, end):
    return date(end.year + 1, 4, 15)


def _march_1(start, end):
    return date(end.year + 1, 3, 1)


def _with_income_tax(start, end):
    """The SAWT goes with the income tax return that claims the credit."""
    return date(end.year + 1, 4, 15) if end.month == 12 else end + timedelta(days=60)


def _individual_quarterly(start, end):
    return {3: date(end.year, 5, 15), 6: date(end.year, 8, 15),
            9: date(end.year, 11, 15)}[end.month]


# --- Figures from the books ----------------------------------------------------

def sales_totals(conn, start: date, end: date) -> dict[str, Decimal]:
    """Issued (not voided) invoices by sale type, net of VAT, with output VAT."""
    out = {VATABLE: ZERO, ZERO_RATED: ZERO, VAT_EXEMPT: ZERO, "vat": ZERO}
    for r in conn.execute("SELECT trans_class, total_php FROM invoices WHERE status = 'issued' "
                          "AND issue_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        net = Decimal(r["total_php"])
        out[r["trans_class"]] += net
        if r["trans_class"] == VATABLE:
            out["vat"] += money.vat_of(net)
    return out


def purchase_totals(conn, start: date, end: date) -> dict[str, Decimal]:
    out = {k: ZERO for k in ("services", "capital", "goods", "zero", "exempt", "vat")}
    for r in conn.execute("SELECT category, net, vat FROM bills WHERE status = 'posted' "
                          "AND bill_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        out[r["category"]] += Decimal(r["net"])
        out["vat"] += Decimal(r["vat"])
    return out


def _withheld(conn, kind: str, start: date, end: date) -> list[sqlite3.Row]:
    if kind == "issued":
        sql = """SELECT s.id AS pid, s.name, s.tin, s.branch, p.pay_date AS d, p.atc,
                        p.ewt_rate AS rate, p.ewt_base AS base, p.ewt AS tax
                 FROM payments p JOIN suppliers s ON s.id = p.supplier_id
                 WHERE p.status = 'posted' AND CAST(p.ewt AS REAL) > 0
                   AND p.pay_date BETWEEN ? AND ?"""
    else:
        sql = """SELECT c.id AS pid, c.name, c.tin, c.branch, k.coll_date AS d, k.atc,
                        k.cwt_rate AS rate, k.cwt_base AS base, k.cwt AS tax
                 FROM collections k JOIN customers c ON c.id = k.customer_id
                 WHERE k.status = 'posted' AND CAST(k.cwt AS REAL) > 0
                   AND k.coll_date BETWEEN ? AND ?"""
    return conn.execute(sql + " ORDER BY name, atc", (start.isoformat(), end.isoformat())).fetchall()


def _total(rows, key) -> Decimal:
    return sum((Decimal(r[key]) for r in rows), ZERO)


def income_statement(conn, start: date, end: date) -> dict[str, Decimal]:
    """Income and expense account movements in a period.

    revenue: the sales account; other_income: other income accounts;
    cost: expense accounts marked "cost of sales / services"; expenses: the rest."""
    out = {"revenue": ZERO, "other_income": ZERO, "cost": ZERO, "expenses": ZERO}
    rows = conn.execute(
        """SELECT a.type, a.role, a.is_cost, l.debit, l.credit FROM journal_lines l
           JOIN journal_entries e ON e.id = l.entry_id JOIN accounts a ON a.code = l.account_code
           WHERE a.type IN ('income', 'expense') AND e.entry_date BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat()))
    for r in rows:
        net = Decimal(r["debit"]) - Decimal(r["credit"])
        if r["type"] == "income":
            out["revenue" if r["role"] == "sales" else "other_income"] -= net
        else:
            out["cost" if r["is_cost"] else "expenses"] += net
    return out


# --- Worksheet helpers ---------------------------------------------------------

def _sheet(key: str, title: str, start: date, end: date,
           columns: list[Column] | None = None) -> Report:
    return Report(key, title, start, end,
                  columns or [Column("Item", width=4.0), Column("Amount (PHP)", "num", 1.4)])


def _line(r: Report, label: str, amount, kind: str = "") -> None:
    cells = [label] + [None] * (len(r.columns) - 2) + [amount]
    r.rows.append(Row(cells, kind))


def _group(r: Report, label: str) -> None:
    r.rows.append(Row([label], "group"))


COMMON_NOTE = ("A worksheet of the figures for the return, computed from the books. "
               "Copy them onto the official form; check them with the accountant before filing.")


# --- VAT and percentage tax ------------------------------------------------------

def _vat_position(conn, start: date, end: date) -> tuple[Decimal, Decimal]:
    s, p = sales_totals(conn, start, end), purchase_totals(conn, start, end)
    return s["vat"], p["vat"]


def _vat_carryover(conn, before: date) -> Decimal:
    """Excess input VAT carried into the quarter that starts on `before`,
    assuming every earlier quarter's excess was carried over (none refunded)."""
    first = conn.execute(
        """SELECT MIN(d) FROM (SELECT MIN(issue_date) AS d FROM invoices
                               UNION ALL SELECT MIN(bill_date) FROM bills)""").fetchone()[0]
    if not first:
        return ZERO
    qs, qe = quarter_of(date.fromisoformat(first))
    carry = ZERO
    while qs < before:
        output, input_ = _vat_position(conn, qs, qe)
        net = output - input_ - carry
        carry = -net if net < 0 else ZERO
        qs, qe = quarter_of(qe + timedelta(days=1))
    return carry


def vat_2550q(conn, settings, start, end, params=None) -> Report:
    r = _sheet("2550Q", "BIR Form 2550Q worksheet - Quarterly Value-Added Tax", start, end)
    s, p = sales_totals(conn, start, end), purchase_totals(conn, start, end)
    _group(r, "Sales for the quarter (as in the Summary List of Sales)")
    _line(r, "VATable sales", s[VATABLE])
    _line(r, "Zero-rated sales", s[ZERO_RATED])
    _line(r, "Exempt sales", s[VAT_EXEMPT])
    _line(r, "Total sales", s[VATABLE] + s[ZERO_RATED] + s[VAT_EXEMPT], "total")
    _line(r, "Output VAT (12% of VATable sales)", s["vat"])
    _group(r, "Purchases for the quarter (as in the Summary List of Purchases)")
    _line(r, "Domestic purchases of services", p["services"])
    _line(r, "Domestic purchases of capital goods", p["capital"])
    _line(r, "Domestic purchases of goods other than capital goods", p["goods"])
    _line(r, "Zero-rated and exempt purchases", p["zero"] + p["exempt"])
    _line(r, "Total purchases", sum((p[k] for k in ("services", "capital", "goods", "zero",
                                                    "exempt")), ZERO), "total")
    _line(r, "Input VAT on purchases this quarter", p["vat"])
    carry = _vat_carryover(conn, start)
    _group(r, "VAT due")
    _line(r, "Output VAT", s["vat"])
    _line(r, "Less: input VAT carried over from the previous quarter", carry)
    _line(r, "Less: input VAT on purchases this quarter", p["vat"])
    net = s["vat"] - carry - p["vat"]
    if net >= 0:
        _line(r, "VAT payable", net, "total")
    else:
        _line(r, "VAT payable", ZERO, "total")
        _line(r, "Excess input VAT, carried over to the next quarter", -net)
    r.notes += [COMMON_NOTE,
                "Not in the books, so not included: VAT withheld by government payors, VAT on "
                "importations, and input VAT disallowed or claimed for refund. Add them on the form if any.",
                "The carry-over assumes each earlier quarter's excess input VAT was carried over, none refunded."]
    if settings["vat_registered"] != "yes":
        r.notes.insert(0, "The tax settings say non-VAT: this return does not apply.")
    return r


def percentage_2551q(conn, settings, start, end, params=None) -> Report:
    r = _sheet("2551Q", "BIR Form 2551Q worksheet - Quarterly Percentage Tax", start, end)
    s = sales_totals(conn, start, end)
    gross = s[VATABLE] + s[ZERO_RATED] + s[VAT_EXEMPT]
    rate = _pct(settings, "percentage_rate")
    _line(r, "Gross sales / receipts for the quarter", gross)
    _line(r, f"Percentage tax rate ({settings['percentage_rate']}%)", None)
    _line(r, "Percentage tax due", round2(gross * rate), "total")
    r.notes += [COMMON_NOTE,
                "For non-VAT taxpayers. Not due for a quarter covered by the 8% income tax option."]
    if settings["vat_registered"] == "yes":
        r.notes.insert(0, "The tax settings say VAT-registered: this return does not apply.")
    return r


# --- Withholding -----------------------------------------------------------------

def _by_atc(rows) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for x in rows:
        a = out.setdefault((x["atc"], x["rate"]), {"base": ZERO, "tax": ZERO})
        a["base"] += Decimal(x["base"])
        a["tax"] += Decimal(x["tax"])
    return out


_ATC_COLUMNS = [Column("Item", width=3.0), Column("ATC", width=0.8), Column("Rate %", "num", 0.7),
                Column("Tax base (PHP)", "num", 1.3), Column("Tax withheld (PHP)", "num", 1.3)]


def ewt_0619e(conn, settings, start, end, params=None) -> Report:
    r = _sheet("0619E", "BIR Form 0619E worksheet - Monthly Remittance of Expanded Withholding Tax",
               start, end, list(_ATC_COLUMNS))
    total = ZERO
    for (atc, rate), a in sorted(_by_atc(_withheld(conn, "issued", start, end)).items()):
        r.rows.append(Row(["Tax withheld from suppliers", atc, Decimal(rate), a["base"], a["tax"]]))
        total += a["tax"]
    _line(r, "Amount to remit for the month", total, "total")
    r.notes += [COMMON_NOTE, "Filed for the first two months of each quarter; the third month "
                "is covered by the quarterly 1601EQ."]
    return r


def ewt_1601eq(conn, settings, start, end, params=None) -> Report:
    r = _sheet("1601EQ", "BIR Form 1601EQ worksheet - Quarterly Remittance of Expanded Withholding Tax",
               start, end, list(_ATC_COLUMNS))
    total = ZERO
    for (atc, rate), a in sorted(_by_atc(_withheld(conn, "issued", start, end)).items()):
        r.rows.append(Row(["Tax withheld from suppliers", atc, Decimal(rate), a["base"], a["tax"]]))
        total += a["tax"]
    _line(r, "Total tax withheld for the quarter", total, "total")
    remitted = ZERO
    for i in range(2):
        ms, me = month_range(start.year, start.month + i)
        m_tax = _total(_withheld(conn, "issued", ms, me), "tax")
        remitted += m_tax
        _line(r, f"Less: remitted with 0619E for {ms:%B}", m_tax)
    _line(r, "Tax still due for the quarter", total - remitted, "total")
    r.notes += [COMMON_NOTE, "Attach the QAP (Quarterly Alphalist of Payees) for the same quarter.",
                "The 0619E amounts assume each month's tax withheld was remitted in full."]
    return r


def _alphalist(key, title, conn, kind, start, end, who) -> Report:
    r = Report(key, title, start, end, [
        Column("Seq", "num", 0.4), Column("TIN", width=1.3), Column(who, width=3.0),
        Column("ATC", width=0.8), Column("Income payment (PHP)", "num", 1.3),
        Column("Rate %", "num", 0.6), Column("Tax withheld (PHP)", "num", 1.3)])
    per: dict[tuple, dict] = {}
    for x in _withheld(conn, kind, start, end):
        p = per.setdefault((x["name"], x["pid"], x["atc"], x["rate"]),
                           {"row": x, "base": ZERO, "tax": ZERO})
        p["base"] += Decimal(x["base"])
        p["tax"] += Decimal(x["tax"])
    tb = tt = ZERO
    for seq, p in enumerate(per.values(), start=1):
        x = p["row"]
        tb, tt = tb + p["base"], tt + p["tax"]
        r.rows.append(Row([str(seq), tin_text(x["tin"], x["branch"]), x["name"], x["atc"],
                           p["base"], Decimal(x["rate"]), p["tax"]]))
    r.rows.append(Row(["", "", "Total", "", tb, None, tt], "total"))
    return r


def qap(conn, settings, start, end, params=None) -> Report:
    r = _alphalist("QAP", "Quarterly Alphalist of Payees (QAP) - attachment to 1601EQ",
                   conn, "issued", start, end, "Payee")
    r.notes.append("Suppliers Micronet withheld tax from in the quarter. BIR receives the QAP as a "
                   "validated .DAT file; key these figures into BIR's alphalist module.")
    return r


def sawt(conn, settings, start, end, params=None) -> Report:
    r = _alphalist("SAWT", "Summary Alphalist of Withholding Taxes (SAWT)",
                   conn, "received", start, end, "Withholding agent (client)")
    r.notes.append("Clients that withheld tax from Micronet in the period (from their Forms 2307). "
                   "Attached to the income tax return that claims the credit. BIR receives the "
                   "SAWT as a validated .DAT file; key these figures into BIR's alphalist module.")
    return r


def ewt_1604e(conn, settings, start, end, params=None) -> Report:
    r = Report("1604E", "BIR Form 1604E worksheet - Annual Information Return of Creditable "
               "Income Taxes Withheld (Expanded)", start, end, [
                   Column("Month / payee", width=3.0), Column("TIN", width=1.3),
                   Column("ATC", width=0.8), Column("Income payment (PHP)", "num", 1.3),
                   Column("Tax withheld (PHP)", "num", 1.3)])
    r.rows.append(Row(["Remittances per month"], "group"))
    tb = tt = ZERO
    for m in range(1, 13):
        rows = _withheld(conn, "issued", *month_range(start.year, m))
        b, t = _total(rows, "base"), _total(rows, "tax")
        tb, tt = tb + b, tt + t
        r.rows.append(Row([date(start.year, m, 1).strftime("%B"), "", "", b, t]))
    r.rows.append(Row(["Total for the year", "", "", tb, tt], "total"))
    r.rows.append(Row(["Alphalist of payees"], "group"))
    per: dict[tuple, dict] = {}
    for x in _withheld(conn, "issued", start, end):
        p = per.setdefault((x["name"], x["pid"], x["atc"]), {"row": x, "base": ZERO, "tax": ZERO})
        p["base"] += Decimal(x["base"])
        p["tax"] += Decimal(x["tax"])
    for p in per.values():
        x = p["row"]
        r.rows.append(Row([x["name"], tin_text(x["tin"], x["branch"]), x["atc"],
                           p["base"], p["tax"]]))
    r.notes += [COMMON_NOTE, "The alphalist goes to BIR as a validated .DAT file."]
    return r


# --- Income tax ------------------------------------------------------------------

def _cwt(conn, start, end) -> Decimal:
    return _total(_withheld(conn, "received", start, end), "tax")


def _corp_tax(conn, settings, start, end) -> dict[str, Decimal]:
    i = income_statement(conn, start, end)
    gross_income = i["revenue"] - i["cost"] + i["other_income"]
    taxable = gross_income - i["expenses"]
    rcit = round2(max(taxable, ZERO) * _pct(settings, "corp_rate"))
    mcit = (round2(max(gross_income, ZERO) * _pct(settings, "mcit_rate"))
            if settings["mcit_applies"] == "yes" else ZERO)
    return i | {"gross_income": gross_income, "taxable": taxable, "rcit": rcit, "mcit": mcit,
                "tax": max(rcit, mcit), "cwt": _cwt(conn, start, end)}


def _paid_before(conn, settings, year: int, quarters: int, compute) -> Decimal:
    """Income tax paid with the earlier quarterly returns of the year, assuming
    each was filed and paid as computed here."""
    paid = ZERO
    for q in range(1, quarters + 1):
        t = compute(conn, settings, date(year, 1, 1), quarter_range(year, q)[1])
        payable = t["tax"] - t["cwt"] - paid
        if payable > 0:
            paid += payable
    return paid


def _income_lines(r: Report, t: dict, individual_method: str | None = None) -> None:
    _group(r, "Income and deductions (from the books, cumulative for the year)")
    _line(r, "Sales / revenues (net of VAT)", t["revenue"])
    _line(r, "Less: cost of sales / services", t["cost"])
    _line(r, "Gross income from operations", t["revenue"] - t["cost"], "sub")
    _line(r, "Other income", t["other_income"])
    if individual_method == "osd":
        _line(r, "Less: optional standard deduction (40% of sales)", t["osd"])
    elif individual_method != "eight":
        _line(r, "Less: deductions (other expense accounts)", t["expenses"])
    if individual_method != "eight":
        _line(r, "Taxable income", t["taxable"], "total")


def _corp_sheet(key, title, conn, settings, start, end, prior_quarters: int) -> Report:
    year_start = date(end.year, 1, 1)
    r = _sheet(key, title, year_start, end)
    t = _corp_tax(conn, settings, year_start, end)
    _income_lines(r, t)
    _group(r, "Tax due")
    _line(r, f"Regular income tax ({settings['corp_rate']}% of taxable income)", t["rcit"])
    if settings["mcit_applies"] == "yes":
        _line(r, f"Minimum corporate income tax ({settings['mcit_rate']}% of gross income)", t["mcit"])
    _line(r, "Income tax due (the higher of the two)" if settings["mcit_applies"] == "yes"
          else "Income tax due", t["tax"], "total")
    _line(r, "Less: creditable tax withheld by clients (SAWT, Forms 2307)", t["cwt"])
    paid = _paid_before(conn, settings, end.year, prior_quarters, _corp_tax)
    _line(r, "Less: income tax paid in earlier quarters of the year", paid)
    payable = t["tax"] - t["cwt"] - paid
    _line(r, "Tax payable" if payable >= 0 else "Overpayment", abs(payable), "total")
    r.notes += [COMMON_NOTE,
                "Deductions are every expense account except those marked cost of sales / services. "
                "Book income tax itself, non-deductible expenses and closing entries so they don't "
                "reach these accounts, or adjust the figures.",
                "Not included: NOLCO, special deductions, tax relief and income subject to final tax."]
    if settings["taxpayer"] != "corporation":
        r.notes.insert(0, "The tax settings say individual: this return does not apply.")
    return r


def corp_1702q(conn, settings, start, end, params=None) -> Report:
    q = (end.month - 1) // 3 + 1
    return _corp_sheet("1702Q", f"BIR Form 1702Q worksheet - Quarterly Income Tax (Q{q}, "
                       "cumulative)", conn, settings, start, end, q - 1)


def corp_1702rt(conn, settings, start, end, params=None) -> Report:
    return _corp_sheet("1702RT", "BIR Form 1702RT worksheet - Annual Income Tax (regular rate)",
                       conn, settings, start, end, 3)


# Graduated income tax rates for individuals from 2023 (TRAIN law, second schedule).
BRACKETS = [  # (over, tax on the first part, rate on the excess)
    (Decimal("8000000"), Decimal("2202500"), Decimal("0.35")),
    (Decimal("2000000"), Decimal("402500"), Decimal("0.30")),
    (Decimal("800000"), Decimal("102500"), Decimal("0.25")),
    (Decimal("400000"), Decimal("22500"), Decimal("0.20")),
    (Decimal("250000"), ZERO, Decimal("0.15")),
]
EIGHT_PCT_ALLOWANCE = Decimal("250000")


def graduated_tax(taxable: Decimal) -> Decimal:
    for over, base, rate in BRACKETS:
        if taxable > over:
            return round2(base + (taxable - over) * rate)
    return ZERO


def _individual_tax(conn, settings, start, end) -> dict[str, Decimal]:
    i = income_statement(conn, start, end)
    method = settings["individual_method"]
    out = dict(i)
    if method == "eight":
        out["taxable"] = max(i["revenue"] + i["other_income"] - EIGHT_PCT_ALLOWANCE, ZERO)
        out["tax"] = round2(out["taxable"] * Decimal("0.08"))
    else:
        if method == "osd":
            out["osd"] = round2(i["revenue"] * Decimal("0.40"))
            out["taxable"] = i["revenue"] + i["other_income"] - out["osd"]
        else:
            out["taxable"] = i["revenue"] - i["cost"] + i["other_income"] - i["expenses"]
        out["tax"] = graduated_tax(max(out["taxable"], ZERO))
    out["cwt"] = _cwt(conn, start, end)
    return out


def _individual_sheet(key, title, conn, settings, start, end, prior_quarters: int) -> Report:
    year_start = date(end.year, 1, 1)
    r = _sheet(key, title, year_start, end)
    method = settings["individual_method"]
    t = _individual_tax(conn, settings, year_start, end)
    _income_lines(r, t, method)
    _group(r, "Tax due")
    if method == "eight":
        _line(r, "Gross sales and other income", t["revenue"] + t["other_income"])
        _line(r, "Less: PHP 250,000", EIGHT_PCT_ALLOWANCE)
        _line(r, "Income tax due (8% of the excess)", t["tax"], "total")
    else:
        _line(r, "Income tax due (graduated rates)", t["tax"], "total")
    _line(r, "Less: creditable tax withheld by clients (SAWT, Forms 2307)", t["cwt"])
    paid = _paid_before(conn, settings, end.year, prior_quarters, _individual_tax)
    _line(r, "Less: income tax paid in earlier quarters of the year", paid)
    payable = t["tax"] - t["cwt"] - paid
    _line(r, "Tax payable" if payable >= 0 else "Overpayment", abs(payable), "total")
    r.notes += [COMMON_NOTE, "Method: " + CHOICES["individual_method"][method] + ".",
                "Graduated rates are the schedule in force from 2023."]
    if method == "eight":
        r.notes.append("The 8% option is only for non-VAT taxpayers with gross sales of "
                       "PHP 3,000,000 or less.")
    if settings["taxpayer"] != "individual":
        r.notes.insert(0, "The tax settings say corporation: this return does not apply.")
    return r


def ind_1701q(conn, settings, start, end, params=None) -> Report:
    q = (end.month - 1) // 3 + 1
    return _individual_sheet("1701Q", f"BIR Form 1701Q worksheet - Quarterly Income Tax for "
                             f"Individuals (Q{q}, cumulative)", conn, settings, start, end, q - 1)


def ind_1701(conn, settings, start, end, params=None) -> Report:
    return _individual_sheet("1701", "BIR Form 1701 worksheet - Annual Income Tax for "
                             "Individuals (business / mixed income)", conn, settings, start, end, 3)


def ind_1701a(conn, settings, start, end, params=None) -> Report:
    r = _individual_sheet("1701A", "BIR Form 1701A worksheet - Annual Income Tax for "
                          "Individuals (purely business, 8% or OSD)", conn, settings, start, end, 3)
    if settings["individual_method"] == "itemized":
        r.notes.insert(0, "1701A is for the 8% option or the optional standard deduction. "
                       "With itemized deductions, file 1701.")
    return r


def ind_1700(conn, settings, start, end, params=None) -> Report:
    """Purely compensation income: the figures come from the employer's Form
    2316, not from these books, so they are typed in."""
    params = params or {}
    comp = params.get("compensation", ZERO)
    withheld = params.get("withheld", ZERO)
    r = _sheet("1700", "BIR Form 1700 worksheet - Annual Income Tax for Individuals "
               "(purely compensation income)", start, end)
    _line(r, "Taxable compensation income (from Form 2316)", comp)
    tax = graduated_tax(comp)
    _line(r, "Income tax due (graduated rates)", tax, "total")
    _line(r, "Less: tax withheld by the employer", withheld)
    payable = tax - withheld
    _line(r, "Tax payable" if payable >= 0 else "Overpayment", abs(payable), "total")
    r.notes += [COMMON_NOTE, "Not from the books: type the figures from the employee's Form 2316.",
                "Graduated rates are the schedule in force from 2023."]
    return r


# --- The forms -------------------------------------------------------------------

@dataclass
class TaxForm:
    code: str
    title: str
    period: str                       # "month", "quarter" or "year"
    build: Callable
    due: Callable[[date, date], date]
    applies: Callable[[dict], bool]
    group: str
    only: tuple[int, ...] = ()        # allowed months (0619E) or quarters (1702Q, 1701Q)
    attaches_to: str = ""
    inputs: list[tuple[str, str]] = field(default_factory=list)  # typed-in amounts


def _vat(s): return s["vat_registered"] == "yes"
def _corp(s): return s["taxpayer"] == "corporation"
def _ind(s): return s["taxpayer"] == "individual"


FORMS = [
    TaxForm("2550Q", "Quarterly VAT return", "quarter", vat_2550q, _days_after(25), _vat,
            "Business tax"),
    TaxForm("2551Q", "Quarterly percentage tax return", "quarter", percentage_2551q,
            _days_after(25), lambda s: not _vat(s), "Business tax"),
    TaxForm("0619E", "Monthly remittance of expanded withholding tax", "month", ewt_0619e,
            _tenth_of_next_month, lambda s: True, "Withholding tax",
            only=(1, 2, 4, 5, 7, 8, 10, 11)),
    TaxForm("1601EQ", "Quarterly remittance of expanded withholding tax", "quarter", ewt_1601eq,
            _end_of_next_month, lambda s: True, "Withholding tax"),
    TaxForm("QAP", "Quarterly Alphalist of Payees", "quarter", qap, _end_of_next_month,
            lambda s: True, "Withholding tax", attaches_to="1601EQ"),
    TaxForm("1604E", "Annual information return, expanded withholding", "year", ewt_1604e,
            _march_1, lambda s: True, "Withholding tax"),
    TaxForm("SAWT", "Summary Alphalist of Withholding Taxes (withheld from Micronet)", "quarter",
            sawt, _with_income_tax, lambda s: True, "Income tax", attaches_to="1702Q / 1702RT / 1701Q / 1701"),
    TaxForm("1702Q", "Quarterly corporate income tax return", "quarter", corp_1702q,
            _days_after(60), _corp, "Income tax", only=(1, 2, 3)),
    TaxForm("1702RT", "Annual corporate income tax return", "year", corp_1702rt, _april_15,
            _corp, "Income tax"),
    TaxForm("1701Q", "Quarterly income tax return, individuals", "quarter", ind_1701q,
            _individual_quarterly, _ind, "Income tax", only=(1, 2, 3)),
    TaxForm("1701", "Annual income tax return, individuals (business / mixed)", "year", ind_1701,
            _april_15, lambda s: _ind(s) and s["individual_method"] == "itemized", "Income tax"),
    TaxForm("1701A", "Annual income tax return, individuals (purely business, 8% or OSD)", "year",
            ind_1701a, _april_15, lambda s: _ind(s) and s["individual_method"] != "itemized",
            "Income tax"),
    TaxForm("1700", "Annual income tax return, individuals (purely compensation)", "year",
            ind_1700, _april_15, lambda s: False, "Income tax",
            inputs=[("compensation", "Taxable compensation income"),
                    ("withheld", "Tax withheld by the employer")]),
]
BY_CODE = {f.code: f for f in FORMS}


def period_for(form: TaxForm, year: int, n: int = 1) -> tuple[date, date]:
    if form.period == "month":
        return month_range(year, n)
    if form.period == "quarter":
        return quarter_range(year, n)
    return year_range(year)


def period_label(form: TaxForm, start: date, end: date) -> str:
    if form.period == "month":
        return f"{start:%B %Y}"
    if form.period == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return str(start.year)


def periods(form: TaxForm, year: int) -> list[int]:
    n = {"month": 12, "quarter": 4, "year": 1}[form.period]
    return [i for i in range(1, n + 1) if not form.only or i in form.only]


def worksheet(conn, form: TaxForm, year: int, n: int, params=None) -> Report:
    if n not in periods(form, year):
        raise ledger.BooksError(f"{form.code} is not filed for that period.")
    start, end = period_for(form, year, n)
    r = form.build(conn, tax_settings(conn), start, end, params)
    r.key = f"{form.code}-{period_label(form, start, end).replace(' ', '')}"
    if end >= today_ph():
        r.notes.insert(0, "The period is not over yet: the figures will change.")
    return r


# --- Calendar ------------------------------------------------------------------

@dataclass
class Deadline:
    form: TaxForm | None
    code: str
    label: str
    period: str
    start: date
    end: date
    due: date
    status: str          # "open", "ready", "past"
    applies: bool
    n: int = 0
    link: tuple = ()     # for non-form items: (endpoint, kwargs)


def calendar(conn, *, around: date | None = None, show_all: bool = False,
             back_days: int = 45, ahead_days: int = 120) -> list[Deadline]:
    """Due dates from `back_days` ago to `ahead_days` ahead."""
    today = around or today_ph()
    lo, hi = today - timedelta(days=back_days), today + timedelta(days=ahead_days)
    settings = tax_settings(conn)
    out = []
    for year in (today.year - 1, today.year):
        for form in FORMS:
            ok = form.applies(settings)
            if not ok and not show_all:
                continue
            for n in periods(form, year):
                start, end = period_for(form, year, n)
                due = next_working_day(form.due(start, end))
                if lo <= due <= hi:
                    out.append(Deadline(form, form.code, form.title, period_label(form, start, end),
                                        start, end, due, _status(today, end, due), ok, n))
        for q in range(1, 5):  # the summary lists go with the quarterly VAT return
            start, end = quarter_range(year, q)
            due = next_working_day(end + timedelta(days=25))
            if lo <= due <= hi and (_vat(settings) or show_all):
                for key, name in (("sls", "Summary List of Sales"), ("slp", "Summary List of Purchases")):
                    out.append(Deadline(None, key.upper(), name, f"Q{q} {year}", start, end, due,
                                        _status(today, end, due), _vat(settings),
                                        link=("books.report", {"key": key, "from": start, "to": end})))
        due = next_working_day(date(year + 1, 1, 30))
        if lo <= due <= hi:
            out.append(Deadline(None, "Books", "Computerized books of accounts (yearly submission)",
                                str(year), *year_range(year), due,
                                _status(today, date(year, 12, 31), due), True,
                                link=("books.archive", {"year": year})))
    return sorted(out, key=lambda d: (d.due, d.code))


def _status(today: date, end: date, due: date) -> str:
    if today <= end:
        return "open"
    return "past" if due < today else "ready"

"""Invoice data as the user enters it, plus the PHP amounts derived from it.

Saved invoices are never edited: a mistake is fixed by voiding the invoice
and issuing a new one with a new control number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import money

# Philippine time (UTC+8, no daylight saving) decides what "today" is.
PH_TZ = timezone(timedelta(hours=8))

# Max lengths from the EIS field table (docs/EIS_SPEC_NOTES.md section 2),
# checked at entry so every issued invoice can later be exported.
MAX_LEN = {
    "buyer_name": 200,      # BuyerInfo.RegNm
    "buyer_address": 300,   # BuyerInfo.RegAddr
    "buyer_email": 100,     # BuyerInfo.Email
    "business_style": 200,  # printed only
    "item_name": 100,       # ItemList.Nm
    "description": 100,     # ItemList.Desc
    "unit": 50,             # ItemList.Unit
    "rate_source": 200,     # stored and printed only
}

# Sale types the user picks from (EIS TransClass codes, docs/EIS_SPEC_NOTES.md
# section 1). 04 is for Registered Business Enterprises only, which Micronet
# is not, so it is never offered.
VATABLE, ZERO_RATED, VAT_EXEMPT = "01", "02", "03"
SALE_TYPES = {
    VATABLE: "VATable (12%)",
    ZERO_RATED: "VAT Zero-Rated (0%)",
    VAT_EXEMPT: "VAT-Exempt",
}

DEFAULT_ITEM_NAME = "IT Services"
DEFAULT_UNIT = "LOT"
NO_TIN = "000000000"     # buyer not registered with BIR
DEFAULT_BRANCH = "00000"
MAX_LINES = 20


def today_ph() -> date:
    return datetime.now(PH_TZ).date()


@dataclass
class InvoiceLine:
    item_name: str
    description: str
    qty: Decimal
    unit: str
    unit_cost_jpy: Decimal
    # Derived (filled by Invoice.compute)
    amount_jpy: Decimal = money.ZERO
    unit_cost_php: Decimal = money.ZERO
    sales_php: Decimal = money.ZERO


@dataclass
class Invoice:
    issue_date: date
    buyer_name: str
    buyer_tin: str            # 9 digits; "000000000" when the client has no PH TIN
    buyer_branch: str         # 5 digits
    buyer_address: str
    business_style: str
    rate: Decimal             # PHP per 1 JPY, as entered
    rate_date: date
    rate_source: str
    lines: list[InvoiceLine]
    buyer_email: str = ""     # where the invoice PDF is e-mailed; BuyerInfo.Email
    trans_class: str = ZERO_RATED  # sale type, a SALE_TYPES key; EIS TransClass
    control_no: str = ""      # assigned when the invoice is issued
    status: str = "draft"     # draft / issued / void
    eis_key: str = ""         # last 8 chars of the EisUniqueId, fixed at issue
    void_reason: str = ""
    created_at: str = ""
    voided_at: str = ""
    id: int | None = None

    # Totals (filled by compute)
    total_jpy: Decimal = money.ZERO
    total_php: Decimal = money.ZERO

    def compute(self) -> "Invoice":
        for line in self.lines:
            amounts = money.line_amounts(line.qty, line.unit_cost_jpy, self.rate)
            line.amount_jpy = amounts["amount_jpy"]
            line.unit_cost_php = amounts["unit_cost_php"]
            line.sales_php = amounts["sales_php"]
        self.total_jpy = sum((l.amount_jpy for l in self.lines), money.ZERO)
        self.total_php = sum((l.sales_php for l in self.lines), money.ZERO)
        return self

    @property
    def is_vatable(self) -> bool:
        return self.trans_class == VATABLE

    @property
    def sale_type(self) -> str:
        return SALE_TYPES.get(self.trans_class, self.trans_class)

    # Line amounts are net of VAT. VATable sales add 12% on top, worked out on
    # the invoice total (PHP for BIR, JPY for what the client pays).
    @property
    def vat_php(self) -> Decimal:
        return money.vat_of(self.total_php) if self.is_vatable else money.ZERO

    @property
    def vat_jpy(self) -> Decimal:
        return money.vat_of(self.total_jpy) if self.is_vatable else money.ZERO

    @property
    def total_due_php(self) -> Decimal:
        return self.total_php + self.vat_php

    @property
    def total_due_jpy(self) -> Decimal:
        return self.total_jpy + self.vat_jpy

    @property
    def has_tin(self) -> bool:
        return self.buyer_tin != NO_TIN

    @property
    def buyer_tin_display(self) -> str:
        if not self.has_tin:
            return "N/A (not registered with BIR)"
        t = self.buyer_tin
        return f"{t[0:3]}-{t[3:6]}-{t[6:9]}-{self.buyer_branch}"


class FormErrors(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def is_email(text: str) -> bool:
    return bool(_EMAIL_RE.match(text or ""))


_TIN_RE = re.compile(r"^(\d{9})(\d{3}|\d{5})?$")


def parse_tin(text: str) -> tuple[str, str]:
    """'' -> ('000000000', '00000'); '123-456-789-00101' -> ('123456789', '00101').

    A 3-digit branch code is padded to 5 digits ("101" -> "00101").
    """
    digits = re.sub(r"[\s-]", "", text or "")
    if not digits:
        return NO_TIN, DEFAULT_BRANCH
    m = _TIN_RE.match(digits)
    if not m:
        raise ValueError("TIN must be 9 digits, optionally followed by a 3- or "
                         "5-digit branch code (e.g. 123-456-789-00000). "
                         "Leave it blank if the client has no Philippine TIN.")
    return m.group(1), (m.group(2) or DEFAULT_BRANCH).zfill(5)


def _parse_date(text: str, label: str, errors: dict, key: str) -> date | None:
    try:
        d = date.fromisoformat((text or "").strip())
    except ValueError:
        errors[key] = f"{label} is required (YYYY-MM-DD)."
        return None
    if d > today_ph():
        errors[key] = f"{label} cannot be in the future."
    return d


def _text(form: dict, key: str, label: str, errors: dict, required=True) -> str:
    value = " ".join(str(form.get(key, "")).split())  # trim + collapse spaces
    if required and not value:
        errors[key] = f"{label} is required."
    elif key in MAX_LEN and len(value) > MAX_LEN[key]:
        errors[key] = f"{label} is limited to {MAX_LEN[key]} characters."
    return value


def invoice_from_form(form: dict) -> Invoice:
    """Build and validate an Invoice from submitted form fields.

    Line fields are lists: item_name[], description[], qty[], unit[],
    unit_cost_jpy[]. Completely blank lines are ignored.
    """
    errors: dict[str, str] = {}
    issue_date = _parse_date(form.get("issue_date"), "Invoice date", errors, "issue_date")
    buyer_name = _text(form, "buyer_name", "Client name", errors)
    buyer_address = _text(form, "buyer_address", "Client address", errors)
    business_style = _text(form, "business_style", "Business style", errors, required=False)
    buyer_email = _text(form, "buyer_email", "Client e-mail", errors, required=False)
    if buyer_email and "buyer_email" not in errors and not is_email(buyer_email):
        errors["buyer_email"] = "Client e-mail is not a valid address (one address only)."
    trans_class = str(form.get("trans_class", "")).strip()
    if trans_class not in SALE_TYPES:
        errors["trans_class"] = "Choose the sale type (VATable, zero-rated or VAT-exempt)."
    rate_source = _text(form, "rate_source", "Rate source", errors)
    rate_date = _parse_date(form.get("rate_date"), "Rate date", errors, "rate_date")

    try:
        buyer_tin, buyer_branch = parse_tin(form.get("buyer_tin", ""))
    except ValueError as e:
        errors["buyer_tin"] = str(e)
        buyer_tin, buyer_branch = NO_TIN, DEFAULT_BRANCH

    rate = None
    try:
        rate = money.parse_rate(form.get("rate", ""))
    except money.MoneyError as e:
        errors["rate"] = str(e)

    lines: list[InvoiceLine] = []
    columns = ["item_name", "description", "qty", "unit", "unit_cost_jpy"]
    rows = zip(*(form.get(c + "[]", []) for c in columns))
    for i, (name, desc, qty, unit, cost) in enumerate(rows, start=1):
        name, desc, unit = (" ".join(str(v).split()) for v in (name, desc, unit))
        if not (desc or str(cost).strip()):
            continue  # blank row (item name/qty/unit have defaults)
        key = f"line{i}"
        problems = []
        if not name:
            problems.append("item name is required")
        elif len(name) > MAX_LEN["item_name"]:
            problems.append(f"item name is limited to {MAX_LEN['item_name']} characters")
        if not desc:
            problems.append("description is required")
        elif len(desc) > MAX_LEN["description"]:
            problems.append(f"description is limited to {MAX_LEN['description']} characters")
        if len(unit) > MAX_LEN["unit"]:
            problems.append(f"unit is limited to {MAX_LEN['unit']} characters")
        try:
            q = money.parse_qty(qty or "1")
        except money.MoneyError as e:
            problems.append(str(e).lower())
            q = None
        try:
            c = money.parse_jpy(cost, field="Unit price (JPY)")
        except money.MoneyError as e:
            problems.append(str(e).lower())
            c = None
        if problems:
            errors[key] = f"Line {i}: " + "; ".join(problems) + "."
        else:
            lines.append(InvoiceLine(name, desc, q, unit, c))

    if not lines and not any(k.startswith("line") for k in errors):
        errors["lines"] = "Add at least one line with a description and a JPY amount."
    if len(lines) > MAX_LINES:
        errors["lines"] = f"At most {MAX_LINES} lines per invoice."
    if errors:
        raise FormErrors(errors)

    return Invoice(
        issue_date=issue_date, buyer_name=buyer_name, buyer_tin=buyer_tin,
        buyer_branch=buyer_branch, buyer_address=buyer_address,
        business_style=business_style, buyer_email=buyer_email, trans_class=trans_class,
        rate=rate, rate_date=rate_date,
        rate_source=rate_source, lines=lines,
    ).compute()

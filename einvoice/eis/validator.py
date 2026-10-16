"""Checks an e-invoice JSON document before it may be exported.

Every rule in docs/EIS_SPEC_NOTES.md section 3 is enforced, using BIR's own
result codes (SYN003, SYN004, ERR001 ...) so a problem caught here reads the
same as BIR's response would. Checks that BIR does not document as a result
code but that keep our data honest use LOCAL-* codes:

  LOCAL-MATH    amounts don't add up
  LOCAL-FX      ForCur doesn't reconcile with the PHP amounts
  LOCAL-CONFIG  BIR-issued values (EIS Cert Number, PTU) are placeholders/test values

Validation works on the parsed JSON text (numbers as exact Decimals), i.e.
exactly what would be sent, not on the Python objects that produced it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..config import PLACEHOLDER_PREFIX, TEST_EIS_CERT_NO, TEST_PTU_NUM
from ..models import today_ph
from ..money import VAT_RATE, ZERO, decimals_of, round2
from . import codes, unique_id
from .serializer import loads

S, N, OBJ, LIST = "String", "Number", "object", "array"
M, O, C = "Mandatory", "Optional", "Conditional"

AMOUNT_DECIMALS = 2
# Decision 2 in PLAN.md: the rate is kept as entered. The notes say Number
# fields have 2 decimals; whether ConvRate may have more is still to be
# confirmed against the downloaded official spec.
CONV_RATE_DECIMALS = 6

# (name, type, max length, required) per object, from the field table in
# docs/EIS_SPEC_NOTES.md section 2. None = no length given in the notes.
SELLER = [
    ("Tin", S, 9, M), ("BranchCd", S, 5, M), ("Type", S, 1, M),
    ("RegNm", S, 200, M), ("BusinessNm", S, 200, O), ("Email", S, 100, O),
    ("RegAddr", S, 300, M),
]
BUYER = [
    ("Tin", S, 9, M), ("BranchCd", S, 5, M), ("RegNm", S, 200, M),
    ("BusinessNm", S, 200, O), ("Email", S, 100, O), ("RegAddr", S, 300, O),
    ("DevAddr", S, 300, O), ("AirNum", S, 50, O), ("AirNumDt", S, 8, O),
    ("LadNum", S, 50, O), ("LadNumDt", S, 8, O),
]
ITEM = [
    ("Nm", S, 100, M), ("Desc", S, 100, M), ("Qty", N, 12, M), ("Unit", S, 50, O),
    ("UnitCost", N, 30, M), ("SalesAmt", N, 30, M), ("RegDscntAmt", N, 30, M),
    ("SpeDscntAmt", N, 30, M), ("NetSales", N, 30, M),
]
DISCOUNT = [
    ("ScAmt", N, 30, M), ("PwdAmt", N, 30, M), ("RegAmt", N, 30, M),
    ("SpeAmt", N, 30, M), ("Rmk2", S, None, O),
]
FOR_CUR = [("Currency", S, 3, M), ("ConvRate", N, 30, M), ("ForexAmt", N, 30, M)]
TOP = [
    ("CompInvoiceId", S, 50, M), ("IssueDtm", S, 8, M), ("EisUniqueId", S, 24, M),
    ("DocType", S, 2, M), ("TransClass", S, 2, M), ("CorrYN", S, 1, M),
    ("CorrectionCd", S, 2, C), ("PrevUniqueId", S, 24, C), ("Rmk1", S, 500, O),
    ("SellerInfo", OBJ, SELLER, M), ("BuyerInfo", OBJ, BUYER, M),
    ("ItemList", LIST, ITEM, M), ("TotNetItemSales", N, 30, M),
    ("Discount", OBJ, DISCOUNT, M), ("OtherTaxRev", N, 30, M),
    ("TotNetSalesAftDisct", N, 30, M), ("VATAmt", N, 30, M),
    ("WithholdIncome", N, 30, M), ("WithholdBusVAT", N, 30, M),
    ("WithholdBusPT", N, 30, M), ("OtherNonTaxCharge", N, 30, M),
    ("NetAmtPay", N, 30, M), ("ForCur", OBJ, FOR_CUR, O), ("PtuNum", S, 50, M),
]
ITEMS_MIN, ITEMS_MAX = 1, 1000


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}" if self.path else \
            f"[{self.code}] {self.message}"


@dataclass
class Context:
    """What the validator needs to know beyond the document itself."""
    eis_cert_no: str                         # this system's EIS Cert Number
    known_ids: set[str] = field(default_factory=set)  # EisUniqueIds already used
    today: date = field(default_factory=today_ph)
    test_mode: bool = False                  # allow the dummy TEST values


def validate_text(text: str, ctx: Context) -> list[Issue]:
    try:
        doc = loads(text)
    except ValueError as e:
        return [Issue("SYN004", "", f"Not valid JSON: {e}")]
    return validate(doc, ctx)


def validate(doc, ctx: Context) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(doc, dict):
        return [Issue("SYN004", "", "The e-invoice must be a JSON object.")]
    _check_schema(doc, TOP, "", issues)
    # Only run the business rules on values that have the right shape.
    get = _Getter(doc)
    _check_codes(get, issues)
    _check_tins(get, issues)
    _check_dates_and_id(get, ctx, issues)
    _check_correction(get, issues)
    _check_vat(get, issues)
    _check_math(get, issues)
    _check_forcur(get, issues)
    _check_config(get, ctx, issues)
    return issues


# --- schema (SYN004) -------------------------------------------------------

def _check_schema(obj: dict, spec: list, prefix: str, issues: list[Issue]) -> None:
    names = {name for name, *_ in spec}
    for key in obj:
        if key not in names:
            issues.append(Issue("SYN004", prefix + key, "Field is not part of the EIS format."))
    for name, typ, rule, required in spec:
        path = prefix + name
        if name not in obj:
            if required == M:
                issues.append(Issue("SYN004", path, "Mandatory field is missing."))
            continue
        value = obj[name]
        if typ == OBJ:
            if value is None and required != M:
                continue
            if not isinstance(value, dict):
                issues.append(Issue("SYN004", path, "Must be an object."))
                continue
            _check_schema(value, rule, path + ".", issues)
        elif typ == LIST:
            if not isinstance(value, list):
                issues.append(Issue("SYN004", path, "Must be an array."))
                continue
            if not ITEMS_MIN <= len(value) <= ITEMS_MAX:
                issues.append(Issue("SYN004", path,
                                    f"Must have {ITEMS_MIN} to {ITEMS_MAX} items."))
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    issues.append(Issue("SYN004", f"{path}[{i}]", "Must be an object."))
                else:
                    _check_schema(item, rule, f"{path}[{i}].", issues)
        elif typ == S:
            _check_string(value, path, rule, required, issues)
        else:
            _check_number(value, path, rule, issues)


def _check_string(value, path, max_len, required, issues) -> None:
    if value is None:  # "send null or leave blank"
        if required == M:
            issues.append(Issue("SYN004", path, "Mandatory field is empty."))
        return
    if not isinstance(value, str):
        issues.append(Issue("SYN004", path, "Must be a string."))
        return
    if required == M and not value.strip():
        issues.append(Issue("SYN004", path, "Mandatory field is empty."))
    if max_len is not None and len(value) > max_len:
        issues.append(Issue("SYN004", path,
                            f"Longer than {max_len} characters ({len(value)})."))


def _check_number(value, path, max_len, issues) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        issues.append(Issue("SYN004", path, "Must be a number."))
        return
    allowed = CONV_RATE_DECIMALS if path.endswith("ConvRate") else AMOUNT_DECIMALS
    if decimals_of(value) > allowed:
        issues.append(Issue("SYN004", path, f"More than {allowed} decimal places."))
    if len(str(abs(value))) > max_len:
        issues.append(Issue("SYN004", path, f"Longer than {max_len} digits."))


class _Getter:
    """Safe access: returns None when a value is missing or has the wrong type."""

    def __init__(self, doc: dict):
        self.doc = doc

    def str(self, *path) -> str | None:
        v = self._dig(path)
        return v if isinstance(v, str) else None

    def num(self, *path) -> Decimal | None:
        v = self._dig(path)
        return v if isinstance(v, Decimal) and v.is_finite() else None

    def obj(self, *path) -> dict | None:
        v = self._dig(path)
        return v if isinstance(v, dict) else None

    def items(self) -> list[dict]:
        v = self.doc.get("ItemList")
        return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []

    def _dig(self, path):
        v = self.doc
        for key in path:
            if not isinstance(v, dict):
                return None
            v = v.get(key)
        return v


# --- codes (ERR005, ERR011, ERR012, ERR013/015, ERR014) ----------------------

def _check_codes(get: _Getter, issues: list[Issue]) -> None:
    doc_type = get.str("DocType")
    if doc_type is not None and doc_type not in codes.DOC_TYPES:
        issues.append(Issue("ERR011", "DocType",
                            f"{doc_type!r} is not a v2.1 Document Type (01, 02, 03)."))
    trans = get.str("TransClass")
    if trans is not None:
        if trans not in codes.TRANS_CLASSES:
            issues.append(Issue("ERR014", "TransClass",
                                f"{trans!r} is not a v2.1 Transaction Classification."))
        elif trans == codes.RBE_TRANS_CLASS:
            issues.append(Issue("ERR013", "TransClass",
                                "04 is for Registered Business Enterprises only; "
                                "Micronet is not an RBE."))
    seller_type = get.str("SellerInfo", "Type")
    if seller_type is not None:
        if seller_type not in codes.SELLER_TYPES:
            issues.append(Issue("ERR012", "SellerInfo.Type",
                                f"{seller_type!r} is not a v2.1 Seller Type (0, 1, 2)."))
        elif seller_type == codes.RBE_SELLER_TYPE:
            issues.append(Issue("ERR015", "SellerInfo.Type",
                                "2 is for Registered Business Enterprises only; "
                                "Micronet is not an RBE."))
    corr_yn = get.str("CorrYN")
    if corr_yn is not None and corr_yn not in ("Y", "N"):
        issues.append(Issue("ERR005", "CorrYN", 'Must be "Y" or "N".'))
    corr_cd = get.str("CorrectionCd")
    if corr_cd and corr_cd not in codes.CORRECTION_CODES:
        issues.append(Issue("ERR005", "CorrectionCd",
                            f"{corr_cd!r} is not a v2.1 Correction Code."))


# --- TINs (ERR001) -----------------------------------------------------------

_TIN = re.compile(r"^\d{9}$")
_BRANCH = re.compile(r"^\d{5}$")


def _check_tins(get: _Getter, issues: list[Issue]) -> None:
    for party, code in (("SellerInfo", "ERR001"), ("BuyerInfo", "SYN004")):
        tin = get.str(party, "Tin")
        if tin is not None and not _TIN.match(tin):
            issues.append(Issue(code, f"{party}.Tin",
                                "Must be 9 digits, numbers only (no dashes)."))
        branch = get.str(party, "BranchCd")
        if branch is not None and not _BRANCH.match(branch):
            issues.append(Issue("SYN004", f"{party}.BranchCd",
                                'Must be 5 digits, e.g. "00000" or "00101".'))


# --- dates and EisUniqueId (ERR002, ERR006, SYN003) ---------------------------

def _parse_yyyymmdd(text: str) -> date | None:
    if not re.fullmatch(r"\d{8}", text or ""):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _check_dates_and_id(get: _Getter, ctx: Context, issues: list[Issue]) -> None:
    issue_dtm = get.str("IssueDtm")
    issued = _parse_yyyymmdd(issue_dtm) if issue_dtm is not None else None
    if issue_dtm is not None:
        if issued is None:
            issues.append(Issue("ERR002", "IssueDtm", "Must be a real date as YYYYMMDD."))
        elif issued > ctx.today:
            issues.append(Issue("ERR002", "IssueDtm", "Issue date is in the future."))

    for name in ("AirNumDt", "LadNumDt"):
        v = get.str("BuyerInfo", name)
        if v and _parse_yyyymmdd(v) is None:
            issues.append(Issue("SYN004", f"BuyerInfo.{name}", "Must be a date as YYYYMMDD."))

    eid = get.str("EisUniqueId")
    if eid is None:
        return
    if len(eid) != unique_id.LENGTH:
        issues.append(Issue("SYN004", "EisUniqueId",
                            f"Must be exactly {unique_id.LENGTH} characters."))
        return
    if issue_dtm is not None and eid[unique_id.DATE] != issue_dtm:
        issues.append(Issue("ERR002", "EisUniqueId",
                            f"Date part {eid[unique_id.DATE]} differs from IssueDtm {issue_dtm}."))
    cert = eid[unique_id.CERT]
    if cert != ctx.eis_cert_no:
        issues.append(Issue("ERR006", "EisUniqueId",
                            f"Accreditation ID part {cert!r} is not this system's "
                            f"EIS Cert Number {ctx.eis_cert_no!r}."))
    elif not cert.startswith(codes.SOURCE_CAS):
        issues.append(Issue("ERR006", "EisUniqueId",
                            f"Accreditation ID {cert!r} must start with "
                            f"{codes.SOURCE_CAS} (CAS)."))
    if not unique_id.is_valid_key(eid[unique_id.KEY]):
        issues.append(Issue("SYN004", "EisUniqueId",
                            "Last 8 characters must be A-Z or 0-9."))
    if eid in ctx.known_ids:
        issues.append(Issue("SYN003", "EisUniqueId", f"{eid} has already been used."))


# --- corrections (ERR007) ------------------------------------------------------

def _check_correction(get: _Getter, issues: list[Issue]) -> None:
    corr_yn = get.str("CorrYN")
    corr_cd = get.str("CorrectionCd") or ""
    prev = get.str("PrevUniqueId") or ""
    if corr_yn == "N":
        if corr_cd:
            issues.append(Issue("ERR007", "CorrectionCd", 'Must be empty when CorrYN is "N".'))
        if prev:
            issues.append(Issue("ERR007", "PrevUniqueId", 'Must be empty when CorrYN is "N".'))
    elif corr_yn == "Y":
        if not corr_cd:
            issues.append(Issue("ERR007", "CorrectionCd", 'Mandatory when CorrYN is "Y".'))
        if len(prev) != unique_id.LENGTH:
            issues.append(Issue("ERR007", "PrevUniqueId",
                                'Mandatory when CorrYN is "Y": the 24-character '
                                "EisUniqueId being corrected (24 zeros if the "
                                "original was not issued in the CAS)."))


# --- VAT (ERR004) --------------------------------------------------------------

def _check_vat(get: _Getter, issues: list[Issue]) -> None:
    vat, total = get.num("VATAmt"), get.num("NetAmtPay")
    if vat is None:
        return
    if get.str("TransClass") in ("02", "03") and vat != 0:
        issues.append(Issue("ERR004", "VATAmt",
                            "Must be 0.00 for zero-rated and VAT-exempt sales."))
    if total is not None and vat != 0 and total != 0 and (vat > 0) != (total > 0):
        issues.append(Issue("ERR004", "VATAmt", "Sign differs from NetAmtPay."))


# --- arithmetic (LOCAL-MATH) ----------------------------------------------------

def _check_math(get: _Getter, issues: list[Issue]) -> None:
    def bad(path, expected, actual):
        issues.append(Issue("LOCAL-MATH", path,
                            f"Expected {expected:.2f}, found {actual:.2f}."))

    vatable = get.str("TransClass") == "01"
    net_sum, complete = ZERO, True
    for i, item in enumerate(get.items()):
        g = _Getter(item)
        qty, cost, sales = g.num("Qty"), g.num("UnitCost"), g.num("SalesAmt")
        reg, spe, net = g.num("RegDscntAmt"), g.num("SpeDscntAmt"), g.num("NetSales")
        path = f"ItemList[{i}]."
        if None not in (qty, cost, sales) and sales != round2(qty * cost):
            bad(path + "SalesAmt", round2(qty * cost), sales)
        if None not in (sales, reg, spe, net):
            # For VATable sales the special discount is not considered.
            expected = sales - reg - (ZERO if vatable else spe)
            if net != expected:
                bad(path + "NetSales", expected, net)
        if net is None:
            complete = False
        else:
            net_sum += net

    tot_items = get.num("TotNetItemSales")
    if complete and get.items() and tot_items is not None and tot_items != net_sum:
        bad("TotNetItemSales", net_sum, tot_items)

    disc = [get.num("Discount", k) for k in ("ScAmt", "PwdAmt", "RegAmt", "SpeAmt")]
    other, after = get.num("OtherTaxRev"), get.num("TotNetSalesAftDisct")
    if None not in (tot_items, other, after, *disc):
        expected = tot_items + other - sum(disc)
        if after != expected:
            bad("TotNetSalesAftDisct", expected, after)

    # NetAmtPay is the invoice's total payment: what the buyer pays. That is
    # the sales after discounts plus VAT and other charges, less what the
    # buyer withholds. For a zero-rated invoice without withholding it equals
    # TotNetSalesAftDisct (BIR sample JSON-02) and the PDF's TOTAL AMOUNT DUE.
    vat, other_charge = get.num("VATAmt"), get.num("OtherNonTaxCharge")
    withheld = [get.num(k) for k in ("WithholdIncome", "WithholdBusVAT", "WithholdBusPT")]
    pay = get.num("NetAmtPay")
    if None not in (pay, after, vat, other_charge, *withheld):
        expected = after + vat + other_charge - sum(withheld)
        if pay != expected:
            bad("NetAmtPay", expected, pay)

    # VATable: amounts are net of VAT, so VATAmt is 12% of the net sales.
    if vatable and None not in (after, vat):
        expected = round2(after * VAT_RATE)
        if vat != expected:
            issues.append(Issue("LOCAL-MATH", "VATAmt",
                                f"12% VAT on {after:.2f} is {expected:.2f}, found {vat:.2f}."))


# --- foreign currency (SYN004, LOCAL-FX) ----------------------------------------

def _check_forcur(get: _Getter, issues: list[Issue]) -> None:
    fc = get.obj("ForCur")
    if fc is None:
        return
    g = _Getter(fc)
    cur = g.str("Currency")
    if cur is not None and not re.fullmatch(r"[A-Z]{3}", cur):
        issues.append(Issue("SYN004", "ForCur.Currency",
                            'Must be a 3-letter ISO currency code, e.g. "JPY".'))
    elif cur == "PHP":
        issues.append(Issue("SYN004", "ForCur.Currency",
                            "ForCur is for currencies other than PHP."))
    rate, forex, pay = g.num("ConvRate"), g.num("ForexAmt"), get.num("NetAmtPay")
    if rate is not None and rate <= 0:
        issues.append(Issue("LOCAL-FX", "ForCur.ConvRate", "Must be greater than zero."))
        return
    if None in (rate, forex, pay):
        return
    # PHP is converted per unit and then multiplied by Qty, so each line may
    # be off by up to half a centavo per unit plus half a centavo.
    qty_sum = sum((_Getter(i).num("Qty") or ZERO for i in get.items()), ZERO)
    tolerance = Decimal("0.005") * (qty_sum + len(get.items()) + 1)
    vat = get.num("VATAmt")
    if vat:
        # VAT is rounded separately in JPY and in PHP, and grows the
        # per-line rounding by 12%.
        tolerance = tolerance * (1 + VAT_RATE) + Decimal("0.005") * (rate + 1)
    if any(get.num(k) for k in ("WithholdIncome", "WithholdBusVAT", "WithholdBusPT")):
        # The withholding is typed in PHP and converted to JPY once, rounded.
        tolerance += Decimal("0.005") * rate
    if abs(forex * rate - pay) > tolerance:
        issues.append(Issue("LOCAL-FX", "ForCur",
                            f"ForexAmt x ConvRate = {round2(forex * rate):.2f} PHP "
                            f"but NetAmtPay is {pay:.2f}."))


# --- BIR-issued values (LOCAL-CONFIG) ------------------------------------------

def _check_config(get: _Getter, ctx: Context, issues: list[Issue]) -> None:
    if ctx.eis_cert_no.startswith(PLACEHOLDER_PREFIX) or not unique_id.is_valid_cert_no(ctx.eis_cert_no):
        issues.append(Issue("LOCAL-CONFIG", "",
                            "EIS_CERT_NO in .env is still a placeholder. "
                            "BIR issues it after EIS enrollment."))
    ptu = get.str("PtuNum") or ""
    if ptu.startswith(PLACEHOLDER_PREFIX):
        issues.append(Issue("LOCAL-CONFIG", "PtuNum",
                            "PTU_NUM in .env is still a placeholder. "
                            "BIR issues it after CAS registration."))
    if not ctx.test_mode and (ctx.eis_cert_no == TEST_EIS_CERT_NO or ptu == TEST_PTU_NUM):
        issues.append(Issue("LOCAL-CONFIG", "",
                            "Test values (20TEST00 / TEST-PTU-NOT-VALID) are only "
                            "allowed in a test export."))

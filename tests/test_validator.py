"""One or more tests per validator rule (docs/EIS_SPEC_NOTES.md section 3)."""
import copy
import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from einvoice.eis.serializer import Num, dumps, loads
from einvoice.eis.validator import Context, validate, validate_text

ROOT = Path(__file__).resolve().parent.parent
DRAFT = Path(__file__).parent / "fixtures" / "draft_0001773.json"
CERT = "20TEST00"


def base():
    return loads(DRAFT.read_text(encoding="utf-8"))


def run(doc, **ctx):
    ctx = {"eis_cert_no": CERT, "test_mode": True, "today": date(2026, 10, 14)} | ctx
    return validate(doc, Context(**ctx))


def codes_of(doc, **ctx):
    return {i.code for i in run(doc, **ctx)}


def paths_of(doc, **ctx):
    return {i.path for i in run(doc, **ctx)}


def test_valid_draft_passes():
    assert run(base()) == []


# --- SYN004 schema --------------------------------------------------------

@pytest.mark.parametrize("path", [
    ("CompInvoiceId",), ("SellerInfo", "RegNm"), ("BuyerInfo", "Tin"),
    ("ItemList", 0, "Nm"), ("Discount", "ScAmt"), ("NetAmtPay",), ("PtuNum",),
    ("ForCur", "ConvRate"),
])
def test_missing_mandatory_field(path):
    doc = base()
    target = doc
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    assert "SYN004" in codes_of(doc)


def test_missing_optional_field_is_fine():
    doc = base()
    del doc["Rmk1"], doc["BuyerInfo"]["DevAddr"], doc["ForCur"]
    assert run(doc) == []


def test_extra_field_rejected():
    doc = base()
    doc["BuyerInfo"]["BusinessStyle"] = "Graphic Design"  # not an EIS field
    issues = run(doc)
    assert [(i.code, i.path) for i in issues] == [("SYN004", "BuyerInfo.BusinessStyle")]


def test_empty_mandatory_string():
    doc = base()
    doc["SellerInfo"]["RegAddr"] = "  "
    doc["ItemList"][0]["Desc"] = None
    assert paths_of(doc) == {"SellerInfo.RegAddr", "ItemList[0].Desc"}


def test_null_optional_string_is_fine():
    doc = base()
    doc["Rmk1"] = None
    doc["BuyerInfo"]["Email"] = None
    assert run(doc) == []


@pytest.mark.parametrize("field, limit", [
    ("CompInvoiceId", 50), ("Rmk1", 500), ("PtuNum", 50),
])
def test_max_length_top(field, limit):
    doc = base()
    doc[field] = "X" * (limit + 1)
    assert field in paths_of(doc)


@pytest.mark.parametrize("obj, field, limit", [
    ("SellerInfo", "RegNm", 200), ("SellerInfo", "RegAddr", 300),
    ("SellerInfo", "Email", 100), ("BuyerInfo", "RegNm", 200),
    ("BuyerInfo", "RegAddr", 300), ("BuyerInfo", "DevAddr", 300),
    ("BuyerInfo", "AirNum", 50),
])
def test_max_length_nested(obj, field, limit):
    doc = base()
    doc[obj][field] = "X" * (limit + 1)
    assert f"{obj}.{field}" in paths_of(doc)


@pytest.mark.parametrize("field, limit", [("Nm", 100), ("Desc", 100), ("Unit", 50)])
def test_max_length_item(field, limit):
    doc = base()
    doc["ItemList"][0][field] = "X" * limit
    assert run(doc) == []
    doc["ItemList"][0][field] = "X" * (limit + 1)
    assert f"ItemList[0].{field}" in paths_of(doc)


def test_wrong_types():
    doc = base()
    doc["VATAmt"] = "0.00"          # string instead of number
    doc["DocType"] = 1              # number instead of string
    doc["SellerInfo"] = "Micronet"  # string instead of object
    doc["ItemList"] = {}            # object instead of array
    assert {"VATAmt", "DocType", "SellerInfo", "ItemList"} <= paths_of(doc)


def test_amounts_allow_two_decimals_only():
    doc = base()
    doc["OtherTaxRev"] = D("0.001")
    assert "OtherTaxRev" in paths_of(doc)


def test_conv_rate_allows_six_decimals():
    doc = base()
    doc["ForCur"]["ConvRate"] = D("0.380000")
    assert run(doc) == []
    doc["ForCur"]["ConvRate"] = D("0.3800001")
    assert "ForCur.ConvRate" in paths_of(doc)


def test_item_count_limits():
    doc = base()
    doc["ItemList"] = []
    doc["TotNetItemSales"] = doc["TotNetSalesAftDisct"] = doc["NetAmtPay"] = D("0")
    assert "ItemList" in paths_of(doc)
    doc = base()
    item = doc["ItemList"][0] | {"UnitCost": D("1.00"), "SalesAmt": D("1.00"), "NetSales": D("1.00")}
    doc["ItemList"] = [dict(item) for _ in range(1001)]
    assert "ItemList" in paths_of(doc)


def test_invalid_json_text():
    assert {i.code for i in validate_text("{not json", Context(CERT))} == {"SYN004"}


# --- SYN003 duplicate ------------------------------------------------------

def test_duplicate_eis_unique_id():
    doc = base()
    assert "SYN003" in codes_of(doc, known_ids={doc["EisUniqueId"]})
    assert "SYN003" not in codes_of(doc, known_ids={"2024112520TEST00ZZZZ9999"})


# --- ERR001 seller TIN ------------------------------------------------------

@pytest.mark.parametrize("tin", ["002-372-466", "00237246", "0023724660", "00237246A", ""])
def test_seller_tin_nine_digits(tin):
    doc = base()
    doc["SellerInfo"]["Tin"] = tin
    assert "ERR001" in codes_of(doc) or "SYN004" in codes_of(doc)
    if tin:
        assert "ERR001" in codes_of(doc)


@pytest.mark.parametrize("branch", ["0", "101", "000000", "00A00"])
def test_branch_code_five_digits(branch):
    doc = base()
    doc["SellerInfo"]["BranchCd"] = branch
    assert "SellerInfo.BranchCd" in paths_of(doc)


def test_buyer_tin_nine_digits():
    doc = base()
    doc["BuyerInfo"]["Tin"] = "N/A"
    assert "BuyerInfo.Tin" in paths_of(doc)


# --- ERR002 dates -----------------------------------------------------------

@pytest.mark.parametrize("value", ["2024-11-25", "20241325", "20240230", "2024112"])
def test_issue_date_must_be_real_yyyymmdd(value):
    doc = base()
    doc["IssueDtm"] = value
    assert "ERR002" in codes_of(doc)


def test_issue_date_not_in_future():
    doc = base()
    assert "ERR002" in codes_of(doc, today=date(2024, 11, 24))
    assert "ERR002" not in codes_of(doc, today=date(2024, 11, 25))


def test_eis_unique_id_date_must_equal_issue_date():
    doc = base()
    doc["IssueDtm"] = "20241126"
    issues = run(doc)
    assert any(i.code == "ERR002" and i.path == "EisUniqueId" for i in issues)


def test_shipping_dates_format():
    doc = base()
    doc["BuyerInfo"]["AirNumDt"] = "25/11/2024"
    assert "BuyerInfo.AirNumDt" in paths_of(doc)


# --- ERR004 VAT -------------------------------------------------------------

@pytest.mark.parametrize("trans_class", ["02", "03"])
def test_vat_must_be_zero_for_zero_rated_and_exempt(trans_class):
    doc = base()
    doc["TransClass"] = trans_class
    doc["VATAmt"] = D("547200.00")
    assert "ERR004" in codes_of(doc)


def test_vat_sign_must_match_total():
    doc = base()
    doc["TransClass"] = "01"
    doc["VATAmt"] = D("-12.00")
    assert "ERR004" in codes_of(doc)


# --- ERR005 / ERR011 / ERR012 / ERR013 / ERR014 / ERR015 codes --------------

@pytest.mark.parametrize("value", ["04", "05", "1", ""])
def test_doc_type_v21_only(value):
    doc = base()
    doc["DocType"] = value
    assert "ERR011" in codes_of(doc) or (value == "" and "SYN004" in codes_of(doc))


@pytest.mark.parametrize("value", ["05", "2", "00"])
def test_trans_class_v21_only(value):
    doc = base()
    doc["TransClass"] = value
    assert "ERR014" in codes_of(doc)


def test_rbe_codes_refused():
    doc = base()
    doc["TransClass"] = "04"
    doc["SellerInfo"]["Type"] = "2"
    assert {"ERR013", "ERR015"} <= codes_of(doc)


@pytest.mark.parametrize("value", ["3", "V", "01"])
def test_seller_type(value):
    doc = base()
    doc["SellerInfo"]["Type"] = value
    assert "ERR012" in codes_of(doc) or "SYN004" in codes_of(doc)


def test_corr_yn_and_correction_code_values():
    doc = base()
    doc["CorrYN"] = "X"
    assert "ERR005" in codes_of(doc)
    doc = base()
    doc |= {"CorrYN": "Y", "CorrectionCd": "06", "PrevUniqueId": "0" * 24}
    assert "ERR005" in codes_of(doc)


# --- ERR006 accreditation ID ------------------------------------------------

def test_cert_part_must_match_this_system():
    doc = base()
    assert "ERR006" in codes_of(doc, eis_cert_no="20OTHER0")


def test_cert_must_be_cas():
    doc = base()
    doc["EisUniqueId"] = "20241125" + "31POS000" + "ABCD1234"
    assert "ERR006" in codes_of(doc, eis_cert_no="31POS000")


def test_key_part_uppercase_alphanumeric():
    doc = base()
    doc["EisUniqueId"] = doc["EisUniqueId"][:16] + "abcd-123"
    assert "EisUniqueId" in paths_of(doc)


def test_unique_id_length():
    doc = base()
    doc["EisUniqueId"] = doc["EisUniqueId"][:23]
    assert "SYN004" in codes_of(doc)


# --- ERR007 corrections -----------------------------------------------------

def test_prev_id_and_code_must_be_empty_when_not_a_correction():
    doc = base()
    doc["PrevUniqueId"] = "0" * 24
    doc["CorrectionCd"] = "01"
    assert {i.path for i in run(doc) if i.code == "ERR007"} == {"PrevUniqueId", "CorrectionCd"}


def test_correction_needs_code_and_previous_id():
    doc = base()
    doc["CorrYN"] = "Y"
    assert {i.path for i in run(doc) if i.code == "ERR007"} == {"PrevUniqueId", "CorrectionCd"}
    doc |= {"CorrectionCd": "01", "PrevUniqueId": "0" * 24}  # original not in CAS
    assert "ERR007" not in codes_of(doc)


# --- LOCAL-MATH -------------------------------------------------------------

def test_sales_amount_must_equal_qty_times_unit_cost():
    doc = base()
    doc["ItemList"][0]["Qty"] = D("2")
    assert "ItemList[0].SalesAmt" in paths_of(doc)


def test_net_sales_subtracts_discounts():
    doc = base()
    doc["ItemList"][0]["RegDscntAmt"] = D("100.00")
    assert "ItemList[0].NetSales" in paths_of(doc)


def test_totals_must_add_up():
    doc = base()
    doc["TotNetItemSales"] = D("4559999.99")
    assert {"TotNetItemSales", "TotNetSalesAftDisct"} <= paths_of(doc)
    doc = base()
    doc["NetAmtPay"] = D("4560000.01")
    assert "NetAmtPay" in paths_of(doc)


def test_invoice_discounts_reduce_total_after_discount():
    doc = base()
    doc["Discount"]["RegAmt"] = D("1000.00")
    assert "TotNetSalesAftDisct" in paths_of(doc)


# --- LOCAL-FX ---------------------------------------------------------------

def test_forcur_must_reconcile():
    doc = base()
    doc["ForCur"]["ConvRate"] = D("0.39")
    assert "LOCAL-FX" in codes_of(doc)


def test_forcur_currency_code():
    doc = base()
    doc["ForCur"]["Currency"] = "yen"
    assert "ForCur.Currency" in paths_of(doc)
    doc["ForCur"]["Currency"] = "PHP"
    assert "ForCur.Currency" in paths_of(doc)


def test_forcur_tolerates_per_unit_rounding():
    # 3 units at JPY 333.33, rate 0.384215: PHP 128.07 per unit, 384.21 total,
    # while 999.99 x 0.384215 = 384.211... -> within half a centavo per unit.
    doc = base()
    doc["ItemList"][0] |= {"Qty": D("3"), "UnitCost": D("128.07"),
                           "SalesAmt": D("384.21"), "NetSales": D("384.21")}
    for k in ("TotNetItemSales", "TotNetSalesAftDisct", "NetAmtPay"):
        doc[k] = D("384.21")
    doc["ForCur"] |= {"ForexAmt": D("999.99"), "ConvRate": D("0.384215")}
    assert run(doc) == []


# --- LOCAL-CONFIG -----------------------------------------------------------

def test_placeholders_refused():
    doc = base()
    doc["PtuNum"] = "__PLACEHOLDER_PTU_NUMBER__"
    assert "LOCAL-CONFIG" in codes_of(doc, eis_cert_no="__PLACEHOLDER_EIS_CERT_NO__")


def test_test_values_only_in_test_mode():
    assert "LOCAL-CONFIG" in codes_of(base(), test_mode=False)
    assert "LOCAL-CONFIG" not in codes_of(base(), test_mode=True)


# --- BIR's own sample ---------------------------------------------------------

def test_bir_sample_is_flagged_as_expected():
    """docs/sample-einvoice.json: valid structure and line totals, but RBE codes
    (not allowed for Micronet) and dummy NetAmtPay / ForCur values (as noted in
    docs/EIS_SPEC_NOTES.md)."""
    text = (ROOT / "docs" / "sample-einvoice.json").read_text(encoding="utf-8")
    issues = validate_text(text, Context("20146IT4", today=date(2026, 10, 14), test_mode=True))
    found = {i.code for i in issues}
    assert found == {"ERR013", "ERR015", "LOCAL-MATH", "LOCAL-FX"}
    assert {i.path for i in issues if i.code == "LOCAL-MATH"} == {"NetAmtPay"}
    assert not any(i.code == "SYN004" and "not part of" in i.message for i in issues)


# --- serializer ---------------------------------------------------------------

def test_serializer_number_forms():
    text = dumps({"a": Num.amount(D("5")), "q": Num.qty(D("1.50")), "r": Num.rate(D("0.3842")),
                  "s": "A&B \"quoted\"", "n": None, "l": []})
    assert json.loads(text) == {"a": 5.0, "q": 1.5, "r": 0.3842, "s": 'A&B "quoted"',
                                "n": None, "l": []}
    assert '"a": 5.00' in text and '"q": 1.5' in text and '"r": 0.3842' in text


def test_serializer_rejects_floats():
    with pytest.raises(TypeError):
        dumps({"x": 1.5})


def test_net_amount_payable_is_the_total_payment():
    # Total payment = sales after discounts + VAT + other charges - withheld.
    doc = base()
    doc["OtherNonTaxCharge"] = D("500.00")
    doc["WithholdIncome"] = D("1000.00")
    assert "NetAmtPay" in paths_of(doc)
    doc["NetAmtPay"] = D("4559500.00")
    doc["ForCur"]["ForexAmt"] = D("11998684.21")  # keep ForCur consistent
    assert "NetAmtPay" not in paths_of(doc)


def test_net_amount_payable_includes_vat():
    doc = base()
    doc["TransClass"] = "01"
    doc["VATAmt"] = D("547200.00")
    assert "NetAmtPay" in paths_of(doc)
    doc["NetAmtPay"] = D("5107200.00")
    assert "NetAmtPay" not in paths_of(doc)

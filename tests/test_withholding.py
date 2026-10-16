"""Withholding tax: typed in PHP, printed as "Less: Withholding Tax" in the
invoice's amount box and sent to BIR as WithholdIncome."""
import json
from decimal import Decimal as D

import pytest

from einvoice.eis import export as eis_export
from einvoice.models import FormErrors, invoice_from_form
from einvoice.pdf.service_invoice import render
from einvoice.web.app import create_app
from tests.conftest import make_invoice, sign_in
from tests.test_form import form
from tests.test_pdf import _text

# Invoice 0001773 made VATable, with 2% withheld on the amount net of VAT.
WHT = D("91200.00")  # 2% of PHP 4,560,000.00


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return sign_in(app)


def test_amount_box_math():
    inv = make_invoice(trans_class="01", withholding_php=WHT)
    assert inv.gross_php == D("5107200.00")        # Total Sales (VAT Inclusive)
    assert inv.vat_php == D("547200.00")           # Less: VAT / Add: VAT
    assert inv.total_php == D("4560000.00")        # Amount net of VAT
    assert inv.amount_due_php == D("4468800.00")   # Amount Due
    assert inv.total_due_php == D("5016000.00")    # Total Amount Due
    assert inv.withholding_jpy == D("240000.00")   # 91,200 / 0.38
    assert inv.total_due_jpy == D("13200000.00")


def test_no_withholding_changes_nothing():
    inv = make_invoice(trans_class="02")
    assert inv.withholding_php == inv.withholding_jpy == D("0")
    assert inv.total_due_php == inv.gross_php == inv.total_php == D("4560000.00")


@pytest.mark.parametrize("value, expected", [("", D("0")), ("  ", D("0")),
                                             ("91,200", WHT), ("0", D("0"))])
def test_form_accepts(value, expected):
    assert invoice_from_form(form(withholding_php=value)).withholding_php == expected


@pytest.mark.parametrize("value, message", [
    ("-5", "can't be negative"),
    ("1.234", "at most 2 decimal places"),
    ("abc", "must be a number"),
    ("4,560,000", "less than the amount net of VAT"),
])
def test_form_rejects(value, message):
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(withholding_php=value))
    assert set(e.value.errors) == {"withholding_php"}
    assert message in e.value.errors["withholding_php"]


def test_saved_and_loaded(repo):
    inv = repo.issue(make_invoice(trans_class="01", withholding_php=WHT))
    again = repo.get(inv.control_no)
    assert again.withholding_php == WHT and again.total_due_php == D("5016000.00")


def test_eis_json(repo, config):
    inv = repo.issue(make_invoice(trans_class="01", withholding_php=WHT))
    result = eis_export.export(inv, config, repo.conn, test=True)  # raises if invalid
    doc = json.loads(result.path.read_text(encoding="utf-8"))
    assert doc["WithholdIncome"] == 91200.00
    assert doc["WithholdBusVAT"] == doc["WithholdBusPT"] == 0
    assert doc["TotNetSalesAftDisct"] == 4560000.00 and doc["VATAmt"] == 547200.00
    assert doc["NetAmtPay"] == 5016000.00
    assert doc["ForCur"]["ForexAmt"] == 13200000.00


def test_eis_json_zero_rated_with_withholding(repo, config):
    inv = repo.issue(make_invoice(trans_class="02", withholding_php=D("45600.00")))
    result = eis_export.export(inv, config, repo.conn, test=True)
    doc = json.loads(result.path.read_text(encoding="utf-8"))
    assert doc["NetAmtPay"] == 4514400.00 and doc["ForCur"]["ForexAmt"] == 11880000.00


def test_pdf_amount_box(repo, config):
    text = _text(render(repo.issue(make_invoice(trans_class="01", withholding_php=WHT)), config))
    for expected in ["PARTICULARS", "Total Sales (VAT Inclusive)", "5,107,200.00", "Less: VAT",
                     "Amount net of VAT", "Less: Withholding Tax", "91,200.00", "Amount Due",
                     "4,468,800.00", "Add: VAT", "TOTAL AMOUNT DUE", "PHP 5,016,000.00",
                     "VATable Sales", "VAT-Exempt Sales", "Zero-Rated Sales", "VAT Amount",
                     "TOTAL SALES", "LESS: WITHHOLDING TAX (JPY)", "240,000.00", "13,440,000.00",
                     "13,200,000.00", "THIRTEEN MILLION TWO HUNDRED THOUSAND YEN ONLY"]:
        assert expected in text, expected


def test_books_receivable_is_the_full_amount(repo):
    inv = repo.issue(make_invoice(trans_class="01", withholding_php=WHT))
    rows = repo.conn.execute(
        """SELECT a.role, l.debit, l.credit FROM journal_lines l
           JOIN journal_entries e ON e.id = l.entry_id JOIN accounts a ON a.code = l.account_code
           WHERE e.source_type = 'invoice' AND e.source_id = ?""", (inv.id,)).fetchall()
    by_role = {r["role"]: (D(r["debit"]), D(r["credit"])) for r in rows}
    assert by_role["ar"][0] == D("5107200.00")  # the 2307 settles the withholding


def test_review_page_and_collection_prefill(client):
    data = form(trans_class="01", withholding_php="91,200")
    page = client.post("/invoices/review", data=data, follow_redirects=True).get_data(as_text=True)
    for expected in ["Less: Withholding Tax", "91,200.00", "Amount Due", "4,468,800.00",
                     "5,016,000.00", "TOTAL SALES", 'name="withholding_php" value="91,200"']:
        assert expected in page, expected
    r = client.post("/invoices/issue", data=data | {"token": "w1"})
    assert r.status_code == 302
    new = client.get("/collections/new?invoice=1").get_data(as_text=True)
    assert 'value="91200.00"' in new and 'value="4560000.00"' in new
    # Choosing the invoice in the list (not through its link) fills it in too.
    plain = client.get("/collections/new").get_data(as_text=True)
    assert 'data-wht="91200.00"' in plain and 'data-net="4560000.00"' in plain


def test_form_has_withholding_field(client):
    page = client.get("/invoices/new").get_data(as_text=True)
    assert 'name="withholding_php"' in page and "Withholding tax (PHP)" in page

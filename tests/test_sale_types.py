"""Sale type dropdown: VATable (12%), VAT Zero-Rated (0%) and VAT-Exempt."""
import json
from decimal import Decimal as D

import pytest

from einvoice.eis import export as eis_export
from einvoice.eis.export import ExportRefused
from einvoice.models import FormErrors, InvoiceLine, invoice_from_form
from einvoice.pdf.service_invoice import render
from einvoice.web.app import create_app
from tests.conftest import make_invoice
from tests.test_form import form
from tests.test_pdf import _text
from tests.conftest import sign_in


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return sign_in(app)


def _exported(repo, config, **kw):
    inv = repo.issue(make_invoice(**kw))
    result = eis_export.export(inv, config, repo.conn, test=True)  # raises if invalid
    return inv, json.loads(result.path.read_text(encoding="utf-8"))


# --- form ---------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "04", "zero"])
def test_sale_type_must_be_chosen(value):
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(trans_class=value))
    assert set(e.value.errors) == {"trans_class"}


@pytest.mark.parametrize("code", ["01", "02", "03"])
def test_each_sale_type_accepted(code):
    assert invoice_from_form(form(trans_class=code)).trans_class == code


def test_new_form_has_no_sale_type_selected(client):
    client.post("/invoices/issue", data=form(trans_class="01") | {"token": "s1"})
    page = client.get("/invoices/new").get_data(as_text=True)  # copies the last client
    assert 'name="trans_class"' in page
    assert 'value="" selected disabled' in page
    for label in ["VATable (12%)", "VAT Zero-Rated (0%)", "VAT-Exempt"]:
        assert label in page
    assert 'value="01" selected' not in page


def test_missing_sale_type_shown_on_form(client):
    r = client.post("/invoices/review", data=form(trans_class=""))
    assert r.status_code == 422 and "Choose the sale type" in r.get_data(as_text=True)


# --- amounts ------------------------------------------------------------------

def test_vatable_adds_12_percent_on_top():
    inv = make_invoice(trans_class="01")
    assert inv.total_jpy == D("12000000.00") and inv.total_php == D("4560000.00")
    assert inv.vat_jpy == D("1440000.00") and inv.vat_php == D("547200.00")
    assert inv.total_due_jpy == D("13440000.00") and inv.total_due_php == D("5107200.00")


@pytest.mark.parametrize("code", ["02", "03"])
def test_no_vat_for_zero_rated_and_exempt(code):
    inv = make_invoice(trans_class=code)
    assert inv.vat_php == inv.vat_jpy == D("0.00")
    assert inv.total_due_php == inv.total_php and inv.total_due_jpy == inv.total_jpy


# --- storage ------------------------------------------------------------------

def test_sale_type_saved(repo):
    repo.issue(make_invoice(trans_class="03"))
    assert repo.get("0001773").trans_class == "03"
    assert repo.list()[0].sale_type == "VAT-Exempt"


# --- BIR JSON -----------------------------------------------------------------

def test_vatable_json(repo, config):
    _, doc = _exported(repo, config, trans_class="01")
    assert doc["TransClass"] == "01"
    assert doc["TotNetSalesAftDisct"] == 4560000.00
    assert doc["VATAmt"] == 547200.00
    assert doc["NetAmtPay"] == 5107200.00
    assert doc["ItemList"][0]["UnitCost"] == 4560000.00  # net of VAT
    assert doc["ForCur"]["ForexAmt"] == 13440000.00     # what the client pays in JPY


def test_exempt_json(repo, config):
    _, doc = _exported(repo, config, trans_class="03")
    assert doc["TransClass"] == "03"
    assert doc["VATAmt"] == 0 and doc["NetAmtPay"] == 4560000.00


def test_vatable_with_awkward_rate_and_quantities_still_reconciles(repo, config):
    lines = [InvoiceLine("IT Services", "Support", D("3"), "HR", D("333333.33")),
             InvoiceLine("IT Services", "Hosting", D("2.5"), "MO", D("12345.67"))]
    _exported(repo, config, trans_class="01", rate=D("0.376543"), lines=lines)


def test_wrong_vat_amount_caught(repo, config, monkeypatch):
    from einvoice.models import Invoice
    monkeypatch.setattr(Invoice, "vat_php", property(lambda self: D("1.00")))
    inv = repo.issue(make_invoice(trans_class="01"))
    with pytest.raises(ExportRefused) as e:
        eis_export.export(inv, config, repo.conn, test=True)
    assert "VATAmt" in {i.path for i in e.value.issues}


# --- PDF ----------------------------------------------------------------------

def test_vatable_pdf(repo, config):
    text = _text(render(repo.issue(make_invoice(trans_class="01")), config))
    for expected in ["VATABLE SALE", "VAT Amount (12%)", "547,200.00", "5,107,200.00",
                     "ADD: 12% VAT (JPY)", "1,440,000.00", "13,440,000.00",
                     "THIRTEEN MILLION FOUR HUNDRED FORTY THOUSAND YEN ONLY"]:
        assert expected in text, expected
    assert "ZERO-RATED SALE" not in text


def test_exempt_pdf(repo, config):
    text = _text(render(repo.issue(make_invoice(trans_class="03")), config))
    assert "VAT-EXEMPT SALE" in text and "ZERO-RATED SALE" not in text
    assert "ADD: 12% VAT" not in text


def test_review_page_carries_sale_type_to_issue(client):
    page = client.post("/invoices/review", data=form(trans_class="01")).get_data(as_text=True)
    assert '<input type="hidden" name="trans_class" value="01">' in page
    assert "VATable (12%)" in page and "5,107,200.00" in page

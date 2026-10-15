from decimal import Decimal as D

import pytest

from einvoice.eis import builder
from einvoice.models import FormErrors, invoice_from_form
from einvoice.pdf.service_invoice import render
from einvoice.web.app import create_app
from tests.conftest import sign_in
from tests.test_form import form


def lines(*rows):
    """rows of (item_name, description, qty, unit, unit price)."""
    cols = ["item_name[]", "description[]", "qty[]", "unit[]", "unit_cost_jpy[]"]
    return {c: [r[i] for r in rows] for i, c in enumerate(cols)}


def test_qty_unit_and_price_may_be_blank(repo, config):
    data = form() | lines(("IT Services", "IT services rendered for Nov 2024", "", "", "12000000"),
                          ("IT Services", "Period covered: Nov 1-30, 2024", "", "", ""))
    inv = repo.issue(invoice_from_form(data))
    saved = repo.get(inv.control_no)
    first, note = saved.lines
    assert (first.qty, first.unit, first.unit_cost_jpy) == (None, "", D("12000000"))
    assert first.sales_php == D("4560000.00") and saved.total_php == D("4560000.00")
    assert note.is_text and note.unit_cost_jpy is None and note.sales_php == 0
    items = builder.build(saved, config.with_(eis_cert_no="20TEST00", ptu_num="X"))["ItemList"]
    assert len(items) == 1  # the note line is not an item sold
    assert items[0]["Qty"].text == "1" and items[0]["Unit"] == ""
    assert render(saved, config).startswith(b"%PDF")


def test_at_least_one_line_needs_a_price():
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form() | lines(("", "Just a note", "", "", "")))
    assert "unit price" in e.value.errors["lines"]


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return sign_in(app)


def test_review_has_its_own_address_and_back_keeps_the_form(client):
    r = client.post("/invoices/review", data=form(buyer_name="BACK TEST CO."))
    assert r.status_code == 302 and "/invoices/review/" in r.location
    draft = r.location.rsplit("/", 1)[1]
    page = client.get(r.location).get_data(as_text=True)  # can be reloaded / returned to
    assert "BACK TEST CO." in page and "Issue invoice No. 0001773" in page
    form_page = client.get(f"/invoices/new?draft={draft}").get_data(as_text=True)
    assert 'value="BACK TEST CO."' in form_page and f'value="{draft}"' in form_page

    client.post("/invoices/issue", data=form(buyer_name="BACK TEST CO.") | {"token": draft})
    again = client.get(f"/invoices/review/{draft}").get_data(as_text=True)
    assert "was issued as" in again and "Issue invoice No." not in again
    fresh = client.get(f"/invoices/new?draft={draft}").get_data(as_text=True)
    assert "already issued as No. 0001773" in fresh
    assert "0001774" in client.get("/").get_data(as_text=True)  # only one was issued


def test_every_page_has_a_back_button(client):
    for url in ("/", "/books", "/invoices/new", "/collections"):
        assert 'id="back-btn"' in client.get(url).get_data(as_text=True), url

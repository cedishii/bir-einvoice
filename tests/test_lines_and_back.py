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


def amount_lines(*rows):
    """rows of (description, qty, unit, unit price, amount)."""
    return {
        "item_name[]": ["IT Services"] * len(rows),
        "description[]": [r[0] for r in rows], "qty[]": [r[1] for r in rows],
        "unit[]": [r[2] for r in rows], "unit_cost_jpy[]": [r[3] for r in rows],
        "amount_jpy[]": [r[4] for r in rows]}


def test_amount_typed_directly_or_computed(repo, config):
    data = form() | amount_lines(("Rendered for September 2026", "", "", "", "12,000,000"),
                                 ("Support", "2", "MONTH", "150000", "300,000.00"))
    inv = repo.issue(invoice_from_form(data))
    saved = repo.get(inv.control_no)
    typed, computed = saved.lines
    assert typed.unit_cost_jpy is None and typed.amount_entered == D("12000000")
    assert typed.amount_jpy == D("12000000.00") and typed.sales_php == D("4560000.00")
    assert computed.amount_entered is None and computed.amount_jpy == D("300000.00")
    assert saved.total_jpy == D("12300000.00")
    items = builder.build(saved, config.with_(eis_cert_no="20TEST00", ptu_num="X"))["ItemList"]
    assert [i["UnitCost"].text for i in items] == ["4560000.00", "57000.00"]
    assert render(saved, config).startswith(b"%PDF")


def test_amount_checks():
    with pytest.raises(FormErrors, match="not qty x unit price"):
        invoice_from_form(form() | amount_lines(("x", "2", "", "100", "250")))
    with pytest.raises(FormErrors, match="divide evenly"):
        invoice_from_form(form() | amount_lines(("x", "3", "", "", "100")))
    ok = invoice_from_form(form() | amount_lines(("x", "4", "", "", "100")))
    assert ok.lines[0].billed_unit_cost_jpy == D("25") and ok.total_jpy == D("100.00")


def test_form_has_amount_column_and_total(client):
    page = client.get("/invoices/new").get_data(as_text=True)
    assert page.count('<input name="amount_jpy[]"') == 2 and 'id="lines-total"' in page


def test_drafts_survive_restarting_the_app(config):
    first = sign_in(create_app(config))
    r = first.post("/invoices/review", data=form(buyer_name="KEPT AFTER RESTART"))
    draft = r.location.rsplit("/", 1)[1]
    again = sign_in(create_app(config))  # the app was closed and opened again
    assert 'value="KEPT AFTER RESTART"' in again.get(f"/invoices/new?draft={draft}").get_data(as_text=True)
    assert "Issue invoice No." in again.get(f"/invoices/review/{draft}").get_data(as_text=True)

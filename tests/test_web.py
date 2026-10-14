import pytest

from einvoice.web.app import create_app
from tests.test_form import form


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return app.test_client()


def test_list_starts_empty(client):
    page = client.get("/").get_data(as_text=True)
    assert "No invoices yet" in page and "0001773" in page
    assert "Prototype mode" in page


def test_new_form_has_two_default_lines(client):
    page = client.get("/invoices/new").get_data(as_text=True)
    assert page.count('<input name="description[]"') == 2
    assert page.count('value="IT Services"') == 2
    assert 'name="rate" value=""' in page  # never pre-filled


def test_review_shows_php_and_does_not_use_a_number(client):
    page = client.post("/invoices/review", data=form()).get_data(as_text=True)
    assert "4,560,000.00" in page and "Issue invoice No. 0001773" in page
    assert "No invoices yet" in client.get("/").get_data(as_text=True)


def test_review_reports_errors(client):
    r = client.post("/invoices/review", data=form(rate=""))
    assert r.status_code == 422
    assert "Exchange rate is required" in r.get_data(as_text=True)


def test_issue_view_pdf_and_void(client):
    r = client.post("/invoices/issue", data=form() | {"token": "t1"})
    assert r.status_code == 302 and r.location.endswith("/invoices/0001773")
    # The same review page submitted twice must not issue a second invoice.
    again = client.post("/invoices/issue", data=form() | {"token": "t1"})
    assert again.location.endswith("/invoices/0001773")
    assert "0001774" in client.get("/").get_data(as_text=True)  # next number

    detail = client.get("/invoices/0001773").get_data(as_text=True)
    assert "MICRONET CO., LTD." in detail and "Void this invoice" in detail

    pdf = client.get("/invoices/0001773/pdf")
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")

    client.post("/invoices/0001773/void", data={"reason": "Wrong address"})
    assert "Wrong address" in client.get("/invoices/0001773").get_data(as_text=True)


def test_new_form_copies_last_client_but_not_the_rate(client):
    client.post("/invoices/issue", data=form() | {"token": "t2"})
    page = client.get("/invoices/new").get_data(as_text=True)
    assert 'value="MICRONET CO., LTD."' in page
    assert 'name="rate" value=""' in page


def test_unknown_invoice_404(client):
    assert client.get("/invoices/9999999").status_code == 404

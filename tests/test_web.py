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


def test_review_says_eis_check_passed(client):
    page = client.post("/invoices/review", data=form()).get_data(as_text=True)
    assert "Passes the BIR e-invoice" in page


def test_prototype_detail_disables_real_export(client):
    client.post("/invoices/issue", data=form() | {"token": "t3"})
    page = client.get("/invoices/0001773").get_data(as_text=True)
    assert "Test export" in page and "Waiting for BIR-issued values" in page
    r = client.post("/invoices/0001773/export")  # forced anyway
    assert r.status_code == 422 and "LOCAL-CONFIG" in r.get_data(as_text=True)


def test_test_export_page_and_download(client):
    client.post("/invoices/issue", data=form() | {"token": "t4"})
    page = client.post("/invoices/0001773/test-export").get_data(as_text=True)
    assert "Passed every validation rule" in page and "&#34;TransClass&#34;: &#34;02&#34;" in page
    dl = client.get("/invoices/0001773/test-export.json")
    assert dl.headers["Content-Disposition"].startswith("attachment; filename=TEST-0001773-")
    assert b'"NetAmtPay": 4560000.00' in dl.data


def test_real_export_flow(config):
    from einvoice.web.app import create_app
    app = create_app(config.with_(eis_cert_no="20ABC123", ptu_num="PTU-0001"))
    c = app.test_client()
    c.post("/invoices/issue", data=form() | {"token": "t5"})
    assert "Prototype mode" not in c.get("/").get_data(as_text=True)
    r = c.post("/invoices/0001773/export")
    assert r.status_code == 302
    page = c.get("/invoices/0001773").get_data(as_text=True)
    assert "2024112520ABC123" in page and "exported" in page
    dl = c.get("/invoices/0001773/export.json")
    assert b'"PtuNum": "PTU-0001"' in dl.data
    # Exported invoices can't be silently voided.
    c.post("/invoices/0001773/void", data={"reason": "oops"})
    assert "already been exported" in c.get("/invoices/0001773").get_data(as_text=True)

import io
import zipfile

import pytest

from einvoice.web.app import create_app
from tests.test_form import form
from tests.conftest import sign_in


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return sign_in(app)


def page(client, url):
    r = client.get(url)
    assert r.status_code == 200, url
    return r.get_data(as_text=True)


def issue(client, token="t1", **overrides):
    r = client.post("/invoices/issue", data=form(**overrides) | {"token": token})
    assert r.status_code == 302
    return r.location.rsplit("/", 1)[1]


def test_pages_open_on_an_empty_database(client):
    for url in ["/books", "/books/entries", "/books/entries/new", "/collections",
                "/collections/new", "/bills", "/bills/new", "/payments", "/payments/new",
                "/accounts", "/suppliers", "/customers"]:
        page(client, url)
    for key in ["SJ", "PJ", "CRJ", "CDJ", "GJ", "gl", "ar", "ap", "tb", "sls", "slp",
                "wt-issued", "wt-received", "audit"]:
        page(client, f"/books/report/{key}")
        assert client.get(f"/books/report/{key}?format=pdf").data.startswith(b"%PDF")
    assert "Journal check OK" in page(client, "/books")


def test_invoice_shows_its_books_entry_and_collection(client):
    no = issue(client)
    detail = page(client, f"/invoices/{no}")
    assert "SJ-1" in detail and "4,560,000.00" in detail
    sj = page(client, "/books/report/SJ?from=2024-11-01&to=2024-11-30")
    assert "SI 0001773" in sj and "1100 Accounts Receivable" in sj

    new = page(client, "/collections/new?invoice=1")
    assert 'data-balance="4560000.00"' in new
    r = client.post("/collections/new", data={
        "invoice_id": "1", "coll_date": "2024-12-05", "reference": "OR 77",
        "applied": "4,560,000.00", "cash": "4,550,000.00", "cash_account": "1020"})
    assert r.status_code == 302 and "/books/entries/" in r.location
    entry = page(client, r.location)
    assert "CRJ-1" in entry and "5900 Foreign Exchange Loss" in entry and "10,000.00" in entry
    assert "to collect: <strong>PHP 0.00" in page(client, f"/invoices/{no}")


def test_collection_errors_are_shown(client):
    issue(client)
    r = client.post("/collections/new", data={
        "invoice_id": "1", "coll_date": "2024-12-05", "reference": "",
        "applied": "100", "cash": "100", "cash_account": "1020"})
    assert r.status_code == 422 and "Reference" in r.get_data(as_text=True)


def test_supplier_bill_payment_and_reports(client):
    r = client.post("/suppliers", data={"name": "ACME  OFFICE", "tin": "123-456-789",
                                        "address": "Makati"})
    assert r.status_code == 302
    assert "123-456-789-00000" in page(client, "/suppliers")
    r = client.post("/bills/new", data={
        "supplier_id": "1", "bill_date": "2024-11-03", "supplier_ref": "SI 88",
        "description": "Office rent", "account_code": "5100", "category": "services",
        "net": "50,000", "vat": "6,000"})
    assert r.status_code == 302
    assert "56,000.00" in page(client, "/bills")
    r = client.post("/payments/new", data={
        "bill_id": "1", "pay_date": "2024-11-05", "reference": "CHK 1", "amount": "56000",
        "cash_account": "1020", "atc": "WI100", "ewt_base": "50000", "ewt_rate": "5",
        "ewt": "2500"})
    assert r.status_code == 302
    assert "53,500.00" in page(client, "/payments")

    q = "?from=2024-10-01&to=2024-12-31"
    assert "ACME OFFICE" in page(client, "/books/report/slp" + q)
    wt = page(client, "/books/report/wt-issued" + q)
    assert "WI100" in wt and "2,500.00" in wt
    pdf = client.get("/books/report/wt-issued" + q + "&format=pdf")
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF")
    csv = client.get("/books/report/slp" + q + "&format=csv")
    assert csv.mimetype == "text/csv" and b"50000.00" in csv.data
    assert "print" in page(client, "/books/report/audit")  # printing is in the audit trail


def test_manual_entry_and_reversal(client):
    data = {"entry_date": "2024-11-30", "reference": "JV-1", "particulars": "Opening capital",
            "party": "", "account[]": ["1020", "3000"], "debit[]": ["100,000", ""],
            "credit[]": ["", "100000"], "memo[]": ["", ""]}
    r = client.post("/books/entries/new", data=data)
    assert r.status_code == 302
    entry = page(client, r.location)
    assert "CRJ-1" in entry and "Reverse this entry" in entry
    r = client.post(r.location + "/reverse", data={"reason": "Wrong amount"})
    assert "GJ-1" in page(client, r.location)

    bad = dict(data, **{"credit[]": ["", "99999"]})
    r = client.post("/books/entries/new", data=bad)
    assert r.status_code == 422 and "does not balance" in r.get_data(as_text=True)


def test_accounts_close_and_archive(client):
    client.post("/accounts", data={"code": "1030", "name": "Cash in Bank - USD",
                                   "type": "asset", "is_cash": "1"})
    assert "Cash in Bank - USD" in page(client, "/accounts")
    assert "1030 Cash in Bank - USD" in page(client, "/collections/new")
    r = client.post("/accounts/1100", data={"name": "Trade Receivables"})  # auto: stays active
    assert "stays active" in page(client, "/accounts")

    issue(client)
    client.post("/books/close", data={"through": "2024-11-30"})
    assert "2024-11-30" in page(client, "/books")
    r = client.post("/invoices/issue", data=form() | {"token": "t9"})
    assert r.status_code == 422 and "closed through 2024-11-30" in r.get_data(as_text=True)

    z = client.get("/books/archive?year=2024")
    assert z.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.data)).namelist()
    assert "MANIFEST.txt" in names and "gl.pdf" in names

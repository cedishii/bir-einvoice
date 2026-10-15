from datetime import date
from decimal import Decimal as D

import pytest

from einvoice.books import postings, taxes
from einvoice.books.ledger import BooksError
from einvoice.models import VATABLE
from einvoice.web.app import create_app
from tests.conftest import make_invoice
from tests.conftest import sign_in


def amounts(report) -> dict[str, D]:
    """Worksheet label -> amount (last cell)."""
    return {r.cells[0]: r.cells[-1] for r in report.rows if r.kind != "group"}


def supplier(conn):
    return postings.create_supplier(conn, name="ACME", tin="123456789", branch="00000",
                                    address="Makati")


def bill(conn, sid, d, net, vat, account="61270", category="services"):
    return postings.create_bill(conn, supplier_id=sid, bill_date=d, supplier_ref=str(d),
                                description="x", account_code=account, category=category,
                                net=D(net), vat=D(vat))


def pay(conn, sid, d, ewt, base):
    return postings.create_payment(conn, supplier_id=sid, pay_date=d, reference=str(d),
                                   amount=D(base) + D("1000"), cash_account="11040",
                                   ewt=D(ewt), atc="WI100", ewt_base=D(base), ewt_rate=D("5"))


def issue(repo, d, **kw):
    return repo.issue(make_invoice(issue_date=d, rate_date=d, **kw))


def sheet(conn, code, year, n, params=None):
    return taxes.worksheet(conn, taxes.BY_CODE[code], year, n, params)


def test_2550q_with_input_vat_carried_over(repo):
    conn = repo.conn
    sid = supplier(conn)
    bill(conn, sid, date(2024, 8, 1), "100000", "12000")      # Q3: input VAT, no sales
    issue(repo, date(2024, 11, 25), trans_class=VATABLE)      # Q4: output VAT 547,200
    bill(conn, sid, date(2024, 11, 3), "50000", "6000")
    q3 = amounts(sheet(conn, "2550Q", 2024, 3))
    assert q3["VAT payable"] == 0
    assert q3["Excess input VAT, carried over to the next quarter"] == D("12000.00")
    q4 = amounts(sheet(conn, "2550Q", 2024, 4))
    assert q4["VATable sales"] == D("4560000.00") and q4["Output VAT"] == D("547200.00")
    assert q4["Less: input VAT carried over from the previous quarter"] == D("12000.00")
    assert q4["VAT payable"] == D("529200.00")  # 547,200 - 12,000 - 6,000


def test_withholding_returns_and_alphalists(repo):
    conn = repo.conn
    sid = supplier(conn)
    for d, tax, base in [(date(2024, 10, 5), "2500", "50000"), (date(2024, 11, 5), "1000", "20000"),
                         (date(2024, 12, 5), "500", "10000")]:
        bill(conn, sid, d, base, "0", category="exempt")
        pay(conn, sid, d, tax, base)
    assert amounts(sheet(conn, "0619E", 2024, 11))["Amount to remit for the month"] == D("1000.00")
    with pytest.raises(BooksError, match="not filed"):
        sheet(conn, "0619E", 2024, 12)  # the third month goes on the 1601EQ
    eq = sheet(conn, "1601EQ", 2024, 4)
    a = amounts(eq)
    assert a["Total tax withheld for the quarter"] == D("4000.00")
    assert a["Less: remitted with 0619E for October"] == D("2500.00")
    assert a["Tax still due for the quarter"] == D("500.00")
    q = sheet(conn, "QAP", 2024, 4)
    assert q.rows[0].cells[1:5] == ["123-456-789-00000", "ACME", "WI100", D("80000.00")]
    annual = sheet(conn, "1604E", 2024, 1)
    assert ["October", "", "", D("50000.00"), D("2500.00")] in [r.cells for r in annual.rows]


def test_corporate_income_tax_quarters_and_year(repo):
    conn = repo.conn
    sid = supplier(conn)
    issue(repo, date(2024, 2, 10))                              # 4,560,000
    bill(conn, sid, date(2024, 2, 15), "1000000", "120000")
    issue(repo, date(2024, 5, 10))                              # another 4,560,000
    q1 = amounts(sheet(conn, "1702Q", 2024, 1))
    assert q1["Taxable income"] == D("3560000.00")
    assert q1["Regular income tax (25% of taxable income)"] == D("890000.00")
    assert q1["Minimum corporate income tax (2% of gross income)"] == D("91200.00")
    assert q1["Tax payable"] == D("890000.00")
    q2 = amounts(sheet(conn, "1702Q", 2024, 2))
    assert q2["Income tax due (the higher of the two)"] == D("2030000.00")
    assert q2["Less: income tax paid in earlier quarters of the year"] == D("890000.00")
    assert q2["Tax payable"] == D("1140000.00")
    year = amounts(sheet(conn, "1702RT", 2024, 1))
    assert year["Less: income tax paid in earlier quarters of the year"] == D("2030000.00")
    assert year["Tax payable"] == 0
    with pytest.raises(BooksError):
        sheet(conn, "1702Q", 2024, 4)  # Q4 is the annual return


def test_mcit_and_cost_of_sales(repo):
    conn = repo.conn
    sid = supplier(conn)
    issue(repo, date(2024, 2, 10))                              # 4,560,000
    bill(conn, sid, date(2024, 2, 15), "4500000", "0", category="exempt")
    a = amounts(sheet(conn, "1702Q", 2024, 1))
    assert a["Regular income tax (25% of taxable income)"] == D("15000.00")
    assert a["Income tax due (the higher of the two)"] == D("91200.00")  # MCIT
    conn.execute("UPDATE accounts SET is_cost = 1 WHERE code = '61270'")  # rent as cost of services
    a = amounts(sheet(conn, "1702Q", 2024, 1))
    assert a["Gross income from operations"] == D("60000.00")
    assert a["Income tax due (the higher of the two)"] == D("15000.00")


def test_graduated_rates_and_individual_methods(repo):
    assert [taxes.graduated_tax(D(x)) for x in ("250000", "400000", "800000", "1000000")] == \
        [0, D("22500.00"), D("102500.00"), D("152500.00")]
    conn = repo.conn
    issue(repo, date(2024, 2, 10))                              # 4,560,000
    with postings.transaction(conn):
        taxes.save_tax_settings(conn, {"taxpayer": "individual", "vat_registered": "no",
                                       "individual_method": "eight"})
    a = amounts(sheet(conn, "1701Q", 2024, 1))
    assert a["Income tax due (8% of the excess)"] == D("344800.00")  # 8% x 4,310,000
    with postings.transaction(conn):
        taxes.save_tax_settings(conn, {"individual_method": "osd"})
    a = amounts(sheet(conn, "1701A", 2024, 1))
    assert a["Taxable income"] == D("2736000.00")  # 60% of 4,560,000
    assert a["Income tax due (graduated rates)"] == D("623300.00")  # 402,500 + 30% x 736,000
    p = amounts(sheet(conn, "2551Q", 2024, 1))
    assert p["Percentage tax due"] == D("136800.00")
    c = amounts(sheet(conn, "1700", 2024, 1, {"compensation": D("500000"), "withheld": D("40000")}))
    assert c["Income tax due (graduated rates)"] == D("42500.00") and c["Tax payable"] == D("2500.00")


def test_settings_validation(repo):
    with pytest.raises(BooksError, match="only for non-VAT"):
        taxes.save_tax_settings(repo.conn, {"individual_method": "eight"})
    with pytest.raises(BooksError, match="0 to 100"):
        taxes.save_tax_settings(repo.conn, {"corp_rate": "120"})
    assert taxes.tax_settings(repo.conn)["corp_rate"] == "25"


def test_calendar(repo):
    issue(repo, date(2024, 11, 25))
    cal = taxes.calendar(repo.conn, around=date(2024, 12, 20))
    seen = {(d.code, d.period): d for d in cal}
    assert seen[("1702Q", "Q3 2024")].due == date(2024, 11, 29)
    assert seen[("1702Q", "Q3 2024")].status == "past"
    assert seen[("0619E", "November 2024")].due == date(2024, 12, 10)
    assert seen[("2550Q", "Q4 2024")].due == date(2025, 1, 27)  # Jan 25 is a Saturday
    assert seen[("2550Q", "Q4 2024")].status == "open"
    assert ("Books", "2024") in seen and ("SLS", "Q4 2024") in seen
    assert not any(d.code in ("2551Q", "1701Q") for d in cal)  # not for a VAT corporation
    assert any(d.code == "2551Q" for d in taxes.calendar(repo.conn, around=date(2024, 12, 20),
                                                         show_all=True))


@pytest.fixture
def client(config):
    app = create_app(config)
    app.testing = True
    return sign_in(app)


def test_tax_pages(client):
    page = client.get("/taxes").get_data(as_text=True)
    assert "Calendar" in page and "2550Q" in page and "2550M" in page  # 2550M only in the note
    for f in taxes.FORMS:
        r = client.get(f"/taxes/{f.code}?year=2024&n={taxes.periods(f, 2024)[0]}")
        assert r.status_code == 200, f.code
    assert client.get("/taxes/2550Q?year=2024&n=4&format=pdf").data.startswith(b"%PDF")
    assert b"Output VAT" in client.get("/taxes/2550Q?year=2024&n=4&format=csv").data
    r = client.get("/taxes/1700?year=2024&compensation=500,000&withheld=40000")
    assert "42,500.00" in r.get_data(as_text=True)
    r = client.post("/taxes/settings", data={"taxpayer": "corporation", "vat_registered": "yes",
                                             "corp_rate": "20", "mcit_rate": "2",
                                             "mcit_applies": "yes", "individual_method": "itemized",
                                             "percentage_rate": "3"})
    assert r.status_code == 302
    assert "Regular income tax (20%" in client.get("/taxes/1702Q?year=2024&n=1").get_data(as_text=True)
    client.post("/accounts/61270", data={"name": "Rent Expense", "active": "1", "is_cost": "1"})
    assert 'name="is_cost" value="1" form="acct-61270" checked' in client.get("/accounts").get_data(as_text=True)

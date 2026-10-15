from datetime import date
from decimal import Decimal as D

from einvoice.books import chart, ledger, postings, reports, taxes
from einvoice.books.ledger import Line
from einvoice.models import VATABLE
from einvoice.storage import db
from einvoice.web.app import create_app
from tests.conftest import make_invoice, sign_in

YEAR = (date(2024, 1, 1), date(2024, 12, 31))


def amounts(report) -> dict[str, D]:
    return {r.cells[0]: r.cells[-1] for r in report.rows if r.kind != "group"}


def entry(conn, d, particulars, *lines):
    return postings.create_manual_entry(conn, entry_date=d, reference="", particulars=particulars,
                                        lines=[Line(a, debit=D(dr), credit=D(cr)) for a, dr, cr in lines])


def test_starter_chart_is_installed(repo):
    accounts = ledger.accounts(repo.conn)
    assert len(accounts) == len(chart.STARTER_CHART)
    roles = {a["role"]: a["code"] for a in accounts if a["role"]}
    assert roles == {"bank": "11040", "ar": "11110", "cwt": "11140", "input_vat": "11410",
                     "ap": "21010", "output_vat": "21210", "ewt_payable": "21250",
                     "retained_earnings": "31030", "sales": "41010", "fx_gain": "42030",
                     "fx_loss": "62030"}
    assert {a["section"] for a in accounts} == set(chart.STATEMENT_ORDER)
    assert all(a["is_cost"] for a in accounts if a["section"] == "Cost of services")


def test_earning_versus_borrowing(repo):
    """Earning PHP 500k is income; borrowing PHP 500k is a liability, not income."""
    conn = repo.conn
    repo.issue(make_invoice(issue_date=date(2024, 3, 1), rate_date=date(2024, 3, 1),
                            rate=D("0.04"), lines=make_invoice().lines[:1]))  # 12M JPY = 480,000
    entry(conn, date(2024, 3, 5), "Bank loan", ("11040", "500000", "0"), ("21510", "0", "500000"))
    i = amounts(reports.income_statement(conn, *YEAR))
    assert i["41010 Service income"] == D("480000.00")
    assert i["NET INCOME (LOSS)"] == D("480000.00")      # the loan is not income
    b = amounts(reports.balance_sheet(conn, YEAR[1]))
    assert b["21510 Loans payable"] == D("500000.00")
    assert b["TOTAL ASSETS"] == D("980000.00")            # receivable 480k + cash 500k
    assert b["TOTAL LIABILITIES AND EQUITY"] == D("980000.00")
    assert b["Net income (loss) not yet closed to retained earnings"] == D("480000.00")


def test_income_statement_sections_and_monthly(repo):
    conn = repo.conn
    repo.issue(make_invoice(trans_class=VATABLE))                       # Nov: 4,560,000 income
    entry(conn, date(2024, 10, 31), "October payroll", ("51010", "300000", "0"),
          ("11040", "0", "300000"))                                     # cost of services
    entry(conn, date(2024, 11, 30), "Rent", ("61270", "50000", "0"), ("11040", "0", "50000"))
    entry(conn, date(2024, 12, 31), "Income tax", ("69010", "100000", "0"),
          ("21270", "0", "100000"))
    i = amounts(reports.income_statement(conn, *YEAR))
    assert i["Gross profit"] == D("4260000.00")
    assert i["Income from operations"] == D("4210000.00")
    assert i["NET INCOME (LOSS)"] == D("4110000.00")
    m = reports.monthly_summary(conn, date(2024, 10, 1), date(2024, 12, 31))
    assert [r.cells for r in m.rows] == [
        ["October 2024", D("0"), D("300000.00"), D("-300000.00")],
        ["November 2024", D("4560000.00"), D("50000.00"), D("4510000.00")],
        ["December 2024", D("0"), D("100000.00"), D("-100000.00")],
        ["Total", D("4560000.00"), D("450000.00"), D("4110000.00")]]
    # Income tax worksheets leave the provision for income tax out of deductions.
    t = taxes.income_statement(conn, *YEAR)
    assert (t["cost"], t["expenses"]) == (D("300000.00"), D("50000.00"))
    b = reports.balance_sheet(conn, YEAR[1])
    assert not b.notes[:-1]  # balanced: only the explanation note


def test_upgrade_keeps_used_starter_accounts(config, monkeypatch):
    with monkeypatch.context() as m:  # a database from before migration 9
        m.setattr(db, "MIGRATIONS", db.MIGRATIONS[:8])
        conn = db.connect(config.db_path)
        with postings.transaction(conn):
            ledger.post(conn, book="GJ", entry_date=date(2024, 1, 2), particulars="Capital",
                        lines=[Line("1020", debit=D("1000")), Line("3000", credit=D("1000"))])
        conn.close()
    conn = db.connect(config.db_path)
    old = {a["code"]: a for a in ledger.accounts(conn) if len(a["code"]) == 4}
    assert set(old) == {"1020", "3000"} and not any(a["active"] for a in old.values())
    assert ledger.account_for(conn, "ar") == "11110"
    assert ledger.verify(conn) == [] and ledger.verify_audit(conn) == []
    tb = reports.trial_balance(conn, date(2024, 12, 31))
    assert tb.rows[-1].cells[3] == tb.rows[-1].cells[4] == D("1000.00")
    conn.close()


def test_statement_pages(config):
    app = create_app(config)
    c = sign_in(app)
    for key in ("is", "bs", "monthly"):
        r = c.get(f"/books/report/{key}?from=2024-01-01&to=2024-12-31")
        assert r.status_code == 200, key
        assert c.get(f"/books/report/{key}?from=2024-01-01&to=2024-12-31&format=pdf").data.startswith(b"%PDF")
    page = c.get("/books").get_data(as_text=True)
    assert "Income vs expenses" in page and "Balance Sheet" in page
    accounts = c.get("/accounts").get_data(as_text=True)
    assert "Cost of services" in accounts and "51010" in accounts and "Loans payable" in accounts


CHART_FILE = """code,name,type,section,flags
11040,Cash in bank - Example Bank,asset,Current assets,cash role:bank
11110,Trade receivables,asset,Current assets,role:ar
11140,EWT receivable,asset,Current assets,role:cwt
11410,Input VAT,asset,Current assets,role:input_vat
21010,Trade payables,liability,Current liabilities,role:ap
21210,Output VAT,liability,Current liabilities,role:output_vat
21250,EWT payable,liability,Current liabilities,role:ewt_payable
31030,Retained earnings,equity,Equity,role:retained_earnings
41010,Service income,income,Revenue,role:sales
42030,Forex gain,income,Other income,role:fx_gain
62030,Forex loss,expense,Other expenses,role:fx_loss
51500,Subcontractors,expense,Cost of services,cost
"""


def test_company_chart_file_is_applied_once(config):
    import pytest
    (config.data_dir / chart.LOCAL_FILE).write_text(CHART_FILE, encoding="utf-8")
    conn = db.connect(config.db_path)
    active = {a["code"]: a for a in ledger.accounts(conn, active_only=True)}
    assert set(active) == {line.split(",")[0] for line in CHART_FILE.splitlines()[1:]}
    assert active["11040"]["name"] == "Cash in bank - Example Bank" and active["11040"]["is_cash"]
    assert active["51500"]["is_cost"] and ledger.account_for(conn, "ar") == "11110"
    assert not chart.apply_local(conn, config.data_dir / chart.LOCAL_FILE)  # unchanged: skipped
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'chart of accounts'"
                        ).fetchone()[0] == 1
    conn.close()
    (config.data_dir / chart.LOCAL_FILE).write_text(
        CHART_FILE.replace("role:fx_loss", ""), encoding="utf-8")
    with pytest.raises(chart.ChartFileError, match="fx_loss"):
        db.connect(config.db_path)

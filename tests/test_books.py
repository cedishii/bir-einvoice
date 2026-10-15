import io
import sqlite3
import zipfile
from datetime import date
from decimal import Decimal as D

import pytest

from einvoice.books import ledger, postings, printing, reports
from einvoice.books.ledger import BooksError, Line
from einvoice.models import VATABLE, today_ph
from einvoice.storage.db import connect
from tests.conftest import make_invoice

Q4 = (date(2024, 10, 1), date(2024, 12, 31))


def lines_of(conn, entry_id):
    return [(l["account_code"], D(l["debit"]), D(l["credit"]))
            for l in ledger.entry_lines(conn, entry_id)]


def supplier(conn, name="ACME OFFICE SUPPLY", tin="123456789"):
    return postings.create_supplier(conn, name=name, tin=tin, branch="00000",
                                    address="Makati City")


def test_issuing_posts_to_the_sales_journal(repo):
    inv = repo.issue(make_invoice())
    e = ledger.entry_for(repo.conn, "invoice", inv.id)
    assert (e["book"], e["entry_no"], e["reference"]) == ("SJ", 1, "SI 0001773")
    assert lines_of(repo.conn, e["id"]) == [("11110", D("4560000.00"), D("0")),
                                            ("41010", D("0"), D("4560000.00"))]
    c = repo.conn.execute("SELECT * FROM customers").fetchone()
    assert c["name"] == "MICRONET CO. LTD." and e["party_id"] == c["id"]


def test_vatable_invoice_credits_output_vat(repo):
    inv = repo.issue(make_invoice(trans_class=VATABLE))
    e = ledger.entry_for(repo.conn, "invoice", inv.id)
    assert lines_of(repo.conn, e["id"]) == [("11110", D("5107200.00"), D("0")),
                                            ("41010", D("0"), D("4560000.00")),
                                            ("21210", D("0"), D("547200.00"))]


def test_void_posts_a_reversal(repo):
    inv = repo.issue(make_invoice())
    repo.void(inv.control_no, "Wrong client")
    rev = ledger.reversal_of(repo.conn, ledger.entry_for(repo.conn, "invoice", inv.id)["id"])
    assert rev["book"] == "GJ" and "Wrong client" in rev["particulars"]
    assert lines_of(repo.conn, rev["id"]) == [("11110", D("0"), D("4560000.00")),
                                              ("41010", D("4560000.00"), D("0"))]
    assert postings.invoice_balance(repo.conn, inv.id) == 0


def test_posted_entries_cannot_be_changed(repo):
    repo.issue(make_invoice())
    for sql in ("UPDATE journal_entries SET particulars = 'x'",
                "DELETE FROM journal_entries",
                "UPDATE journal_lines SET debit = '1.00'",
                "DELETE FROM journal_lines"):
        with pytest.raises(sqlite3.IntegrityError, match="final"):
            repo.conn.execute(sql)


def test_verify_detects_tampering(repo):
    repo.issue(make_invoice())
    repo.issue(make_invoice())
    assert ledger.verify(repo.conn) == []
    repo.conn.execute("DROP TRIGGER journal_lines_final_u")  # someone edits the file
    repo.conn.execute("UPDATE journal_lines SET debit = '1.00' WHERE entry_id = 1 AND line_no = 1")
    problems = ledger.verify(repo.conn)
    assert any("SJ 1: its contents were changed" in p for p in problems)
    assert any("SJ 1: debits and credits do not balance" in p for p in problems)


def test_unbalanced_entry_is_refused(repo):
    with pytest.raises(BooksError, match="does not balance"):
        postings.create_manual_entry(repo.conn, entry_date=date(2024, 11, 1), reference="",
                                     particulars="x", lines=[Line("11040", debit=D("10")),
                                                             Line("31010", credit=D("9"))])
    assert repo.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_manual_entries_go_to_the_right_book(repo):
    conn = repo.conn
    capital = postings.create_manual_entry(
        conn, entry_date=date(2024, 10, 1), reference="DS-1", particulars="Capital paid in",
        lines=[Line("11040", debit=D("100000")), Line("31010", credit=D("100000"))])
    rent = postings.create_manual_entry(
        conn, entry_date=date(2024, 10, 5), reference="CV-1", particulars="October rent",
        lines=[Line("61270", debit=D("30000")), Line("11040", credit=D("30000"))])
    accrual = postings.create_manual_entry(
        conn, entry_date=date(2024, 10, 31), reference="", particulars="Depreciation",
        lines=[Line("61190", debit=D("500")), Line("12120", credit=D("500"))])
    books = [ledger.entry(conn, i)["book"] for i in (capital, rent, accrual)]
    assert books == ["CRJ", "CDJ", "GJ"]
    rev = postings.reverse_manual(conn, rent, "Paid twice")
    assert ledger.entry(conn, rev)["book"] == "GJ"
    with pytest.raises(BooksError, match="already reversed"):
        postings.reverse_manual(conn, rent, "again")


def test_old_invoices_are_posted_once_on_upgrade(config, monkeypatch):
    conn = connect(config.db_path)
    from einvoice.storage.repository import Repository
    repo = Repository(conn, config.control_no_start)
    with monkeypatch.context() as m:  # as if issued before the books existed
        m.setattr(postings, "post_invoice", lambda *a, **k: None)
        m.setattr(postings, "post_invoice_void", lambda *a, **k: None)
        repo.issue(make_invoice())
        voided = repo.issue(make_invoice())
        repo.void(voided.control_no, "Duplicate")
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    conn.close()

    conn = connect(config.db_path)  # the upgrade runs on connect
    books = [r["book"] for r in conn.execute("SELECT book FROM journal_entries ORDER BY id")]
    assert books == ["SJ", "SJ", "GJ"]
    assert all(r["created_by"] == "system (books upgrade)"
               for r in conn.execute("SELECT created_by FROM journal_entries"))
    conn.close()
    conn = connect(config.db_path)  # and only once
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 3
    assert ledger.verify(conn) == []
    conn.close()


def test_collection_with_withholding_and_fx_loss(repo):
    conn = repo.conn
    inv = repo.issue(make_invoice(trans_class=VATABLE))  # AR 5,107,200.00
    cust = conn.execute("SELECT customer_id FROM invoices").fetchone()[0]
    cid = postings.create_collection(
        conn, customer_id=cust, invoice_id=inv.id, coll_date=date(2024, 12, 10),
        reference="OR 501", applied=D("5107200.00"), cash=D("5000000.00"),
        cash_account="11040", cwt=D("91200.00"), atc="wc160", cwt_base=D("4560000.00"),
        cwt_rate=D("2"))
    e = ledger.entry_for(conn, "collection", cid)
    assert e["book"] == "CRJ"
    assert lines_of(conn, e["id"]) == [("11040", D("5000000.00"), D("0")),
                                       ("11140", D("91200.00"), D("0")),
                                       ("11110", D("0"), D("5107200.00")),
                                       ("62030", D("16000.00"), D("0"))]
    assert postings.invoice_balance(conn, inv.id) == 0
    with pytest.raises(ValueError, match="collections recorded"):
        repo.void(inv.control_no, "x")
    assert repo.get(inv.control_no).status == "issued"  # the void was rolled back


def test_collection_fx_gain_and_limits(repo):
    conn = repo.conn
    inv = repo.issue(make_invoice())  # AR 4,560,000.00
    cust = conn.execute("SELECT customer_id FROM invoices").fetchone()[0]
    with pytest.raises(BooksError, match="only 4,560,000.00 left"):
        postings.create_collection(conn, customer_id=cust, invoice_id=inv.id,
                                   coll_date=date(2024, 12, 1), reference="R1",
                                   applied=D("5000000"), cash=D("5000000"), cash_account="11040")
    cid = postings.create_collection(conn, customer_id=cust, invoice_id=inv.id,
                                     coll_date=date(2024, 12, 1), reference="R1",
                                     applied=D("4560000"), cash=D("4600000"), cash_account="11040")
    e = ledger.entry_for(conn, "collection", cid)
    assert ("42030", D("0"), D("40000.00")) in lines_of(conn, e["id"])
    postings.cancel(conn, "collection", cid, "Bounced")
    assert postings.invoice_balance(conn, inv.id) == D("4560000.00")
    assert ledger.reversal_of(conn, e["id"]) is not None


def test_bill_and_payment_with_expanded_withholding(repo):
    conn = repo.conn
    sid = supplier(conn)
    bid = postings.create_bill(conn, supplier_id=sid, bill_date=date(2024, 11, 3),
                               supplier_ref="SI 88", description="Office rent November",
                               account_code="61270", category="services",
                               net=D("50000"), vat=D("6000"))
    e = ledger.entry_for(conn, "bill", bid)
    assert e["book"] == "PJ"
    assert lines_of(conn, e["id"]) == [("61270", D("50000.00"), D("0")),
                                       ("11410", D("6000.00"), D("0")),
                                       ("21010", D("0"), D("56000.00"))]
    pid = postings.create_payment(conn, supplier_id=sid, bill_id=bid, pay_date=date(2024, 11, 5),
                                  reference="CHK 1001", amount=D("56000"), cash_account="11040",
                                  ewt=D("2500"), atc="WI100", ewt_base=D("50000"),
                                  ewt_rate=D("5"))
    p = ledger.entry_for(conn, "payment", pid)
    assert p["book"] == "CDJ"
    assert lines_of(conn, p["id"]) == [("21010", D("56000.00"), D("0")),
                                       ("11040", D("0"), D("53500.00")),
                                       ("21250", D("0"), D("2500.00"))]
    assert postings.bill_balance(conn, bid) == 0
    with pytest.raises(BooksError, match="payments recorded"):
        postings.cancel(conn, "bill", bid, "x")


def test_bill_rules(repo):
    conn = repo.conn
    sid = supplier(conn)
    base = dict(supplier_id=sid, bill_date=date(2024, 11, 3), supplier_ref="1",
                description="Books", account_code="61200", net=D("100"))
    with pytest.raises(BooksError, match="no input VAT"):
        postings.create_bill(conn, category="exempt", vat=D("12"), **base)
    with pytest.raises(BooksError, match="expense or asset account"):
        postings.create_bill(conn, category="goods", vat=D("12"), **base | {"account_code": "11410"})
    with pytest.raises(BooksError, match="future"):
        postings.create_bill(conn, category="goods", vat=D("12"),
                             **base | {"bill_date": date(today_ph().year + 1, 1, 1)})


def test_closed_period(repo):
    conn = repo.conn
    with postings.transaction(conn):
        ledger.close_through(conn, date(2024, 11, 30))
    with pytest.raises(BooksError, match="closed through 2024-11-30"):
        postings.create_manual_entry(conn, entry_date=date(2024, 11, 30), reference="",
                                     particulars="x", lines=[Line("11040", debit=D("1")),
                                                             Line("31010", credit=D("1"))])
    with pytest.raises(BooksError, match="closed"):
        repo.issue(make_invoice())
    assert repo.next_control_no() == "0001773"  # the number was not used up
    with pytest.raises(BooksError, match="can't be reopened"):
        with postings.transaction(conn):
            ledger.close_through(conn, date(2024, 10, 31))
    repo.issue(make_invoice(issue_date=date(2024, 12, 1), rate_date=date(2024, 12, 1)))


def _sample_books(repo):
    conn = repo.conn
    a = repo.issue(make_invoice(trans_class=VATABLE))
    repo.issue(make_invoice(issue_date=date(2024, 12, 2), rate_date=date(2024, 12, 2)))
    cust = conn.execute("SELECT customer_id FROM invoices").fetchone()[0]
    postings.create_collection(conn, customer_id=cust, invoice_id=a.id,
                               coll_date=date(2024, 12, 10), reference="OR 1",
                               applied=D("1000000"), cash=D("980000"), cash_account="11040",
                               cwt=D("20000"), atc="WC160", cwt_base=D("1000000"),
                               cwt_rate=D("2"))
    sid = supplier(conn)
    bid = postings.create_bill(conn, supplier_id=sid, bill_date=date(2024, 10, 15),
                               supplier_ref="88", description="Rent", account_code="61270",
                               category="services", net=D("50000"), vat=D("6000"))
    postings.create_bill(conn, supplier_id=sid, bill_date=date(2024, 11, 15),
                         supplier_ref="89", description="Books", account_code="61200",
                         category="exempt", net=D("1000"), vat=D("0"))
    postings.create_payment(conn, supplier_id=sid, bill_id=bid, pay_date=date(2024, 11, 5),
                            reference="CHK 1", amount=D("56000"), cash_account="11040",
                            ewt=D("2500"), atc="WI100", ewt_base=D("50000"), ewt_rate=D("5"))
    return conn


def test_trial_balance_and_ledgers(repo):
    conn = _sample_books(repo)
    tb = reports.trial_balance(conn, date(2024, 12, 31))
    total = tb.rows[-1].cells
    assert total[3] == total[4] and not tb.notes
    by_code = {r.cells[0]: r.cells[3:] for r in tb.rows[:-1]}
    # AR: 5,107,200 + 4,560,000 - 1,000,000
    assert by_code["11110"] == [D("8667200.00"), None]
    assert by_code["21210"] == [None, D("547200.00")]
    assert by_code["21010"] == [None, D("1000.00")]

    gl = reports.general_ledger(conn, *Q4, account="11040")
    assert gl.rows[-1].cells[-2:] == [D("926500.00"), None]  # 980,000 - 53,500
    ar = reports.subsidiary_ledger(conn, "customer", *Q4)
    assert ar.rows[-1].cells[-1] == D("8667200.00")
    ap = reports.subsidiary_ledger(conn, "supplier", *Q4)
    assert ap.rows[-1].cells[-1] == D("1000.00")
    sj = reports.journal(conn, "SJ", *Q4)
    assert sj.rows[-1].cells[5] == sj.rows[-1].cells[6] == D("9667200.00")


def test_sls_slp_and_withholding(repo):
    conn = _sample_books(repo)
    s = reports.sls(conn, *Q4)
    assert s.rows[0].cells[3:] == [D("0"), D("4560000.00"), D("4560000.00"),
                                   D("547200.00"), D("5107200.00")]
    p = reports.slp(conn, *Q4)
    assert p.rows[0].cells[0] == "123-456-789-00000"
    assert p.rows[0].cells[3:] == [D("57000.00"), D("1000.00"), D("0"), D("50000.00"),
                                   D("50000.00"), D("0"), D("0"), D("6000.00"), D("56000.00")]
    w = reports.withholding(conn, "issued", *Q4)
    assert [c.label for c in w.columns][4:7] == ["Oct income", "Nov income", "Dec income"]
    assert w.rows[0].cells[2:] == ["WI100", D("5"), D("0"), D("50000.00"), D("0"),
                                   D("50000.00"), D("2500.00")]
    r = reports.withholding(conn, "received", *Q4)
    assert r.rows[0].cells[2] == "WC160" and r.rows[0].cells[-1] == D("20000.00")
    with pytest.raises(BooksError, match="calendar quarter"):
        reports.withholding(conn, "issued", date(2024, 10, 1), date(2024, 10, 31))


def test_pdf_csv_and_year_archive(repo, config):
    conn = _sample_books(repo)
    gl = reports.general_ledger(conn, *Q4)
    assert printing.report_pdf(gl, config).startswith(b"%PDF")
    text = printing.report_csv(gl, config).decode("utf-8-sig")
    assert "General Ledger" in text and "4560000.00" in text
    z = zipfile.ZipFile(io.BytesIO(printing.year_archive(conn, config, 2024)))
    names = z.namelist()
    assert "MANIFEST.txt" in names and "journal-SJ.pdf" in names and "sls-2024Q4.csv" in names
    manifest = z.read("MANIFEST.txt").decode()
    assert "Journal integrity check: OK" in manifest and "journal-SJ.csv" in manifest


def test_opening_balances_go_to_the_general_journal(repo):
    eid = postings.create_manual_entry(
        repo.conn, entry_date=date(2024, 10, 1), reference="", particulars="Opening balances",
        lines=[Line("11040", debit=D("2000000")), Line("31010", credit=D("2000000"))],
        opening=True)
    e = ledger.entry(repo.conn, eid)
    assert (e["book"], e["source_type"]) == ("GJ", "opening")
    postings.reverse_manual(repo.conn, eid, "Wrong amount")

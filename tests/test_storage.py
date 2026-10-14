import re
import threading
from decimal import Decimal as D

import pytest

from einvoice.models import InvoiceLine
from einvoice.storage.db import connect
from einvoice.storage.repository import Repository
from tests.conftest import make_invoice


def test_first_number_comes_from_config(repo):
    assert repo.next_control_no() == "0001773"
    assert repo.issue(make_invoice()).control_no == "0001773"
    assert repo.next_control_no() == "0001774"


def test_numbers_increase_by_one(repo):
    numbers = [repo.issue(make_invoice()).control_no for _ in range(3)]
    assert numbers == ["0001773", "0001774", "0001775"]


def test_void_keeps_its_number_and_it_is_never_reused(repo):
    first = repo.issue(make_invoice())
    repo.void(first.control_no, "Wrong client address")
    assert repo.get("0001773").status == "void"
    assert repo.issue(make_invoice()).control_no == "0001774"


def test_failed_save_does_not_consume_a_number(repo):
    bad = make_invoice()
    bad.lines[0].item_name = None  # NOT NULL violation inside the transaction
    with pytest.raises(Exception):
        repo.issue(bad)
    assert bad.control_no == "" and bad.status == "draft"
    assert repo.issue(make_invoice()).control_no == "0001773"


def test_counter_survives_restart(config):
    conn = connect(config.db_path)
    Repository(conn, 1773).issue(make_invoice())
    conn.close()
    conn = connect(config.db_path)
    # A different start value is ignored once numbers have been issued.
    assert Repository(conn, 1).next_control_no() == "0001774"
    conn.close()


def test_concurrent_issues_never_share_a_number(config):
    connect(config.db_path).close()
    results, errors = [], []

    def worker():
        conn = connect(config.db_path)
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            for _ in range(5):
                results.append(Repository(conn, 1773).issue(make_invoice()).control_no)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == [f"{n:07d}" for n in range(1773, 1793)]


def test_round_trip_keeps_exact_decimals(repo):
    inv = make_invoice(rate=D("0.384215"), lines=[
        InvoiceLine("IT Services", "Dev", D("1.5"), "HOUR", D("333.33")),
        InvoiceLine("IT Services", "Support", D("1"), "", D("1000")),
    ])
    saved = repo.issue(inv)
    loaded = repo.get(saved.control_no)
    assert loaded.rate == D("0.384215")
    assert [l.sales_php for l in loaded.lines] == [l.sales_php for l in inv.lines]
    assert loaded.total_php == inv.total_php
    assert loaded.lines[1].unit == ""


def test_eis_key_is_8_uppercase_alphanumerics(repo):
    inv = repo.issue(make_invoice())
    assert re.fullmatch(r"[A-Z0-9]{8}", inv.eis_key)
    assert repo.get(inv.control_no).eis_key == inv.eis_key


def test_void_needs_reason_and_issued_invoice(repo):
    inv = repo.issue(make_invoice())
    with pytest.raises(ValueError):
        repo.void(inv.control_no, "  ")
    repo.void(inv.control_no, "Duplicate")
    with pytest.raises(ValueError):
        repo.void(inv.control_no, "Again")


def test_cannot_issue_twice(repo):
    inv = repo.issue(make_invoice())
    with pytest.raises(ValueError):
        repo.issue(inv)

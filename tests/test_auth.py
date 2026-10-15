import json
import sqlite3
from datetime import date

import pytest

from einvoice import auth, backup
from einvoice.books import ledger, reports
from einvoice.storage.db import connect
from einvoice.web.app import create_app
from tests.conftest import make_invoice, sign_in
from tests.test_form import form

PW = "secret1"


@pytest.fixture
def app(config):
    a = create_app(config)
    a.testing = True
    return a


@pytest.fixture
def conn(config):
    c = connect(config.db_path)
    yield c
    c.close()


def audit_rows(conn, **where):
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return conn.execute(sql + " ORDER BY id", list(where.values())).fetchall()


def test_sign_in_page_comes_first(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and r.location.startswith("/login")
    page = c.get("/login").get_data(as_text=True)
    assert "Sign in" in page and "Create account" in page and "auth-logo" in page


def test_create_account_and_use_it_right_away(app, conn):
    c = app.test_client()
    r = c.post("/signup", data={"full_name": "Ana  Reyes", "username": "Ana",
                                "password": PW, "password2": PW})
    assert r.status_code == 302 and r.location.endswith("/")
    assert "Ana Reyes" in c.get("/").get_data(as_text=True)
    assert conn.execute("SELECT username, full_name FROM users").fetchone()[:] == ("ana", "Ana Reyes")
    c.post("/logout")
    assert c.get("/").status_code == 302
    r = c.post("/login", data={"username": "ANA", "password": PW})
    assert r.status_code == 302
    assert [x["action"] for x in audit_rows(conn, entity_id="ana")] == [
        "create account", "sign in", "sign out", "sign in"]


def test_sign_up_problems(app):
    c = app.test_client()
    for data, msg in [({"password2": "other1"}, "don&#39;t match"),
                      ({"password": "12345", "password2": "12345"}, "at least 6"),
                      ({"username": "a"}, "Username")]:
        r = c.post("/signup", data={"full_name": "X", "username": "xavier", "password": PW,
                                    "password2": PW} | data)
        assert r.status_code == 422 and msg in r.get_data(as_text=True), msg
    c.post("/signup", data={"full_name": "X", "username": "xavier", "password": PW, "password2": PW})
    r = app.test_client().post("/signup", data={"full_name": "Y", "username": "xavier",
                                                "password": PW, "password2": PW})
    assert "taken" in r.get_data(as_text=True)


def test_wrong_passwords_are_recorded_and_lock_for_a_while(app, conn):
    auth.create_user(conn, username="maria", full_name="Maria Santos", password=PW)
    c = app.test_client()
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert c.post("/login", data={"username": "maria", "password": "nope"}).status_code == 401
    r = c.post("/login", data={"username": "maria", "password": PW})
    assert r.status_code == 401 and "Too many wrong passwords" in r.get_data(as_text=True)
    assert len(audit_rows(conn, action="sign-in failed", user="maria")) == 5


def test_change_password(app, conn):
    c = sign_in(app)
    c.post("/account/password", data={"current": "Passw0rd-test", "new": "newpass1",
                                      "new2": "newpass1"})
    assert app.test_client().post("/login", data={"username": "admin",
                                                  "password": "newpass1"}).status_code == 302


def test_both_users_see_what_the_other_did(app, conn):
    ana, jose = sign_in(app, "ana"), sign_in(app, "jose")
    jose.post("/invoices/issue", data=form() | {"token": "t1"})
    ana.post("/invoices/0001773/void", data={"reason": "Wrong client"})
    inv = conn.execute("SELECT * FROM invoices").fetchone()
    assert (inv["issued_by"], inv["voided_by"]) == ("jose", "ana")
    by = {r["action"]: r["user"] for r in audit_rows(conn, entity="invoice")}
    assert by == {"issue invoice": "jose", "void invoice": "ana"}
    assert conn.execute("SELECT created_by FROM journal_entries ORDER BY id").fetchall()[0][0] == "jose"
    for c in (ana, jose):
        page = c.get("/books/report/audit?from=2000-01-01").get_data(as_text=True)
        assert "issue invoice" in page and "void invoice" in page and "Wrong client" in page
    row = audit_rows(conn, action="issue invoice")[0]
    assert "@" in row["workstation"]


def test_changes_keep_before_and_after(app, conn):
    c = sign_in(app)
    c.post("/suppliers", data={"name": "ACME", "tin": "123456789", "address": "Makati"})
    c.post("/suppliers/1", data={"name": "ACME INC.", "tin": "123456789", "address": "Pasig"})
    row = audit_rows(conn, action="change supplier")[0]
    assert json.loads(row["changes"]) == {"name": ["ACME", "ACME INC."],
                                          "address": ["Makati", "Pasig"]}
    c.post("/accounts/5100", data={"name": "Office Rent", "active": "1"})
    assert json.loads(audit_rows(conn, action="change account")[0]["changes"]) == {
        "name": ["Rent Expense", "Office Rent"]}
    page = c.get("/books/report/audit?from=2000-01-01").get_data(as_text=True)
    assert "name: ACME -&gt; ACME INC." in page


def test_nothing_can_be_deleted_and_the_history_is_sealed(repo):
    conn = repo.conn
    repo.issue(make_invoice())
    for sql in ("DELETE FROM invoices", "DELETE FROM invoice_lines", "DELETE FROM customers",
                "DELETE FROM accounts WHERE code = '5990'", "DELETE FROM audit_log",
                "UPDATE audit_log SET user = 'someone else'", "UPDATE invoice_lines SET qty = '2'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert ledger.verify_audit(conn) == []
    conn.execute("DROP TRIGGER audit_log_final_u")  # someone edits the file by hand
    conn.execute("UPDATE audit_log SET user = 'someone else' WHERE id = 1")
    assert any("row 1: changed after" in p for p in ledger.verify_audit(conn))


def test_rows_from_before_the_upgrade_are_sealed(conn):
    conn.execute("INSERT INTO audit_log (at, user, action, detail) VALUES "
                 "('2026-01-01T00:00:00+08:00', 'old', 'post', 'legacy row')")
    ledger.seal_audit(conn)
    assert conn.execute("SELECT hash FROM audit_log").fetchone()[0] != ""
    assert ledger.verify_audit(conn) == []


def test_idle_sessions_end(app):
    c = sign_in(app)
    app.config["SESSION_IDLE_MINUTES"] = -1
    assert c.get("/").location.startswith("/login")


def test_backups(app, config, conn):
    c = sign_in(app)
    r = c.get("/backup")
    assert r.status_code == 200 and r.data.startswith(b"SQLite format 3")
    assert audit_rows(conn, action="backup")
    path = backup.daily(config)
    assert path is not None and path.exists() and backup.daily(config) is None  # once a day


def test_history_filters(app, conn):
    a = sign_in(app)
    sign_in(app, "jose").post("/invoices/issue", data=form() | {"token": "x"})
    r = reports.audit_log(conn, date(2000, 1, 1), date(2100, 1, 1), user="jose")
    assert {row.cells[1] for row in r.rows} == {"jose"}
    page = a.get("/books/report/audit?from=2000-01-01&user=jose").get_data(as_text=True)
    assert "issue invoice" in page and "Filtered by user jose" in page

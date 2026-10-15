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

PW = "Passw0rd-test"


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


def test_first_run_creates_the_administrator(app, conn):
    c = app.test_client()
    assert c.get("/").location.endswith("/setup")
    r = c.post("/setup", data={"full_name": "Ana Reyes", "username": "Ana",
                               "password": "Secret123", "password2": "Secret123"})
    assert r.status_code == 302 and r.location.endswith("/login")
    u = conn.execute("SELECT * FROM users").fetchone()
    assert (u["username"], u["role"], u["must_change"]) == ("ana", "admin", 0)
    assert c.get("/setup").location.endswith("/login")  # closed once a user exists
    assert c.get("/").location.startswith("/login")
    r = c.post("/login", data={"username": "ANA", "password": "Secret123"})
    assert r.status_code == 302
    page = c.get("/").get_data(as_text=True)
    assert "Ana Reyes" in page and "Add at least" not in page  # the notice is on /users
    assert "Add at least 2 active users" in c.get("/users").get_data(as_text=True)


def test_password_rules():
    for bad, msg in [("short1", "at least 8"), ("lettersonly", "letters and numbers"),
                     ("maria2024x", "username")]:
        with pytest.raises(auth.AuthError, match=msg):
            auth.check_password_rules(bad, "maria")


def test_failed_logins_are_audited_and_lock_the_account(app, conn):
    auth.create_user(conn, username="maria", full_name="Maria Santos", role="encoder",
                     password=PW, by="test")
    c = app.test_client()
    for _ in range(auth.MAX_FAILED_LOGINS):
        r = c.post("/login", data={"username": "maria", "password": "wrong-pass1"})
        assert r.status_code == 401
    r = c.post("/login", data={"username": "maria", "password": PW})  # right, but locked
    assert r.status_code == 401 and "Too many failed logins" in r.get_data(as_text=True)
    actions = [x["action"] for x in audit_rows(conn, entity="user", entity_id="maria")]
    assert actions.count("login failed") == 5 and actions[-1] == "login refused"
    c.post("/login", data={"username": "nobody", "password": "x"})
    assert audit_rows(conn, action="login failed", user="nobody")[0]["detail"] == "unknown username"


def test_new_user_must_choose_a_password(app, conn):
    admin = sign_in(app)
    r = admin.post("/users", data={"full_name": "Jose Cruz", "username": "jose",
                                   "role": "encoder", "password": "Temp1234"})
    assert r.status_code == 302
    jose = app.test_client()
    jose.post("/login", data={"username": "jose", "password": "Temp1234"})
    assert jose.get("/").location.endswith("/account")
    r = jose.post("/account/password", data={"current": "Temp1234", "new": "Mine5678x",
                                             "new2": "Mine5678x"})
    assert r.location.endswith("/")
    assert jose.get("/").status_code == 200


@pytest.mark.parametrize("role,can_issue,can_void,can_users", [
    ("auditor", False, False, False),
    ("encoder", True, False, False),
    ("accountant", True, True, False),
    ("admin", True, True, True),
])
def test_roles(app, conn, role, can_issue, can_void, can_users):
    boss = sign_in(app)  # an admin issues one invoice to work on
    boss.post("/invoices/issue", data=form() | {"token": "seed"})
    c = sign_in(app, username=f"u-{role}", role=role)
    c.post("/invoices/issue", data=form() | {"token": f"t-{role}"})
    issued = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    assert issued == (2 if can_issue else 1)
    c.post("/invoices/0001773/void", data={"reason": "test"})
    status = conn.execute("SELECT status FROM invoices WHERE control_no = '0001773'").fetchone()[0]
    assert status == ("void" if can_void else "issued")
    assert (c.get("/users").status_code == 200) == can_users
    assert c.get("/books/report/audit").status_code == 200  # everyone can read the trail
    if not can_issue:
        assert audit_rows(conn, action="refused", user=f"u-{role}")


def test_everything_is_recorded_under_the_signed_in_user(app, conn):
    c = sign_in(app, username="enc", role="encoder")
    c.post("/invoices/issue", data=form() | {"token": "t1"})
    inv = conn.execute("SELECT * FROM invoices").fetchone()
    assert inv["issued_by"] == "enc"
    assert conn.execute("SELECT created_by FROM journal_entries").fetchone()[0] == "enc"
    row = audit_rows(conn, action="issue invoice")[0]
    assert row["user"] == "enc" and row["entity_id"] == "0001773" and "@" in row["workstation"]
    c.get("/invoices/0001773/pdf")
    assert audit_rows(conn, action="print invoice", user="enc")
    detail = c.get("/invoices/0001773").get_data(as_text=True)
    assert "History" in detail and "issue invoice" in detail


def test_modifications_keep_before_and_after(app, conn):
    c = sign_in(app, username="acct", role="accountant")
    c.post("/suppliers", data={"name": "ACME", "tin": "123456789", "address": "Makati"})
    c.post("/suppliers/1", data={"name": "ACME INC.", "tin": "123456789", "address": "Pasig"})
    row = audit_rows(conn, action="change supplier")[0]
    assert json.loads(row["changes"]) == {"name": ["ACME", "ACME INC."],
                                          "address": ["Makati", "Pasig"]}
    c.post("/accounts/5100", data={"name": "Office Rent", "active": "1"})
    row = audit_rows(conn, action="change account")[0]
    assert json.loads(row["changes"]) == {"name": ["Rent Expense", "Office Rent"]}
    trail = c.get("/books/report/audit?entity=supplier&id=1").get_data(as_text=True)
    assert "name: ACME -&gt; ACME INC." in trail and "Office Rent" not in trail


def test_nothing_can_be_deleted_and_the_trail_is_sealed(repo):
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
    assert conn.execute("SELECT hash FROM audit_log").fetchone()[0] == ""
    ledger.seal_audit(conn)
    assert conn.execute("SELECT hash FROM audit_log").fetchone()[0] != ""
    assert ledger.verify_audit(conn) == []


def test_admin_safeguards(app, conn):
    c = sign_in(app)
    me = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    with pytest.raises(auth.AuthError, match="own role"):
        auth.update_user(conn, me, full_name="x", role="encoder", active=True, by_id=me)
    other = auth.create_user(conn, username="enc2", full_name="E", role="encoder", password=PW)
    auth.update_user(conn, other, full_name="E", role="encoder", active=False, by_id=me)
    assert c.get("/users").status_code == 200
    blocked = app.test_client()
    r = blocked.post("/login", data={"username": "enc2", "password": PW})
    assert r.status_code == 401 and "deactivated" in r.get_data(as_text=True)


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
    enc = sign_in(app, username="enc", role="encoder")
    assert enc.get("/backup").status_code == 302  # refused


def test_audit_report_filters(app, conn):
    a = sign_in(app)
    sign_in(app, username="enc", role="encoder").post(
        "/invoices/issue", data=form() | {"token": "x"})
    r = reports.audit_log(conn, date(2000, 1, 1), date(2100, 1, 1), user="enc")
    assert {row.cells[1] for row in r.rows} == {"enc"}
    page = a.get("/books/report/audit?from=2000-01-01&user=enc").get_data(as_text=True)
    assert "issue invoice" in page and "Filtered by user enc" in page

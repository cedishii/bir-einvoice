"""User accounts: who may do what, and who did it.

Every person who uses the app logs in with their own account, so the audit
trail and every posted entry name the person who made it. Accounts are never
deleted, only deactivated, so old entries keep pointing at a real user.

Roles:
  admin       everything, including user accounts and backups
  accountant  entries plus voids / cancellations / reversals, closing
              periods, chart of accounts, tax settings, sending to BIR
  encoder     enters invoices, collections, bills, payments, journal entries
  auditor     read-only: books, reports, audit trail (e.g. for a tax audit)
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from .books import ledger
from .books.ledger import BooksError
from .models import PH_TZ

ROLES = {
    "admin": "Administrator",
    "accountant": "Accountant",
    "encoder": "Encoder",
    "auditor": "Auditor (read-only)",
}
CAPABILITIES = {
    "admin": {"view", "encode", "approve", "admin"},
    "accountant": {"view", "encode", "approve"},
    "encoder": {"view", "encode"},
    "auditor": {"view"},
}
MIN_USERS = 2            # separate logins, so entries can be traced to a person
MIN_PASSWORD = 8
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


class AuthError(BooksError):
    pass


def can(role: str, capability: str) -> bool:
    return capability in CAPABILITIES.get(role, set())


def users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM users ORDER BY active DESC, username").fetchall()


def user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def has_users(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def active_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]


def check_password_rules(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.")
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise AuthError("The password needs both letters and numbers.")
    if username and username.lower() in password.lower():
        raise AuthError("The password can't contain the username.")


def _clean_name(text: str) -> str:
    return " ".join((text or "").split())


def create_user(conn: sqlite3.Connection, *, username: str, full_name: str, role: str,
                password: str, must_change: bool = True, by: str | None = None) -> int:
    username = (username or "").strip().lower()
    full_name = _clean_name(full_name)
    if not _USERNAME_RE.match(username):
        raise AuthError("Username: 3 to 30 characters, lowercase letters, numbers, . _ or -.")
    if not full_name:
        raise AuthError("The person's full name is required (it appears in the audit trail).")
    if role not in ROLES:
        raise AuthError("Choose a role.")
    check_password_rules(password, username)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"The username {username} is taken.")
    by = by or ledger.current_user()
    with _transaction(conn):
        uid = conn.execute(
            """INSERT INTO users (username, full_name, role, password_hash, must_change,
                   created_at, created_by) VALUES (?,?,?,?,?,?,?)""",
            (username, full_name, role, generate_password_hash(password), int(must_change),
             ledger.now(), by)).lastrowid
        ledger.audit(conn, "add user", f"{username} ({full_name}), {ROLES[role]}", by,
                     entity="user", entity_id=username)
    return uid


def update_user(conn: sqlite3.Connection, user_id: int, *, full_name: str, role: str,
                active: bool, by_id: int) -> None:
    u = user(conn, user_id)
    if u is None:
        raise AuthError("No such user.")
    full_name = _clean_name(full_name) or u["full_name"]
    if role not in ROLES:
        raise AuthError("Choose a role.")
    if user_id == by_id and (not active or role != u["role"]):
        raise AuthError("You can't change your own role or deactivate yourself; "
                        "ask another administrator.")
    after = {"full_name": full_name, "role": role, "active": int(active)}
    changes = ledger.changed(dict(u), after)
    if not changes:
        return
    with _transaction(conn):
        conn.execute("UPDATE users SET full_name = ?, role = ?, active = ? WHERE id = ?",
                     (full_name, role, int(active), user_id))
        admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
                              ).fetchone()[0]
        if admins == 0:
            raise AuthError("At least one active administrator is needed.")
        ledger.audit(conn, "change user", u["username"], entity="user",
                     entity_id=u["username"], changes=changes)


def reset_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    """By an administrator: a temporary password the user must change."""
    u = user(conn, user_id)
    if u is None:
        raise AuthError("No such user.")
    check_password_rules(new_password, u["username"])
    with _transaction(conn):
        conn.execute("UPDATE users SET password_hash = ?, must_change = 1, failed_logins = 0, "
                     "locked_until = '' WHERE id = ?",
                     (generate_password_hash(new_password), user_id))
        ledger.audit(conn, "reset password", f"temporary password set for {u['username']}",
                     entity="user", entity_id=u["username"])


def change_password(conn: sqlite3.Connection, user_id: int, current: str, new: str) -> None:
    u = user(conn, user_id)
    if u is None or not check_password_hash(u["password_hash"], current):
        raise AuthError("The current password is wrong.")
    if current == new:
        raise AuthError("Choose a password different from the current one.")
    check_password_rules(new, u["username"])
    with _transaction(conn):
        conn.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?",
                     (generate_password_hash(new), user_id))
        ledger.audit(conn, "change password", u["username"], u["username"],
                     entity="user", entity_id=u["username"])


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row:
    """The user, if the password is right. Failures are counted and audited;
    too many in a row lock the account for a while."""
    username = (username or "").strip().lower()
    u = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    now = datetime.now(PH_TZ)
    if u is not None and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
        ledger.audit(conn, "login refused", "account locked after failed logins",
                     username, entity="user", entity_id=username)
        raise AuthError(f"Too many failed logins. Try again after "
                        f"{datetime.fromisoformat(u['locked_until']):%H:%M}.")
    if u is None or not check_password_hash(u["password_hash"], password or ""):
        with _transaction(conn):
            if u is not None:
                failed = u["failed_logins"] + 1
                locked = ((now + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
                          if failed >= MAX_FAILED_LOGINS else "")
                conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                             (0 if locked else failed, locked, u["id"]))
            ledger.audit(conn, "login failed",
                         "unknown username" if u is None else "wrong password",
                         username or "(blank)", entity="user", entity_id=username)
        raise AuthError("Wrong username or password.")
    if not u["active"]:
        ledger.audit(conn, "login refused", "account deactivated", username,
                     entity="user", entity_id=username)
        raise AuthError("This account is deactivated.")
    with _transaction(conn):
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = '', last_login_at = ? "
                     "WHERE id = ?", (ledger.now(), u["id"]))
        ledger.audit(conn, "login", "", username, entity="user", entity_id=username)
    return user(conn, u["id"])


def _transaction(conn):
    from .books.postings import transaction
    return transaction(conn)

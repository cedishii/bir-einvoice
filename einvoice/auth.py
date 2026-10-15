"""User accounts, so the history (audit trail) shows who did what.

Anyone can create an account from the sign-in page and use it right away;
everyone signed in can do everything. Accounts are never deleted, so old
entries keep pointing at a real person.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from .books import ledger
from .books.ledger import BooksError
from .models import PH_TZ

MIN_PASSWORD = 6
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


class AuthError(BooksError):
    pass


def user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.")


def create_user(conn: sqlite3.Connection, *, username: str, full_name: str,
                password: str) -> int:
    username = (username or "").strip().lower()
    full_name = " ".join((full_name or "").split())
    if not _USERNAME_RE.match(username):
        raise AuthError("Username: 3 to 30 characters, lowercase letters, numbers, . _ or -.")
    if not full_name:
        raise AuthError("Your name is required (it shows in the history).")
    check_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"The username {username} is taken.")
    with _transaction(conn):
        # Everyone has the same access; the role column is kept as "admin".
        uid = conn.execute(
            """INSERT INTO users (username, full_name, role, password_hash, must_change,
                   created_at, created_by) VALUES (?, ?, 'admin', ?, 0, ?, ?)""",
            (username, full_name, generate_password_hash(password), ledger.now(),
             username)).lastrowid
        ledger.audit(conn, "create account", full_name, username, entity="user",
                     entity_id=username)
    return uid


def change_password(conn: sqlite3.Connection, user_id: int, current: str, new: str) -> None:
    u = user(conn, user_id)
    if u is None or not check_password_hash(u["password_hash"], current or ""):
        raise AuthError("The current password is wrong.")
    check_password(new)
    with _transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(new), user_id))
        ledger.audit(conn, "change password", "", u["username"], entity="user",
                     entity_id=u["username"])


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row:
    """The user, if the password is right. Failed sign-ins go in the history,
    and too many in a row lock the account for a while."""
    username = (username or "").strip().lower()
    u = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    now = datetime.now(PH_TZ)
    if u is not None and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now:
        raise AuthError(f"Too many wrong passwords. Try again after "
                        f"{datetime.fromisoformat(u['locked_until']):%H:%M}.")
    if u is None or not check_password_hash(u["password_hash"], password or ""):
        with _transaction(conn):
            if u is not None:
                failed = u["failed_logins"] + 1
                locked = ((now + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
                          if failed >= MAX_FAILED_LOGINS else "")
                conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                             (0 if locked else failed, locked, u["id"]))
            ledger.audit(conn, "sign-in failed",
                         "unknown username" if u is None else "wrong password",
                         username or "(blank)", entity="user", entity_id=username)
        raise AuthError("Wrong username or password.")
    with _transaction(conn):
        conn.execute("UPDATE users SET failed_logins = 0, locked_until = '', last_login_at = ? "
                     "WHERE id = ?", (ledger.now(), u["id"]))
        ledger.audit(conn, "sign in", "", username, entity="user", entity_id=username)
    return user(conn, u["id"])


def _transaction(conn):
    from .books.postings import transaction
    return transaction(conn)

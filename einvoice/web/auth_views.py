"""Login, first-run setup, own password, user accounts and backups, plus the
gate that checks every request: logged in, not idle too long, and allowed by
the user's role."""
from __future__ import annotations

import time

from flask import (Blueprint, Flask, Response, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from .. import auth, backup
from ..books import ledger
from ..models import today_ph

bp = Blueprint("auth", __name__)

PUBLIC = {"auth.login", "auth.setup", "static", "favicon", "logo", "window_ping", "window_bye"}

# What each page or action needs beyond being logged in ("view").
NEEDS = {
    # entering documents
    "new_invoice": "encode", "review_invoice": "encode", "edit_invoice": "encode",
    "issue_invoice": "encode", "email_invoice_route": "encode",
    "test_export_invoice": "encode", "download_test_export": "encode",
    "books.new_collection": "encode", "books.create_collection": "encode",
    "books.new_bill": "encode", "books.create_bill": "encode",
    "books.new_payment": "encode", "books.create_payment": "encode",
    "books.new_entry": "encode", "books.create_entry": "encode",
    "books.add_supplier": "encode",
    # corrections, period closing, settings, BIR
    "void_invoice": "approve", "export_invoice": "approve", "send_invoice": "approve",
    "check_invoice": "approve", "bir_login": "approve", "bir_send_pending": "approve",
    "bir_check": "approve", "books.cancel": "approve", "books.reverse_entry": "approve",
    "books.close_books": "approve", "books.add_account": "approve",
    "books.update_account": "approve", "books.save_tax_settings": "approve",
    "books.update_supplier": "approve",
    # user accounts and backups
    "auth.users": "admin", "auth.create_user": "admin", "auth.update_user": "admin",
    "auth.reset_password": "admin", "auth.download_backup": "admin",
}


def conn():
    return current_app.extensions["einvoice"]["repo"]().conn


def cfg():
    return current_app.extensions["einvoice"]["cfg"]()


def install(app: Flask) -> None:
    app.register_blueprint(bp)
    app.config.setdefault("SESSION_IDLE_MINUTES", 30)
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)

    @app.before_request
    def gate():
        g.user = None
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC:
            return None
        c = conn()
        if not auth.has_users(c):
            return redirect(url_for("auth.setup"))
        u = auth.user(c, session.get("uid", 0)) if session.get("uid") else None
        if u is None or not u["active"]:
            session.clear()
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        idle = time.time() - session.get("seen", 0)
        if idle > current_app.config["SESSION_IDLE_MINUTES"] * 60:
            ledger.audit(c, "logout", "signed out after inactivity", u["username"],
                         entity="user", entity_id=u["username"])
            session.clear()
            flash("You were signed out after a period of inactivity.", "error")
            return redirect(url_for("auth.login"))
        session["seen"] = time.time()
        g.user = u
        g.audit_token = ledger.set_acting_user(u["username"])
        if u["must_change"] and endpoint not in ("auth.account", "auth.change_password",
                                                 "auth.logout"):
            flash("Choose your own password before continuing.", "error")
            return redirect(url_for("auth.account"))
        need = NEEDS.get(endpoint, "view")
        if not auth.can(u["role"], need):
            ledger.audit(c, "refused", f"{request.method} {request.path} needs '{need}'",
                         entity="user", entity_id=u["username"])
            flash(f"Your role ({auth.ROLES[u['role']]}) can't do that.", "error")
            return redirect(url_for("index") if request.method == "GET"
                            else (request.referrer or url_for("index")))
        return None

    @app.teardown_request
    def forget_user(_exc):
        token = g.pop("audit_token", None)
        if token is not None:
            ledger.reset_acting_user(token)

    app.add_template_filter(ledger.change_list, "changes")

    @app.context_processor
    def user_globals():
        u = g.get("user")
        return {"me": u, "roles": auth.ROLES,
                "can": (lambda cap: bool(u) and auth.can(u["role"], cap))}


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("index")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: create the first administrator. Closed once any user exists."""
    c = conn()
    if auth.has_users(c):
        return redirect(url_for("auth.login"))
    data = request.form.to_dict() if request.method == "POST" else {}
    if request.method == "POST":
        try:
            if data.get("password") != data.get("password2"):
                raise auth.AuthError("The two passwords don't match.")
            auth.create_user(c, username=data.get("username", ""), full_name=data.get("full_name", ""),
                             role="admin", password=data.get("password", ""), must_change=False,
                             by="setup")
            flash("Administrator created. Sign in, then add at least one more user.")
            return redirect(url_for("auth.login"))
        except auth.AuthError as e:
            flash(str(e), "error")
    return render_template("auth/setup.html", data=data)


@bp.route("/login", methods=["GET", "POST"])
def login():
    c = conn()
    if not auth.has_users(c):
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        try:
            u = auth.authenticate(c, request.form.get("username", ""),
                                  request.form.get("password", ""))
        except auth.AuthError as e:
            flash(str(e), "error")
            return render_template("auth/login.html", username=request.form.get("username", "")), 401
        session.clear()
        session.update(uid=u["id"], seen=time.time())
        return redirect(_safe_next(request.args.get("next")))
    return render_template("auth/login.html", username="")


@bp.post("/logout")
def logout():
    u = g.get("user")
    if u is not None:
        ledger.audit(conn(), "logout", "", u["username"], entity="user", entity_id=u["username"])
    session.clear()
    flash("Signed out.")
    return redirect(url_for("auth.login"))


@bp.get("/account")
def account():
    return render_template("auth/account.html",
                           logins=conn().execute(
                               """SELECT * FROM audit_log WHERE entity = 'user' AND entity_id = ?
                                  AND action IN ('login', 'login failed', 'login refused', 'logout')
                                  ORDER BY id DESC LIMIT 15""", (g.user["username"],)).fetchall())


@bp.post("/account/password")
def change_password():
    f = request.form
    try:
        if f.get("new") != f.get("new2"):
            raise auth.AuthError("The two new passwords don't match.")
        auth.change_password(conn(), g.user["id"], f.get("current", ""), f.get("new", ""))
        flash("Password changed.")
        return redirect(url_for("index"))
    except auth.AuthError as e:
        flash(str(e), "error")
        return redirect(url_for("auth.account"))


@bp.get("/users")
def users():
    c = conn()
    return render_template("auth/users.html", users=auth.users(c), active=auth.active_count(c),
                           min_users=auth.MIN_USERS, data={})


@bp.post("/users")
def create_user():
    c = conn()
    data = request.form.to_dict()
    try:
        auth.create_user(c, username=data.get("username", ""), full_name=data.get("full_name", ""),
                         role=data.get("role", ""), password=data.get("password", ""))
        flash(f"User {data['username'].strip().lower()} added. Give them the temporary password; "
              "they choose their own at the first sign-in.")
        return redirect(url_for("auth.users"))
    except auth.AuthError as e:
        flash(str(e), "error")
        return render_template("auth/users.html", users=auth.users(c), active=auth.active_count(c),
                               min_users=auth.MIN_USERS, data=data), 422


@bp.post("/users/<int:user_id>")
def update_user(user_id):
    try:
        auth.update_user(conn(), user_id, full_name=request.form.get("full_name", ""),
                         role=request.form.get("role", ""), active=bool(request.form.get("active")),
                         by_id=g.user["id"])
        flash("User saved.")
    except auth.AuthError as e:
        flash(str(e), "error")
    return redirect(url_for("auth.users"))


@bp.post("/users/<int:user_id>/password")
def reset_password(user_id):
    try:
        auth.reset_password(conn(), user_id, request.form.get("password", ""))
        flash("Temporary password set. The user chooses their own at the next sign-in.")
    except auth.AuthError as e:
        flash(str(e), "error")
    return redirect(url_for("auth.users"))


@bp.get("/backup")
def download_backup():
    content = backup.snapshot(cfg())
    ledger.audit(conn(), "backup", "database downloaded", entity="database")
    return Response(content, mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="einvoice-backup-{today_ph():%Y%m%d}.db"'})

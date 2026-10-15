"""Sign in, create an account, sign out, own password and backups, plus the
check on every request: signed in and not idle too long. Everyone signed in
can do everything; what matters is that the history names who did it."""
from __future__ import annotations

import time

from flask import (Blueprint, Flask, Response, current_app, flash, g, redirect,
                   render_template, request, session, url_for)

from .. import auth, backup
from ..books import ledger
from ..models import today_ph

bp = Blueprint("auth", __name__)

PUBLIC = {"auth.login", "auth.signup", "static", "favicon", "logo", "window_ping", "window_bye"}


def conn():
    return current_app.extensions["einvoice"]["repo"]().conn


def cfg():
    return current_app.extensions["einvoice"]["cfg"]()


def install(app: Flask) -> None:
    app.register_blueprint(bp)
    app.config.setdefault("SESSION_IDLE_MINUTES", 30)
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)
    app.add_template_filter(ledger.change_list, "changes")

    @app.before_request
    def gate():
        g.user = None
        if (request.endpoint or "") in PUBLIC:
            return None
        c = conn()
        u = auth.user(c, session["uid"]) if session.get("uid") else None
        if u is None or not u["active"]:
            session.clear()
            return redirect(url_for("auth.login",
                                    next=request.full_path if request.method == "GET" else None))
        if time.time() - session.get("seen", 0) > current_app.config["SESSION_IDLE_MINUTES"] * 60:
            ledger.audit(c, "sign out", "after a period of inactivity", u["username"],
                         entity="user", entity_id=u["username"])
            session.clear()
            flash("You were signed out after a period of inactivity.", "error")
            return redirect(url_for("auth.login"))
        session["seen"] = time.time()
        g.user = u
        g.audit_token = ledger.set_acting_user(u["username"])
        return None

    @app.teardown_request
    def forget_user(_exc):
        token = g.pop("audit_token", None)
        if token is not None:
            ledger.reset_acting_user(token)

    @app.context_processor
    def user_globals():
        return {"me": g.get("user")}


def _start_session(u) -> None:
    session.clear()
    session.update(uid=u["id"], seen=time.time())


def _next() -> str:
    target = request.args.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else url_for("index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            u = auth.authenticate(conn(), request.form.get("username", ""),
                                  request.form.get("password", ""))
        except auth.AuthError as e:
            flash(str(e), "error")
            return render_template("auth/login.html", mode="signin", data=request.form), 401
        _start_session(u)
        return redirect(_next())
    return render_template("auth/login.html", mode=request.args.get("mode", "signin"), data={})


@bp.post("/signup")
def signup():
    f = request.form
    try:
        if f.get("password") != f.get("password2"):
            raise auth.AuthError("The two passwords don't match.")
        uid = auth.create_user(conn(), username=f.get("username", ""),
                               full_name=f.get("full_name", ""), password=f.get("password", ""))
    except auth.AuthError as e:
        flash(str(e), "error")
        return render_template("auth/login.html", mode="signup", data=f), 422
    u = auth.user(conn(), uid)
    ledger.audit(conn(), "sign in", "new account", u["username"], entity="user",
                 entity_id=u["username"])
    _start_session(u)
    flash(f"Welcome, {u['full_name']}. Your account is ready.")
    return redirect(url_for("index"))


@bp.post("/logout")
def logout():
    u = g.get("user")
    if u is not None:
        ledger.audit(conn(), "sign out", "", u["username"], entity="user", entity_id=u["username"])
    session.clear()
    flash("Signed out.")
    return redirect(url_for("auth.login"))


@bp.get("/account")
def account():
    return render_template("auth/account.html",
                           logins=conn().execute(
                               """SELECT * FROM audit_log WHERE entity = 'user' AND entity_id = ?
                                  AND action IN ('sign in', 'sign-in failed', 'sign out')
                                  ORDER BY id DESC LIMIT 15""", (g.user["username"],)).fetchall())


@bp.post("/account/password")
def change_password():
    f = request.form
    try:
        if f.get("new") != f.get("new2"):
            raise auth.AuthError("The two new passwords don't match.")
        auth.change_password(conn(), g.user["id"], f.get("current", ""), f.get("new", ""))
        flash("Password changed.")
    except auth.AuthError as e:
        flash(str(e), "error")
    return redirect(url_for("auth.account"))


@bp.get("/backup")
def download_backup():
    content = backup.snapshot(cfg())
    ledger.audit(conn(), "backup", "database downloaded", entity="database")
    return Response(content, mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="einvoice-backup-{today_ph():%Y%m%d}.db"'})

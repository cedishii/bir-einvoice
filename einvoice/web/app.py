"""Local web UI: invoice list, entry form, review, detail, PDF, void."""
from __future__ import annotations

import secrets
from datetime import date

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, url_for)

from .. import money
from ..config import LOGO_PATH, Config, load_config
from ..models import (DEFAULT_ITEM_NAME, DEFAULT_UNIT, MAX_LEN, FormErrors,
                      invoice_from_form, today_ph)
from ..pdf.service_invoice import render as render_pdf
from ..storage.db import connect
from ..storage.repository import Repository

LINE_FIELDS = ["item_name", "description", "qty", "unit", "unit_cost_jpy"]
FORM_FIELDS = ["issue_date", "buyer_name", "buyer_tin", "buyer_address",
               "business_style", "rate", "rate_date", "rate_source"]
DEFAULT_LINES = 2


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    app.config["EINVOICE"] = config or load_config()
    app.secret_key = secrets.token_hex(16)
    # Review-page tokens already used to issue an invoice -> control number,
    # so a double click or a browser "resubmit" never issues twice.
    issued_tokens: dict[str, str] = {}

    def cfg() -> Config:
        return app.config["EINVOICE"]

    def repo() -> Repository:
        if "repo" not in g:
            g.conn = connect(cfg().db_path)
            g.repo = Repository(g.conn, cfg().control_no_start)
        return g.repo

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def globals_():
        return {"cfg": cfg(), "fmt": money.fmt, "fmt_rate": money.fmt_rate,
                "fmt_qty": money.fmt_qty,
                "max_len": MAX_LEN}

    def form_data() -> dict:
        data = {k: request.form.get(k, "") for k in FORM_FIELDS}
        for k in LINE_FIELDS:
            data[k + "[]"] = request.form.getlist(k + "[]")
        return data

    def render_form(data: dict, errors: dict | None = None, status=200):
        rows = list(zip(*(data[k + "[]"] for k in LINE_FIELDS)))
        return render_template("form.html", data=data, rows=rows, errors=errors or {},
                               next_no=repo().next_control_no()), status

    @app.get("/")
    def index():
        return render_template("list.html", invoices=repo().list(),
                               next_no=repo().next_control_no())

    @app.get("/invoices/new")
    def new_invoice():
        today = today_ph().isoformat()
        data = {k: "" for k in FORM_FIELDS} | {"issue_date": today, "rate_date": today}
        last = repo().list()
        if last:  # most invoices go to the same client: start from the last one
            src = repo().get(last[0].control_no)
            data |= {
                "buyer_name": src.buyer_name,
                "buyer_tin": "" if not src.has_tin else src.buyer_tin + src.buyer_branch,
                "buyer_address": src.buyer_address,
                "business_style": src.business_style,
                "rate_source": src.rate_source,  # the source, never the rate itself
            }
        blank = [DEFAULT_ITEM_NAME, "", "1", DEFAULT_UNIT, ""]
        for i, k in enumerate(LINE_FIELDS):
            data[k + "[]"] = [blank[i]] * DEFAULT_LINES
        return render_form(data)

    @app.post("/invoices/review")
    def review_invoice():
        data = form_data()
        try:
            inv = invoice_from_form(data)
        except FormErrors as e:
            return render_form(data, e.errors, status=422)
        return render_template("review.html", inv=inv, data=data, rows=list(
            zip(*(data[k + "[]"] for k in LINE_FIELDS))), token=secrets.token_hex(16),
            next_no=repo().next_control_no())

    @app.post("/invoices/edit")
    def edit_invoice():
        return render_form(form_data())

    @app.post("/invoices/issue")
    def issue_invoice():
        token = request.form.get("token", "")
        if token in issued_tokens:
            return redirect(url_for("show_invoice", control_no=issued_tokens[token]))
        data = form_data()
        try:
            inv = invoice_from_form(data)
        except FormErrors as e:
            return render_form(data, e.errors, status=422)
        inv = repo().issue(inv)
        issued_tokens[token] = inv.control_no
        flash(f"Service Invoice No. {inv.control_no} issued.")
        return redirect(url_for("show_invoice", control_no=inv.control_no))

    def get_or_404(control_no: str):
        inv = repo().get(control_no)
        if inv is None:
            abort(404)
        return inv

    @app.get("/invoices/<control_no>")
    def show_invoice(control_no):
        return render_template("detail.html", inv=get_or_404(control_no))

    @app.get("/invoices/<control_no>/pdf")
    def invoice_pdf(control_no):
        inv = get_or_404(control_no)
        pdf = render_pdf(inv, cfg())
        name = f"Service-Invoice-{inv.control_no}.pdf"
        disposition = "attachment" if request.args.get("download") else "inline"
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": f'{disposition}; filename="{name}"'})

    @app.post("/invoices/<control_no>/void")
    def void_invoice(control_no):
        get_or_404(control_no)
        try:
            repo().void(control_no, request.form.get("reason", ""))
            flash(f"Invoice {control_no} voided. Its number will not be used again.")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("show_invoice", control_no=control_no))

    @app.get("/logo.png")
    def logo():
        return send_file(LOGO_PATH, mimetype="image/png", max_age=3600)

    @app.template_filter("longdate")
    def longdate(d: date) -> str:
        return d.strftime("%B %d, %Y")

    return app

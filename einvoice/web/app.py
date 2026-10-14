"""Local web UI: invoice list, entry form, review, detail, PDF, void."""
from __future__ import annotations

import secrets
from datetime import date

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, url_for)

from .. import emailer, money
from ..config import LOGO_PATH, Config, load_config
from ..models import (DEFAULT_ITEM_NAME, DEFAULT_UNIT, MAX_LEN, FormErrors,
                      invoice_from_form, today_ph)
from ..eis import builder
from ..eis import export as eis_export
from ..eis.export import ExportRefused
from ..eis.transport import results as eis_results
from ..eis.transport import service as eis_service
from ..eis.transport.client import EisApiError, EisClient
from ..eis.transport.settings import TransportSettings, load_transport_settings
from ..pdf.service_invoice import render as render_pdf
from ..storage.db import connect
from ..storage.repository import Repository

LINE_FIELDS = ["item_name", "description", "qty", "unit", "unit_cost_jpy"]
FORM_FIELDS = ["issue_date", "buyer_name", "buyer_tin", "buyer_address",
               "business_style", "buyer_email", "rate", "rate_date", "rate_source"]
DEFAULT_LINES = 2


def create_app(config: Config | None = None, transport: TransportSettings | None = None,
               http=None, email: emailer.EmailSettings | None = None,
               email_sender: emailer.Sender | None = None) -> Flask:
    """`transport`/`http` and `email`/`email_sender` let tests use fakes."""
    app = Flask(__name__)
    app.config["EINVOICE"] = config or load_config()
    app.config["EIS_TRANSPORT"] = transport or load_transport_settings()
    app.config["EMAIL"] = email or emailer.load_email_settings()
    send_mail = email_sender if email_sender is not None else emailer.smtp_send
    eis_clients: list[EisClient] = []  # one client, so the login session is reused
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

    def transport() -> TransportSettings:
        return app.config["EIS_TRANSPORT"]

    def eis_client() -> EisClient:
        if not eis_clients:
            eis_clients.append(EisClient(cfg(), transport(), http=http))
        return eis_clients[0]

    def email_settings() -> emailer.EmailSettings:
        return app.config["EMAIL"]

    def email_invoice(inv, automatic=False):
        try:
            result = emailer.send_invoice(repo().conn, inv, cfg(), email_settings(),
                                          automatic=automatic, sender=send_mail)
            flash(result.message)
        except emailer.EmailError as e:
            flash(("Automatic e-mail not sent: " if automatic else "") + str(e), "error")

    def can_send() -> bool:
        return not cfg().is_prototype and transport().is_configured

    @app.context_processor
    def globals_():
        return {"cfg": cfg(), "transport": transport(), "can_send": can_send(),
                "email": email_settings(),
                "fmt": money.fmt, "fmt_rate": money.fmt_rate,
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

    def eis_states() -> dict[int, str]:
        rows = repo().conn.execute("SELECT invoice_id, eis_unique_id FROM eis_exports").fetchall()
        return {r["invoice_id"]: eis_service.status_of(repo().conn, r["eis_unique_id"]).state
                for r in rows}

    @app.get("/")
    def index():
        return render_template("list.html", invoices=repo().list(), eis=eis_states(),
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
                "buyer_email": src.buyer_email,
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
        next_no = repo().next_control_no()
        problems = eis_export.precheck(inv, cfg(), next_no)
        return render_template("review.html", inv=inv, data=data, rows=list(
            zip(*(data[k + "[]"] for k in LINE_FIELDS))), token=secrets.token_hex(16),
            next_no=next_no, problems=problems)

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
        # Never issue an invoice that couldn't later be exported to EIS.
        problems = eis_export.precheck(inv, cfg(), repo().next_control_no())
        if problems:
            return render_form(data, {f"eis{i}": f"BIR e-invoice check: {p}"
                                      for i, p in enumerate(problems)}, status=422)
        inv = repo().issue(inv)
        issued_tokens[token] = inv.control_no
        flash(f"Service Invoice No. {inv.control_no} issued.")
        if email_settings().auto_send:
            email_invoice(inv, automatic=True)
        return redirect(url_for("show_invoice", control_no=inv.control_no))

    def get_or_404(control_no: str):
        inv = repo().get(control_no)
        if inv is None:
            abort(404)
        return inv

    def render_detail(inv, issues=None, status=200):
        exported = eis_export.existing_export(repo().conn, cfg(), inv)
        bir = eis_service.status_of(repo().conn, exported.eis_unique_id) if exported else None
        return render_template("detail.html", inv=inv, exported=exported, bir=bir,
                               emails=emailer.history(repo().conn, inv),
                               issues=issues or [], eis_id=_eis_id(inv)), status

    def _eis_id(inv):
        if cfg().is_prototype:
            return None
        return builder.eis_unique_id(inv, cfg())

    @app.get("/invoices/<control_no>")
    def show_invoice(control_no):
        return render_detail(get_or_404(control_no))

    @app.post("/invoices/<control_no>/export")
    def export_invoice(control_no):
        inv = get_or_404(control_no)
        try:
            result = eis_export.export(inv, cfg(), repo().conn)
        except ExportRefused as e:
            return render_detail(inv, e.issues, status=422)
        if not result.already_exported:
            flash(f"e-Invoice JSON exported: {result.path.name}")
        return redirect(url_for("show_invoice", control_no=control_no))

    @app.get("/invoices/<control_no>/export.json")
    def download_export(control_no):
        inv = get_or_404(control_no)
        done = eis_export.existing_export(repo().conn, cfg(), inv)
        if done is None or not done.path.exists():
            abort(404)
        return send_file(done.path, mimetype="application/json", as_attachment=True,
                         download_name=done.path.name)

    @app.post("/invoices/<control_no>/test-export")
    def test_export_invoice(control_no):
        inv = get_or_404(control_no)
        try:
            result = eis_export.export(inv, cfg(), repo().conn, test=True)
        except ExportRefused as e:
            return render_detail(inv, e.issues, status=422)
        return render_template("test_export.html", inv=inv, result=result)

    @app.get("/invoices/<control_no>/test-export.json")
    def download_test_export(control_no):
        inv = get_or_404(control_no)
        result = eis_export.export(inv, cfg(), repo().conn, test=True)
        return send_file(result.path, mimetype="application/json", as_attachment=True,
                         download_name=result.path.name)

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

    @app.post("/invoices/<control_no>/email")
    def email_invoice_route(control_no):
        email_invoice(get_or_404(control_no))
        return redirect(url_for("show_invoice", control_no=control_no))

    # --- Phase 3: sending to BIR -------------------------------------------

    def bir_action(action, back):
        if not can_send():
            flash("Sending to BIR needs the BIR settings in .env (see the BIR page).", "error")
            return redirect(back)
        try:
            message = action()
            if message:
                flash(message)
        except EisApiError as e:
            flash(f"BIR: {e}", "error")
        except (ValueError, OSError) as e:
            flash(str(e), "error")
        return redirect(back)

    @app.post("/invoices/<control_no>/send")
    def send_invoice(control_no):
        inv = get_or_404(control_no)
        done = eis_export.existing_export(repo().conn, cfg(), inv)
        back = url_for("show_invoice", control_no=control_no)
        if done is None:
            flash("Export the e-invoice JSON first.", "error")
            return redirect(back)

        def go():
            [sid] = eis_service.send(repo().conn, cfg(), eis_client(), [done.eis_unique_id])
            return f"Sent to BIR (submitId {sid}). Use \"Check result\" in a moment."
        return bir_action(go, back)

    @app.post("/invoices/<control_no>/check")
    def check_invoice(control_no):
        inv = get_or_404(control_no)
        done = eis_export.existing_export(repo().conn, cfg(), inv)
        back = url_for("show_invoice", control_no=control_no)

        def go():
            st = eis_service.status_of(repo().conn, done.eis_unique_id)
            out = eis_service.check_results(repo().conn, eis_client(), [st.submit_id])
            return f"BIR: {eis_results.PROCESS_CODES.get(out[st.submit_id], out[st.submit_id])}."
        return bir_action(go, back)

    @app.get("/bir")
    def bir_page():
        conn = repo().conn
        submissions = conn.execute(
            """SELECT s.*, COUNT(i.eis_unique_id) AS n,
                      SUM(i.result_code = 'SUC001') AS accepted
               FROM eis_submissions s LEFT JOIN eis_submission_items i USING (submit_id)
               GROUP BY s.submit_id ORDER BY s.sent_at DESC LIMIT 50""").fetchall()
        return render_template("bir.html", pending=eis_service.pending_exports(conn),
                               open_count=len(eis_service.open_submissions(conn)),
                               submissions=submissions, describe=eis_results.describe,
                               process_codes=eis_results.PROCESS_CODES)

    @app.post("/bir/login")
    def bir_login():
        def go():
            session = eis_client().authenticate(force_refresh=True)
            return f"Logged in to {transport().base_url}. Session valid until {session.expires_at:%Y-%m-%d %H:%M}."
        return bir_action(go, url_for("bir_page"))

    @app.post("/bir/send-pending")
    def bir_send_pending():
        def go():
            ids = [r["eis_unique_id"] for r in eis_service.pending_exports(repo().conn)]
            if not ids:
                return "Nothing to send."
            sids = eis_service.send(repo().conn, cfg(), eis_client(), ids)
            return f"Sent {len(ids)} e-invoice(s) in {len(sids)} submission(s)."
        return bir_action(go, url_for("bir_page"))

    @app.post("/bir/check")
    def bir_check():
        def go():
            out = eis_service.check_results(repo().conn, eis_client())
            if not out:
                return "No submissions are waiting for a result."
            done = sum(1 for v in out.values() if v == eis_results.PROCESS_COMPLETED)
            return f"Checked {len(out)} submission(s): {done} completed."
        return bir_action(go, url_for("bir_page"))

    @app.get("/favicon.ico")
    def favicon():
        return send_file(LOGO_PATH.with_name("app.ico"), mimetype="image/x-icon", max_age=3600)

    @app.get("/logo.png")
    def logo():
        return send_file(LOGO_PATH, mimetype="image/png", max_age=3600)

    @app.template_filter("longdate")
    def longdate(d: date) -> str:
        return d.strftime("%B %d, %Y")

    return app

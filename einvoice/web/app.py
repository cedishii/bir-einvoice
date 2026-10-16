"""Local web UI: invoice list, entry form, review, detail, PDF, void."""
from __future__ import annotations

import re
import secrets
from datetime import date
from itertools import zip_longest

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, url_for)

from werkzeug.exceptions import HTTPException

from .. import emailer, health, money
from ..config import LOGO_PATH, Config, load_config
from ..models import (DEFAULT_ITEM_NAME, DEFAULT_UNIT, MAX_LEN, SALE_TYPES, FormErrors,
                      invoice_from_form, today_ph)
from ..eis import builder
from ..eis import export as eis_export
from ..eis.export import ExportRefused
from ..eis.transport import results as eis_results
from ..eis.transport import service as eis_service
from ..eis.transport.client import EisApiError, EisClient
from ..eis.transport.settings import TransportSettings, load_transport_settings
from ..books import ledger, postings
from ..books.ledger import BooksError
from ..pdf.service_invoice import render as render_pdf
from ..storage.db import connect
from ..storage import drafts
from ..storage.repository import Repository
from . import auth_views, books, signing_views

LINE_FIELDS = ["item_name", "description", "qty", "unit", "unit_cost_jpy", "amount_jpy"]
FORM_FIELDS = ["issue_date", "buyer_name", "buyer_tin", "buyer_address",
               "business_style", "buyer_email", "trans_class", "withholding_php", "rate",
               "rate_date", "rate_source"]
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

    def cfg() -> Config:
        return app.config["EINVOICE"]

    def repo() -> Repository:
        if "repo" not in g:
            g.conn = connect(cfg().db_path)
            g.repo = Repository(g.conn, cfg().control_no_start)
        return g.repo

    app.extensions["einvoice"] = {"repo": repo, "cfg": cfg}
    app.register_blueprint(books.bp)
    app.register_blueprint(signing_views.bp)
    auth_views.install(app)  # sign-in, so the history shows who did what

    # Start-up checks (database, journal and history chains, today's backup);
    # problems show at the top of every page. Errors go to data/logs/app.log.
    health.setup_logging(cfg())
    with app.app_context():
        app.config["HEALTH"] = health.startup_checks(cfg(), repo().conn)

    @app.errorhandler(Exception)
    def unexpected_error(e):
        if isinstance(e, HTTPException):
            return e
        ref = secrets.token_hex(3).upper()
        app.logger.error("error %s on %s %s", ref, request.method, request.path, exc_info=e)
        health.log.error("error %s on %s %s", ref, request.method, request.path, exc_info=e)
        return render_template("error.html", ref=ref), 500

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

    def email_invoice(inv, automatic=False, to=None, cc=""):
        try:
            result = emailer.send_invoice(repo().conn, inv, cfg(), email_settings(), to=to, cc=cc,
                                          automatic=automatic, sender=send_mail)
            ledger.audit(repo().conn, "e-mail invoice", result.message, entity="invoice",
                         entity_id=inv.control_no)
            flash(result.message)
        except emailer.EmailError as e:
            ledger.audit(repo().conn, "e-mail invoice", f"not sent: {e}", entity="invoice",
                         entity_id=inv.control_no)
            flash(("Automatic e-mail not sent: " if automatic else "") + str(e), "error")

    def can_send() -> bool:
        return not cfg().is_prototype and transport().is_configured

    @app.context_processor
    def globals_():
        return {"cfg": cfg(), "transport": transport(), "can_send": can_send(),
                "email": email_settings(),
                "fmt": money.fmt, "fmt_rate": money.fmt_rate,
                "fmt_qty": money.fmt_qty,
                "max_len": MAX_LEN, "sale_types": SALE_TYPES}

    def form_data() -> dict:
        data = {k: request.form.get(k, "") for k in FORM_FIELDS}
        for k in LINE_FIELDS:
            data[k + "[]"] = request.form.getlist(k + "[]")
        return data

    def render_form(data: dict, errors: dict | None = None, status=200, draft_id: str = "",
                    from_last: bool = False):
        rows = list(zip_longest(*(data[k + "[]"] for k in LINE_FIELDS), fillvalue=""))
        return render_template("form.html", data=data, rows=rows, errors=errors or {},
                               next_no=repo().next_control_no(), from_last=from_last,
                               draft_id=draft_id or secrets.token_hex(16)), status

    def keep_draft(data: dict) -> str:
        draft_id = request.form.get("draft", "")
        if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
            draft_id = secrets.token_hex(16)
        drafts.save(repo().conn, draft_id, data)
        return draft_id

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
        draft_id = request.args.get("draft", "")
        issued = drafts.issued_as(repo().conn, draft_id)
        saved = drafts.get(repo().conn, draft_id)
        if issued:
            flash(f"That invoice was already issued as No. {issued}. This is a new one.")
        elif saved is not None:
            return render_form(saved, draft_id=draft_id)
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
        blank = [DEFAULT_ITEM_NAME, "", "1", DEFAULT_UNIT, "", ""]
        for i, k in enumerate(LINE_FIELDS):
            data[k + "[]"] = [blank[i]] * DEFAULT_LINES
        return render_form(data, from_last=bool(last))

    @app.post("/invoices/review")
    def review_invoice():
        data = form_data()
        draft_id = keep_draft(data)
        try:
            invoice_from_form(data)
        except FormErrors as e:
            return render_form(data, e.errors, status=422, draft_id=draft_id)
        return redirect(url_for("show_review", draft=draft_id))

    @app.get("/invoices/review/<draft>")
    def show_review(draft):
        data = drafts.get(repo().conn, draft)
        if data is None:
            flash("That draft is no longer open. Please fill in the invoice again.", "error")
            return redirect(url_for("new_invoice"))
        try:
            inv = invoice_from_form(data)
        except FormErrors:
            return redirect(url_for("new_invoice", draft=draft))
        next_no = repo().next_control_no()
        problems = eis_export.precheck(inv, cfg(), next_no)
        return render_template("review.html", inv=inv, data=data, rows=list(
            zip_longest(*(data[k + "[]"] for k in LINE_FIELDS), fillvalue="")), token=draft,
            next_no=next_no, problems=problems,
            issued_as=drafts.issued_as(repo().conn, draft))

    @app.post("/invoices/issue")
    def issue_invoice():
        token = request.form.get("token", "")
        issued = drafts.issued_as(repo().conn, token) if token else None
        if issued:
            return redirect(url_for("show_invoice", control_no=issued))
        data = form_data()
        try:
            inv = invoice_from_form(data)
        except FormErrors as e:
            return render_form(data, e.errors, status=422, draft_id=token)
        # Never issue an invoice that couldn't later be exported to EIS.
        problems = eis_export.precheck(inv, cfg(), repo().next_control_no())
        if problems:
            return render_form(data, {f"eis{i}": f"BIR e-invoice check: {p}"
                                      for i, p in enumerate(problems)}, status=422,
                               draft_id=token)
        try:
            inv = repo().issue(inv)
        except BooksError as e:  # e.g. the invoice date is in a closed period
            return render_form(data, {"books": str(e)}, status=422, draft_id=token)
        if token:
            drafts.mark_issued(repo().conn, token, inv.control_no)
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
        books_entry = ledger.entry_for(repo().conn, "invoice", inv.id)
        return render_template("detail.html", inv=inv, exported=exported, bir=bir,
                               books_entry=books_entry,
                               books_reversal=books_entry and ledger.reversal_of(repo().conn, books_entry["id"]),
                               balance=postings.invoice_balance(repo().conn, inv.id),
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
            ledger.audit(repo().conn, "EIS export", result.path.name, entity="invoice",
                         entity_id=control_no)
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
        ledger.audit(repo().conn, "print invoice",
                     "PDF downloaded" if disposition == "attachment" else "PDF opened",
                     entity="invoice", entity_id=control_no)
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
        email_invoice(get_or_404(control_no), to=request.form.get("to", ""),
                      cc=request.form.get("cc", ""))
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
            ledger.audit(repo().conn, "send to BIR", f"submitId {sid}", entity="invoice",
                         entity_id=control_no)
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
            ledger.audit(repo().conn, "send to BIR",
                         f"{len(ids)} e-invoice(s), submitId(s) {', '.join(sids)}",
                         entity="bir")
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

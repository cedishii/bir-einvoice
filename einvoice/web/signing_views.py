"""Signatures page, the signing page and the signed PDFs (einvoice/signing.py).

Any PDF the app makes can be signed: its address (e.g. /invoices/0001775/pdf)
is passed as `src`, the PDF is made again here exactly as it would open, and
kept for a while (PENDING) while the user places the signature.
"""
from __future__ import annotations

import re
import secrets
import time

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from .. import signing
from ..models import today_ph
from ..signing import SigningError

bp = Blueprint("signing", __name__)

PENDING_SECONDS = 2 * 60 * 60
PENDING_MAX = 20


def conn():
    return current_app.extensions["einvoice"]["repo"]().conn


def pending() -> dict:
    return current_app.extensions.setdefault("einvoice_signing", {})


# --- Making the PDF to sign -------------------------------------------------------

def make_pdf(src: str) -> tuple[bytes, str]:
    """The PDF at the app address `src`, made as if opened by this user.

    Runs the page in its own request (with this user's sign-in cookie), so it
    is checked and recorded in the history exactly like opening it.
    """
    if not src.startswith("/") or src.startswith("//") or src.startswith("/sign"):
        raise SigningError("Only the app's own PDFs can be signed.")
    app = current_app._get_current_object()
    path, _, query = src.partition("?")
    with app.app_context(), app.test_request_context(
            path, query_string=query, headers={"Cookie": request.headers.get("Cookie", "")}):
        resp = app.full_dispatch_request()
    if resp.status_code != 200 or resp.mimetype != "application/pdf":
        raise SigningError("That PDF couldn't be made; open it again from its page.")
    m = re.search(r'filename="([^"]+)"', resp.headers.get("Content-Disposition", ""))
    return resp.get_data(), (m.group(1) if m else "document.pdf")


def _title_from(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return re.sub(r"[-_]+", " ", stem).strip() or "Document"


def _prune() -> None:
    now = time.time()
    store = pending()
    for token in [t for t, v in store.items() if now - v["at"] > PENDING_SECONDS]:
        store.pop(token, None)
    while len(store) > PENDING_MAX:
        store.pop(min(store, key=lambda t: store[t]["at"]))


def _pending_or_404(token: str) -> dict:
    item = pending().get(token)
    if item is None or item["user"] != g.user["username"]:
        abort(404)
    return item


# --- Pages ---------------------------------------------------------------------

@bp.get("/signatures")
def signatures():
    c = conn()
    docs = c.execute("""SELECT id, title, signed_by, signed_at, sha256 FROM signed_documents
                        ORDER BY id DESC LIMIT 200""").fetchall()
    return render_template("signatures.html", mine=signing.signature_of(c, g.user["username"]),
                           docs=docs, next=request.args.get("next", ""))


@bp.post("/signatures")
def save_signature():
    nxt = request.form.get("next", "")
    try:
        upload = request.files.get("picture")
        data = upload.read() if upload and upload.filename else \
            signing.image_from_data_url(request.form.get("drawing", ""))
        signing.save_signature(conn(), g.user["username"], signing.clean_image(data))
    except SigningError as e:
        flash(str(e), "error")
        return redirect(url_for("signing.signatures", next=nxt))
    flash("Your signature is saved.")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("signing.signatures"))


@bp.post("/signatures/remove")
def remove_signature():
    signing.remove_signature(conn(), g.user["username"])
    flash("Your signature was removed. Signed documents keep theirs.")
    return redirect(url_for("signing.signatures"))


@bp.get("/signatures/mine.png")
def my_signature_png():
    row = signing.signature_of(conn(), g.user["username"])
    if row is None:
        abort(404)
    return Response(row["image"], mimetype="image/png", headers={"Cache-Control": "no-store"})


@bp.get("/sign")
def sign_page():
    src = request.args.get("src", "")
    try:
        pdf, filename = make_pdf(src)
    except SigningError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))
    _prune()
    token = secrets.token_urlsafe(12)
    title = " ".join((request.args.get("title") or _title_from(filename)).split())[:200]
    pending()[token] = {"src": src, "pdf": pdf, "filename": filename, "title": title,
                        "user": g.user["username"], "at": time.time()}
    return redirect(url_for("signing.place", token=token))


@bp.get("/sign/<token>")
def place(token):
    item = _pending_or_404(token)
    me = g.user
    return render_template("sign.html", token=token, item=item,
                           sizes=signing.page_sizes(item["pdf"]),
                           mine=signing.signature_of(conn(), me["username"]),
                           caption=_caption(), error=request.args.get("error", ""))


@bp.get("/sign/<token>/page/<int:page>.png")
def page_png(token, page):
    item = _pending_or_404(token)
    try:
        png = signing.preview(item["pdf"], page)
    except SigningError:
        abort(404)
    return Response(png, mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})


@bp.post("/sign/<token>")
def apply(token):
    item = _pending_or_404(token)
    c = conn()
    sig = signing.signature_of(c, g.user["username"])
    if sig is None:
        flash("Save your signature first.", "error")
        return redirect(url_for("signing.signatures", next=url_for("signing.place", token=token)))
    try:
        places = signing.parse_placements(request.form.get("placements", ""),
                                          len(signing.page_sizes(item["pdf"])))
    except SigningError as e:
        flash(str(e), "error")
        return redirect(url_for("signing.place", token=token))
    caption = _caption() if request.form.get("caption") else None
    signed = signing.stamp(item["pdf"], sig["image"], places, caption=caption,
                           title=item["title"], signer=g.user["full_name"])
    doc_id = signing.record(c, title=item["title"], source_path=item["src"],
                            source_pdf=item["pdf"], signed_pdf=signed, placements=places,
                            signature_id=sig["id"])
    pending().pop(token, None)
    return redirect(url_for("signing.signed", doc_id=doc_id))


@bp.get("/signed/<int:doc_id>")
def signed(doc_id):
    doc = conn().execute("SELECT id, title, signed_by, signed_at, sha256, source_path "
                         "FROM signed_documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        abort(404)
    return render_template("signed.html", doc=doc)


@bp.get("/signed/<int:doc_id>.pdf")
def signed_pdf(doc_id):
    doc = conn().execute("SELECT title, pdf FROM signed_documents WHERE id = ?",
                         (doc_id,)).fetchone()
    if doc is None:
        abort(404)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", doc["title"]).strip("-") + f"-signed-{doc_id}.pdf"
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(doc["pdf"], mimetype="application/pdf",
                    headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


def _caption() -> list[str]:
    return [g.user["full_name"], today_ph().strftime("%B %d, %Y").replace(" 0", " ")]

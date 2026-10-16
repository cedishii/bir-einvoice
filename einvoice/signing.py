"""Signatures on the app's PDFs, like "Fill & Sign" in a PDF reader.

Each user saves a picture of their signature once (drawn in the app or a
photo / scan of one signed on paper). When a PDF is opened, the user is asked
whether to sign it; if so, they drag the signature onto the pages and the app
stamps it into a copy of the PDF, which is kept (signed_documents) and
recorded in the history. Not signing leaves the PDF as it was, for a wet
signature or notarization.

This is a visible signature (a picture of the handwriting), not a
certificate-based digital signature.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from io import BytesIO

import pymupdf
from PIL import Image, ImageOps

from .books import ledger
from .books.postings import transaction

MAX_UPLOAD = 5 * 1024 * 1024
MAX_WIDTH = 1000          # px, the saved signature picture
PREVIEW_ZOOM = 1.5        # page previews on the signing page (108 dpi)
CAPTION_PT = 7.5


class SigningError(ValueError):
    pass


# --- The signature picture ----------------------------------------------------

def clean_image(data: bytes) -> bytes:
    """Any picture of a signature -> a cropped PNG with a see-through background.

    Light pixels (the paper of a photo or scan, or the empty part of a drawing)
    become transparent, so the signature sits on the PDF like ink.
    """
    if not data:
        raise SigningError("Draw your signature or choose a picture of it.")
    if len(data) > MAX_UPLOAD:
        raise SigningError("The picture is too big (5 MB at most).")
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGBA")
    except Exception:
        raise SigningError("That file isn't a picture (use PNG or JPG).") from None
    if img.width > 3000 or img.height > 3000:
        img.thumbnail((3000, 3000))
    lum = img.convert("L")
    # Ink is dark: fully opaque below 100, fading out to clear above 220.
    ink = lum.point(lambda v: 255 if v < 100 else 0 if v > 220 else int((220 - v) * 255 / 120))
    alpha = Image.composite(ink, Image.new("L", img.size, 0), img.getchannel("A"))
    img.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if box is None:
        raise SigningError("No signature found in the picture. Sign with dark ink on "
                           "white paper, or draw it here.")
    pad = 6
    box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
           min(box[2] + pad, img.width), min(box[3] + pad, img.height))
    img = img.crop(box)
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    out = BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def image_from_data_url(text: str) -> bytes:
    """The drawing pad sends 'data:image/png;base64,...'."""
    head, _, body = (text or "").partition(",")
    if not head.startswith("data:image/") or ";base64" not in head:
        raise SigningError("Draw your signature first.")
    try:
        return base64.b64decode(body, validate=True)
    except ValueError:
        raise SigningError("The drawing couldn't be read; try again.") from None


def save_signature(conn: sqlite3.Connection, username: str, png: bytes) -> int:
    """Make `png` the user's signature. Earlier ones are kept, inactive."""
    with transaction(conn):
        replaced = conn.execute("UPDATE signatures SET active = 0 WHERE username = ? AND active = 1",
                                (username,)).rowcount
        sid = conn.execute(
            "INSERT INTO signatures (username, image, sha256, created_at) VALUES (?, ?, ?, ?)",
            (username, png, _sha(png), ledger.now())).lastrowid
        ledger.audit(conn, "save signature", "replaced the earlier one" if replaced else "first one",
                     entity="signature", entity_id=username)
    return sid


def remove_signature(conn: sqlite3.Connection, username: str) -> None:
    with transaction(conn):
        if conn.execute("UPDATE signatures SET active = 0 WHERE username = ? AND active = 1",
                        (username,)).rowcount:
            ledger.audit(conn, "remove signature", "kept on file, no longer used",
                         entity="signature", entity_id=username)


def signature_of(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM signatures WHERE username = ? AND active = 1",
                        (username,)).fetchone()


# --- PDFs ---------------------------------------------------------------------

@dataclass
class Placement:
    """Where a signature goes: page index and the box's top-left corner and
    width, as fractions of the page's width / height (0..1)."""
    page: int
    x: float
    y: float
    w: float


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [(p.rect.width, p.rect.height) for p in doc]


def preview(pdf: bytes, page: int) -> bytes:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        if not 0 <= page < doc.page_count:
            raise SigningError("No such page.")
        return doc[page].get_pixmap(matrix=pymupdf.Matrix(PREVIEW_ZOOM, PREVIEW_ZOOM)).tobytes("png")


def parse_placements(text: str, pages: int) -> list[Placement]:
    try:
        items = json.loads(text or "[]")
        out = [Placement(int(i["page"]), float(i["x"]), float(i["y"]), float(i["w"]))
               for i in items]
    except (ValueError, KeyError, TypeError):
        raise SigningError("The signature positions couldn't be read; try again.") from None
    if not out:
        raise SigningError("Put your signature on a page first (click where it goes).")
    for p in out:
        if not (0 <= p.page < pages and 0 <= p.x < 1 and 0 <= p.y < 1 and 0.02 <= p.w <= 1):
            raise SigningError("A signature is outside the page; move it back onto the page.")
    return out


def stamp(pdf: bytes, signature_png: bytes, placements: list[Placement], *,
          caption: list[str] | None = None, title: str = "", signer: str = "") -> bytes:
    """Copy of `pdf` with the signature drawn at each placement, and the
    caption lines (e.g. name and date) under each one."""
    sig = Image.open(BytesIO(signature_png))
    aspect = sig.height / sig.width
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for p in placements:
            page = doc[p.page]
            pw, ph = page.rect.width, page.rect.height
            x0, y0, w = p.x * pw, p.y * ph, p.w * pw
            rect = pymupdf.Rect(x0, y0, x0 + w, y0 + w * aspect)
            page.insert_image(rect, stream=signature_png, keep_proportion=True, overlay=True)
            y = rect.y1 + CAPTION_PT
            for line in caption or []:
                page.insert_text((x0, y), line, fontsize=CAPTION_PT, fontname="helv",
                                 color=(0.15, 0.15, 0.15))
                y += CAPTION_PT * 1.25
        meta = doc.metadata or {}
        meta.update({"title": title or meta.get("title", ""),
                     "subject": f"Signed by {signer} with Micronet e-Invoice" if signer else "",
                     "modDate": pymupdf.get_pdf_now()})
        doc.set_metadata(meta)
        return doc.tobytes(garbage=3, deflate=True)


def record(conn: sqlite3.Connection, *, title: str, source_path: str, source_pdf: bytes,
           signed_pdf: bytes, placements: list[Placement], signature_id: int) -> int:
    """Keep the signed copy and write it to the history."""
    with transaction(conn):
        doc_id = conn.execute(
            """INSERT INTO signed_documents (title, source_path, source_sha256, pdf, sha256,
                   placements, signature_id, signed_by, signed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, source_path, _sha(source_pdf), signed_pdf, _sha(signed_pdf),
             json.dumps([p.__dict__ for p in placements]), signature_id,
             ledger.current_user(), ledger.now())).lastrowid
        pages = sorted({p.page + 1 for p in placements})
        ledger.audit(conn, "sign document",
                     f"{title}, page{'s' if len(pages) > 1 else ''} "
                     f"{', '.join(map(str, pages))} (SHA-256 {_sha(signed_pdf)[:12]})",
                     entity="signed document", entity_id=str(doc_id))
    return doc_id


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

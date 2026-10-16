"""Signatures on the app's PDFs (einvoice/signing.py, web/signing_views.py)."""
import base64
import json
import sqlite3
from io import BytesIO

import pymupdf
import pytest
from PIL import Image, ImageDraw

from einvoice import signing
from einvoice.signing import SigningError
from einvoice.web.app import create_app
from tests.conftest import sign_in
from tests.test_form import form


def signature_picture(fmt="PNG", background=(255, 255, 255, 255)) -> bytes:
    """A 'photo' of a signature: dark strokes on white paper."""
    img = Image.new("RGBA", (600, 300), background)
    d = ImageDraw.Draw(img)
    d.line([(100, 200), (200, 100), (300, 210), (420, 90), (500, 180)], fill=(20, 20, 60, 255), width=8)
    out = BytesIO()
    img.convert("RGB" if fmt == "JPEG" else "RGBA").save(out, fmt)
    return out.getvalue()


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


@pytest.fixture
def app(config):
    a = create_app(config)
    a.testing = True
    return a


@pytest.fixture
def client(app):
    return sign_in(app)


def issue(client) -> str:
    r = client.post("/invoices/issue", data=form() | {"token": "s1"})
    assert r.status_code == 302
    return r.location.rsplit("/", 1)[1]


def start_signing(client, src) -> str:
    r = client.get("/sign", query_string={"src": src, "title": "Service Invoice X"})
    assert r.status_code == 302 and "/sign/" in r.location, r.location
    return r.location.rsplit("/", 1)[1]


# --- the picture ------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_paper_becomes_transparent_and_is_cropped(fmt):
    png = signing.clean_image(signature_picture(fmt))
    img = Image.open(BytesIO(png))
    assert img.mode == "RGBA"
    assert img.width < 600 and img.height < 300          # cropped to the strokes
    assert img.getpixel((0, 0))[3] == 0                   # paper is see-through
    assert img.getchannel("A").getextrema()[1] == 255   # ink stays solid


def blank_page() -> bytes:
    out = BytesIO()
    Image.new("RGB", (200, 100), "white").save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("data, message", [
    (b"", "Draw your signature"),
    (b"not a picture", "isn't a picture"),
    (blank_page(), "No signature found"),
])
def test_bad_pictures(data, message):
    with pytest.raises(SigningError, match=message):
        signing.clean_image(data)


def test_placements_are_checked():
    ok = signing.parse_placements('[{"page": 0, "x": 0.5, "y": 0.8, "w": 0.2}]', 1)
    assert ok[0].page == 0 and ok[0].w == 0.2
    for bad in ["[]", "nonsense", '[{"page": 1, "x": 0.5, "y": 0.5, "w": 0.2}]',
                '[{"page": 0, "x": 1.2, "y": 0.5, "w": 0.2}]', '[{"page": 0, "x": 0.5}]']:
        with pytest.raises(SigningError):
            signing.parse_placements(bad, 1)


# --- pages --------------------------------------------------------------------

def test_pdf_links_ask_first(client):
    no = issue(client)
    page = client.get(f"/invoices/{no}").get_data(as_text=True)
    assert "data-sign" in page and 'id="sign-ask"' in page
    assert "Open without signature" in page and "Sign it" in page
    report = client.get("/books/report/SJ").get_data(as_text=True)
    assert "data-sign" in report


def test_unsigned_pdf_is_unchanged(client):
    no = issue(client)
    r = client.get(f"/invoices/{no}/pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"


def test_save_drawn_and_uploaded_signature(client, app):
    r = client.post("/signatures", data={"drawing": data_url(signature_picture())})
    assert r.status_code == 302
    assert client.get("/signatures/mine.png").mimetype == "image/png"
    r = client.post("/signatures", data={"picture": (BytesIO(signature_picture("JPEG")), "sig.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    conn = sqlite3.connect(app.config["EINVOICE"].db_path)
    rows = conn.execute("SELECT active FROM signatures ORDER BY id").fetchall()
    assert rows == [(0,), (1,)]                           # the earlier one is kept, inactive
    actions = [a for (a,) in conn.execute("SELECT action FROM audit_log")]
    assert actions.count("save signature") == 2


def test_bad_signature_is_explained(client):
    r = client.post("/signatures", data={"drawing": ""}, follow_redirects=True)
    assert "Draw your signature first" in r.get_data(as_text=True)


def test_sign_an_invoice(client, app):
    no = issue(client)
    client.post("/signatures", data={"drawing": data_url(signature_picture())})
    token = start_signing(client, f"/invoices/{no}/pdf")
    page = client.get(f"/sign/{token}").get_data(as_text=True)
    assert "Sign: Service Invoice X" in page and "Page 1 of 1" in page
    png = client.get(f"/sign/{token}/page/0.png")
    assert png.status_code == 200 and png.mimetype == "image/png"

    places = json.dumps([{"page": 0, "x": 0.65, "y": 0.74, "w": 0.22}])
    r = client.post(f"/sign/{token}", data={"placements": places, "caption": "1"})
    assert r.status_code == 302 and r.location.endswith("/signed/1")
    assert "Signed: Service Invoice X" in client.get(r.location).get_data(as_text=True)

    pdf = client.get("/signed/1.pdf").data
    original = client.get(f"/invoices/{no}/pdf").data
    with pymupdf.open(stream=pdf, filetype="pdf") as doc, \
            pymupdf.open(stream=original, filetype="pdf") as before:
        assert len(doc[0].get_images()) == len(before[0].get_images()) + 1
        assert "Test admin" in doc[0].get_text()           # the caption
    conn = sqlite3.connect(app.config["EINVOICE"].db_path)
    detail = conn.execute("SELECT detail FROM audit_log WHERE action = 'sign document'").fetchone()[0]
    assert detail.startswith("Service Invoice X, page 1")
    # The signed copy can't be changed or deleted.
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM signed_documents")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE signed_documents SET title = 'x'")
    # Signing used up the pending copy.
    assert client.get(f"/sign/{token}").status_code == 404
    assert "Service Invoice X" in client.get("/signatures").get_data(as_text=True)


def test_sign_without_caption_and_on_a_report(client):
    issue(client)
    client.post("/signatures", data={"drawing": data_url(signature_picture())})
    token = start_signing(client, "/books/report/SJ?from=2024-11-01&to=2024-11-30&format=pdf")
    r = client.post(f"/sign/{token}", data={
        "placements": json.dumps([{"page": 0, "x": 0.1, "y": 0.8, "w": 0.15}])})
    assert r.status_code == 302
    with pymupdf.open(stream=client.get("/signed/1.pdf").data, filetype="pdf") as doc:
        assert "Test admin" not in doc[0].get_text()


def test_no_signature_yet(client):
    no = issue(client)
    token = start_signing(client, f"/invoices/{no}/pdf")
    assert "You have no saved signature yet" in client.get(f"/sign/{token}").get_data(as_text=True)
    r = client.post(f"/sign/{token}", data={"placements": "[]"})
    assert r.status_code == 302 and "/signatures" in r.location and "next=" in r.location


def test_only_app_pdfs(client):
    for src in ["https://example.com/x.pdf", "//evil/x.pdf", "/sign?src=/x", "/books"]:
        r = client.get("/sign", query_string={"src": src}, follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "can be signed" in text or "couldn&#39;t be made" in text, src


def test_someone_elses_signing_page(app, client):
    no = issue(client)
    token = start_signing(client, f"/invoices/{no}/pdf")
    other = sign_in(app, "second")
    assert other.get(f"/sign/{token}").status_code == 404
    assert other.get(f"/sign/{token}/page/0.png").status_code == 404

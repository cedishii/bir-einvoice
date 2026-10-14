from decimal import Decimal as D

import pytest

from einvoice.models import InvoiceLine
from einvoice.pdf.service_invoice import render
from tests.conftest import make_invoice


def _text(pdf: bytes) -> str:
    pymupdf = pytest.importorskip("pymupdf")  # optional dev tool, used only to read text back
    text = " ".join(page.get_text() for page in pymupdf.open(stream=pdf))
    return " ".join(text.split())  # wrapped lines -> single spaces


def _issued(repo, **kw):
    return repo.issue(make_invoice(**kw))


def test_invoice_shows_required_details(repo, config):
    text = _text(render(_issued(repo), config))
    for expected in [
        "SERVICE INVOICE", "No. 0001773", "November 25, 2024",
        "MICRONET SOFTWARE MANILA, INC.", "002-372-466-00000", "02-8812-8174",
        "Legaspi Village, Makati City",
        "MICRONET CO., LTD.", "SAPPORO, JAPAN", "Graphic Design",
        "IT services rendered for Nov 2024",
        "ZERO-RATED SALE", "Zero-Rated Sales",
        "12,000,000.00", "4,560,000.00", "PHP 0.38 per JPY 1",
        "TWELVE MILLION YEN ONLY",
    ]:
        assert expected in text, expected


def test_prototype_watermark_while_bir_values_are_placeholders(repo, config):
    inv = _issued(repo)
    assert "PROTOTYPE" in _text(render(inv, config))
    real = config.with_(eis_cert_no="20ABC123", ptu_num="PTU-123")
    text = _text(render(inv, real))
    assert "PROTOTYPE" not in text and "PTU-123" in text


def test_void_invoice_is_marked(repo, config):
    inv = _issued(repo)
    repo.void(inv.control_no, "Wrong amount")
    text = _text(render(repo.get(inv.control_no), config))
    assert "VOID" in text and "Wrong amount" in text


def test_many_lines_flow_to_more_pages(repo, config):
    lines = [InvoiceLine("IT Services", f"Task {i}", D("1"), "LOT", D("1000"))
             for i in range(20)]
    pdf = render(_issued(repo, lines=lines), config)
    assert pdf.startswith(b"%PDF")
    assert "Task 19" in _text(pdf)


def test_draft_cannot_be_printed(config):
    with pytest.raises(ValueError):
        render(make_invoice(), config)

"""Printable SERVICE INVOICE (letter size), laid out after docs/old-receipt.jpg.

The PDF is always rebuilt from the saved invoice, so a reprint matches the
saved data exactly. While the BIR-issued EIS Cert Number / PTU number are
placeholders, every page carries a "PROTOTYPE" watermark.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from ..config import LOGO_PATH, Config
from ..models import PH_TZ, Invoice
from ..money import ZERO, amount_in_words, fmt, fmt_rate

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#555555")
RED = colors.HexColor("#c8102e")
LINE = colors.HexColor("#333333")
SHADE = colors.HexColor("#efefef")
ZR_FILL = colors.HexColor("#fdeaea")

PAGE_W, PAGE_H = LETTER
MARGIN = 0.55 * inch
CONTENT_W = PAGE_W - 2 * MARGIN


def _style(name, size=9, leading=None, bold=False, align=None, color=INK):
    s = ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica",
                       fontSize=size, leading=leading or size * 1.25, textColor=color)
    if align is not None:
        s.alignment = align
    return s


S_BODY = _style("body", 9)
S_SMALL = _style("small", 7.5, color=MUTED)
S_LABEL = _style("label", 8, bold=True, color=MUTED)
S_VALUE = _style("value", 10)
S_VALUE_B = _style("valueb", 10, bold=True)
S_CO = _style("co", 15, bold=True)
S_ADDR = _style("addr", 9, leading=12)
S_TITLE = _style("title", 18, bold=True, align=TA_RIGHT)
S_NO = _style("no", 16, bold=True, align=TA_RIGHT, color=RED)
S_RIGHT = _style("right", 9, align=TA_RIGHT)
S_RIGHT_B = _style("rightb", 9, bold=True, align=TA_RIGHT)
S_CELL = _style("cell", 8.5, leading=10.5)
S_HEAD = _style("head", 8, bold=True)
S_HEAD_R = _style("headr", 8, bold=True, align=TA_RIGHT)
S_BANNER = _style("banner", 11, bold=True, align=TA_CENTER, color=RED)


def _p(text, style=S_BODY):
    return Paragraph(_esc(text), style)


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _money(v: Decimal) -> str:
    return fmt(v)


def _qty(v: Decimal) -> str:
    """1 -> '1', 1.50 -> '1.5'."""
    return f"{int(v):,}" if v == v.to_integral_value() else f"{v.normalize():f}"


def _header(inv: Invoice, cfg: Config):
    seller = cfg.seller
    logo_w = 1.55 * inch
    logo = Image(str(LOGO_PATH), width=logo_w, height=logo_w * 656 / 901)
    company = [
        Paragraph(_esc(seller.reg_nm), S_CO),
        Spacer(0, 3),
        Paragraph(_esc(seller.reg_addr), S_ADDR),
        Paragraph(f"VAT Reg. TIN: <b>{seller.tin_display}</b>", S_ADDR),
        Paragraph(f"Tel. No.: {_esc(seller.tel)}", S_ADDR),
    ]
    if seller.email:
        company.append(Paragraph(_esc(seller.email), S_ADDR))
    title = [
        Paragraph("SERVICE INVOICE", S_TITLE),
        Spacer(0, 4),
        Paragraph(f"No. {_esc(inv.control_no)}", S_NO),
        Spacer(0, 6),
        Paragraph(f"Date: <b>{inv.issue_date.strftime('%B %d, %Y')}</b>", S_RIGHT),
    ]
    t = Table([[logo, company, title]],
              colWidths=[logo_w + 0.2 * inch, 2.95 * inch, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, INK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _banner():
    t = Table([[Paragraph("ZERO-RATED SALE", S_BANNER)]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.2, RED),
        ("BACKGROUND", (0, 0), (-1, -1), ZR_FILL),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _client(inv: Invoice):
    rows = [
        ["SOLD TO", _p(inv.buyer_name, S_VALUE_B)],
        ["TIN", _p(inv.buyer_tin_display, S_VALUE)],
        ["ADDRESS", _p(inv.buyer_address, S_VALUE)],
        ["BUSINESS STYLE", _p(inv.business_style or "-", S_VALUE)],
    ]
    t = Table([[_p(k, S_LABEL), v] for k, v in rows],
              colWidths=[1.25 * inch, CONTENT_W - 1.25 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (1, 0), (1, -1), 0.4, colors.HexColor("#bbbbbb")),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _lines(inv: Invoice):
    head = [_p("#", S_HEAD), _p("PARTICULARS (payment for)", S_HEAD), _p("QTY", S_HEAD_R),
            _p("UNIT", S_HEAD), _p("UNIT PRICE (JPY)", S_HEAD_R),
            _p("AMOUNT (JPY)", S_HEAD_R), _p("AMOUNT (PHP)", S_HEAD_R)]
    rows = [head]
    for i, line in enumerate(inv.lines, start=1):
        particulars = Paragraph(
            f"<b>{_esc(line.item_name)}</b><br/>{_esc(line.description)}", S_CELL)
        rows.append([
            _p(i, S_CELL), particulars, _p(_qty(line.qty), _style("q", 8.5, align=TA_RIGHT)),
            _p(line.unit or "", S_CELL),
            _p(_money(line.unit_cost_jpy), _style("u", 8.5, align=TA_RIGHT)),
            _p(_money(line.amount_jpy), _style("a", 8.5, align=TA_RIGHT)),
            _p(_money(line.sales_php), _style("p", 8.5, align=TA_RIGHT)),
        ])
    widths = [0.3, None, 0.55, 0.6, 1.2, 1.2, 1.2]
    fixed = sum(w for w in widths if w) * inch
    col_w = [w * inch if w else CONTENT_W - fixed for w in widths]
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SHADE),
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _summary(inv: Invoice):
    # Left: totals in JPY, the rate used and the amount in words.
    left_rows = [
        [_p("TOTAL AMOUNT (JPY)", S_LABEL), _p(_money(inv.total_jpy), S_RIGHT_B)],
        [_p("EXCHANGE RATE", S_LABEL),
         _p(f"PHP {fmt_rate(inv.rate)} per JPY 1", S_RIGHT)],
        [_p("RATE DATE", S_LABEL), _p(inv.rate_date.strftime("%B %d, %Y"), S_RIGHT)],
        [_p("RATE SOURCE", S_LABEL), _p(inv.rate_source, S_RIGHT)],
        [_p("PHP EQUIVALENT", S_LABEL), _p(_money(inv.total_php), S_RIGHT_B)],
    ]
    left = Table(left_rows, colWidths=[1.6 * inch, 1.9 * inch])
    left.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    words = [
        Spacer(0, 6),
        _p("THE SUM OF", S_LABEL),
        _p(amount_in_words(inv.total_jpy, "YEN"), _style("w", 8.5, bold=True)),
        _p(f"(PHP equivalent: {amount_in_words(inv.total_php, 'PESOS')})", S_SMALL),
    ]

    # Right: the BIR sales summary box, as on the paper receipt.
    zr = inv.total_php
    box = [
        ["VATable Sales", ZERO],
        ["VAT-Exempt Sales", ZERO],
        ["Zero-Rated Sales", zr],
        ["Total Sales", zr],
        ["VAT Amount", ZERO],
        ["Less: Withholding Tax", ZERO],
        ["TOTAL AMOUNT DUE (PHP)", zr],
    ]
    right_rows = [[_p(k, S_BODY), _p(_money(v), S_RIGHT)] for k, v in box]
    right_rows[2] = [_p("Zero-Rated Sales", _style("zr", 9, bold=True, color=RED)),
                     _p(_money(zr), _style("zrv", 9, bold=True, align=TA_RIGHT, color=RED))]
    right_rows[-1] = [_p("TOTAL AMOUNT DUE (PHP)", _style("tad", 9, bold=True)),
                      _p(_money(zr), S_RIGHT_B)]
    right = Table(right_rows, colWidths=[1.85 * inch, 1.45 * inch])
    right.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 2), (-1, 2), ZR_FILL),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, LINE),
        ("BACKGROUND", (0, -1), (-1, -1), SHADE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))

    outer = Table([[[left] + words, right]],
                  colWidths=[CONTENT_W - 3.3 * inch, 3.3 * inch])
    outer.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 12),
    ]))
    return outer


def _signature():
    t = Table([["", _p("By:", S_BODY)], ["", _p("Authorized Representative", S_SMALL)]],
              colWidths=[CONTENT_W - 2.8 * inch, 2.8 * inch], rowHeights=[0.45 * inch, None])
    t.setStyle(TableStyle([
        ("LINEBELOW", (1, 0), (1, 0), 0.6, INK),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _cas_footer(cfg: Config):
    ptu = "[to be issued by BIR]" if "PTU_NUM" in cfg.placeholders else cfg.ptu_num
    return [
        _p("This is a system-generated Service Invoice. All amounts are in Philippine pesos "
           "(PHP) unless marked JPY; JPY amounts are converted at the exchange rate shown.",
           S_SMALL),
        _p(f"CAS Permit To Use / Acknowledgment Certificate No.: {ptu}", S_SMALL),
    ]


def _decorate(cfg: Config, inv: Invoice):
    printed = datetime.now(PH_TZ).strftime("%Y-%m-%d %H:%M")

    def draw(canvas, doc):
        canvas.saveState()
        stamps = []
        if inv.status == "void":
            stamps.append(("VOID", RED, 110))
        if cfg.is_prototype:
            stamps.append(("PROTOTYPE - NOT A VALID INVOICE", colors.HexColor("#999999"), 34))
        for i, (text, color, size) in enumerate(stamps):
            canvas.saveState()
            canvas.setFillColor(color)
            canvas.setFillAlpha(0.18)
            canvas.translate(PAGE_W / 2, PAGE_H / 2 - i * 150)
            canvas.rotate(30)
            canvas.setFont("Helvetica-Bold", size)
            canvas.drawCentredString(0, 0, text)
            canvas.restoreState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 0.35 * inch,
                          f"Service Invoice No. {inv.control_no}  |  printed {printed}")
        canvas.drawRightString(PAGE_W - MARGIN, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def render(inv: Invoice, cfg: Config) -> bytes:
    if not inv.control_no:
        raise ValueError("Only issued invoices can be printed")
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=0.6 * inch,
        title=f"Service Invoice {inv.control_no}", author=cfg.seller.reg_nm,
        subject="Service Invoice (zero-rated sale)",
    )
    story = [
        _header(inv, cfg), Spacer(0, 10),
        _banner(), Spacer(0, 10),
        _client(inv), Spacer(0, 12),
        _lines(inv), Spacer(0, 12),
        KeepTogether([_summary(inv), Spacer(0, 14), _signature()]),
        Spacer(0, 18),
        *_cas_footer(cfg),
    ]
    if inv.status == "void":
        story.insert(4, _p(f"VOIDED {inv.voided_at[:10]}: {inv.void_reason}",
                           _style("void", 10, bold=True, color=RED)))
    draw = _decorate(cfg, inv)
    doc.build(story, onFirstPage=draw, onLaterPages=draw)
    return buf.getvalue()

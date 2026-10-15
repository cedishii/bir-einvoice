"""Reports as PDF (printed books) and CSV, and the year-end archive.

Every PDF page carries the company name, TIN and address, the report title
and period, the page number ("Page 3 of 12") and the PTU / Acknowledgment
Certificate number of the system. While that number is a placeholder, the
pages are watermarked as a prototype.
"""
from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from ..config import Config
from ..models import PH_TZ
from . import ledger, reports
from .reports import Report

PAGE = landscape(LETTER)
MARGIN = 0.45 * inch
HEADER_H = 0.85 * inch
FOOTER_H = 0.35 * inch
SHADE = colors.HexColor("#ececec")
LINE = colors.HexColor("#999999")

S_CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7.2, leading=8.6)
S_NUM = ParagraphStyle("num", parent=S_CELL, alignment=TA_RIGHT)
S_HEAD = ParagraphStyle("head", parent=S_CELL, fontName="Helvetica-Bold")
S_HEAD_NUM = ParagraphStyle("headnum", parent=S_HEAD, alignment=TA_RIGHT)
S_NOTE = ParagraphStyle("note", parent=S_CELL, fontSize=7.5, leading=9.5,
                        textColor=colors.HexColor("#444444"))


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    return str(value)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def system_line(cfg: Config) -> str:
    ptu = "not issued (prototype)" if "PTU_NUM" in cfg.placeholders else cfg.ptu_num
    return f"Micronet e-Invoice books · PTU / Acknowledgment Certificate No.: {ptu}"


def report_pdf(report: Report, cfg: Config) -> bytes:
    buf = io.BytesIO()
    width = PAGE[0] - 2 * MARGIN
    printed = datetime.now(PH_TZ).strftime("%Y-%m-%d %H:%M")
    user = ledger.current_user()
    seller = cfg.seller
    prototype = "PTU_NUM" in cfg.placeholders

    def decorate(c: rl_canvas.Canvas, page: int, pages: int):
        w, h = PAGE
        top = h - MARGIN
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(MARGIN, top - 10, seller.reg_nm)
        c.setFont("Helvetica", 8)
        c.drawString(MARGIN, top - 22, f"VAT REG TIN {seller.tin_display}")
        c.drawString(MARGIN, top - 32, seller.reg_addr)
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(w - MARGIN, top - 10, report.title)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(w - MARGIN, top - 22,
                          report.period + ("" if report.key == "audit" else " · Amounts in PHP"))
        c.setStrokeColor(LINE)
        c.line(MARGIN, top - 40, w - MARGIN, top - 40)
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.HexColor("#555555"))
        c.drawString(MARGIN, MARGIN, f"{system_line(cfg)} · Printed {printed} by {user}")
        c.drawRightString(w - MARGIN, MARGIN, f"Page {page} of {pages}")
        if prototype:
            c.saveState()
            c.setFillColor(colors.Color(0.8, 0.1, 0.15, alpha=0.08))
            c.setFont("Helvetica-Bold", 34)
            c.translate(w / 2, h / 2)
            c.rotate(25)
            c.drawCentredString(0, 0, "PROTOTYPE - NOT REGISTERED WITH BIR")
            c.restoreState()

    class NumberedCanvas(rl_canvas.Canvas):
        """Draws headers and footers after the last page, so "of N" is known."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for i, state in enumerate(self._pages, start=1):
                self.__dict__.update(state)
                decorate(self, i, len(self._pages))
                super().showPage()
            super().save()

    doc = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN + HEADER_H - 0.25 * inch,
                            bottomMargin=MARGIN + FOOTER_H, title=report.title,
                            author=seller.reg_nm)
    total_w = sum(c.width for c in report.columns)
    widths = [width * c.width / total_w for c in report.columns]
    head = [Paragraph(_esc(c.label), S_HEAD_NUM if c.align == "num" else S_HEAD)
            for c in report.columns]
    data, style = [head], [
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, row in enumerate(report.rows, start=1):
        if row.kind == "group":
            data.append([Paragraph(_esc(cell_text(row.cells[0])), S_HEAD)]
                        + [""] * (len(report.columns) - 1))
            style += [("SPAN", (0, i), (-1, i)), ("BACKGROUND", (0, i), (-1, i), SHADE)]
            continue
        bold = row.kind == "total"
        cells = []
        for col, value in zip(report.columns, row.cells):
            st = S_NUM if col.align == "num" else S_CELL
            if bold:
                st = S_HEAD_NUM if col.align == "num" else S_HEAD
            cells.append(Paragraph(_esc(cell_text(value)), st))
        data.append(cells)
        if bold:
            style.append(("LINEABOVE", (0, i), (-1, i), 0.6, colors.black))
    if not report.rows:
        data.append([Paragraph("No entries in this period.", S_CELL)]
                    + [""] * (len(report.columns) - 1))
        style.append(("SPAN", (0, 1), (-1, 1)))
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style))
    story = [table] + [Paragraph(_esc(n), S_NOTE) for n in report.notes]
    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def report_csv(report: Report, cfg: Config) -> bytes:
    """CSV for Excel (UTF-8 with BOM). Amounts without thousands separators."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([cfg.seller.reg_nm])
    w.writerow([f"VAT REG TIN {cfg.seller.tin_display}"])
    w.writerow([report.title, report.period, "Amounts in PHP"])
    w.writerow([])
    w.writerow([c.label for c in report.columns])
    for row in report.rows:
        w.writerow(["" if v is None else f"{v:.2f}" if isinstance(v, Decimal) else str(v)
                    for v in row.cells])
    for n in report.notes:
        w.writerow([])
        w.writerow([n])
    return out.getvalue().encode("utf-8-sig")


# --- Year-end archive --------------------------------------------------------

def year_reports(conn: sqlite3.Connection, year: int) -> list[Report]:
    start, end = date(year, 1, 1), date(year, 12, 31)
    out = [reports.journal(conn, b, start, end) for b in ledger.BOOKS]
    out += [reports.general_ledger(conn, start, end),
            reports.subsidiary_ledger(conn, "customer", start, end),
            reports.subsidiary_ledger(conn, "supplier", start, end),
            reports.trial_balance(conn, end),
            reports.income_statement(conn, start, end),
            reports.balance_sheet(conn, end),
            reports.monthly_summary(conn, start, end)]
    for q in range(4):
        qs, qe = reports.quarter_of(date(year, 3 * q + 1, 1))
        out += [reports.sls(conn, qs, qe), reports.slp(conn, qs, qe),
                reports.withholding(conn, "issued", qs, qe),
                reports.withholding(conn, "received", qs, qe)]
    out.append(reports.audit_log(conn, start, end))
    return out


def file_stem(report: Report) -> str:
    stem = report.key
    if report.key in ("sls", "slp") or report.key.startswith("wt-"):
        stem += f"-{report.end.year}Q{(report.end.month - 1) // 3 + 1}"
    return stem


def year_archive(conn: sqlite3.Connection, cfg: Config, year: int) -> bytes:
    """ZIP of every book and report for a year (PDF + CSV), with a manifest of
    SHA-256 checksums and the result of the journal integrity check."""
    buf = io.BytesIO()
    lines = [f"{cfg.seller.reg_nm} - VAT REG TIN {cfg.seller.tin_display}",
             f"Books of accounts for calendar year {year}",
             system_line(cfg),
             f"Created {datetime.now(PH_TZ):%Y-%m-%d %H:%M} by {ledger.current_user()}", ""]
    problems = ledger.verify(conn)
    lines.append("Journal integrity check: " + ("OK" if not problems else "FAILED"))
    lines += [f"  {p}" for p in problems]
    last = conn.execute("SELECT hash FROM journal_entries ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        lines.append(f"Last journal entry hash: {last['hash']}")
    lines += ["", "SHA-256 checksums:"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in year_reports(conn, year):
            for ext, content in (("pdf", report_pdf(r, cfg)), ("csv", report_csv(r, cfg))):
                name = f"{file_stem(r)}.{ext}"
                z.writestr(name, content)
                lines.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
        z.writestr("MANIFEST.txt", "\r\n".join(lines) + "\r\n")
    ledger.audit(conn, "export", f"year-end archive of the books for {year}", entity="report",
                 entity_id=f"archive-{year}")
    return buf.getvalue()

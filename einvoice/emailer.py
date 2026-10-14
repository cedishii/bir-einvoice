"""E-mail the Service Invoice PDF to the client over SMTP.

Works with any mail provider that allows SMTP (Microsoft 365 / Outlook,
Gmail / Google Workspace, the company's own server). Settings are in .env.

Safety: while the app is in prototype mode (BIR values still placeholders)
the PDF carries a "PROTOTYPE - NOT A VALID INVOICE" watermark, so e-mails
never go to the client. They go to EMAIL_TEST_TO instead, marked [TEST],
or are not sent at all if EMAIL_TEST_TO is empty.
"""
from __future__ import annotations

import os
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Callable

from .config import Config
from .models import PH_TZ, Invoice, is_email
from .money import fmt, fmt_rate
from .pdf.service_invoice import render as render_pdf

SECURITY = ("starttls", "ssl", "none")


@dataclass(frozen=True)
class EmailSettings:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    user: str = ""
    password: str = ""
    from_addr: str = ""
    from_name: str = ""
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    auto_send: bool = False
    test_to: str = ""
    timeout_seconds: int = 30

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.host:
            out.append("SMTP_HOST")
        if not is_email(self.from_addr):
            out.append("EMAIL_FROM")
        return out

    @property
    def is_configured(self) -> bool:
        return not self.missing


def _addresses(text: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip())


def load_email_settings() -> EmailSettings:
    env = os.environ.get
    security = env("SMTP_SECURITY", "starttls").strip().lower()
    if security not in SECURITY:
        raise ValueError(f"SMTP_SECURITY must be one of {SECURITY}")
    return EmailSettings(
        host=env("SMTP_HOST", "").strip(),
        port=int(env("SMTP_PORT", "465" if security == "ssl" else "587")),
        security=security,
        user=env("SMTP_USER", "").strip(),
        password=env("SMTP_PASSWORD", ""),
        from_addr=env("EMAIL_FROM", "").strip(),
        from_name=env("EMAIL_FROM_NAME", "").strip(),
        cc=_addresses(env("EMAIL_CC", "")),
        bcc=_addresses(env("EMAIL_BCC", "")),
        auto_send=env("EMAIL_AUTO_SEND", "false").strip().lower() in ("1", "true", "yes", "on"),
        test_to=env("EMAIL_TEST_TO", "").strip(),
        timeout_seconds=int(env("SMTP_TIMEOUT_SECONDS", "30")),
    )


class EmailError(Exception):
    pass


Sender = Callable[[EmailSettings, EmailMessage], None]


def smtp_send(settings: EmailSettings, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.security == "ssl":
        server = smtplib.SMTP_SSL(settings.host, settings.port, context=context,
                                  timeout=settings.timeout_seconds)
    else:
        server = smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds)
    with server:
        if settings.security == "starttls":
            server.starttls(context=context)
        if settings.user:
            server.login(settings.user, settings.password)
        server.send_message(msg)


def build_message(inv: Invoice, cfg: Config, settings: EmailSettings, pdf: bytes,
                  to: str, test: bool) -> EmailMessage:
    seller = cfg.seller
    subject = f"Service Invoice No. {inv.control_no} - {seller.reg_nm}"
    if test:
        subject = "[TEST] " + subject
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.from_name or seller.reg_nm, settings.from_addr))
    msg["To"] = to
    if settings.cc and not test:
        msg["Cc"] = ", ".join(settings.cc)
    if settings.bcc and not test:
        msg["Bcc"] = ", ".join(settings.bcc)
    msg["Message-ID"] = make_msgid(domain=settings.from_addr.split("@")[-1])

    lines = [
        f"Dear {inv.buyer_name},",
        "",
        f"Please find attached Service Invoice No. {inv.control_no} dated "
        f"{inv.issue_date.strftime('%B %d, %Y')}.",
        "",
        f"  Amount:          JPY {fmt(inv.total_jpy)}",
        f"  PHP equivalent:  PHP {fmt(inv.total_php)} "
        f"(at PHP {fmt_rate(inv.rate)} per JPY 1)",
        "  Sale type:       Zero-rated sale (export of services)",
        "",
        *(f"  {line.description}" for line in inv.lines),
        "",
        "Thank you for your business.",
        "",
        seller.reg_nm,
        seller.reg_addr,
        f"Tel. {seller.tel}",
    ]
    if test:
        lines = [f"*** TEST e-mail. In real use this would go to {to_original(inv)}. ***",
                 "*** The attached invoice is a PROTOTYPE and not valid. ***", ""] + lines
    msg.set_content("\n".join(lines))
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename=f"Service-Invoice-{inv.control_no}.pdf")
    return msg


def to_original(inv: Invoice) -> str:
    return inv.buyer_email or "(no client e-mail on file)"


@dataclass
class EmailResult:
    sent: bool
    to: str
    test: bool
    message: str


def send_invoice(conn: sqlite3.Connection, inv: Invoice, cfg: Config, settings: EmailSettings,
                 *, automatic: bool = False, sender: Sender = smtp_send) -> EmailResult:
    """Send (and log) the invoice e-mail. Raises EmailError before sending if
    it can't be sent; a failed SMTP attempt is logged and re-raised."""
    if inv.status != "issued":
        raise EmailError("Only issued invoices can be e-mailed.")
    if not settings.is_configured:
        raise EmailError("E-mail is not set up. Missing in .env: " + ", ".join(settings.missing))
    test = cfg.is_prototype
    if test:
        if not is_email(settings.test_to):
            raise EmailError("Prototype mode: invoices are only e-mailed to EMAIL_TEST_TO, "
                             "which is not set in .env.")
        to = settings.test_to
    else:
        if not inv.buyer_email:
            raise EmailError("This invoice has no client e-mail.")
        to = inv.buyer_email

    msg = build_message(inv, cfg, settings, render_pdf(inv, cfg), to, test)
    now = datetime.now(PH_TZ).isoformat(timespec="seconds")
    cc = "" if test else ", ".join(settings.cc)
    try:
        sender(settings, msg)
    except (smtplib.SMTPException, OSError) as e:
        _log(conn, inv, now, to, cc, msg["Subject"], "failed", str(e), test, automatic)
        raise EmailError(f"Sending failed: {e}") from None
    _log(conn, inv, now, to, cc, msg["Subject"], "sent", "", test, automatic)
    label = f"TEST e-mail sent to {to}" if test else f"E-mailed to {to}"
    return EmailResult(True, to, test, label + (f" (cc {cc})" if cc else "") + ".")


def _log(conn, inv, when, to, cc, subject, status, error, test, automatic):
    conn.execute(
        """INSERT INTO invoice_emails (invoice_id, sent_at, to_addr, cc, subject, status,
               error, test, automatic) VALUES (?,?,?,?,?,?,?,?,?)""",
        (inv.id, when, to, cc, subject, status, error, int(test), int(automatic)))


def history(conn: sqlite3.Connection, inv: Invoice) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM invoice_emails WHERE invoice_id = ? ORDER BY id DESC",
                        (inv.id,)).fetchall()

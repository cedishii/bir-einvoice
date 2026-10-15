"""E-mail the Service Invoice PDF to the client over SMTP.

Works with any mail provider that allows SMTP (Microsoft 365 / Outlook,
Gmail / Google Workspace, the company's own server). Settings are in .env.

The user types the recipients on the invoice page (the client's e-mail is
filled in); automatic e-mails on issue go to the client's e-mail. Every
e-mail says it was sent automatically and needs no reply.
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
from .money import fmt
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


MAX_RECIPIENTS = 10


def _addresses(text: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip())


def parse_recipients(text: str, label: str) -> tuple[str, ...]:
    """'a@x.com; b@y.com' -> ('a@x.com', 'b@y.com'), checking each address."""
    found = _addresses(text)
    bad = [a for a in found if not is_email(a)]
    if bad:
        raise EmailError(f"{label}: not a valid e-mail address: {', '.join(bad)}")
    if len(found) > MAX_RECIPIENTS:
        raise EmailError(f"{label}: at most {MAX_RECIPIENTS} addresses.")
    return found


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


AUTO_NOTE = ("This is an automatically generated e-mail. Please do not reply to this "
             "message; replies to this address are not monitored.")


def build_message(inv: Invoice, cfg: Config, settings: EmailSettings, pdf: bytes,
                  to: tuple[str, ...], cc: tuple[str, ...]) -> EmailMessage:
    seller = cfg.seller
    msg = EmailMessage()
    msg["Subject"] = f"Service Invoice No. {inv.control_no} - {seller.reg_nm}"
    msg["From"] = formataddr((settings.from_name or seller.reg_nm, settings.from_addr))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if settings.bcc:
        msg["Bcc"] = ", ".join(settings.bcc)
    msg["Auto-Submitted"] = "auto-generated"  # tells mail programs not to auto-reply
    msg["Message-ID"] = make_msgid(domain=settings.from_addr.split("@")[-1])

    lines = [
        f"Dear {inv.buyer_name},",
        "",
        f"Please find attached Service Invoice No. {inv.control_no} dated "
        f"{inv.issue_date.strftime('%B %d, %Y')}.",
        "",
        *([f"  Amount:          JPY {fmt(inv.total_jpy)} + 12% VAT JPY {fmt(inv.vat_jpy)}"]
          if inv.is_vatable else []),
        f"  Total due:       JPY {fmt(inv.total_due_jpy)}",
        f"  PHP equivalent:  PHP {fmt(inv.total_due_php)}",
        f"  Sale type:       {inv.sale_type}",
        "",
        *(f"  {line.description}" for line in inv.lines),
        "",
        "Thank you for your business.",
        "",
        seller.reg_nm,
        seller.reg_addr,
        f"Tel. {seller.tel}",
        "",
        "-" * 60,
        AUTO_NOTE,
        "For questions about this invoice, please contact us at "
        + (f"{seller.email} or " if seller.email else "") + f"tel. {seller.tel}.",
    ]
    msg.set_content("\n".join(lines))
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename=f"Service-Invoice-{inv.control_no}.pdf")
    return msg


@dataclass
class EmailResult:
    sent: bool
    to: str
    message: str


def send_invoice(conn: sqlite3.Connection, inv: Invoice, cfg: Config, settings: EmailSettings,
                 *, to: str | None = None, cc: str = "", automatic: bool = False,
                 sender: Sender = smtp_send) -> EmailResult:
    """Send (and log) the invoice e-mail to `to` (one or more addresses,
    separated by commas or semicolons; default: the client's e-mail), with
    `cc` plus EMAIL_CC. Raises EmailError before sending if it can't be sent;
    a failed SMTP attempt is logged and re-raised."""
    if inv.status != "issued":
        raise EmailError("Only issued invoices can be e-mailed.")
    if not settings.is_configured:
        raise EmailError("E-mail is not set up. Missing in .env: " + ", ".join(settings.missing))
    recipients = parse_recipients(inv.buyer_email if to is None else to, "To")
    if not recipients:
        raise EmailError("Enter at least one e-mail address to send to." if to is not None
                         else "This invoice has no client e-mail.")
    copies = tuple(dict.fromkeys(parse_recipients(cc, "Cc") + settings.cc))

    msg = build_message(inv, cfg, settings, render_pdf(inv, cfg), recipients, copies)
    now = datetime.now(PH_TZ).isoformat(timespec="seconds")
    to_text, cc_text = ", ".join(recipients), ", ".join(copies)
    try:
        sender(settings, msg)
    except (smtplib.SMTPException, OSError) as e:
        _log(conn, inv, now, to_text, cc_text, msg["Subject"], "failed", str(e), automatic)
        raise EmailError(f"Sending failed: {e}") from None
    _log(conn, inv, now, to_text, cc_text, msg["Subject"], "sent", "", automatic)
    return EmailResult(True, to_text, f"E-mailed to {to_text}"
                       + (f" (cc {cc_text})" if cc_text else "") + ".")


def _log(conn, inv, when, to, cc, subject, status, error, automatic):
    conn.execute(
        """INSERT INTO invoice_emails (invoice_id, sent_at, to_addr, cc, subject, status,
               error, automatic) VALUES (?,?,?,?,?,?,?,?)""",
        (inv.id, when, to, cc, subject, status, error, int(automatic)))


def history(conn: sqlite3.Connection, inv: Invoice) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM invoice_emails WHERE invoice_id = ? ORDER BY id DESC",
                        (inv.id,)).fetchall()

import smtplib

import pytest

from einvoice import emailer
from einvoice.emailer import EmailError, EmailSettings
from tests.conftest import make_invoice
from tests.test_form import form
from tests.conftest import sign_in

SETTINGS = EmailSettings(host="smtp.example.com", from_addr="billing@micronet.example",
                         cc=("accounting@micronet.example",))


class Outbox(list):
    def __call__(self, settings, msg):
        self.append(msg)


def real(config):
    return config.with_(eis_cert_no="20ABC123", ptu_num="PTU-1")


def test_message_contents(repo, config):
    inv = repo.issue(make_invoice(buyer_email="ap@micronet.co.jp"))
    out = Outbox()
    result = emailer.send_invoice(repo.conn, inv, real(config), SETTINGS, sender=out)
    msg = out[0]
    assert result.sent
    assert msg["To"] == "ap@micronet.co.jp" and msg["Cc"] == "accounting@micronet.example"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Subject"] == "Service Invoice No. 0001773 - MICRONET SOFTWARE MANILA, INC."
    body = msg.get_body(("plain",)).get_content()
    assert "JPY 12,000,000.00" in body and "PHP 4,560,000.00" in body
    assert "automatically generated e-mail. Please do not reply" in body
    assert "[TEST]" not in msg["Subject"] and "TEST e-mail" not in body
    [pdf] = list(msg.iter_attachments())
    assert pdf.get_filename() == "Service-Invoice-0001773.pdf"
    assert pdf.get_content().startswith(b"%PDF")


def test_typed_recipients_and_cc(repo, config):
    """Any addresses can be typed, even in prototype mode (no test-only mode)."""
    inv = repo.issue(make_invoice(buyer_email="ap@micronet.co.jp"))
    out = Outbox()
    result = emailer.send_invoice(repo.conn, inv, config, SETTINGS, sender=out,
                                  to="boss@client.jp; ap@micronet.co.jp", cc="me@micronet.example")
    assert out[0]["To"] == "boss@client.jp, ap@micronet.co.jp"
    assert out[0]["Cc"] == "me@micronet.example, accounting@micronet.example"
    assert result.message.startswith("E-mailed to boss@client.jp, ap@micronet.co.jp")
    with pytest.raises(EmailError, match="not a valid e-mail address: nope"):
        emailer.send_invoice(repo.conn, inv, config, SETTINGS, sender=out, to="nope")
    with pytest.raises(EmailError, match="Enter at least one"):
        emailer.send_invoice(repo.conn, inv, config, SETTINGS, sender=out, to=" ")
    assert len(out) == 1


def test_refuses_without_settings_or_address(repo, config):
    inv = repo.issue(make_invoice())
    with pytest.raises(EmailError, match="SMTP_HOST"):
        emailer.send_invoice(repo.conn, inv, real(config), EmailSettings(), sender=Outbox())
    with pytest.raises(EmailError, match="no client e-mail"):
        emailer.send_invoice(repo.conn, inv, real(config), SETTINGS, sender=Outbox())


def test_failure_is_logged(repo, config):
    inv = repo.issue(make_invoice(buyer_email="ap@micronet.co.jp"))

    def broken(settings, msg):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    with pytest.raises(EmailError, match="Authentication failed"):
        emailer.send_invoice(repo.conn, inv, real(config), SETTINGS, sender=broken)
    [row] = emailer.history(repo.conn, inv)
    assert row["status"] == "failed" and "535" in row["error"]


def test_void_invoice_not_mailed(repo, config):
    inv = repo.issue(make_invoice(buyer_email="ap@micronet.co.jp"))
    repo.void(inv.control_no, "x")
    with pytest.raises(EmailError):
        emailer.send_invoice(repo.conn, repo.get(inv.control_no), real(config), SETTINGS,
                             sender=Outbox())


def test_settings_from_env(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.office365.com", "EMAIL_FROM": "a@b.co",
                 "EMAIL_CC": "x@b.co; y@b.co", "EMAIL_AUTO_SEND": "true",
                 "SMTP_SECURITY": "starttls"}.items():
        monkeypatch.setenv(k, v)
    s = emailer.load_email_settings()
    assert s.is_configured and s.auto_send and s.port == 587 and s.cc == ("x@b.co", "y@b.co")


def test_email_field_validated():
    from einvoice.models import FormErrors, invoice_from_form
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(buyer_email="a@b.co, c@d.co"))
    assert "buyer_email" in e.value.errors
    assert invoice_from_form(form(buyer_email="ap@micronet.co.jp")).buyer_email == "ap@micronet.co.jp"


def test_email_goes_into_the_bir_json(repo, config):
    from einvoice.eis import builder
    inv = repo.issue(make_invoice(buyer_email="ap@micronet.co.jp"))
    cfg = config.with_(eis_cert_no="20TEST00", ptu_num="TEST-PTU-NOT-VALID")
    assert builder.build(inv, cfg)["BuyerInfo"]["Email"] == "ap@micronet.co.jp"


# --- through the app --------------------------------------------------------------

def app_client(config, settings, out):
    from einvoice.web.app import create_app
    return sign_in(create_app(config, email=settings, email_sender=out))


def test_auto_send_on_issue(config):
    from dataclasses import replace
    out = Outbox()
    c = app_client(real(config), replace(SETTINGS, auto_send=True), out)
    c.post("/invoices/issue", data=form(buyer_email="ap@micronet.co.jp") | {"token": "e1"})
    assert len(out) == 1 and out[0]["To"] == "ap@micronet.co.jp"
    page = c.get("/invoices/0001773").get_data(as_text=True)
    assert "E-mailed to ap@micronet.co.jp" in page and "(auto)" in page


def test_no_auto_send_when_off(config):
    out = Outbox()
    c = app_client(real(config), SETTINGS, out)
    c.post("/invoices/issue", data=form(buyer_email="ap@micronet.co.jp") | {"token": "e2"})
    assert out == []
    c.post("/invoices/0001773/email", data={"to": "other@client.jp", "cc": ""})
    assert len(out) == 1 and out[0]["To"] == "other@client.jp"
    assert "Send again" in c.get("/invoices/0001773").get_data(as_text=True)


def test_auto_send_failure_does_not_block_issuing(config):
    from dataclasses import replace

    def broken(settings, msg):
        raise OSError("connection refused")

    c = app_client(real(config), replace(SETTINGS, auto_send=True), broken)
    r = c.post("/invoices/issue", data=form(buyer_email="ap@micronet.co.jp") | {"token": "e3"},
               follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Service Invoice No. 0001773 issued" in page
    assert "Automatic e-mail not sent" in page and "connection refused" in page

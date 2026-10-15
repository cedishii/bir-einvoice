import smtplib
from pathlib import Path

import pytest

from einvoice import backup, emailer, health
from einvoice.config import _backup_copy_dir
from einvoice.storage.db import connect
from einvoice.web.app import create_app
from tests.conftest import sign_in


class Flaky:
    """A sender that fails with `errors` (in order) before it works."""
    def __init__(self, *errors):
        self.errors, self.calls = list(errors), 0

    def __call__(self, settings, msg):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def test_temporary_failures_are_retried_with_growing_waits(monkeypatch):
    monkeypatch.setattr(emailer, "RETRY_DELAYS", (3, 9))
    waits = []
    sender = Flaky(OSError("network down"), smtplib.SMTPServerDisconnected("bye"))
    assert emailer.send_with_retries(sender, None, None, sleep=waits.append) == 3
    assert waits == [3, 9]


def test_permanent_failures_are_not_retried():
    sender = Flaky(smtplib.SMTPAuthenticationError(535, b"bad password"))
    with pytest.raises(smtplib.SMTPAuthenticationError):
        emailer.send_with_retries(sender, None, None, sleep=lambda s: None)
    assert sender.calls == 1


def test_gives_up_after_three_tries():
    sender = Flaky(*[OSError("down")] * 5)
    with pytest.raises(OSError):
        emailer.send_with_retries(sender, None, None, sleep=lambda s: None)
    assert sender.calls == 3


def test_daily_backup_is_checked_and_copied(config, tmp_path):
    connect(config.db_path).close()
    second = tmp_path / "OneDrive" / "Micronet e-Invoice backups"
    made = backup.daily(config.with_(backup_copy_dir=second))
    assert made.exists() and (second / made.name).exists()
    backup.check(second / made.name)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises(backup.BackupError):
        backup.check(bad)


def test_backup_copy_folder_setting(monkeypatch):
    monkeypatch.setenv("OneDrive", r"C:\Users\x\OneDrive")
    monkeypatch.delenv("BACKUP_COPY_DIR", raising=False)
    assert _backup_copy_dir() == Path(r"C:\Users\x\OneDrive") / "Micronet e-Invoice backups"
    monkeypatch.setenv("BACKUP_COPY_DIR", "off")
    assert _backup_copy_dir() is None
    monkeypatch.setenv("BACKUP_COPY_DIR", r"D:\Backups")
    assert _backup_copy_dir() == Path(r"D:\Backups")


def test_startup_checks_warn_on_every_page(config):
    conn = connect(config.db_path)
    assert health.startup_checks(config, conn) == []
    conn.execute("DROP TRIGGER audit_log_final_u")   # someone edits the file by hand
    conn.execute("INSERT INTO audit_log (at, user, action, detail) VALUES ('x', 'x', 'x', 'x')")
    conn.execute("UPDATE audit_log SET hash = 'f00d' WHERE id = (SELECT MAX(id) FROM audit_log)")
    conn.close()
    c = sign_in(create_app(config))
    for url in ("/", "/books"):
        assert "Check needed:" in c.get(url).get_data(as_text=True)


def test_unexpected_errors_are_logged_with_a_reference(config):
    app = create_app(config)

    @app.get("/boom")
    def boom():
        raise RuntimeError("something broke")

    c = sign_in(app)
    r = c.get("/boom")
    page = r.get_data(as_text=True)
    assert r.status_code == 500 and "Something went wrong" in page
    ref = page.split("Reference <strong>")[1].split("<")[0]
    log = (config.data_dir / "logs" / "app.log").read_text(encoding="utf-8")
    assert f"error {ref} on GET /boom" in log and "RuntimeError: something broke" in log


def test_second_copy_is_made_even_if_todays_backup_exists(config, tmp_path):
    connect(config.db_path).close()
    backup.daily(config)                                  # earlier today, no second folder yet
    second = tmp_path / "second"
    assert backup.daily(config.with_(backup_copy_dir=second)) is not None
    [copy] = second.glob("einvoice-*.db")
    backup.check(copy)
    assert not list(second.glob("*-wal")) and backup.daily(config.with_(backup_copy_dir=second)) is None

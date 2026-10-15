from datetime import date
from decimal import Decimal

import pytest

from einvoice.config import Seller, Config
from einvoice.models import Invoice, InvoiceLine
from einvoice.storage.db import connect
from einvoice.storage.repository import Repository


@pytest.fixture
def config(tmp_path):
    return Config(eis_cert_no="__PLACEHOLDER_EIS_CERT_NO__",
                  ptu_num="__PLACEHOLDER_PTU_NUMBER__",
                  control_no_start=1773, data_dir=tmp_path, seller=Seller())


@pytest.fixture
def repo(config):
    conn = connect(config.db_path)
    yield Repository(conn, config.control_no_start)
    conn.close()


def make_invoice(**overrides) -> Invoice:
    """The required test case: invoice 0001773 (rate is an example only)."""
    fields = dict(
        issue_date=date(2024, 11, 25),
        buyer_name="MICRONET CO. LTD.",
        buyer_tin="000000000",
        buyer_branch="00000",
        buyer_address="SAPPORO, JAPAN",
        business_style="Graphic Design",
        rate=Decimal("0.38"),
        rate_date=date(2024, 11, 25),
        rate_source="Example rate for testing",
        lines=[InvoiceLine("IT Services", "IT services rendered for Nov 2024",
                           Decimal("1"), "LOT", Decimal("12000000"))],
    )
    fields.update(overrides)
    return Invoice(**fields).compute()


def sign_in(app, username="admin", role="admin", password="Passw0rd-test"):
    """A test client signed in as `username` (created with `role` if new)."""
    from einvoice import auth
    cfg = app.config["EINVOICE"]
    conn = connect(cfg.db_path)
    try:
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            auth.create_user(conn, username=username, full_name=f"Test {username}", role=role,
                             password=password, must_change=False, by="test")
    finally:
        conn.close()
    client = app.test_client()
    r = client.post("/login", data={"username": username, "password": password})
    assert r.status_code == 302, r.get_data(as_text=True)
    return client

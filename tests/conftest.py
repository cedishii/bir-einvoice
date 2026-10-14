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

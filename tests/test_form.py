from datetime import timedelta
from decimal import Decimal as D

import pytest

from einvoice.models import FormErrors, invoice_from_form, parse_tin, today_ph


def form(**overrides):
    data = {
        "issue_date": "2024-11-25",
        "buyer_name": "MICRONET CO., LTD.",
        "buyer_tin": "",
        "buyer_address": "SAPPORO, JAPAN",
        "business_style": "Graphic Design",
        "rate": "0.38",
        "rate_date": "2024-11-25",
        "rate_source": "Example rate",
        "item_name[]": ["IT Services", "IT Services"],
        "description[]": ["IT services rendered for Nov 2024", ""],
        "qty[]": ["1", "1"],
        "unit[]": ["LOT", "LOT"],
        "unit_cost_jpy[]": ["12,000,000", ""],
    }
    data.update(overrides)
    return data


def test_test_case_from_form():
    inv = invoice_from_form(form())
    assert len(inv.lines) == 1          # blank second row ignored
    assert inv.buyer_tin == "000000000" and inv.buyer_branch == "00000"
    assert inv.total_jpy == D("12000000.00")
    assert inv.total_php == D("4560000.00")


def test_errors_are_reported_per_field():
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(buyer_name=" ", rate="abc", rate_source=""))
    assert set(e.value.errors) == {"buyer_name", "rate", "rate_source"}


def test_future_date_rejected():
    tomorrow = (today_ph() + timedelta(days=1)).isoformat()
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(issue_date=tomorrow))
    assert "issue_date" in e.value.errors


def test_no_lines_rejected():
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(**{"description[]": ["", ""], "unit_cost_jpy[]": ["", ""]}))
    assert "lines" in e.value.errors


def test_line_problems_reported():
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(**{"description[]": ["x" * 101, "Support"],
                                  "unit_cost_jpy[]": ["100", "-1"]}))
    assert "100 characters" in e.value.errors["line1"]
    assert "greater than zero" in e.value.errors["line2"]


def test_length_limits_match_eis():
    with pytest.raises(FormErrors) as e:
        invoice_from_form(form(buyer_name="A" * 201, buyer_address="B" * 301))
    assert set(e.value.errors) == {"buyer_name", "buyer_address"}


@pytest.mark.parametrize("text, expected", [
    ("", ("000000000", "00000")),
    ("123-456-789", ("123456789", "00000")),
    ("123-456-789-101", ("123456789", "00101")),
    ("123 456 789 00101", ("123456789", "00101")),
])
def test_parse_tin(text, expected):
    assert parse_tin(text) == expected


@pytest.mark.parametrize("text", ["12345678", "1234567890", "ABC-DEF-GHI", "123-456-789-1"])
def test_parse_tin_rejects(text):
    with pytest.raises(ValueError):
        parse_tin(text)

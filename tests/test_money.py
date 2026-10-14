from decimal import Decimal as D

import pytest

from einvoice import money
from einvoice.money import MoneyError
from tests.conftest import make_invoice
from einvoice.models import InvoiceLine


def test_test_case_12m_yen_at_038():
    assert money.to_php(D("12000000"), D("0.38")) == D("4560000.00")


def test_rounds_half_up_to_centavos():
    assert money.to_php(D("1"), D("0.385")) == D("0.39")    # 0.385 -> 0.39
    assert money.to_php(D("1"), D("0.384999")) == D("0.38")
    assert money.to_php(D("3"), D("0.123456")) == D("0.37")  # 0.370368


def test_full_precision_rate_is_kept():
    # 0.38 vs 0.3842 on 12M yen is a PHP 50,400 difference: the rate must not be cut.
    assert money.to_php(D("12000000"), D("0.3842")) == D("4610400.00")


def test_no_float_artifacts():
    # 0.1 + 0.2 style errors must not happen.
    assert money.to_php(D("100.10"), D("0.3")) == D("30.03")


def test_line_amounts_keep_salesamt_equal_qty_times_unitcost():
    a = money.line_amounts(D("3"), D("1000"), D("0.3842"))
    assert a["unit_cost_php"] == D("384.20")
    assert a["sales_php"] == D("1152.60") == a["unit_cost_php"] * 3
    assert a["amount_jpy"] == D("3000.00")


def test_fractional_quantity():
    a = money.line_amounts(D("1.5"), D("333.33"), D("0.38"))
    assert a["unit_cost_php"] == D("126.67")       # 126.6654
    assert a["sales_php"] == D("190.01")           # 190.005 -> half up
    assert a["amount_jpy"] == D("500.00")          # 499.995 -> half up


def test_invoice_totals_sum_lines():
    inv = make_invoice(lines=[
        InvoiceLine("IT Services", "Development, Nov 2024", D("1"), "LOT", D("8000000")),
        InvoiceLine("IT Services", "Maintenance, Nov 2024", D("2"), "MONTH", D("2000000")),
    ], rate=D("0.3842"))
    assert inv.total_jpy == D("12000000.00")
    assert inv.total_php == D("3073600.00") + D("1536800.00")


@pytest.mark.parametrize("text, expected", [
    ("12,000,000", D("12000000")),
    (" 0.3842 ", D("0.3842")),
    ("1 000", D("1000")),
])
def test_parse_accepts_common_input(text, expected):
    assert money.parse_decimal(text, field="x", max_decimals=6) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "NaN", "Infinity"])
def test_parse_rejects_bad_amounts(text):
    with pytest.raises(MoneyError):
        money.parse_jpy(text)


def test_rate_decimal_limits():
    assert money.parse_rate("0.384215") == D("0.384215")
    with pytest.raises(MoneyError):
        money.parse_rate("0.3842151")
    with pytest.raises(MoneyError):
        money.parse_jpy("100.123")


def test_formatting():
    assert money.fmt(D("4560000")) == "4,560,000.00"
    assert money.fmt_rate(D("0.38")) == "0.38"
    assert money.fmt_rate(D("0.3842")) == "0.3842"
    assert money.fmt_rate(D("1")) == "1.00"


@pytest.mark.parametrize("value, currency, words", [
    (D("12000000"), "YEN", "TWELVE MILLION YEN ONLY"),
    (D("4560000.00"), "PESOS", "FOUR MILLION FIVE HUNDRED SIXTY THOUSAND PESOS ONLY"),
    (D("1234.56"), "PESOS",
     "ONE THOUSAND TWO HUNDRED THIRTY-FOUR AND 56/100 PESOS ONLY"),
    (D("1000001"), "YEN", "ONE MILLION ONE YEN ONLY"),
    (D("0.5"), "PESOS", "ZERO AND 50/100 PESOS ONLY"),
])
def test_amount_in_words(value, currency, words):
    assert money.amount_in_words(value, currency) == words

"""Money math. Everything is Decimal; floats never touch an amount.

Rules (docs/EIS_SPEC_NOTES.md, section 6 decision 2, and PLAN.md):
- PHP amounts are rounded to 2 decimals, half up (0.005 -> 0.01).
- The exchange rate is kept as entered (up to RATE_MAX_DECIMALS decimals).
- Each line is converted per unit, then multiplied by the quantity, so the
  EIS rule SalesAmt = Qty x UnitCost always holds in PHP.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
RATE_MAX_DECIMALS = 6
AMOUNT_MAX_DECIMALS = 2
QTY_MAX_DECIMALS = 2


class MoneyError(ValueError):
    pass


def round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimals_of(value: Decimal) -> int:
    exp = value.normalize().as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def parse_decimal(text: str | int | Decimal, *, field: str, max_decimals: int,
                  positive: bool = True) -> Decimal:
    """Parse user input such as "12,000,000" or "0.3842"."""
    if isinstance(text, Decimal):
        value = text
    else:
        cleaned = str(text).strip().replace(",", "").replace(" ", "")
        if not cleaned:
            raise MoneyError(f"{field} is required.")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise MoneyError(f"{field} must be a number.") from None
    if not value.is_finite():
        raise MoneyError(f"{field} must be a number.")
    if positive and value <= 0:
        raise MoneyError(f"{field} must be greater than zero.")
    if decimals_of(value) > max_decimals:
        raise MoneyError(f"{field} allows at most {max_decimals} decimal places.")
    return value


def parse_rate(text) -> Decimal:
    return parse_decimal(text, field="Exchange rate", max_decimals=RATE_MAX_DECIMALS)


def parse_jpy(text, field: str = "JPY amount") -> Decimal:
    return parse_decimal(text, field=field, max_decimals=AMOUNT_MAX_DECIMALS)


def parse_qty(text) -> Decimal:
    return parse_decimal(text, field="Quantity", max_decimals=QTY_MAX_DECIMALS)


def to_php(jpy: Decimal, rate: Decimal) -> Decimal:
    return round2(jpy * rate)


def line_amounts(qty: Decimal, unit_cost_jpy: Decimal, rate: Decimal) -> dict:
    """JPY and PHP amounts for one invoice line."""
    unit_cost_php = to_php(unit_cost_jpy, rate)
    return {
        "amount_jpy": round2(qty * unit_cost_jpy),
        "unit_cost_php": unit_cost_php,
        "sales_php": round2(qty * unit_cost_php),
    }


def fmt(value: Decimal, places: int = 2) -> str:
    """1234567.5 -> '1,234,567.50'."""
    return f"{value:,.{places}f}"


def fmt_rate(rate: Decimal) -> str:
    """Rate as entered, with at least 2 decimals: 0.38 -> '0.38', 0.3842 -> '0.3842'."""
    places = max(2, decimals_of(rate))
    return f"{rate:.{places}f}"


# --- Amount in words (printed as "the sum of ...") --------------------------

_ONES = ["", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT",
         "NINE", "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN",
         "SIXTEEN", "SEVENTEEN", "EIGHTEEN", "NINETEEN"]
_TENS = ["", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY",
         "EIGHTY", "NINETY"]
_SCALES = ["", "THOUSAND", "MILLION", "BILLION", "TRILLION"]


def _under_1000(n: int) -> str:
    words = []
    if n >= 100:
        words += [_ONES[n // 100], "HUNDRED"]
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        words.append(_ONES[n])
    return " ".join(words)


def int_to_words(n: int) -> str:
    if n == 0:
        return "ZERO"
    if n < 0:
        return "MINUS " + int_to_words(-n)
    parts, scale = [], 0
    while n:
        n, chunk = divmod(n, 1000)
        if chunk:
            parts.append(_under_1000(chunk) + (f" {_SCALES[scale]}" if scale else ""))
        scale += 1
    return " ".join(reversed(parts))


def amount_in_words(value: Decimal, currency_word: str) -> str:
    """12000000 -> 'TWELVE MILLION YEN ONLY'; 1500.25 -> '... AND 25/100 PESOS ONLY'."""
    value = round2(value)
    whole = int(value)
    cents = int((value - whole) * 100)
    text = f"{int_to_words(whole)} {currency_word}"
    if cents:
        text = f"{int_to_words(whole)} AND {cents:02d}/100 {currency_word}"
    return text + " ONLY"


def fmt_qty(qty: Decimal) -> str:
    """1 -> '1', 1.50 -> '1.5', 1000 -> '1,000'."""
    return f"{int(qty):,}" if qty == qty.to_integral_value() else f"{qty.normalize():f}"

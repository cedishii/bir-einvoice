"""JSON writer that controls how numbers look.

Python's json module would print Decimal("4560000.00") as 4560000.0 or fail.
Here amounts are always printed with exactly 2 decimals (4560000.00), Qty
without needless decimals (1, 1.5) and ConvRate as entered (0.38, 0.3842),
matching the draft in docs/EIS_SPEC_NOTES.md section 5.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from ..money import fmt_rate, round2


@dataclass(frozen=True)
class Num:
    """A JSON number with a fixed textual form."""
    text: str

    @classmethod
    def amount(cls, value: Decimal) -> "Num":
        return cls(f"{round2(value):.2f}")

    @classmethod
    def qty(cls, value: Decimal) -> "Num":
        return cls(f"{int(value)}" if value == value.to_integral_value()
                   else f"{value.normalize():f}")

    @classmethod
    def rate(cls, value: Decimal) -> "Num":
        return cls(fmt_rate(value))


def dumps(value, indent: int = 2) -> str:
    return _dump(value, indent, 0) + "\n"


def _dump(value, indent: int, level: int) -> str:
    pad, inner = " " * (indent * level), " " * (indent * (level + 1))
    if isinstance(value, Num):
        return value.text
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{json.dumps(k, ensure_ascii=False)}: {_dump(v, indent, level + 1)}"
                 for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [inner + _dump(v, indent, level + 1) for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    if value is None or isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"Unsupported value in e-invoice JSON: {value!r} ({type(value).__name__})")


def loads(text: str):
    """Parse e-invoice JSON keeping every number as an exact Decimal."""
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)

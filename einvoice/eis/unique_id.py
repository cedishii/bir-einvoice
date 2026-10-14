"""EisUniqueId = IssueDate YYYYMMDD (8) + EIS Cert Number (8) + key (8, A-Z 0-9)."""
from __future__ import annotations

import re
from datetime import date

LENGTH = 24
DATE = slice(0, 8)
CERT = slice(8, 16)
KEY = slice(16, 24)

_CERT_RE = re.compile(r"^\d{2}[A-Za-z0-9]{6}$")
_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")


def is_valid_cert_no(cert_no: str) -> bool:
    """2-digit source code + 6 characters."""
    return bool(_CERT_RE.match(cert_no or ""))


def is_valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def build(issue_date: date, cert_no: str, key: str) -> str:
    if not is_valid_cert_no(cert_no):
        raise ValueError(f"EIS Cert Number must be 8 characters (2 digits + 6), got {cert_no!r}")
    if not is_valid_key(key):
        raise ValueError(f"EisUniqueId key must be 8 characters A-Z/0-9, got {key!r}")
    return issue_date.strftime("%Y%m%d") + cert_no + key

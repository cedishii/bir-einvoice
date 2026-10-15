"""Chart of accounts.

The code holds only a generic starter chart. The company's own chart is a
local file, `chart_of_accounts.csv` in the data folder (kept out of git),
with the columns code, name, type, section, flags. When the file is there
(or changes), it is applied on top: its accounts are added or updated, and
starter accounts it doesn't list are deactivated. The change is recorded
in the history.

Codes: 1xxxx assets, 2xxxx liabilities, 3xxxx equity, 4xxxx income,
5xxxx cost of services, 6xxxx expenses.

Flags (space-separated):
  cash         selectable as the cash / bank account of a collection or payment
  role:<name>  used by the automatic postings (see postings.py): bank, ar, cwt,
               input_vat, ap, output_vat, ewt_payable, retained_earnings,
               sales, fx_gain, fx_loss
  cost         cost of services (direct cost): gross income for income tax
  tax_exclude  left out of the income tax worksheets (provisions for income
               tax, penalties, unrealized forex, interest income under final tax)
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

from . import ledger

SECTIONS = {
    "asset": ["Current assets", "Non-current assets"],
    "liability": ["Current liabilities", "Non-current liabilities"],
    "equity": ["Equity"],
    "income": ["Revenue", "Other income"],
    "expense": ["Cost of services", "Operating expenses", "Other expenses", "Income tax"],
}
STATEMENT_ORDER = [s for group in SECTIONS.values() for s in group]
ROLES = ("bank", "ar", "cwt", "input_vat", "ap", "output_vat", "ewt_payable",
         "retained_earnings", "sales", "fx_gain", "fx_loss")
LOCAL_FILE = "chart_of_accounts.csv"


def default_section(account_type: str) -> str:
    return SECTIONS[account_type][0 if account_type != "expense" else 1]


# (code, name, type, section, flags)
STARTER_CHART = [
    ("11010", "Cash on hand", "asset", "Current assets", "cash"),
    ("11040", "Cash in bank", "asset", "Current assets", "cash role:bank"),
    ("11110", "Accounts receivable - trade", "asset", "Current assets", "role:ar"),
    ("11140", "Creditable withholding tax", "asset", "Current assets", "role:cwt"),
    ("11310", "Prepayments", "asset", "Current assets", ""),
    ("11410", "Input VAT", "asset", "Current assets", "role:input_vat"),
    ("12120", "Office equipment", "asset", "Non-current assets", ""),
    ("12220", "Accumulated depreciation - office equipment", "asset", "Non-current assets", ""),
    ("21010", "Accounts payable - trade", "liability", "Current liabilities", "role:ap"),
    ("21210", "Output VAT payable", "liability", "Current liabilities", "role:output_vat"),
    ("21240", "Withholding tax payable - compensation", "liability", "Current liabilities", ""),
    ("21250", "Expanded withholding tax payable", "liability", "Current liabilities",
     "role:ewt_payable"),
    ("21270", "Income tax payable", "liability", "Current liabilities", ""),
    ("21510", "Loans payable", "liability", "Current liabilities", ""),
    ("22020", "Retirement benefit obligation", "liability", "Non-current liabilities", ""),
    ("31010", "Capital stock", "equity", "Equity", ""),
    ("31030", "Retained earnings", "equity", "Equity", "role:retained_earnings"),
    ("41010", "Service income", "income", "Revenue", "role:sales"),
    ("42010", "Interest income", "income", "Other income", "tax_exclude"),
    ("42020", "Other income", "income", "Other income", ""),
    ("42030", "Realized forex gain", "income", "Other income", "role:fx_gain"),
    ("51010", "Salaries - direct", "expense", "Cost of services", "cost"),
    ("51090", "Other direct cost", "expense", "Cost of services", "cost"),
    ("61150", "Interest and bank charges", "expense", "Operating expenses", ""),
    ("61160", "Light and water", "expense", "Operating expenses", ""),
    ("61190", "Miscellaneous", "expense", "Operating expenses", ""),
    ("61200", "Office supplies", "expense", "Operating expenses", ""),
    ("61230", "Postage, telephone and communications", "expense", "Operating expenses", ""),
    ("61250", "Professional fees", "expense", "Operating expenses", ""),
    ("61270", "Rent", "expense", "Operating expenses", ""),
    ("61320", "Salaries - administrative", "expense", "Operating expenses", ""),
    ("61370", "Taxes and licenses", "expense", "Operating expenses", ""),
    ("61380", "Transportation and travel", "expense", "Operating expenses", ""),
    ("62020", "Penalties and surcharges", "expense", "Other expenses", "tax_exclude"),
    ("62030", "Realized forex loss", "expense", "Other expenses", "role:fx_loss"),
    ("69010", "Provision for income tax", "expense", "Income tax", "tax_exclude"),
]


def _flags(flags: str) -> tuple[int, str | None, int, int]:
    f = flags.split()
    role = next((x[5:] for x in f if x.startswith("role:")), None)
    return int("cash" in f), role, int("cost" in f), int("tax_exclude" in f)


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def install_sql() -> str:
    """Migration 9: statement sections, the income-tax exclusion flag, and the
    starter chart in place of the first one. Old accounts already used in
    the books are kept, inactive; unused ones are removed."""
    rows = []
    for code, name, kind, section, flags in STARTER_CHART:
        cash, role, cost, excl = _flags(flags)
        rows.append(f"({_q(code)}, {_q(name)}, {_q(kind)}, {_q(section)}, {cash}, "
                    f"{_q(role) if role else 'NULL'}, {cost}, {excl})")
    return f"""
    ALTER TABLE accounts ADD COLUMN section TEXT NOT NULL DEFAULT '';
    ALTER TABLE accounts ADD COLUMN tax_exclude INTEGER NOT NULL DEFAULT 0;
    DROP TRIGGER accounts_no_delete;
    UPDATE accounts SET role = NULL, active = 0;
    DELETE FROM accounts
     WHERE code NOT IN (SELECT account_code FROM journal_lines)
       AND code NOT IN (SELECT account_code FROM bills)
       AND code NOT IN (SELECT cash_account FROM payments)
       AND code NOT IN (SELECT cash_account FROM collections);
    CREATE TRIGGER accounts_no_delete BEFORE DELETE ON accounts
    BEGIN SELECT RAISE(ABORT, 'Records are never deleted; deactivate the account instead.'); END;
    UPDATE accounts SET section = CASE type
        WHEN 'asset' THEN 'Current assets' WHEN 'liability' THEN 'Current liabilities'
        WHEN 'equity' THEN 'Equity' WHEN 'income' THEN 'Revenue'
        ELSE CASE WHEN is_cost = 1 THEN 'Cost of services' ELSE 'Operating expenses' END END;
    INSERT INTO accounts (code, name, type, section, is_cash, role, is_cost, tax_exclude)
    VALUES {", ".join(rows)};
    """


class ChartFileError(ValueError):
    pass


def read_file(path: Path) -> list[tuple[str, str, str, str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out, seen, roles = [], set(), set()
    for n, r in enumerate(rows, start=2):
        code, name = (r.get("code") or "").strip(), " ".join((r.get("name") or "").split())
        kind, section = (r.get("type") or "").strip(), (r.get("section") or "").strip()
        flags = (r.get("flags") or "").strip()
        where = f"{path.name} line {n}"
        if not (code.isdigit() and 3 <= len(code) <= 8) or code in seen:
            raise ChartFileError(f"{where}: missing, invalid or repeated code {code!r}.")
        if not name or kind not in SECTIONS or section not in SECTIONS[kind]:
            raise ChartFileError(f"{where}: name, type or section is missing or invalid.")
        role = _flags(flags)[1]
        if role is not None and (role not in ROLES or role in roles):
            raise ChartFileError(f"{where}: unknown or repeated role {role!r}.")
        seen.add(code)
        roles.add(role)
        out.append((code, name, kind, section, flags))
    missing = set(ROLES) - roles
    if missing:
        raise ChartFileError(f"{path.name}: no account has the role(s) {', '.join(sorted(missing))}.")
    return out


def apply_local(conn: sqlite3.Connection, path: Path) -> bool:
    """Apply the company's chart file if it is there and changed since it was
    last applied. Returns True when something was applied."""
    if not path.exists():
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    done = conn.execute("SELECT value FROM settings WHERE key = 'chart.file_sha256'").fetchone()
    if done and done["value"] == digest:
        return False
    rows = read_file(path)
    listed = {r[0] for r in rows}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE accounts SET role = NULL")
        for code, name, kind, section, flags in rows:
            cash, role, cost, excl = _flags(flags)
            existing = conn.execute("SELECT type FROM accounts WHERE code = ?", (code,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO accounts (code, name, type, section, is_cash, role, "
                             "is_cost, tax_exclude) VALUES (?,?,?,?,?,?,?,?)",
                             (code, name, kind, section, cash, role, cost, excl))
            elif existing["type"] != kind:
                raise ChartFileError(f"Account {code} is a {existing['type']} account in the "
                                     f"books; the file says {kind}.")
            else:
                conn.execute("UPDATE accounts SET name = ?, section = ?, is_cash = ?, role = ?, "
                             "is_cost = ?, tax_exclude = ?, active = 1 WHERE code = ?",
                             (name, section, cash, role, cost, excl, code))
        conn.execute(f"UPDATE accounts SET active = 0 WHERE code NOT IN "
                     f"({', '.join('?' * len(listed))})", sorted(listed))
        conn.execute("INSERT INTO settings (key, value) VALUES ('chart.file_sha256', ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (digest,))
        ledger.audit(conn, "chart of accounts", f"applied {path.name} ({len(rows)} accounts)",
                     "system (chart file)", entity="account")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True

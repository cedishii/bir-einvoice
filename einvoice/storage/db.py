"""SQLite database: one file, schema upgraded in place with PRAGMA user_version.

Amounts and rates are stored as TEXT so Decimal values round-trip exactly.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS = [
    # 1: invoices, lines and the control-number counter
    """
    CREATE TABLE invoices (
        id             INTEGER PRIMARY KEY,
        control_no     TEXT NOT NULL UNIQUE,
        issue_date     TEXT NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('issued', 'void')),
        buyer_name     TEXT NOT NULL,
        buyer_tin      TEXT NOT NULL,
        buyer_branch   TEXT NOT NULL,
        buyer_address  TEXT NOT NULL,
        business_style TEXT NOT NULL,
        rate           TEXT NOT NULL,
        rate_date      TEXT NOT NULL,
        rate_source    TEXT NOT NULL,
        total_jpy      TEXT NOT NULL,
        total_php      TEXT NOT NULL,
        eis_key        TEXT NOT NULL,
        void_reason    TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        voided_at      TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE invoice_lines (
        invoice_id     INTEGER NOT NULL REFERENCES invoices(id),
        line_no        INTEGER NOT NULL,
        item_name      TEXT NOT NULL,
        description    TEXT NOT NULL,
        qty            TEXT NOT NULL,
        unit           TEXT NOT NULL,
        unit_cost_jpy  TEXT NOT NULL,
        amount_jpy     TEXT NOT NULL,
        unit_cost_php  TEXT NOT NULL,
        sales_php      TEXT NOT NULL,
        PRIMARY KEY (invoice_id, line_no)
    );
    -- One row: the last control number handed out. Numbers only go up.
    CREATE TABLE control_counter (
        id       INTEGER PRIMARY KEY CHECK (id = 1),
        last_no  INTEGER NOT NULL
    );
    """,
    # 2: real EIS exports. EisUniqueId is unique, so it can never be reused (SYN003).
    """
    CREATE TABLE eis_exports (
        eis_unique_id  TEXT PRIMARY KEY,
        invoice_id     INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
        exported_at    TEXT NOT NULL,
        file_name      TEXT NOT NULL,
        sha256         TEXT NOT NULL
    );
    """,
    # 3: submissions to BIR (phase 3) and the result per e-invoice
    """
    CREATE TABLE eis_submissions (
        submit_id       TEXT PRIMARY KEY,
        sent_at         TEXT NOT NULL,
        base_url        TEXT NOT NULL,
        ack_id          TEXT NOT NULL DEFAULT '',
        response_dtm    TEXT NOT NULL DEFAULT '',
        process_status  TEXT NOT NULL DEFAULT '',
        fail_reason     TEXT NOT NULL DEFAULT '',
        last_checked_at TEXT NOT NULL DEFAULT '',
        error           TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE eis_submission_items (
        submit_id      TEXT NOT NULL REFERENCES eis_submissions(submit_id),
        eis_unique_id  TEXT NOT NULL REFERENCES eis_exports(eis_unique_id),
        result_code    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (submit_id, eis_unique_id)
    );
    """,
    # 4: client e-mail and the e-mails sent with invoices
    """
    ALTER TABLE invoices ADD COLUMN buyer_email TEXT NOT NULL DEFAULT '';
    CREATE TABLE invoice_emails (
        id          INTEGER PRIMARY KEY,
        invoice_id  INTEGER NOT NULL REFERENCES invoices(id),
        sent_at     TEXT NOT NULL,
        to_addr     TEXT NOT NULL,
        cc          TEXT NOT NULL DEFAULT '',
        subject     TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
        error       TEXT NOT NULL DEFAULT '',
        test        INTEGER NOT NULL DEFAULT 0,
        automatic   INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 5: sale type (EIS TransClass). Invoices before this were all zero-rated.
    """
    ALTER TABLE invoices ADD COLUMN trans_class TEXT NOT NULL DEFAULT '02';
    """,
    # 6: books of accounts (phase 4). Posted journal entries are final: the
    # triggers refuse any change, so a mistake is fixed with a reversing entry.
    """
    CREATE TABLE accounts (
        code     TEXT PRIMARY KEY,
        name     TEXT NOT NULL,
        type     TEXT NOT NULL CHECK (type IN ('asset', 'liability', 'equity',
                                               'income', 'expense')),
        is_cash  INTEGER NOT NULL DEFAULT 0,
        role     TEXT UNIQUE,           -- used by the automatic postings
        active   INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO accounts (code, name, type, is_cash, role) VALUES
        ('1010', 'Cash on Hand', 'asset', 1, NULL),
        ('1020', 'Cash in Bank', 'asset', 1, NULL),
        ('1100', 'Accounts Receivable', 'asset', 0, 'ar'),
        ('1200', 'Input VAT', 'asset', 0, 'input_vat'),
        ('1210', 'Creditable Withholding Tax', 'asset', 0, 'cwt'),
        ('1500', 'Office Equipment', 'asset', 0, NULL),
        ('2000', 'Accounts Payable', 'liability', 0, 'ap'),
        ('2100', 'Output VAT', 'liability', 0, 'output_vat'),
        ('2200', 'Withholding Tax Payable - Expanded', 'liability', 0, 'ewt_payable'),
        ('2210', 'Withholding Tax Payable - Compensation', 'liability', 0, NULL),
        ('3000', 'Share Capital', 'equity', 0, NULL),
        ('3100', 'Retained Earnings', 'equity', 0, 'retained_earnings'),
        ('4000', 'Service Income', 'income', 0, 'sales'),
        ('4100', 'Foreign Exchange Gain', 'income', 0, 'fx_gain'),
        ('5000', 'Salaries and Wages', 'expense', 0, NULL),
        ('5100', 'Rent Expense', 'expense', 0, NULL),
        ('5200', 'Utilities Expense', 'expense', 0, NULL),
        ('5300', 'Professional Fees', 'expense', 0, NULL),
        ('5400', 'Communication Expense', 'expense', 0, NULL),
        ('5500', 'Office Supplies Expense', 'expense', 0, NULL),
        ('5600', 'Transportation and Travel', 'expense', 0, NULL),
        ('5700', 'Taxes and Licenses', 'expense', 0, NULL),
        ('5800', 'Bank Charges', 'expense', 0, NULL),
        ('5900', 'Foreign Exchange Loss', 'expense', 0, 'fx_loss'),
        ('5990', 'Miscellaneous Expense', 'expense', 0, NULL);

    CREATE TABLE customers (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL,
        tin            TEXT NOT NULL,
        branch         TEXT NOT NULL,
        address        TEXT NOT NULL,
        business_style TEXT NOT NULL DEFAULT '',
        email          TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        UNIQUE (name, tin, branch)
    );
    ALTER TABLE invoices ADD COLUMN customer_id INTEGER REFERENCES customers(id);
    CREATE TABLE suppliers (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        tin         TEXT NOT NULL,
        branch      TEXT NOT NULL,
        address     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (name, tin, branch)
    );

    CREATE TABLE journal_entries (
        id           INTEGER PRIMARY KEY,
        book         TEXT NOT NULL CHECK (book IN ('SJ', 'PJ', 'CRJ', 'CDJ', 'GJ')),
        entry_no     INTEGER NOT NULL,
        entry_date   TEXT NOT NULL,
        reference    TEXT NOT NULL DEFAULT '',
        particulars  TEXT NOT NULL,
        party_type   TEXT NOT NULL DEFAULT '' CHECK (party_type IN ('', 'customer', 'supplier')),
        party_id     INTEGER,
        source_type  TEXT NOT NULL,
        source_id    INTEGER,
        created_at   TEXT NOT NULL,
        created_by   TEXT NOT NULL,
        prev_hash    TEXT NOT NULL,
        hash         TEXT NOT NULL UNIQUE,
        UNIQUE (book, entry_no),
        UNIQUE (source_type, source_id)
    );
    CREATE INDEX journal_entries_date ON journal_entries (entry_date, id);
    CREATE TABLE journal_lines (
        entry_id      INTEGER NOT NULL REFERENCES journal_entries(id),
        line_no       INTEGER NOT NULL,
        account_code  TEXT NOT NULL REFERENCES accounts(code),
        debit         TEXT NOT NULL,
        credit        TEXT NOT NULL,
        memo          TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (entry_id, line_no)
    );
    CREATE INDEX journal_lines_account ON journal_lines (account_code);
    CREATE TRIGGER journal_entries_final_u BEFORE UPDATE ON journal_entries
    BEGIN SELECT RAISE(ABORT, 'Posted journal entries are final; post a reversing entry.'); END;
    CREATE TRIGGER journal_entries_final_d BEFORE DELETE ON journal_entries
    BEGIN SELECT RAISE(ABORT, 'Posted journal entries are final; post a reversing entry.'); END;
    CREATE TRIGGER journal_lines_final_u BEFORE UPDATE ON journal_lines
    BEGIN SELECT RAISE(ABORT, 'Posted journal entries are final; post a reversing entry.'); END;
    CREATE TRIGGER journal_lines_final_d BEFORE DELETE ON journal_lines
    BEGIN SELECT RAISE(ABORT, 'Posted journal entries are final; post a reversing entry.'); END;

    -- Source documents. Cancelling one posts a reversing entry.
    CREATE TABLE bills (
        id            INTEGER PRIMARY KEY,
        supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
        bill_date     TEXT NOT NULL,
        supplier_ref  TEXT NOT NULL,
        description   TEXT NOT NULL,
        account_code  TEXT NOT NULL REFERENCES accounts(code),
        category      TEXT NOT NULL CHECK (category IN ('services', 'capital', 'goods',
                                                        'zero', 'exempt')),
        net           TEXT NOT NULL,
        vat           TEXT NOT NULL,
        total         TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'cancelled')),
        cancel_reason TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL
    );
    CREATE TABLE payments (
        id            INTEGER PRIMARY KEY,
        supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
        bill_id       INTEGER REFERENCES bills(id),
        pay_date      TEXT NOT NULL,
        reference     TEXT NOT NULL,
        amount        TEXT NOT NULL,     -- settled in Accounts Payable
        atc           TEXT NOT NULL DEFAULT '',
        ewt_base      TEXT NOT NULL DEFAULT '0.00',  -- income payment (Form 2307)
        ewt_rate      TEXT NOT NULL DEFAULT '0',     -- percent
        ewt           TEXT NOT NULL,
        cash_account  TEXT NOT NULL REFERENCES accounts(code),
        status        TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'cancelled')),
        cancel_reason TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL
    );
    CREATE TABLE collections (
        id            INTEGER PRIMARY KEY,
        customer_id   INTEGER NOT NULL REFERENCES customers(id),
        invoice_id    INTEGER REFERENCES invoices(id),
        coll_date     TEXT NOT NULL,
        reference     TEXT NOT NULL,
        applied       TEXT NOT NULL,     -- settled in Accounts Receivable
        cash          TEXT NOT NULL,
        cwt           TEXT NOT NULL,
        atc           TEXT NOT NULL DEFAULT '',
        cwt_base      TEXT NOT NULL DEFAULT '0.00',  -- income payment (client's 2307)
        cwt_rate      TEXT NOT NULL DEFAULT '0',     -- percent
        cash_account  TEXT NOT NULL REFERENCES accounts(code),
        status        TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'cancelled')),
        cancel_reason TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL
    );

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    CREATE TABLE audit_log (
        id      INTEGER PRIMARY KEY,
        at      TEXT NOT NULL,
        user    TEXT NOT NULL,
        action  TEXT NOT NULL,
        detail  TEXT NOT NULL DEFAULT ''
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    from ..books import postings  # the books need the schema above
    postings.sync(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        # executescript, not split(";"): trigger bodies contain semicolons.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\n"
                               f"PRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

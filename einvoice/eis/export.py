"""Export an issued invoice as an EIS e-invoice JSON file.

Real export: refused while any validation issue exists (including BIR
placeholders in .env). Each invoice is exported once; its file is kept in
data/exports/ and recorded in eis_exports so its EisUniqueId is never reused.

Test export: same JSON with the dummy TEST values from config, written to
data/exports/test/TEST-*.json and never recorded, so it can be repeated.

Phase 3 (signing, encryption, sending to BIR) would take the file written
here as its input; see einvoice/eis/transport/README.md.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..config import TEST_EIS_CERT_NO, TEST_PTU_NUM, Config
from ..models import PH_TZ, Invoice
from . import builder
from .validator import Context, Issue, validate_text


class ExportRefused(Exception):
    def __init__(self, issues: list[Issue]):
        super().__init__("\n".join(map(str, issues)))
        self.issues = issues


@dataclass
class ExportResult:
    eis_unique_id: str
    path: Path
    json_text: str
    test: bool
    exported_at: str
    already_exported: bool = False


def exports_dir(cfg: Config, test: bool) -> Path:
    return cfg.data_dir / "exports" / ("test" if test else "")


def test_config(cfg: Config) -> Config:
    return cfg.with_(eis_cert_no=TEST_EIS_CERT_NO, ptu_num=TEST_PTU_NUM)


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT eis_unique_id FROM eis_exports")}


def existing_export(conn: sqlite3.Connection, cfg: Config, inv: Invoice) -> ExportResult | None:
    row = conn.execute("SELECT * FROM eis_exports WHERE invoice_id = ?", (inv.id,)).fetchone()
    if row is None:
        return None
    path = exports_dir(cfg, test=False) / row["file_name"]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return ExportResult(row["eis_unique_id"], path, text, False, row["exported_at"],
                        already_exported=True)


def check(inv: Invoice, cfg: Config, conn: sqlite3.Connection, *, test: bool) -> tuple[str, list[Issue]]:
    """Build the JSON and validate it. Returns (json_text, issues)."""
    if inv.status != "issued":
        return "", [Issue("LOCAL-STATUS", "", "A voided invoice cannot be exported.")]
    use = test_config(cfg) if test else cfg
    ctx = Context(eis_cert_no=use.eis_cert_no, known_ids=known_ids(conn), test_mode=test)
    if not test and cfg.is_prototype:
        # The EisUniqueId can't even be formed without the real EIS Cert Number.
        return "", [Issue("LOCAL-CONFIG", "", f"{name} in .env is still a placeholder. "
                          "BIR issues it after EIS enrollment.")
                    for name in cfg.placeholders]
    text = builder.to_json(inv, use)
    return text, validate_text(text, ctx)


def precheck(draft: Invoice, cfg: Config, next_control_no: str) -> list[Issue]:
    """Before issuing: would this draft produce a valid e-invoice?

    Uses the test values for the BIR-issued settings so the check works in
    prototype mode too; only the invoice's own data is being checked.
    """
    trial = replace(draft, control_no=next_control_no, status="issued", eis_key="PRECHECK",
                    lines=list(draft.lines))
    use = test_config(cfg)
    ctx = Context(eis_cert_no=use.eis_cert_no, test_mode=True)
    return validate_text(builder.to_json(trial, use), ctx)


def export(inv: Invoice, cfg: Config, conn: sqlite3.Connection, *, test: bool = False) -> ExportResult:
    if not test:
        done = existing_export(conn, cfg, inv)
        if done:
            return done
    text, issues = check(inv, cfg, conn, test=test)
    if issues:
        raise ExportRefused(issues)

    eid = builder.eis_unique_id(inv, test_config(cfg) if test else cfg)
    now = datetime.now(PH_TZ).isoformat(timespec="seconds")
    name = f"TEST-{inv.control_no}-{eid}.json" if test else f"{inv.control_no}-{eid}.json"
    folder = exports_dir(cfg, test)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    data = text.encode("utf-8")

    if test:
        path.write_bytes(data)
        return ExportResult(eid, path, text, True, now)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO eis_exports (eis_unique_id, invoice_id, exported_at, file_name, sha256)"
            " VALUES (?,?,?,?,?)",
            (eid, inv.id, now, name, hashlib.sha256(data).hexdigest()))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ExportResult(eid, path, text, False, now)

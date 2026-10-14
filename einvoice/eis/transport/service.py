"""Glue between the saved exports and the EIS API: what to send, what was sent,
and what BIR answered.

States of an exported e-invoice:
  not sent    -> no submission item
  sent        -> in a submission whose result isn't in yet
  accepted    -> resultStatusCode SUC001
  rejected    -> any other resultStatusCode; it may be sent again once fixed
                 (an EisUniqueId already accepted is never sent again)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ...config import Config
from ...models import PH_TZ
from ..export import exports_dir
from . import results
from .client import MAX_INVOICES_PER_SUBMISSION, EisApiError, EisClient


@dataclass
class EisStatus:
    state: str               # not sent / sent / accepted / rejected
    submit_id: str = ""
    ack_id: str = ""
    result_code: str = ""
    sent_at: str = ""
    error: str = ""

    @property
    def result_text(self) -> str:
        return results.describe(self.result_code) if self.result_code else ""


def _now() -> str:
    return datetime.now(PH_TZ).isoformat(timespec="seconds")


def status_of(conn: sqlite3.Connection, eis_unique_id: str) -> EisStatus:
    row = conn.execute(
        """SELECT s.submit_id, s.ack_id, s.sent_at, s.process_status, s.fail_reason,
                  s.error, i.result_code
           FROM eis_submission_items i JOIN eis_submissions s USING (submit_id)
           WHERE i.eis_unique_id = ?
           ORDER BY (i.result_code = 'SUC001') DESC, s.sent_at DESC LIMIT 1""",
        (eis_unique_id,)).fetchone()
    if row is None:
        return EisStatus("not sent")
    if row["result_code"] == results.SUCCESS:
        state = "accepted"
    elif row["result_code"] or row["process_status"] == results.PROCESS_UNABLE or row["error"]:
        state = "rejected"
    else:
        state = "sent"
    error = row["error"]
    if row["fail_reason"]:
        error = f"{row['fail_reason']}: {results.describe(row['fail_reason'])}"
    return EisStatus(state, row["submit_id"], row["ack_id"], row["result_code"],
                     row["sent_at"], error)


def pending_exports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Exported e-invoices that are not sent yet or were rejected."""
    rows = conn.execute(
        """SELECT e.* FROM eis_exports e
           JOIN invoices inv ON inv.id = e.invoice_id
           ORDER BY inv.control_no""").fetchall()
    return [r for r in rows if status_of(conn, r["eis_unique_id"]).state in ("not sent", "rejected")]


def send(conn: sqlite3.Connection, cfg: Config, client: EisClient,
         eis_unique_ids: list[str]) -> list[str]:
    """Send the given exported e-invoices in batches. Returns the submitIds."""
    rows = []
    for eid in eis_unique_ids:
        row = conn.execute("SELECT * FROM eis_exports WHERE eis_unique_id = ?", (eid,)).fetchone()
        if row is None:
            raise ValueError(f"{eid} has not been exported")
        state = status_of(conn, eid).state
        if state in ("sent", "accepted"):
            raise ValueError(f"{eid} is already {state}")
        rows.append(row)

    submit_ids = []
    folder = exports_dir(cfg, test=False)
    for start in range(0, len(rows), MAX_INVOICES_PER_SUBMISSION):
        batch = rows[start:start + MAX_INVOICES_PER_SUBMISSION]
        texts = [(folder / r["file_name"]).read_text(encoding="utf-8") for r in batch]
        submit_id = client.new_submit_id()
        # Record the attempt first, so even a crash mid-request leaves a trace.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO eis_submissions (submit_id, sent_at, base_url) VALUES (?,?,?)",
                     (submit_id, _now(), client.settings.base_url))
        conn.executemany("INSERT INTO eis_submission_items (submit_id, eis_unique_id) VALUES (?,?)",
                         [(submit_id, r["eis_unique_id"]) for r in batch])
        conn.execute("COMMIT")
        try:
            ack = client.submit(texts, submit_id)
        except (EisApiError, OSError) as e:
            conn.execute("UPDATE eis_submissions SET error = ? WHERE submit_id = ?",
                         (str(e), submit_id))
            raise
        conn.execute("UPDATE eis_submissions SET ack_id = ?, response_dtm = ? WHERE submit_id = ?",
                     (ack.ack_id, ack.response_dtm, submit_id))
        submit_ids.append(submit_id)
    return submit_ids


def open_submissions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT submit_id FROM eis_submissions
           WHERE error = '' AND process_status IN ('', ?) ORDER BY sent_at""",
        (results.PROCESS_IN_PROGRESS,)).fetchall()
    return [r["submit_id"] for r in rows]


def check_results(conn: sqlite3.Connection, client: EisClient,
                  submit_ids: list[str] | None = None) -> dict[str, str]:
    """Ask BIR for the results of open submissions. Returns submitId -> process status."""
    out = {}
    for submit_id in submit_ids or open_submissions(conn):
        try:
            res = client.inquire(submit_id)
        except EisApiError as e:
            if e.code == "E16":  # no result yet
                out[submit_id] = results.PROCESS_IN_PROGRESS
                conn.execute("UPDATE eis_submissions SET last_checked_at = ? WHERE submit_id = ?",
                             (_now(), submit_id))
                continue
            raise
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE eis_submissions SET process_status = ?, fail_reason = ?, last_checked_at = ?
               WHERE submit_id = ?""",
            (res.process_status, res.fail_reason, _now(), submit_id))
        items = [r["eis_unique_id"] for r in conn.execute(
            "SELECT eis_unique_id FROM eis_submission_items WHERE submit_id = ?", (submit_id,))]
        for doc in res.documents:
            # invoiceUid is taken to be the EisUniqueId (UNCONFIRMED). With a
            # single invoice in the submission there is no ambiguity either way.
            uid = doc.invoice_uid if doc.invoice_uid in items else (
                items[0] if len(items) == 1 and len(res.documents) == 1 else None)
            if uid:
                conn.execute(
                    """UPDATE eis_submission_items SET result_code = ?
                       WHERE submit_id = ? AND eis_unique_id = ?""",
                    (doc.result_code, submit_id, uid))
        conn.execute("COMMIT")
        out[submit_id] = res.process_status
    return out

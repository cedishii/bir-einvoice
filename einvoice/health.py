"""Checks run when the app starts, and the error log.

- The database passes SQLite's integrity check.
- The journal and the history (audit trail) chains are intact.
- Today's backup was made, checked, and copied to the second folder.

Problems are listed at the top of every page until the next start. Every
problem, and every unexpected error in a page, is written to
data/logs/app.log (kept to a few small files).
"""
from __future__ import annotations

import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import backup
from .config import Config

log = logging.getLogger("einvoice")


def setup_logging(cfg: Config) -> Path:
    """Write the app's log to data/logs/app.log (5 files of 1 MB at most)."""
    folder = cfg.data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "app.log"
    root = logging.getLogger("einvoice")
    for old in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        if old.baseFilename == str(path):
            return path
        root.removeHandler(old)  # set up again for another data folder
        old.close()
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


def startup_checks(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    """Problems found at start (empty when all is well)."""
    from .books import ledger

    problems: list[str] = []
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            problems.append(f"The database failed its integrity check ({result}). "
                            "Restore yesterday's backup and tell the developer.")
    except sqlite3.Error as e:
        problems.append(f"The database could not be checked: {e}")
    if ledger.verify(conn):
        problems.append("The journal check failed: entries were changed outside the app. "
                        "See Books & reports.")
    if ledger.verify_audit(conn):
        problems.append("The history check failed: rows were changed outside the app. "
                        "See Books & reports.")
    try:
        backup.daily(cfg)
    except (backup.BackupError, OSError) as e:
        problems.append(f"Today's automatic backup had a problem: {e}")
    for p in problems:
        log.warning("start-up check: %s", p)
    return problems

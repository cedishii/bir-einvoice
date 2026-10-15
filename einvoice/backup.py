"""Backups of the database: one automatic copy a day, and a download on
demand. SQLite's backup API copies a consistent snapshot even while the app
is running.

Each daily copy is checked (it must open and pass SQLite's integrity check)
and, when a second folder is set up (BACKUP_COPY_DIR, by default a folder in
OneDrive), also copied there, so a lost or broken laptop doesn't take the
books with it. The last KEEP_DAILY copies are kept in each place.
"""
from __future__ import annotations

import logging
import sqlite3
import tempfile
from pathlib import Path

from .config import Config
from .models import today_ph

KEEP_DAILY = 30
log = logging.getLogger("einvoice.backup")


class BackupError(Exception):
    pass


def copy_to(db_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        # One self-contained file (no -wal / -shm side files), safe to copy.
        out.execute("PRAGMA journal_mode = DELETE")
    finally:
        out.close()
        src.close()
    return dest


def check(path: Path) -> None:
    """Raise BackupError unless the copy opens and passes the integrity check."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            conn.execute("SELECT COUNT(*) FROM invoices").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise BackupError(f"backup {path.name} can't be read: {e}") from None
    if result != "ok":
        raise BackupError(f"backup {path.name} failed the integrity check: {result}")


def _prune(folder: Path) -> None:
    for old in sorted(folder.glob("einvoice-*.db"))[:-KEEP_DAILY]:
        old.unlink()


def daily(cfg: Config) -> Path | None:
    """Today's copy in data/backups (once a day), checked, and copied to the
    second folder if one is set up. Raises BackupError if the copy is bad."""
    if not cfg.db_path.exists():
        return None
    folder = cfg.data_dir / "backups"
    dest = folder / f"einvoice-{today_ph():%Y%m%d}.db"
    made = not dest.exists()
    if made:
        copy_to(cfg.db_path, dest)
        try:
            check(dest)
        except BackupError:
            dest.replace(dest.with_suffix(".bad"))  # the next start tries again
            raise
        _prune(folder)
        log.info("daily backup %s", dest)
    if cfg.backup_copy_dir is not None and not (cfg.backup_copy_dir / dest.name).exists():
        second = cfg.backup_copy_dir / dest.name
        try:
            copy_to(cfg.db_path, second)  # its own snapshot, checked like the first
            check(second)
            _prune(cfg.backup_copy_dir)
            log.info("backup copied to %s", second)
        except (OSError, sqlite3.Error) as e:
            raise BackupError(f"the backup couldn't be copied to {cfg.backup_copy_dir}: {e}") from None
        return dest
    return dest if made else None


def snapshot(cfg: Config) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        return copy_to(cfg.db_path, Path(tmp) / "snapshot.db").read_bytes()

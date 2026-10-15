"""Backups of the database: one automatic copy a day, and a download for the
administrator. SQLite's backup API copies a consistent snapshot even while
the app is running."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from .config import Config
from .models import today_ph

KEEP_DAILY = 30


def copy_to(db_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


def daily(cfg: Config) -> Path | None:
    """Today's copy in data/backups (once a day), keeping the last 30."""
    if not cfg.db_path.exists():
        return None
    folder = cfg.data_dir / "backups"
    dest = folder / f"einvoice-{today_ph():%Y%m%d}.db"
    if dest.exists():
        return None
    copy_to(cfg.db_path, dest)
    for old in sorted(folder.glob("einvoice-*.db"))[:-KEEP_DAILY]:
        old.unlink()
    return dest


def snapshot(cfg: Config) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        return copy_to(cfg.db_path, Path(tmp) / "snapshot.db").read_bytes()

"""Full-text index (SQLite FTS5) over the chunks table, kept in sync by triggers.

CocoIndex owns the `chunks` table; this module adds an external-content FTS5
index on top of it. Triggers mirror every INSERT/UPDATE/DELETE that CocoIndex
makes. `ensure_fts()` creates the index and triggers if missing (e.g. after
CocoIndex recreated the table) and rebuilds the index when it's out of sync.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3

from . import config as cfg

log = logging.getLogger("findling.fts")

FTS_TABLE = f"{cfg.TABLE_NAME}_fts"
# Column order matters: bm25() weights in searcher.py follow it.
FTS_COLUMNS = ("path", "headings", "text")
# trigram: substring matching ("frist" finds "Kündigungsfrist", "water_pl" finds
# "water_plants"); case-insensitive; ä/é/... match a/e/...
# remove_diacritics for trigram needs SQLite >= 3.45 (Python 3.12 from uv has 3.50).
TOKENIZER = "trigram remove_diacritics 1"

_cols = ", ".join(FTS_COLUMNS)
_new = ", ".join(f"new.{c}" for c in FTS_COLUMNS)
_old = ", ".join(f"old.{c}" for c in FTS_COLUMNS)
_T, _F = cfg.TABLE_NAME, FTS_TABLE

_SCHEMA = [
    f"""CREATE VIRTUAL TABLE IF NOT EXISTS {_F} USING fts5(
        {_cols}, content='{_T}', content_rowid='rowid', tokenize='{TOKENIZER}')""",
    f"""CREATE TRIGGER IF NOT EXISTS {_F}_ai AFTER INSERT ON {_T} BEGIN
        INSERT INTO {_F}(rowid, {_cols}) VALUES (new.rowid, {_new});
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS {_F}_ad AFTER DELETE ON {_T} BEGIN
        INSERT INTO {_F}({_F}, rowid, {_cols}) VALUES ('delete', old.rowid, {_old});
    END""",
    f"""CREATE TRIGGER IF NOT EXISTS {_F}_au AFTER UPDATE ON {_T} BEGIN
        INSERT INTO {_F}({_F}, rowid, {_cols}) VALUES ('delete', old.rowid, {_old});
        INSERT INTO {_F}(rowid, {_cols}) VALUES (new.rowid, {_new});
    END""",
]


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone() is not None


def _in_sync(db: sqlite3.Connection) -> bool:
    """Cheap check: same number of rows and same rowids (sum) in both. Both counts
    come from one snapshot, so an indexer commit in between can't fake a mismatch
    (a needless rebuild would block the indexer's writes)."""
    db.execute("BEGIN")
    try:
        chunks = db.execute(f"SELECT count(*), coalesce(sum(rowid), 0) FROM {_T}").fetchone()
        indexed = db.execute(
            f"SELECT count(*), coalesce(sum(id), 0) FROM {_F}_docsize"
        ).fetchone()
    finally:
        db.rollback()
    return tuple(chunks) == tuple(indexed)


def ensure_fts(db_path: pathlib.Path) -> None:
    """Create the FTS index + triggers if needed and rebuild it if out of sync.
    Safe to call any time; cheap when everything is in order."""
    if not db_path.exists():
        return
    db = sqlite3.connect(db_path, timeout=30)
    try:
        if not _table_exists(db, _T):
            return  # CocoIndex hasn't created the table yet
        triggers_missing = not all(
            _table_exists(db, f"{_F}_{suffix}") for suffix in ("ai", "ad", "au")
        )
        with db:
            for statement in _SCHEMA:
                db.execute(statement)
        if triggers_missing or not _in_sync(db):
            log.info("rebuilding full-text index")
            with db:
                db.execute(f"INSERT INTO {_F}({_F}) VALUES ('rebuild')")
    finally:
        db.close()

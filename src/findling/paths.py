"""Agents name files and folders by full path ("/Users/me/Recht/gesetze/BGB.html").
Turns such a path, in any case or Unicode form, into (indexed folder, path inside it)
as the index stores it, and into search scopes for `within`.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import unicodedata
from collections.abc import Sequence

from . import config as cfg
from .searcher import Scope


class AgentError(Exception):
    """A tool call that can't be done as asked; the message tells the agent why."""


def full(folder: str, path: str) -> str:
    return os.path.join(folder, path) if path else folder


def _key(part: str) -> str:
    # macOS file names may hold ü as u + accent (NFD); agents usually send NFC.
    return unicodedata.normalize("NFC", part).casefold()


def _keys(path: str) -> list[str]:
    return [_key(p) for p in pathlib.PurePath(path).parts]


def resolve(db: sqlite3.Connection, folders: Sequence[str], name: str) -> tuple[str, str]:
    """(indexed folder, path inside it as indexed; "" for the folder itself) for a
    full path to an indexed file or a folder holding indexed files. Case and Unicode
    form are ignored. AgentError if nothing indexed is there."""
    listing = ", ".join(folders) or "none"
    name = name.strip()
    if not name.startswith(("/", "~")):
        raise AgentError(f"{name!r} isn't a full path. Indexed folders: {listing}.")
    keys = _keys(os.path.expanduser(name))
    inside = [f for f in folders if _keys(f) == keys[: len(_keys(f))]]
    if not inside:
        raise AgentError(f"{name!r} isn't in an indexed folder. Indexed folders: {listing}.")
    folder = max(inside, key=lambda f: len(_keys(f)))
    rest = keys[len(_keys(folder)):]
    if not rest:
        return folder, ""
    rel = "/".join(pathlib.PurePath(os.path.expanduser(name)).parts[-len(rest):])
    exact = db.execute(
        f"SELECT 1 FROM {cfg.TABLE_NAME} WHERE folder = ? AND (path = ? OR substr(path, 1, ?) = ?) LIMIT 1",
        (folder, rel, len(rel) + 1, rel + "/"),
    ).fetchone()
    if exact:
        return folder, rel
    for (path,) in db.execute(f"SELECT DISTINCT path FROM {cfg.TABLE_NAME} WHERE folder = ?", (folder,)):
        parts = path.split("/")
        if [_key(p) for p in parts[: len(rest)]] == rest:
            return folder, "/".join(parts[: len(rest)])
    raise AgentError(f"Nothing is indexed at {name!r}. `browse` shows the indexed folders and files.")


def scope(folder: str, rel: str) -> Scope:
    """The search scope for an indexed folder, a folder inside one, or a file."""
    if not rel:
        return Scope(folder)
    return Scope(folder, "{path} = ? OR substr({path}, 1, ?) = ?", (rel, len(rel) + 1, rel + "/"))

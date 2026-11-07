"""Reading indexed files for AI agents: a stretch of text around a chunk or section
(`read`), and listings of folders, files and sections (`browse`).

Works on the index alone (no model), so it's ready while the search model loads.
Text comes from the indexed chunks, joined without the parts that overlap.

A position in a file is (chunk, offset into the chunk's text); agents see it as a
cursor "812" or "812:340". Overlapping chunks start with the end of the chunk before;
positions skip that repeated part, so no text is read twice.
"""

from __future__ import annotations

import datetime
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from . import config as cfg
from . import paths
from . import rag
from . import searcher
from .paths import AgentError

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReadResult(BaseModel):
    file: str
    type: str
    modified: str | None = None
    chunks: int = Field(description="Chunks in the file.")
    section: str | None = Field(None, description="Heading path at the starting point.")
    page: int | None = None
    url: str | None = Field(None, description="Shows the starting point highlighted in the user's browser.")
    text: str = Field(
        description="The text. Each new section starts with a Markdown link line, '[heading](url)': "
        "copy it to cite that section."
    )
    prev: str | None = Field(None, description="Where the text starts: pass as `at` with `before` to read further back. None at the file's start.")
    next: str | None = Field(None, description="Where the text stops: pass as `at` to read on. None at the file's end.")


class FolderEntry(BaseModel):
    path: str = Field(description="Use it (or a path inside it) in `within` and `browse`.")
    files: int
    available: bool = Field(description="False if the folder can't be read right now (search still works).")
    problem: str | None = None


class DirEntry(BaseModel):
    name: str
    files: int


class FileEntry(BaseModel):
    file: str
    type: str
    modified: str | None = None
    chunks: int


class SectionEntry(BaseModel):
    chunk: int
    section: str
    page: int | None = None


class BrowseResult(BaseModel):
    folders: list[FolderEntry] | None = Field(None, description="Without `path`: the indexed folders.")
    indexing: str | None = Field(None, description="Indexing phase, e.g. 'catching_up' (results may be incomplete) or 'up_to_date'.")
    search_ready: bool | None = Field(None, description="False while the search model is still loading.")
    path: str | None = None
    dirs: list[DirEntry] | None = None
    files: list[FileEntry] | None = None
    file: FileEntry | None = Field(None, description="For a file: the file itself; its sections follow.")
    sections: list[SectionEntry] | None = None
    total: int | None = Field(None, description="Entries in all (dirs + files, or sections).")
    next_offset: int | None = Field(None, description="Pass as `offset` to get the next entries.")


# (folder, path, chunk) -> URL showing that chunk highlighted
Link = Callable[[str, str, int], str | None]

# ---------------------------------------------------------------------------
# Chunks of a file
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    index: int
    headings: str
    page: int
    text: str


def open_index(db_path) -> sqlite3.Connection:
    if not db_path.exists():
        raise AgentError("Nothing is indexed yet. Add folders in Findling's web app.")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)


def load_chunks(db: sqlite3.Connection, folder: str, path: str) -> list[Chunk]:
    rows = db.execute(
        f"""SELECT chunk_index, headings, page, text FROM {cfg.TABLE_NAME}
            WHERE folder = ? AND path = ? ORDER BY chunk_index""",
        (folder, path),
    ).fetchall()
    return [Chunk(*row) for row in rows]


class Doc:
    """A file's chunks, and where each one's own text starts (after the overlap)."""

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.skip = [0] + [rag.overlap(a.text, b.text) for a, b in zip(chunks, chunks[1:])]
        self.position = {c.index: i for i, c in enumerate(chunks)}

    def __len__(self) -> int:
        return len(self.chunks)

    def pos(self, chunk: int) -> int:
        if chunk not in self.position:
            last = self.chunks[-1].index if self.chunks else -1
            raise AgentError(f"There's no chunk {chunk} in this file (chunks 0-{last}).")
        return self.position[chunk]

    def cursor(self, pos: int, off: int) -> str:
        """The cursor for a position: "812" at a chunk's start, else "812:340"."""
        return f"{self.chunks[pos].index}:{off}" if off else str(self.chunks[pos].index)

    def parse(self, at: str | int) -> tuple[int, int]:
        m = re.fullmatch(r"\s*(\d+)\s*(?::\s*(\d+)\s*)?", str(at))
        if not m:
            raise AgentError(f"`at` must be a chunk number like '812' or a cursor like '812:340', not {at!r}.")
        pos = self.pos(int(m.group(1)))
        off = int(m.group(2) or 0)
        return pos, min(off, len(self.chunks[pos].text))


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    """Comparable form of a heading: case and accents folded, "§1599" = "§ 1599"."""
    text = searcher.normalize(text)
    text = re.sub(r"(§+|art\.)\s*", r"\1 ", text)
    return " ".join(text.split())


def _runs(doc: Doc) -> list[tuple[int, str]]:
    """(position, heading path) where each run of chunks under one heading starts."""
    runs, previous = [], None
    for pos, c in enumerate(doc.chunks):
        if c.headings != previous:
            runs.append((pos, c.headings))
            previous = c.headings
    return runs


def find_section(doc: Doc, name: str) -> tuple[int, int, str]:
    """(first position, end position, heading path) of the section `name`: the
    chunks under that heading, subsections included. A heading matches when one of
    its parts starts with `name` (e.g. "§ 1599" or "Tenor"), else when a part or the
    whole path contains it. Several equally good matches: AgentError listing them."""
    runs = [(pos, h) for pos, h in _runs(doc) if h]
    if not runs:
        raise AgentError("This file has no section headings. Use `at` with a chunk number instead.")
    q = _key(name)
    if not q:
        raise AgentError("`section` is empty.")

    def at_start(text: str) -> bool:
        return text.startswith(q) and (
            len(text) == len(q) or not text[len(q)].isalnum() or not q[-1].isalnum()
        )

    # (heading path up to a part, that part, the path) for every part of every heading
    prefixes: list[tuple[int, str, str, str]] = []
    for pos, h in runs:
        parts = h.split(" > ")
        for j in range(len(parts)):
            prefix = " > ".join(parts[: j + 1])
            prefixes.append((pos, prefix, _key(parts[j]), _key(prefix)))

    tiers = [
        lambda part, path: at_start(part) or at_start(path),
        lambda part, path: q in part,
        lambda part, path: q in path,
    ]
    for test in tiers:
        found: dict[str, int] = {}
        for pos, prefix, part, path in prefixes:
            if test(part, path):
                found.setdefault(prefix, pos)  # a heading repeated later: its first run
        # A heading and its parent both match ("BGB" in "BGB > § 1"): the parent.
        found = {h: p for h, p in found.items() if not _parent_in(h, found)}
        if len(found) == 1:
            ((heading, start),) = found.items()
            return start, _section_end(doc, start, heading), heading
        if found:
            shown = list(found.items())[:10]
            more = f" (and {len(found) - 10} more)" if len(found) > 10 else ""
            listing = "; ".join(f"chunk {doc.chunks[p].index}: {h}" for h, p in shown)
            raise AgentError(
                f"Several sections match {name!r}{more}: {listing}. "
                "Pass a longer `section`, or `at` with the chunk number."
            )
    words = [w for w in q.split() if len(w) >= searcher.MIN_TERM_CHARS and w not in searcher.STOPWORDS]
    scored = sorted(
        ((sum(w in _key(h) for w in words), i, h) for i, (_, h) in enumerate(runs)), key=lambda x: (-x[0], x[1])
    )
    close = [h for n, _, h in scored[:10] if n]
    hint = f" Similar: {'; '.join(close)}." if close else " `browse` lists the file's sections."
    raise AgentError(f"No section matches {name!r}.{hint}")


def _parent_in(heading: str, headings) -> bool:
    parts = heading.split(" > ")
    return any(" > ".join(parts[:j]) in headings for j in range(1, len(parts)))


def _section_end(doc: Doc, start: int, heading: str) -> int:
    end = start
    while end < len(doc) and (
        doc.chunks[end].headings == heading or doc.chunks[end].headings.startswith(heading + " > ")
    ):
        end += 1
    return end


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

MARK_PARAGRAPH = re.compile(r"\n\s*\n")
MARK_LINE = re.compile(r"\n")
MARK_SENTENCE = re.compile(r"(?<=[.!?:;])\s+")
MARK_WORD = re.compile(r"\s+")


def _cut_end(text: str, lo: int, hi: int) -> int:
    """Where to stop reading text[lo:] at most at hi: the last paragraph, line,
    sentence or word end in the second half of the stretch, else the last word end
    anywhere in it, else lo (no word fits)."""
    if hi >= len(text):
        return len(text)
    floor = lo + (hi - lo) // 2
    for mark in (MARK_PARAGRAPH, MARK_LINE, MARK_SENTENCE, MARK_WORD):
        ends = [m.end() for m in mark.finditer(text, floor, hi)]
        if ends:
            return ends[-1]
    ends = [m.end() for m in MARK_WORD.finditer(text, lo, hi)]  # a long word fills the second half
    return ends[-1] if ends else lo


def _cut_start(text: str, lo: int, hi: int) -> int:
    """Where to start reading text[:hi], at lo at the earliest: the first paragraph,
    line, sentence or word start in the first half of the stretch, else the first word
    start anywhere in it, else hi (no word fits)."""
    ceiling = lo + (hi - lo) // 2
    for mark in (MARK_PARAGRAPH, MARK_LINE, MARK_SENTENCE, MARK_WORD):
        m = mark.search(text, lo, ceiling)
        if m:
            return m.end()
    m = MARK_WORD.search(text, lo, hi)  # a long word fills the first half
    return m.end() if m else hi


def read(
    doc: Doc,
    start: tuple[int, int],
    *,
    before: int,
    after: int,
    stop: int | None = None,
    link: Callable[[int], str | None],
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Text from `before` characters before `start` to `after` characters after it
    (or the position `stop`, a section's end), cut at paragraph, sentence or word
    boundaries. Returns (text, first position, end position). `link(chunk)` gives
    the URL for each section's link line."""
    pos, off = start
    if before > 0:
        off = max(off, doc.skip[pos])  # the overlap is read with the chunk before
    pieces: list[tuple[int, int, int]] = []  # (position, start, end) in file order

    # Backwards from the start.
    budget, p, o = before, pos, off
    while budget > 0:
        lo = doc.skip[p] if p > 0 else 0
        if o <= lo:
            if p == 0:
                break
            p, o = p - 1, len(doc.chunks[p - 1].text)
            continue
        first = lo if o - budget <= lo else _cut_start(doc.chunks[p].text, o - budget, o)
        if first == o:  # no word fits: stop before it, or cut a single huge word
            if budget < before:
                break
            first = o - budget
        pieces.insert(0, (p, first, o))
        budget -= o - first
        if first > lo:
            break
        o = lo
    first_pos = (pieces[0][0], pieces[0][1]) if pieces else (pos, off)

    # Forwards from the start.
    end = len(doc) if stop is None else stop
    budget, p, o = after, pos, off
    while budget > 0 and p < end:
        text = doc.chunks[p].text
        if o >= len(text):
            p += 1
            o = doc.skip[p] if p < len(doc) else 0
            continue
        last = _cut_end(text, o, o + budget)
        if last == o:  # no word fits: stop before it, or cut a single huge word
            if budget < after:
                break
            last = o + budget
        pieces.append((p, o, last))
        budget -= last - o
        o = last
        if last < len(text):
            break  # cut inside the chunk: the budget is spent
    end_pos = (p, o)
    if p < len(doc) and o >= len(doc.chunks[p].text):  # a chunk read to its end
        end_pos = (p + 1, doc.skip[p + 1] if p + 1 < len(doc) else 0)

    return _assemble(doc, pieces, link), first_pos, end_pos


def _assemble(doc: Doc, pieces: list[tuple[int, int, int]], link: Callable[[int], str | None]) -> str:
    """The pieces' text, with a link line wherever a new section (or page) starts."""
    out: list[str] = []
    shown_headings: str | None = None
    shown_page = 0
    previous: tuple[int, int] | None = None  # (position, end) of the piece before
    for p, a, b in pieces:
        c = doc.chunks[p]
        text = c.text[a:b]
        if previous == (p, a):
            out.append(text)  # the same chunk, continued
            previous = (p, b)
            continue
        new_page = c.page and c.page != shown_page
        if c.headings != shown_headings or new_page or previous is None:
            label = _label(shown_headings, c.headings, c.page if new_page or previous is None else 0)
            if label:
                url = link(c.index)
                line = f"[{label}]({url})" if url else f"[{label}]"
                out.append(("\n\n" if out else "") + line + "\n")
            elif out:
                out.append("\n\n")
            shown_headings, shown_page = c.headings, c.page or shown_page
        elif previous is not None and previous[0] == p - 1 and a == doc.skip[p] and a > 0:
            pass  # continues the chunk before exactly (overlap removed)
        else:
            out.append("\n\n")
        out.append(text)
        previous = (p, b)
    return "".join(out).strip("\n")


def _label(before: str | None, heading: str, page: int) -> str:
    """The heading path from where it differs from the one before ("§ 1600" after
    "BGB > § 1599"), plus the page when it changes."""
    parts = heading.split(" > ") if heading else []
    if before:
        old = before.split(" > ")
        common = 0
        while common < min(len(old), len(parts)) - 1 and old[common] == parts[common]:
            common += 1
        parts = parts[common:]
    label = " > ".join(parts)
    if page:
        label = f"{label}, p. {page}" if label else f"p. {page}"
    return label


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


def modified(path: str) -> tuple[str | None, float]:
    """(date for agents, timestamp for sorting); None and 0 if the file is gone."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None, 0.0
    return datetime.date.fromtimestamp(mtime).isoformat(), mtime


def indexed_files(db: sqlite3.Connection, scopes) -> list[tuple[str, str, str, int]]:
    """(folder, path, file type, chunks) of the indexed files inside the scopes."""
    where, params = searcher.scope_clause(scopes)
    return db.execute(
        f"""SELECT folder, path, file_type, COUNT(*) FROM {cfg.TABLE_NAME}
            WHERE {where} GROUP BY folder, path ORDER BY folder, path""",
        params,
    ).fetchall()


Sort = Literal["name", "modified"]


def file_entries(rows: list[tuple[str, str, str, int]], sort: Sort) -> list[FileEntry]:
    entries = []
    for folder, path, file_type, chunks in rows:
        date, mtime = modified(os.path.join(folder, path))
        entries.append((mtime, FileEntry(file=paths.full(folder, path), type=file_type, modified=date, chunks=chunks)))
    if sort == "modified":
        entries.sort(key=lambda e: -e[0])  # newest first
    else:
        entries.sort(key=lambda e: e[1].file.casefold())
    return [e for _, e in entries]


def matches_filter(text: str, filter_: str | None) -> bool:
    return not filter_ or _key(filter_) in _key(text)


def list_dir(
    rows: list[tuple[str, str, str, int]], folder: str, rel: str, sort: Sort, filter_: str | None
) -> tuple[list[DirEntry], list[FileEntry]]:
    """One level of an indexed folder (or a folder inside it): subfolders with their
    file counts, then files."""
    prefix = f"{rel}/" if rel else ""
    dirs: dict[str, int] = {}
    files = []
    for row in rows:
        rest = row[1][len(prefix):]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            dirs[name] = dirs.get(name, 0) + 1
        elif matches_filter(rest, filter_):
            files.append(row)
    dir_entries = [
        DirEntry(name=paths.full(folder, prefix + name), files=n)
        for name, n in sorted(dirs.items(), key=lambda d: d[0].casefold())
        if matches_filter(name, filter_)
    ]
    return dir_entries, file_entries(files, sort)


def sections(doc: Doc, filter_: str | None) -> list[SectionEntry]:
    return [
        SectionEntry(chunk=doc.chunks[pos].index, section=h, page=doc.chunks[pos].page or None)
        for pos, h in _runs(doc)
        if h and matches_filter(h, filter_)
    ]

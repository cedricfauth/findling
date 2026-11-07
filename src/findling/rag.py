"""Search results for AI agents (MCP tools, RAG): one entry per file with its best
passages, compact enough to spend the tokens on text.

Built from searcher.SearchResponse, so files and passages are chosen exactly as on the
web page. Shared by the MCP server (mcp_server.py) and `search.py --json`.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from . import searcher

DEFAULT_MAX_CHARS = 24_000
# Plain text and code chunks overlap (RecursiveSplitter, chunk_overlap=200 in
# pipeline.py); joining neighbours drops the repeated part.
_MAX_OVERLAP = 400
_MIN_OVERLAP = 20


class Passage(BaseModel):
    chunk: int = Field(description="The passage's chunk number in the file: `read` and `open` take it.")
    page: int | None = Field(None, description="Page (PDFs etc.).")
    section: str | None = Field(None, description="Heading path, e.g. 'BGB > § 1599 Nichtbestehen der Vaterschaft'.")
    match: Literal["words", "meaning"] = Field(
        description="'words': contains the query words. 'meaning': only similar in meaning."
    )
    text: str = Field(description="The chunk's text as indexed (documents may be reformatted).")
    excerpt: bool | None = Field(
        None, description="True: only an excerpt around the match, to stay within max_chars. `read` has the rest."
    )
    url: str | None = Field(
        None, description="Shows the passage highlighted in its file in the user's browser. Link it when citing."
    )


class FileHit(BaseModel):
    file: str = Field(description="The file: `read`, `browse`, `open` and search's `within` take it.")
    type: str
    modified: str | None = Field(None, description="Last modified (date), if the file still exists.")
    passages: list[Passage] = Field(description="Best passages first.")


MatchQuality = Literal["words_and_meaning", "meaning_only", "no_results"]


class SearchResult(BaseModel):
    query: str
    terms: list[str] = Field(description="Words and phrases used for the exact-word search (filler words removed).")
    match_quality: MatchQuality = Field(
        description="'words_and_meaning': some files contain the query words. 'meaning_only': none do; "
        "results are only the closest by meaning and may be unrelated."
    )
    note: str | None = Field(None, description="Hint about the result quality, if any.")
    total: int = Field(description="Files found.")
    next_offset: int | None = Field(None, description="Pass as `offset` to get the next files.")
    files: list[FileHit] = Field(description="Best first.")


def empty(query: str, note: str | None = None) -> SearchResult:
    return SearchResult(query=query, terms=[], match_quality="no_results", note=note, total=0, files=[])


# (file result, chunk number) -> URL showing that chunk highlighted
ViewLink = Callable[[searcher.FileResult, int], str | None]
# file result -> the name agents use for it
FileName = Callable[[searcher.FileResult], str]


def build(
    response: searcher.SearchResponse,
    *,
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    file_name: FileName | None = None,
    view_link: ViewLink | None = None,
) -> SearchResult:
    """The files from `offset` on. `max_chars`: budget for all passage texts together.
    Every file's best passage comes first, then further passages; a passage that
    doesn't fit becomes an excerpt around its match. Files whose best passage doesn't
    fit even as an excerpt are left for the next page (next_offset). The first
    passage is always included (cut to max_chars if needed)."""
    needles = response.query.needles
    results = response.results[offset:]
    words = bool(needles) and not response.fulltext_stale

    # Spend the budget breadth-first: each file's best passage, then the rest.
    budget = max_chars
    keep: list[list[Passage]] = [[] for _ in results]
    shown = len(results)  # files from here on go to the next page
    rounds = max((len(r.matches) for r in results), default=0)
    for i in range(rounds):
        for slot, result in enumerate(results[:shown]):
            if i >= len(result.matches):
                continue
            options = _options(result.matches[i], words)
            chosen = next((p for p in options if len(p.text) <= budget), None)
            if chosen is None and (slot, i) == (0, 0):
                chosen = _cut(options[-1], max_chars)
            if chosen is None:
                if i == 0:
                    shown = slot  # keep the files in rank order: this one starts the next page
                    break
                continue
            keep[slot].append(chosen)
            budget -= len(chosen.text)

    files = []
    for result, passages in zip(results[:shown], keep):
        if view_link is not None:
            passages = [p.model_copy(update={"url": view_link(result, p.chunk)}) for p in passages]
        name = file_name(result) if file_name else result.full_path
        files.append(FileHit(file=name, type=result.file_type, modified=_modified(result.full_path), passages=passages))

    if not response.results:
        quality: MatchQuality = "no_results"
    elif response.weak or response.fulltext_stale:
        quality = "meaning_only"
    else:
        quality = "words_and_meaning"
    note = None
    if response.fulltext_stale:
        note = "The exact-word index is being rebuilt; these results are by meaning only."
    elif response.weak and not needles:
        note = (
            "The query has no words to match exactly (only filler words or words under 3 letters). "
            "Results are only the closest by meaning and may be unrelated."
        )
    elif response.weak:
        note = "No file contains these words. Results are only the closest by meaning and may be unrelated."
    elif not response.results:
        note = "No matching files."

    total = max(response.total, len(response.results))
    end = offset + shown
    return SearchResult(
        query=response.query.raw,
        terms=needles,
        match_quality=quality,
        note=note,
        total=total,
        next_offset=end if end < total else None,
        files=files,
    )


def _options(m: searcher.Match, words: bool) -> list[Passage]:
    """The passage in full, and an excerpt around the match if that's shorter."""
    full = Passage(
        chunk=m.chunk_index,
        page=m.page or None,  # 0 = no pages
        section=m.headings or None,
        match="words" if words and m.bm25_rank is not None else "meaning",
        text=m.text,
    )
    start, end = m.snippet
    excerpt = m.text[start:end].strip()
    if not excerpt or len(excerpt) >= len(m.text):
        return [full]
    excerpt = ("… " if start > 0 else "") + excerpt + (" …" if end < len(m.text) else "")
    return [full, full.model_copy(update={"text": excerpt, "excerpt": True})]


def _cut(passage: Passage, size: int) -> Passage:
    """The passage with its text cut to about `size` characters, at a word boundary."""
    if len(passage.text) <= size:
        return passage
    text = passage.text[: max(size - 2, 0)]
    space = text.rfind(" ", max(0, len(text) - 80))
    if space > 0:
        text = text[:space]
    return passage.model_copy(update={"text": text.rstrip() + " …", "excerpt": True})


def _modified(path: str) -> str | None:
    try:
        return datetime.date.fromtimestamp(os.stat(path).st_mtime).isoformat()
    except OSError:
        return None  # moved or deleted since indexing


def overlap(a: str, b: str) -> int:
    """How many characters at the start of b repeat the end of a (overlapping chunks),
    the longest such overlap of _MIN_OVERLAP to _MAX_OVERLAP characters, else 0."""
    head = b[:_MIN_OVERLAP]
    if len(head) < _MIN_OVERLAP:
        return 0
    i = a.find(head, max(0, len(a) - _MAX_OVERLAP))
    while i != -1:  # earliest start first = longest overlap
        if b.startswith(a[i:]):
            return len(a) - i
        i = a.find(head, i + 1)
    return 0

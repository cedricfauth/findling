"""Hybrid search: semantic (sqlite-vec) + full text (FTS5), merged with Reciprocal
Rank Fusion. Shared by the web server and search.py.

    1. Semantic list:  top chunks by cosine similarity.
    2. Full-text list: top chunks by BM25 (file path > headings > text).
    3. Both lists are reduced to files (best chunk per file) and merged with
       weighted RRF:  score = w_vec / (k + rank_vec) + w_fts / (k + rank_fts).
       The full-text list only holds chunks containing enough of the query words
       (any for 1-2 word queries, at least half for longer ones), most words first.
    4. RRF only orders. Shown: files with a word match, plus the few files whose
       meaning score is close to the best one. If no file contains the words, the
       response is marked `weak` so the page can say the results are only the
       closest by meaning. (Similarity alone can't tell "no answer" from "answer in
       other words" reliably with this model, so nothing is hidden on that basis.)
"""

from __future__ import annotations

import contextlib
import math
import pathlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

import sqlite_vec

from . import config as cfg
from .fts import FTS_TABLE


@dataclass(frozen=True)
class SearchParams:
    # 20 keeps a strong single-list hit closer to the top than the common 60.
    # Favoring full text for short queries made results worse.
    rrf_k: float = 20
    w_vec: float = 1.0
    w_fts: float = 1.0
    short_query_terms: int = 2  # up to this many words: any word counts as a word match
    n_vec: int = 300  # chunks taken from each list before grouping by file
    n_fts: int = 300
    bm25_weights: tuple[float, float, float] = (2.0, 1.5, 1.0)  # path, headings, text
    rel_keep: float = 0.02  # files without word matches: keep within this similarity of the best...
    max_meaning_only: int = 5  # ...and at most this many of them
    min_coverage: float = 0.5  # 3+ word queries: share of the words a chunk must contain
    # Passages shown per file: those at least as good as the best passage of the
    # weakest file shown (they'd have made the list on their own); always the file's
    # best one, at most this many.
    max_matches_per_file: int = 10
    snippet_chars: int = 320


DEFAULT_PARAMS = SearchParams()


# ---------------------------------------------------------------------------
# Text normalization and query parsing
# ---------------------------------------------------------------------------

# Filler words, dropped from the full-text part of a query (the meaning search always
# gets the whole query). Substring matching makes them match almost everything ("die"
# in "Dienstreise"). Snowball stopword lists for English and German, as shipped by NLTK
# (https://snowballstem.org, BSD license).
_SNOWBALL_EN = """
    i me my myself we our ours ourselves you you're you've you'll you'd your yours yourself
    yourselves he him his himself she she's her hers herself it it's its itself they them
    their theirs themselves what which who whom this that that'll these those am is are was
    were be been being have has had having do does did doing a an the and but if or because
    as until while of at by for with about against between into through during before after
    above below to from up down in out on off over under again further then once here there
    when where why how all any both each few more most other some such no nor not only own
    same so than too very s t can will just don don't should should've now d ll m o re ve y
    ain aren aren't couldn couldn't didn didn't doesn doesn't hadn hadn't hasn hasn't haven
    haven't isn isn't ma mightn mightn't mustn mustn't needn needn't shan shan't shouldn
    shouldn't wasn wasn't weren weren't won won't wouldn wouldn't
"""
_SNOWBALL_DE = """
    aber alle allem allen aller alles als also am an ander andere anderem anderen anderer
    anderes anderm andern anderr anders auch auf aus bei bin bis bist da damit dann der den
    des dem die das dass daß derselbe derselben denselben desselben demselben dieselbe
    dieselben dasselbe dazu dein deine deinem deinen deiner deines denn derer dessen dich dir
    du dies diese diesem diesen dieser dieses doch dort durch ein eine einem einen einer
    eines einig einige einigem einigen einiger einiges einmal er ihn ihm es etwas euer eure
    eurem euren eurer eures für gegen gewesen hab habe haben hat hatte hatten hier hin hinter
    ich mich mir ihr ihre ihrem ihren ihrer ihres euch im in indem ins ist jede jedem jeden
    jeder jedes jene jenem jenen jener jenes jetzt kann kein keine keinem keinen keiner
    keines können könnte machen man manche manchem manchen mancher manches mein meine meinem
    meinen meiner meines mit muss musste nach nicht nichts noch nun nur ob oder ohne sehr
    sein seine seinem seinen seiner seines selbst sich sie ihnen sind so solche solchem
    solchen solcher solches soll sollte sondern sonst über um und uns unsere unserem unseren
    unser unseres unter viel vom von vor während war waren warst was weg weil weiter welche
    welchem welchen welcher welches wenn werde werden wie wieder will wir wird wirst wo
    wollen wollte würde würden zu zum zur zwar zwischen
"""
MIN_TERM_CHARS = 3  # the trigram tokenizer can't match anything shorter

# Accents removed for matching: the "Combining Diacritical Marks" block, which is what
# Latin letters decompose into (ü = u + U+0308). Other scripts keep their marks.
_LATIN_ACCENTS = range(0x0300, 0x0370)


def _is_latin_accent(c: str) -> bool:
    return ord(c) in _LATIN_ACCENTS


def _fold_char(c: str) -> str:
    """Lower-case, accent-free form of one character ("" for a lone accent mark).
    Matches what SQLite's FTS5 does with remove_diacritics for Latin text."""
    if _is_latin_accent(c):
        return ""  # accent of decomposed (NFD) text, e.g. macOS file names
    decomposed = unicodedata.normalize("NFD", c)
    if len(decomposed) > 1 and all(_is_latin_accent(x) for x in decomposed[1:]):
        c = decomposed[0]  # é -> e, ü -> u
    # Everything else stays as it is: Hangul syllables, kana, ligatures, ...
    return c.lower()


class _FoldTable(dict):
    """str.translate table for _fold_char, filled as characters first show up.
    translate loops in C: 7x faster than folding char by char in Python."""

    def __missing__(self, code: int) -> str:
        self[code] = folded = _fold_char(chr(code))
        return folded


_FOLD = _FoldTable()


def normalize(text: str) -> str:
    """Case- and accent-insensitive form of text, for comparing words."""
    return text.translate(_FOLD)


def _normalize_with_positions(text: str) -> tuple[str, list[int]]:
    """normalize(text) plus, for each of its characters, the index in `text` it came
    from (lengths differ: accents of decomposed text vanish, some letters expand)."""
    out: list[str] = []
    positions: list[int] = []
    for i, c in enumerate(text):
        folded = _FOLD[ord(c)]
        out.append(folded)
        positions.extend([i] * len(folded))
    return "".join(out), positions


STOPWORDS = frozenset(normalize(w) for w in (_SNOWBALL_EN + _SNOWBALL_DE).split())


@dataclass
class ParsedQuery:
    raw: str
    terms: list[str]  # normalized single words, filler words and too-short words removed
    phrases: list[str]  # normalized "quoted phrases"

    @property
    def needles(self) -> list[str]:
        return self.phrases + self.terms

    @property
    def size(self) -> int:
        return len(self.needles)

    def fts_expression(self) -> str | None:
        """FTS5 MATCH expression: any needle, each as a literal string (no FTS syntax
        from the user's input can get through)."""
        if not self.needles:
            return None
        return " OR ".join('"' + n.replace('"', '""') + '"' for n in self.needles)


_PHRASE_RE = re.compile(r'"([^"]+)"')
_EDGE_PUNCT = "\"'`.,;:!?()[]{}<>«»„“”‘’*#"


def parse_query(raw: str) -> ParsedQuery:
    phrases = [normalize(p.strip()) for p in _PHRASE_RE.findall(raw) if len(p.strip()) >= MIN_TERM_CHARS]
    rest = _PHRASE_RE.sub(" ", raw)
    terms: list[str] = []
    for token in rest.split():
        # Keep inner punctuation: INV-2024-0042, water_plants, e-mail, v2.3
        token = normalize(token.strip(_EDGE_PUNCT))
        if len(token) < MIN_TERM_CHARS or token in STOPWORDS or token in terms:
            continue
        terms.append(token)
    return ParsedQuery(raw=raw, terms=terms, phrases=phrases)


def words_found(query: ParsedQuery, *texts: str) -> int:
    """How many of the query's needles occur in the texts."""
    haystack = normalize("\n".join(texts))
    return sum(1 for n in query.needles if n in haystack)


def words_required(query: ParsedQuery, params: "SearchParams") -> int:
    """Needles a chunk must contain to count as a word match."""
    if query.size <= params.short_query_terms:
        return 1
    return max(2, math.ceil(query.size * params.min_coverage))


def find_matches(text: str, needles: list[str]) -> list[tuple[int, int, str]]:
    """All (start, end, needle) occurrences of the needles in text, case/accent-
    insensitive. Positions are in the original text."""
    haystack, positions = _normalize_with_positions(text)
    found = []
    for needle in needles:
        start = haystack.find(needle)
        while start != -1:
            first = positions[start]
            last = positions[start + len(needle) - 1] + 1
            while last < len(text) and _is_latin_accent(text[last]):
                last += 1  # include the accent of a decomposed last letter
            found.append((first, last, needle))
            start = haystack.find(needle, start + 1)
    return sorted(found)


def _merge_ranges(matches: list[tuple[int, int, str]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end, _ in matches:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def best_window(text: str, matches: list[tuple[int, int, str]], size: int) -> tuple[int, int]:
    """The ~size-char window showing the most distinct query words (then most matches)."""
    if len(text) <= size:
        return 0, len(text)
    if not matches:
        start = 0
    else:
        best = (-1, -1, 0)
        for anchor, _, _ in matches:
            lo = max(0, anchor - size // 5)
            inside = [m for m in matches if m[0] >= lo and m[1] <= lo + size]
            score = (len({m[2] for m in inside}), len(inside))
            if score > best[:2]:
                best = (*score, lo)
        start = best[2]
        # Start at a word boundary.
        if start > 0:
            space = text.rfind(" ", max(0, start - 30), start)
            start = space + 1 if space != -1 else start
    end = min(len(text), start + size)
    if end < len(text):
        space = text.rfind(" ", end - 30, end)
        end = space if space > start else end
    return start, end


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class Match:
    chunk_index: int
    headings: str
    page: int
    text: str
    snippet: tuple[int, int]  # window into text
    highlights: list[list[int]]  # [start, end] ranges in text
    heading_highlights: list[list[int]]
    similarity: float
    bm25_rank: int | None  # rank in the full-text list (1 = best), None if not in it
    vec_rank: int | None


@dataclass
class FileResult:
    folder: str
    path: str  # relative to folder
    file_type: str
    score: float  # RRF score
    word_match: bool  # contains enough of the query words
    path_highlights: list[list[int]]
    matches: list[Match] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    @property
    def full_path(self) -> str:
        return str(pathlib.Path(self.folder) / self.path)


@dataclass
class SearchResponse:
    query: ParsedQuery
    results: list[FileResult]
    weak: bool  # no file contains the query words; results are only the closest by meaning
    debug: dict = field(default_factory=dict)
    # Set when the full-text index is out of date (search fell back to meaning only):
    # the caller should run fts.ensure_fts() to rebuild it.
    fulltext_stale: bool = False
    total: int = 0  # files found (results holds the first k of them)


@dataclass(frozen=True)
class Scope:
    """Part of an indexed folder to search: the whole folder, or the files whose path
    passes `where`, an SQL condition with {path} standing for the path column."""

    folder: str
    where: str = ""
    params: tuple = ()


def scope_clause(scopes: list[Scope], prefix: str = "") -> tuple[str, list]:
    """SQL condition (and its parameters) for rows inside any of the scopes. `prefix`:
    the table alias with its dot, e.g. "c."."""
    parts, params = [], []
    for s in scopes:
        if s.where:
            parts.append(f"({prefix}folder = ? AND ({s.where.format(path=prefix + 'path')}))")
            params.extend([s.folder, *s.params])
        else:
            parts.append(f"{prefix}folder = ?")
            params.append(s.folder)
    return "(" + " OR ".join(parts or ["0"]) + ")", params


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def open_db(db_path: pathlib.Path) -> sqlite3.Connection | None:
    """Read-only connection with sqlite-vec loaded, or None if there's no index yet."""
    if not db_path.exists():
        return None
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    # Read pages straight from the OS file cache: the vector scan reads past the big
    # text columns of every row (191 -> 105 ms on 45k chunks). Only a limit, not RAM.
    db.execute("PRAGMA mmap_size=1073741824")
    return db


def embed_query(model, query: str):
    return model.encode(cfg.QUERY_PREFIX + query, normalize_embeddings=True)


def _vector_list(db, query_blob, scopes: list[Scope], limit):
    """Top chunks by similarity: [(rowid, similarity)]."""
    where, params = scope_clause(scopes)
    rows = db.execute(
        f"""SELECT rowid, vec_distance_cosine(embedding, ?) AS dist
            FROM {cfg.TABLE_NAME} WHERE {where}
            ORDER BY dist LIMIT ?""",
        [query_blob, *params, limit],
    ).fetchall()
    return [(rowid, 1 - dist) for rowid, dist in rows]


def _fts_list(db, query: ParsedQuery, scopes: list[Scope], params: SearchParams):
    """Chunks containing enough query words: most words first, then BM25.
    Returns ([(rowid, words_found)], stale) where stale means the full-text index is
    out of date and was skipped."""
    expression = query.fts_expression()
    if expression is None:
        return [], False
    bm25 = f"bm25({FTS_TABLE}, {', '.join(str(w) for w in params.bm25_weights)})"
    where, where_params = scope_clause(scopes, "c.")
    try:
        # Filter by folder with a join, not "f.rowid IN (SELECT ...)": SQLite hands
        # that IN list to FTS5, which then runs the MATCH once per chunk in the folders
        # (145 s instead of 0.2 s for a two-word query on ~28k chunks). The texts are
        # fetched after the LIMIT, so sorting doesn't carry thousands of them along.
        rows = db.execute(
            f"""SELECT t.rowid, t.score, d.path, d.headings, d.text
                FROM (SELECT f.rowid AS rowid, {bm25} AS score
                      FROM {FTS_TABLE} f JOIN {cfg.TABLE_NAME} c ON c.rowid = f.rowid
                      WHERE {FTS_TABLE} MATCH ?
                        AND {where}
                      ORDER BY score LIMIT ?) t
                JOIN {cfg.TABLE_NAME} d ON d.rowid = t.rowid
                ORDER BY t.score""",
            (expression, *where_params, params.n_fts * 3),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        message = str(e)
        if "missing row" in message:
            # The index still lists chunks that are gone (e.g. the chunks table was
            # recreated): search by meaning only until it's rebuilt.
            return [], True
        if "no such table" in message or "tokenizer" in message:
            # Not built yet, or this SQLite is too old for the tokenizer.
            return [], False
        raise
    required = words_required(query, params)
    hits = []
    for position, (rowid, _, path, headings, text) in enumerate(rows):
        found = words_found(query, path, headings, text)
        if found >= required:
            hits.append((-found, position, rowid))  # position = BM25 order
    hits.sort()
    return [(rowid, -neg_found) for neg_found, _, rowid in hits[: params.n_fts]], False


def search(
    db: sqlite3.Connection,
    raw_query: str,
    query_vec,
    *,
    folders: list[str],
    k: int = 10,
    folder: str | None = None,
    scopes: list[Scope] | None = None,
    params: SearchParams = DEFAULT_PARAMS,
) -> SearchResponse:
    """`folders`: the configured folders; rows of other folders (removed, but not
    yet deleted from the index) are never returned. `folder`: limit to one of them.
    `scopes`: limit to parts of them (instead of `folder`)."""
    query = parse_query(raw_query)
    if scopes is None:
        scopes = [Scope(f) for f in folders if folder is None or f == folder]
    scopes = [s for s in scopes if s.folder in folders]
    if not scopes:
        return SearchResponse(query, [], weak=False)
    with snapshot(db):
        return _search(db, query, query_vec, scopes, k, params)


@contextlib.contextmanager
def snapshot(db: sqlite3.Connection):
    """One read transaction: all queries inside see the same snapshot, even while the
    indexer writes (otherwise a chunk can vanish between the queries). Wrap search()
    and any follow-up reads (e.g. rag.build) to keep them consistent. Reentrant."""
    if db.in_transaction:
        yield
        return
    db.execute("BEGIN")
    try:
        yield
    finally:
        db.rollback()


def _search(db, query: ParsedQuery, query_vec, scopes: list[Scope], k, params) -> SearchResponse:
    query_blob = sqlite_vec.serialize_float32(query_vec)
    try:
        vec_list = _vector_list(db, query_blob, scopes, params.n_vec)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return SearchResponse(query, [], weak=False)
        raise
    fts_list, fts_stale = _fts_list(db, query, scopes, params)

    vec_rank = {rowid: i + 1 for i, (rowid, _) in enumerate(vec_list)}
    fts_rank = {rowid: i + 1 for i, (rowid, _) in enumerate(fts_list)}
    fts_words = dict(fts_list)
    candidates = list(dict.fromkeys([*vec_rank, *fts_rank]))
    if not candidates:
        return SearchResponse(query, [], weak=False, fulltext_stale=fts_stale)

    # Load the candidates (with their similarity, also for full-text-only ones).
    placeholders = ",".join("?" * len(candidates))
    chunks = {
        row[0]: row[1:]
        for row in db.execute(
            f"""SELECT rowid, folder, path, file_type, chunk_index, headings, page, text,
                       1 - vec_distance_cosine(embedding, ?)
                FROM {cfg.TABLE_NAME} WHERE rowid IN ({placeholders})""",
            [query_blob, *candidates],
        )
    }

    top_sim = vec_list[0][1] if vec_list else 0.0
    needles = query.needles

    # Group into files. A file's rank in each list = rank of its best chunk there.
    files: dict[tuple[str, str], dict] = {}
    for rowid in candidates:
        if rowid not in chunks:
            continue  # can't happen within one snapshot; just in case
        folder_, path, file_type, chunk_index, headings, page, text, sim = chunks[rowid]
        f = files.setdefault(
            (folder_, path),
            {"file_type": file_type, "chunks": [], "vec_rank": None, "fts_rank": None},
        )
        cov = fts_words.get(rowid, 0) / max(query.size, 1)
        f["chunks"].append((rowid, chunk_index, headings, page, text, sim, cov))
    file_order_vec = sorted(
        (f for f in files.values() if any(c[0] in vec_rank for c in f["chunks"])),
        key=lambda f: min(vec_rank[c[0]] for c in f["chunks"] if c[0] in vec_rank),
    )
    file_order_fts = sorted(
        (f for f in files.values() if any(c[0] in fts_rank for c in f["chunks"])),
        key=lambda f: min(fts_rank[c[0]] for c in f["chunks"] if c[0] in fts_rank),
    )
    for i, f in enumerate(file_order_vec):
        f["vec_rank"] = i + 1
    for i, f in enumerate(file_order_fts):
        f["fts_rank"] = i + 1

    w_fts = params.w_fts
    k_ = params.rrf_k

    def chunk_rrf(c) -> float:
        rowid = c[0]
        score = 0.0
        if rowid in vec_rank:
            score += params.w_vec / (k_ + vec_rank[rowid])
        if rowid in fts_rank:
            score += w_fts / (k_ + fts_rank[rowid])
        return score

    scored: list[tuple[float, bool, tuple[str, str], dict]] = []
    for key, f in files.items():
        score = 0.0
        if f["vec_rank"]:
            score += params.w_vec / (k_ + f["vec_rank"])
        if f["fts_rank"]:
            score += w_fts / (k_ + f["fts_rank"])
        best_cov = max(c[6] for c in f["chunks"])
        best_sim = max(c[5] for c in f["chunks"])
        word_match = f["fts_rank"] is not None
        keep = word_match or (
            best_sim >= top_sim - params.rel_keep
            and f["vec_rank"] is not None
            and f["vec_rank"] <= params.max_meaning_only
        )
        f.update(score=score, word_match=word_match, keep=keep, best_sim=best_sim, best_cov=best_cov)
        scored.append((score, keep, key, f))
    scored.sort(key=lambda s: -s[0])

    kept = [s for s in scored if s[1]]
    weak = not fts_list and not fts_stale  # stale: unknown whether words would match
    chosen = kept[:k]

    # The bar for showing more passages of a file: the best passage of the weakest
    # file shown. Each file's best passage clears it, so every file shows at least one.
    bar = min((max(chunk_rrf(c) for c in f["chunks"]) for *_, f in chosen), default=0.0)

    results: list[FileResult] = []
    for score, _, (folder_, path), f in chosen:
        ordered = sorted(f["chunks"], key=lambda c: (-chunk_rrf(c), -c[5]))
        shown = [c for c in ordered if chunk_rrf(c) >= bar][: params.max_matches_per_file]
        matches = []
        for rowid, chunk_index, headings, page, text, sim, _ in shown:
            found = find_matches(text, needles)
            matches.append(
                Match(
                    chunk_index=chunk_index,
                    headings=headings,
                    page=page,
                    text=text,
                    snippet=best_window(text, found, params.snippet_chars),
                    highlights=_merge_ranges(found),
                    heading_highlights=_merge_ranges(find_matches(headings, needles)),
                    similarity=sim,
                    bm25_rank=fts_rank.get(rowid),
                    vec_rank=vec_rank.get(rowid),
                )
            )
        results.append(
            FileResult(
                folder=folder_,
                path=path,
                file_type=f["file_type"],
                score=score,
                word_match=f["word_match"],
                path_highlights=_merge_ranges(find_matches(path, needles)),
                matches=matches,
                debug={
                    "rrf": round(score, 5),
                    "vec_rank": f["vec_rank"],
                    "fts_rank": f["fts_rank"],
                    "best_similarity": round(f["best_sim"], 4),
                    "word_coverage": round(f["best_cov"], 2),
                    "kept_because": (
                        "words" if f["word_match"] else "close to best meaning match"
                    ),
                },
            )
        )
    return SearchResponse(
        query=query,
        results=results,
        weak=weak,
        debug={
            "terms": query.needles,
            "fts_expression": query.fts_expression(),
            "words_required": words_required(query, params),
            "vector_candidates": len(vec_list),
            "fulltext_candidates": len(fts_list),
            "fulltext_stale": fts_stale,
            "passage_bar": round(bar, 5),
        },
        fulltext_stale=fts_stale,
        total=len(kept),
    )

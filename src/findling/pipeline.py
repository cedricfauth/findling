"""Indexing pipeline: text, documents and source code from several folders into SQLite.

Run by the web server (server.py, via indexer.py) in live mode.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import inspect
import logging
import os
import pathlib
import shutil
import sqlite3
import threading
import time
import warnings
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Annotated, Iterator

import lxml.etree
from numpy.typing import NDArray

import cocoindex as coco
from cocoindex.connectors import localfs, sqlite
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.ops.text import RecursiveSplitter, detect_code_language
from cocoindex.resources.file import FileLike, PatternFilePathMatcher

from . import config as cfg
from . import extensions
from . import fts
from . import html_prep

log = logging.getLogger("findling")

# cocoindex 1.1.0 still calls sentence-transformers' old method name; harmless.
warnings.filterwarnings(
    "ignore",
    message=r"The `get_sentence_embedding_dimension` method has been renamed",
    category=FutureWarning,
)


# ---------------------------------------------------------------------------
# Tokenizer (the embedding model's own, used only for counting tokens)
# ---------------------------------------------------------------------------


# Serializes slow lazy loading (docling, tokenizers, the embedding model).
# transformers loads its submodules lazily, which breaks when two threads
# trigger it at the same time ("cannot import name ... from transformers").
_LOAD_LOCK = threading.RLock()


def _load_tokenizer():
    with _LOAD_LOCK:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(cfg.EMBED_MODEL)
    # Only used to measure text, never to feed the model. Lifting the limit stops
    # "Token indices sequence length is longer than ... (N > 512)" warnings when
    # measuring sections that are about to be split anyway.
    tokenizer.model_max_length = 1_000_000
    return tokenizer


_counter_tokenizer = None
_counter_lock = threading.Lock()


def _count_tokens(text: str) -> int:
    """Number of tokens the embedding model sees (incl. special tokens)."""
    global _counter_tokenizer
    with _counter_lock:
        if _counter_tokenizer is None:
            _counter_tokenizer = _load_tokenizer()
        return len(_counter_tokenizer(text)["input_ids"])


# ---------------------------------------------------------------------------
# Docling: conversion + structure-aware chunking with heading context
# ---------------------------------------------------------------------------


@dataclass
class DocChunk:
    text: str  # raw chunk text (for display)
    context: str  # chunk text prefixed with its heading path (for embedding)
    headings: list[str]
    page: int  # first page the chunk appears on, 0 if unknown


class DoclingTools:
    """One shared converter + chunker, created on first use (importing docling and
    loading its models is slow, and isn't needed when no document changed).
    Conversions are serialized: docling already uses all CPU cores per document,
    and model state isn't thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._converters: dict[bool, object] = {}  # by OCR on/off
        self._chunker = None
        self._document_stream = None
        self._formats_seen: set[tuple[str, bool]] = set()

    def _ensure_loaded(self, ocr: bool) -> None:
        if ocr in self._converters:
            return
        with _LOAD_LOCK:
            self._load(ocr)

    def _load(self, ocr: bool) -> None:
        # All docling imports happen here, under _LOAD_LOCK: they import transformers,
        # which breaks if another thread imports it at the same time.
        from docling.chunking import HybridChunker
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.transforms.chunker.tokenizer.huggingface import (
            HuggingFaceTokenizer,
        )

        # OCR engine: docling picks one (macOS: Apple's Vision framework via ocrmac).
        pdf_options = PdfPipelineOptions(do_ocr=ocr, do_table_structure=True)
        self._converters[ocr] = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
        )
        if self._chunker is not None:
            return
        # Size chunks with the embedding model's own tokenizer. A separate
        # instance: this one runs in a worker thread.
        tokenizer = HuggingFaceTokenizer(
            tokenizer=_load_tokenizer(), max_tokens=cfg.MAX_TOKENS
        )
        self._chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)
        self._document_stream = DocumentStream

    def convert_and_chunk(self, name: str, data: bytes, ocr: bool = False) -> list[DocChunk]:
        with self._lock:
            self._ensure_loaded(ocr)
            converter = self._converters[ocr]
            stream = self._document_stream(name=name, stream=BytesIO(data))
            fmt = (pathlib.PurePath(name).suffix.lower(), ocr)
            if fmt in self._formats_seen:
                result = converter.convert(stream)
            else:
                # docling sets up each format's pipeline on first use, and the PDF one
                # loads models that import transformers: also under the load lock.
                with _LOAD_LOCK:
                    result = converter.convert(stream)
                self._formats_seen.add(fmt)
            chunks: list[DocChunk] = []
            for chunk in self._chunker.chunk(dl_doc=result.document):
                pages = sorted(
                    prov.page_no
                    for item in chunk.meta.doc_items
                    for prov in (item.prov or [])
                )
                chunks.append(
                    DocChunk(
                        text=chunk.text,
                        context=self._chunker.contextualize(chunk=chunk),
                        headings=list(chunk.meta.headings or []),
                        page=pages[0] if pages else 0,
                    )
                )
            return chunks


# ---------------------------------------------------------------------------
# Shared resources
# ---------------------------------------------------------------------------

SQLITE_DB = coco.ContextKey[sqlite.ManagedConnection]("sqlite_db")
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder")
DOCLING = coco.ContextKey[DoclingTools]("docling")

# One model instance per process for indexing, on the GPU if there is one.
# Loads lazily on first use (see search_model()).
DEVICE = cfg.embed_device()
EMBEDDER_INSTANCE = SentenceTransformerEmbedder(cfg.EMBED_MODEL, device=DEVICE)
# On the CPU, searches share the indexer's model. With a GPU they get their own CPU copy
# (~275 MB): CocoIndex then is the only user of the GPU model, one batch at a time
# (MPS isn't thread-safe), and a query never waits behind a batch.
_QUERY_EMBEDDER = (
    EMBEDDER_INSTANCE if DEVICE == "cpu" else SentenceTransformerEmbedder(cfg.EMBED_MODEL, device="cpu")
)


def search_model():
    """The loaded SentenceTransformer for search queries. Also loads the indexer's
    model (process_file relies on that)."""
    with _LOAD_LOCK:
        EMBEDDER_INSTANCE._get_model()
        return _QUERY_EMBEDDER._get_model()


@coco.lifespan
def coco_lifespan(builder: coco.EnvironmentBuilder) -> Iterator[None]:
    db_path = cfg.load_config(check_folders=False).db_path
    # CocoIndex's own change-tracking state lives next to the index it describes.
    state_path = cfg.state_path(db_path)
    if not db_path.exists() and state_path.exists():
        # The index was deleted but the state still claims everything is indexed,
        # which would leave an empty index. Start over.
        log.warning("index %s is missing; resetting %s", db_path, state_path)
        shutil.rmtree(state_path)
    builder.settings.db_path = state_path

    # WAL lets searches read while the indexer writes (persists in the db file).
    with sqlite3.connect(db_path) as setup:
        setup.execute("PRAGMA journal_mode=WAL")

    conn = sqlite.connect(db_path, load_vec=True)
    try:
        builder.provide(SQLITE_DB, conn)
        builder.provide(EMBEDDER, EMBEDDER_INSTANCE)
        builder.provide(DOCLING, DoclingTools())
        fts.ensure_fts(db_path)
        yield
    finally:
        conn.close()
        fts.ensure_fts(db_path)  # catch up on anything written before the triggers existed


# ---------------------------------------------------------------------------
# Target table
# ---------------------------------------------------------------------------


@dataclass
class ChunkRow:
    # Primary key: (folder, path, chunk_index)
    folder: str
    path: str  # relative to folder
    file_type: str
    chunk_index: int
    headings: str  # heading path, " > "-joined ("" for code / plain text)
    page: int  # 0 if not applicable
    text: str  # raw chunk text
    embedding: Annotated[NDArray, EMBEDDER]


@dataclass
class IssueRow:
    """A file that was skipped (too large, binary, can't be parsed).
    Primary key: (folder, path). Disappears when the file is fixed or deleted."""

    folder: str
    path: str
    reason: str


# Files being processed right now: absolute path -> start time (for the status page).
ACTIVE_FILES: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_splitter = RecursiveSplitter()


_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16: its BOM starts the same way
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# docling formats that are plain text underneath: decoded here, so files in older
# encodings work too (docling expects UTF-8).
_TEXT_DOCLING_EXTS = {"md", "markdown", "adoc", "asciidoc", "tex", "latex"}


def _decode_text(data: bytes) -> str | None:
    """Text of a plain-text file, or None if it looks binary."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Not UTF-8: most likely an older Windows/Mac file (common for German text).
    # Windows-1252 covers Latin-1's printable characters plus €, quotes, dashes.
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _split_plain_text(text: str, filename: str) -> list[DocChunk]:
    language = detect_code_language(filename=filename)
    chunks = _splitter.split(
        text,
        chunk_size=1200,
        min_chunk_size=300,
        chunk_overlap=200,
        language=language,
    )
    return [DocChunk(text=c.text, context=c.text, headings=[], page=0) for c in chunks]


def _convert_html(
    docling: DoclingTools, name: str, data: bytes, extension_names: tuple[str, ...]
) -> list[DocChunk]:
    """HTML through docling, made robust for real-world pages: extensions and layout-table
    unwrapping first (html_prep), then any text docling still dropped is indexed as
    plain text under its heading path instead of disappearing."""
    try:
        prepared, root = html_prep.prepare_html(data, extensions.load(extension_names))
    except (lxml.etree.LxmlError, ValueError) as e:
        log.warning("cannot pre-process %s (%s); passing it to docling unchanged", name, e)
        return docling.convert_and_chunk(name, data)
    chunks = docling.convert_and_chunk(name, prepared)
    dropped = html_prep.dropped_text(root, [c.context for c in chunks])
    if dropped:
        words = sum(len(text.split()) for _, text in dropped)
        log.warning("docling dropped %d words of %s; indexing them as plain text", words, name)
    for heads, text in dropped:
        for piece in _splitter.split(
            text, chunk_size=1200, min_chunk_size=300, chunk_overlap=200, language=None
        ):
            chunks.append(
                DocChunk(
                    text=piece.text,
                    context="\n".join([*heads, piece.text]),
                    headings=list(heads),
                    page=0,
                )
            )
    return chunks


def _embedding_input(rel_path: str, chunk: DocChunk) -> str:
    # The file path is context too: it often says what the content is about.
    return f"{cfg.PASSAGE_PREFIX}{rel_path}\n{chunk.context}"


def _fit_to_model(chunk: DocChunk, rel_path: str, language: str | None) -> list[DocChunk]:
    """Split a chunk until path + headings + text fit the model's token limit,
    so nothing gets silently truncated (dense code easily exceeds it)."""
    if len(chunk.headings) > 1 and _count_tokens(
        _embedding_input(rel_path, DocChunk("", "\n".join(chunk.headings), [], 0))
    ) > cfg.MODEL_MAX_TOKENS // 2:
        # Extremely long heading path: keep only the innermost heading as context.
        chunk = DocChunk(
            text=chunk.text,
            context="\n".join([chunk.headings[-1], chunk.text]),
            headings=chunk.headings,
            page=chunk.page,
        )
    if (
        len(chunk.text) < 50
        or _count_tokens(_embedding_input(rel_path, chunk)) <= cfg.MODEL_MAX_TOKENS
    ):
        return [chunk]
    size = len(chunk.text) // 2
    pieces = [
        c.text
        for c in _splitter.split(
            chunk.text, chunk_size=size, min_chunk_size=size // 2, language=language
        )
    ]
    if len(pieces) < 2:  # no natural split point: cut in the middle
        pieces = [chunk.text[:size], chunk.text[size:]]
    # Context = heading lines followed by the text; keep the headings for each piece.
    if chunk.context.endswith(chunk.text):
        heading_context = chunk.context[: len(chunk.context) - len(chunk.text)]
    else:
        heading_context = "".join(f"{h}\n" for h in chunk.headings)
    fitted: list[DocChunk] = []
    for piece in pieces:
        sub = DocChunk(
            text=piece,
            context=heading_context + piece,
            headings=chunk.headings,
            page=chunk.page,
        )
        fitted.extend(_fit_to_model(sub, rel_path, language))
    return fitted


def _fit_all(chunks: list[DocChunk], rel_path: str, language: str | None) -> list[DocChunk]:
    return [
        fitted
        for c in chunks
        if c.text.strip()
        for fitted in _fit_to_model(c, rel_path, language)
        if fitted.text.strip()
    ]


def _is_parse_error(error: Exception) -> bool:
    """Errors that mean "this file can't be parsed" (skip it and remember that).
    Anything else (out of memory, I/O, bugs) is re-raised: CocoIndex then keeps
    the file's previous rows and retries on the next run."""
    with _LOAD_LOCK:
        from docling.exceptions import ConversionError

    return isinstance(error, (ConversionError, zipfile.BadZipFile, UnicodeDecodeError))


async def _process_file_body(
    file: FileLike,
    folder: str,
    abs_path: pathlib.PurePath,
    rel_path: str,
    ext: str,
    extension_names: tuple[str, ...],
    ocr: bool,
    table: sqlite.TableTarget[ChunkRow],
    skip,
) -> None:
    try:
        if ext in html_prep.HTML_EXTS:
            data = await file.read()
            name = abs_path.with_suffix("." + cfg.DOCLING_ALIASES.get(ext, ext)).name
            chunks = await asyncio.to_thread(
                _convert_html, coco.use_context(DOCLING), name, data, extension_names
            )
        elif ext in cfg.DOCLING_EXTS:
            data = await file.read()
            if ext in _TEXT_DOCLING_EXTS:
                text = _decode_text(data)
                if text is None:
                    skip("binary file")
                    return
                data = text.encode("utf-8")
            # docling picks the parser from the file name's extension.
            name = abs_path.with_suffix("." + cfg.DOCLING_ALIASES.get(ext, ext)).name
            chunks = await asyncio.to_thread(
                coco.use_context(DOCLING).convert_and_chunk, name, data, ocr
            )
        else:
            text = _decode_text(await file.read())
            if text is None:
                skip("binary file")
                return
            # CPU-bound work runs in a thread: the web server shares this event loop.
            chunks = await asyncio.to_thread(_split_plain_text, text, abs_path.name)
    except Exception as e:
        if not _is_parse_error(e):
            raise
        log.warning("cannot parse %s: %s", abs_path, e)
        skip(f"cannot parse: {e}")
        return

    if not chunks:
        skip("no text found")
        return

    language = None if ext in cfg.DOCLING_EXTS else detect_code_language(filename=abs_path.name)
    kept = await asyncio.to_thread(_fit_all, chunks, rel_path, language)
    if not kept:  # e.g. only whitespace
        skip("no text found")
        return

    embedder = coco.use_context(EMBEDDER)
    # The embedder loads its model lazily on first use. Do that under the load lock:
    # importing transformers while docling loads in another thread breaks imports.
    await asyncio.to_thread(search_model)
    # Concurrent calls get batched by the embedder.
    vectors = await asyncio.gather(*(embedder.embed(_embedding_input(rel_path, c)) for c in kept))

    for i, (chunk, vector) in enumerate(zip(kept, vectors)):
        table.declare_row(
            row=ChunkRow(
                folder=folder,
                path=rel_path,
                file_type=ext or abs_path.name,
                chunk_index=i,
                headings=" > ".join(chunk.headings),
                page=chunk.page,
                text=chunk.text,
                embedding=vector,
            )
        )


# CocoIndex re-runs process_file when its own code changes, but it can't see into
# the plain helpers it calls. Fingerprint them plus the settings they depend on,
# so any change to chunking or embedding input reprocesses all files.
# (Embeddings are memoized by text, so unchanged chunks aren't re-embedded.)
PIPELINE_FINGERPRINT = hashlib.sha256(
    "\n".join(
        [
            *(
                inspect.getsource(obj)
                for obj in (
                    _load_tokenizer,
                    _count_tokens,
                    DoclingTools,
                    _decode_text,
                    _split_plain_text,
                    _convert_html,
                    _embedding_input,
                    _fit_to_model,
                    _fit_all,
                    _is_parse_error,
                    _process_file_body,
                    html_prep,
                    extensions,
                    *extensions.load(extensions.available()),
                )
            ),
            repr(
                (
                    cfg.EMBED_MODEL,
                    cfg.PASSAGE_PREFIX,
                    cfg.MODEL_MAX_TOKENS,
                    cfg.MAX_TOKENS,
                    sorted(cfg.DOCLING_EXTS),
                    sorted(cfg.DOCLING_ALIASES.items()),
                )
            ),
        ]
    ).encode()
).hexdigest()


def _issue_writer(issues, folder: str, rel_path: str, abs_path: pathlib.PurePath):
    def skip(reason: str) -> None:
        log.info("skip (%s): %s", reason, abs_path)
        issues.declare_row(row=IssueRow(folder=folder, path=rel_path, reason=reason))

    return skip


def _file_stat_key(file: FileLike) -> tuple:
    """Cache key for process_file: path, modification time and size, from one stat()
    call. Unlike the default for files, this never reads the content."""
    path = file.file_path.path
    try:
        st = os.stat(path)
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_mtime_ns, st.st_size)


@coco.fn(memo=True, memo_key={"file": _file_stat_key}, deps=PIPELINE_FINGERPRINT)
async def process_file(
    file: FileLike,
    folder: str,
    max_file_bytes: int,
    extension_names: tuple[str, ...],
    ocr: bool,
    table: sqlite.TableTarget[ChunkRow],
    issues: sqlite.TableTarget[IssueRow],
) -> None:
    abs_path = pathlib.PurePath(file.file_path.path)
    rel_path = abs_path.relative_to(folder).as_posix()
    # Size check first, before _index_file: to decide whether its cache is still
    # valid, CocoIndex reads the whole file whenever its modification time changed,
    # which would load a huge growing log file into memory every time. This
    # function itself is cached by stat() only (see _file_stat_key).
    if await file.size() > max_file_bytes:
        skip = _issue_writer(issues, folder, rel_path, abs_path)
        skip(f"larger than {max_file_bytes // (1024 * 1024)} MB")
        return
    # OCR only affects PDFs: turning it on or off reprocesses only those.
    ocr = ocr and abs_path.suffix.lower() == ".pdf"
    await _index_file(file, folder, extension_names, ocr, table, issues)


@coco.fn(memo=True, deps=PIPELINE_FINGERPRINT)
async def _index_file(
    file: FileLike,
    folder: str,
    extension_names: tuple[str, ...],
    ocr: bool,
    table: sqlite.TableTarget[ChunkRow],
    issues: sqlite.TableTarget[IssueRow],
) -> None:
    abs_path = pathlib.PurePath(file.file_path.path)
    rel_path = abs_path.relative_to(folder).as_posix()
    ext = abs_path.suffix.lower().lstrip(".")
    skip = _issue_writer(issues, folder, rel_path, abs_path)

    ACTIVE_FILES[str(abs_path)] = time.time()
    try:
        await _process_file_body(
            file, folder, abs_path, rel_path, ext, extension_names, ocr, table, skip
        )
    finally:
        ACTIVE_FILES.pop(str(abs_path), None)


def _walk_folder(folder: str, exclude: tuple[str, ...], *, live: bool):
    return localfs.walk_dir(
        pathlib.Path(folder),
        recursive=True,
        live=live,
        path_matcher=PatternFilePathMatcher(
            included_patterns=cfg.included_patterns(),
            excluded_patterns=cfg.excluded_patterns(exclude),
        ),
    )


def count_files(config: cfg.Config) -> int:
    """How many files indexing will look at (for the progress display). Uses the
    same walker and rules as indexing; only lists directories, reads no files."""
    return sum(
        sum(1 for _ in _walk_folder(folder, config.exclude, live=False)._walk_sync())
        for folder in config.folders
    )


def list_files(config: cfg.Config) -> set[tuple[str, str]]:
    """(folder, relative path) of every file indexing will look at, as stored in
    the index. Same walker and rules as count_files."""
    return {
        (folder, pathlib.PurePath(f.file_path.path).relative_to(folder).as_posix())
        for folder in config.folders
        for f in _walk_folder(folder, config.exclude, live=False)._walk_sync()
    }


@coco.fn
async def index_folder(
    folder: str,
    exclude: tuple[str, ...],
    max_file_bytes: int,
    extension_names: tuple[str, ...],
    ocr: bool,
    table: sqlite.TableTarget[ChunkRow],
    issues: sqlite.TableTarget[IssueRow],
) -> None:
    # live: keep watching the folder for changes
    files = _walk_folder(folder, exclude, live=True)
    await coco.mount_each(
        process_file, files.items(), folder, max_file_bytes, extension_names, ocr, table, issues
    )


@coco.fn
async def app_main() -> None:
    # Read the folder list on every run, so one app object serves the whole
    # process (the web server restarts it after folder changes).
    config = cfg.load_config(check_folders=False)
    # Refuse to run rather than treat a missing/unreadable folder as empty,
    # which would delete its whole index.
    for folder in config.folders:
        if problem := cfg.folder_problem(pathlib.Path(folder)):
            raise RuntimeError(problem)

    table = await sqlite.mount_table_target(
        SQLITE_DB,
        table_name=cfg.TABLE_NAME,
        table_schema=await sqlite.TableSchema.from_class(
            ChunkRow, primary_key=["folder", "path", "chunk_index"]
        ),
    )
    # Full-text index + sync triggers, before any rows get written in this run.
    await asyncio.to_thread(fts.ensure_fts, config.db_path)

    issues = await sqlite.mount_table_target(
        SQLITE_DB,
        table_name=cfg.ISSUES_TABLE_NAME,
        table_schema=await sqlite.TableSchema.from_class(
            IssueRow, primary_key=["folder", "path"]
        ),
    )
    # One component per folder, keyed by its absolute path: removing a folder
    # from folders.toml removes exactly its rows.
    for folder in config.folders:
        await coco.mount(
            coco.component_subpath("folder", folder),
            index_folder,
            folder,
            config.exclude,
            config.max_file_bytes,
            config.extensions,
            config.ocr,
            table,
            issues,
        )


# The one CocoIndex app of this process (an app name can only be registered once).
APP = coco.App(
    # Old project name kept on purpose: CocoIndex keys its change-tracking state by it,
    # so renaming it would re-index every file.
    coco.AppConfig(name="LocalSearch", max_inflight_components=cfg.MAX_PARALLEL_FILES),
    app_main,
)

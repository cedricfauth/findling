"""Findling web app: search box, folder picker and live indexing status.

    findling            then open http://127.0.0.1:8765

The server answers immediately; the embedding model loads in the background
(search works once it's loaded), then live indexing starts.
"""

from __future__ import annotations

import asyncio
import codecs
import functools
import hmac
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as cfg
from . import mcp_server
from . import paths
from . import rag
from . import reading
from .paths import AgentError

log = logging.getLogger("findling.server")

HOST = "127.0.0.1"  # never reachable from other machines
PORT = int(os.environ.get("FINDLING_PORT") or "8765")
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
STATIC_DIR = pathlib.Path(__file__).parent / "static"
HTML_EXTS = {"html", "htm", "xhtml"}
VERSION = importlib.metadata.version("findling")


class State:
    """Filled in by the background startup task."""

    model_ready = False
    model_error: str | None = None
    model_loaded_at: float | None = None
    started_at = time.time()
    pipeline = None  # module, imported in the background (slow: torch, cocoindex)
    searcher = None
    indexer = None


state = State()


async def _background_startup() -> None:
    def load():
        from . import pipeline
        from . import searcher

        pipeline.search_model()  # load the e5 model once; indexing shares it
        return pipeline, searcher

    try:
        state.pipeline, state.searcher = await asyncio.to_thread(load)
        state.model_ready = True
        state.model_loaded_at = time.time()
        log.info("model ready after %.1fs", state.model_loaded_at - state.started_at)
    except Exception as e:
        log.exception("failed to load the embedding model")
        state.model_error = str(e)
        return

    from . import indexer

    state.indexer = indexer.Indexer()
    await state.indexer.restart()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with _mcp.session_manager.run():
        startup = asyncio.create_task(_background_startup())
        yield
        startup.cancel()
        if state.indexer is not None:
            await state.indexer.stop()


app = FastAPI(title="Findling", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Host check blocks DNS-rebinding (a website pointing its domain at 127.0.0.1).
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.url.path == "/mcp" and request.method != "POST":
        return JSONResponse({"detail": "Method not allowed"}, status_code=405, headers={"Allow": "POST"})
    if request.method not in ("GET", "HEAD"):
        # Other websites must not be able to add folders or open files. Requiring
        # JSON forces a CORS preflight, which this server never approves.
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "JSON required"}, status_code=415)
    return await call_next(request)


@app.exception_handler(cfg.ConfigError)
async def config_error(_request: Request, exc: cfg.ConfigError) -> JSONResponse:
    # A broken folders.toml (e.g. edited by hand) must not take the server down.
    return JSONResponse({"detail": str(exc)}, status_code=409)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/view")
async def view_page() -> FileResponse:
    """Text and code viewer: /view?folder=…&path=…&chunk=N&q=…"""
    return FileResponse(STATIC_DIR / "view.html", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _folder_stats(db_path: pathlib.Path) -> tuple[dict, dict, list]:
    """Per-folder file/chunk counts and skipped files, straight from the index."""
    if not db_path.exists():
        return {}, {}, []
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        counts = {
            folder: (files, chunks)
            for folder, files, chunks in db.execute(
                f"SELECT folder, COUNT(DISTINCT path), COUNT(*) FROM {cfg.TABLE_NAME} GROUP BY folder"
            )
        }
        issue_rows = db.execute(
            f"SELECT folder, path, reason FROM {cfg.ISSUES_TABLE_NAME} ORDER BY folder, path"
        ).fetchall()
    except sqlite3.OperationalError:  # tables not created yet
        return {}, {}, []
    finally:
        db.close()
    issue_counts: dict[str, int] = {}
    for folder, _, _ in issue_rows:
        issue_counts[folder] = issue_counts.get(folder, 0) + 1
    issues = [{"folder": f, "path": p, "reason": r} for f, p, r in issue_rows[:200]]
    return counts, issue_counts, issues


@app.get("/api/status")
async def status() -> dict:
    try:
        config = cfg.load_config(check_folders=False)
    except cfg.ConfigError as e:
        return {
            "version": VERSION,
            "pid": os.getpid(),  # so findling-mcp can restart an outdated server
            "model_ready": state.model_ready,
            "uptime": time.time() - state.started_at,
            "indexing": {"phase": "error", "message": str(e)},
            "folders": [],
            "issues": [],
        }
    counts, issue_counts, issues = await asyncio.to_thread(_folder_stats, config.db_path)
    folders = []
    for folder in config.folders:
        problem = cfg.folder_problem(pathlib.Path(folder))
        files, chunks = counts.get(folder, (0, 0))
        folders.append(
            {
                "path": folder,
                "name": pathlib.Path(folder).name or folder,
                "problem": problem,
                "files": files,
                "chunks": chunks,
                "skipped": issue_counts.get(folder, 0),
            }
        )
    if state.model_error:
        indexing = {"phase": "error", "message": f"Could not load the model: {state.model_error}"}
    elif state.indexer is None:
        indexing = {"phase": "loading_model", "message": ""}
    else:
        indexing = state.indexer.status()
    return {
        "version": VERSION,
        "pid": os.getpid(),
        "model_ready": state.model_ready,
        "uptime": time.time() - state.started_at,
        "indexing": indexing,
        "folders": folders,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


_fulltext_rebuild: asyncio.Task | None = None
T = TypeVar("T")


def _rebuild_fulltext_soon(db_path: pathlib.Path) -> None:
    """Rebuild the full-text index in the background (once at a time)."""
    global _fulltext_rebuild
    if _fulltext_rebuild is not None and not _fulltext_rebuild.done():
        return
    from . import fts

    log.warning("full-text index is out of date; rebuilding")
    _fulltext_rebuild = asyncio.create_task(asyncio.to_thread(fts.ensure_fts, db_path))


# Searches get their own threads: slow ones must never hold up the threads that the
# status panel and the indexer use.
_SEARCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search")


class SearchCancelled(Exception):
    pass


class SearchUnavailable(Exception):
    """The search model isn't loaded (yet)."""


async def run_search(
    q: str,
    k: int,
    folder: str | None,
    finish: Callable[[Any, sqlite3.Connection, float], T],
    cancelled: threading.Event | None = None,
    scopes: list | None = None,
) -> T | None:
    """Run a search on the search threads. `finish(response, db, seconds)` turns the
    searcher's response into the caller's result, on the same thread and connection.
    None if there's no index yet. Setting `cancelled` (or cancelling the awaiting task)
    stops the search: raises SearchCancelled. `scopes` (searcher.Scope) limit the
    search to parts of the folders, instead of `folder`."""
    if not state.model_ready:
        if state.model_error:
            raise SearchUnavailable(f"Could not load the search model: {state.model_error}")
        raise SearchUnavailable("The search model is still loading. Try again in a few seconds.")
    config = cfg.load_config(check_folders=False)
    cancelled = cancelled or threading.Event()

    def run():
        if cancelled.is_set():  # waited in the queue and the caller gave up meanwhile
            raise SearchCancelled
        db = state.searcher.open_db(config.db_path)
        if db is None:
            return None, False
        # Aborts the running SQL statement (any of them) once the search is cancelled.
        db.set_progress_handler(lambda: 1 if cancelled.is_set() else 0, 10_000)
        try:
            started = time.perf_counter()
            vec = state.searcher.embed_query(state.pipeline.search_model(), q)
            if cancelled.is_set():
                raise SearchCancelled
            with state.searcher.snapshot(db):  # finish() reads the same version of the index
                response = state.searcher.search(
                    db, q, vec, folders=list(config.folders), k=k, folder=folder or None, scopes=scopes
                )
                result = finish(response, db, time.perf_counter() - started)
            return result, response.fulltext_stale
        except sqlite3.OperationalError:
            if cancelled.is_set():
                raise SearchCancelled from None
            raise
        finally:
            db.close()

    try:
        result, stale = await asyncio.get_running_loop().run_in_executor(_SEARCH_POOL, run)
    finally:
        cancelled.set()  # e.g. the caller was cancelled (client gone, shutdown)
    if stale:
        _rebuild_fulltext_soon(config.db_path)
    return result


@app.get("/api/search", response_model=None)
async def search(
    request: Request, q: str, k: int = 10, folder: str | None = None, debug: bool = False
) -> dict | Response:
    q = q.strip()
    if not q:  # (the page only searches 1-2 characters on Enter)
        return {"query": q, "results": [], "weak": False}
    k = max(1, min(k, 50))
    cancelled = threading.Event()

    # The page aborts a search when the query changes (live search while typing) or
    # the tab closes. Stop the work then too, instead of finishing a result nobody reads.
    async def watch_for_disconnect():
        # (request.is_disconnected() never sees it behind the "http" middleware)
        while (await request.receive())["type"] != "http.disconnect":
            pass
        cancelled.set()

    watcher = asyncio.create_task(watch_for_disconnect())
    try:
        found = await run_search(q, k, folder, lambda r, _db, s: (r, s), cancelled)
    except SearchUnavailable as e:
        raise HTTPException(503, str(e)) from None
    except SearchCancelled:
        log.debug("search cancelled: %r", q)
        return Response(status_code=499)  # client closed the request
    finally:
        watcher.cancel()
    if found is None:
        return {"query": q, "results": [], "weak": False}
    response, seconds = found
    out = {
        "query": q,
        "terms": response.query.needles,
        "seconds": round(seconds, 3),
        "weak": response.weak,
        "results": [
            {
                "folder": r.folder,
                "path": r.path,
                "full_path": r.full_path,
                "name": pathlib.PurePath(r.path).name,
                "file_type": r.file_type,
                "score": round(r.score, 5),
                "word_match": r.word_match,
                "path_highlights": r.path_highlights,
                "matches": [
                    {
                        "chunk_index": m.chunk_index,
                        "headings": m.headings,
                        "heading_highlights": m.heading_highlights,
                        "page": m.page,
                        "text": m.text,
                        "snippet": list(m.snippet),
                        "highlights": m.highlights,
                        "similarity": round(m.similarity, 4),
                        # Shows the passage highlighted; None for Office files.
                        "view_url": _show_url(r.folder, r.path, m.chunk_index, q),
                        **(
                            {"vec_rank": m.vec_rank, "fts_rank": m.bm25_rank}
                            if debug else {}
                        ),
                    }
                    for m in r.matches
                ],
                **({"debug": r.debug} if debug else {}),
            }
            for r in response.results
        ],
    }
    if debug:
        out["debug"] = response.debug
    return out


# ---------------------------------------------------------------------------
# MCP: the same search for AI agents, at /mcp (Streamable HTTP)
# ---------------------------------------------------------------------------


def _indexing_note() -> str | None:
    """Why results may be incomplete right now, if they may be."""
    if state.indexer is None:
        return "Indexing hasn't started yet: recently changed files may be missing."
    if state.indexer.status()["phase"] in ("starting", "catching_up"):
        return "Indexing is still catching up: some files may be missing or out of date."
    return None


def _link(folder: str, path: str):
    """chunk -> link showing it highlighted (absolute, for agents)."""
    return lambda chunk: _show_url(folder, path, chunk, "", absolute=True)


def _page(items: list, offset: int, limit: int) -> tuple[list, int | None]:
    """items[offset:offset+limit] and the next offset (None at the end)."""
    end = offset + limit
    return items[offset:end], end if end < len(items) else None


def _resolve(config, name: str) -> tuple[str, str]:
    """(indexed folder, path inside it) for a full path from an agent."""
    db = reading.open_index(config.db_path)
    try:
        return paths.resolve(db, config.folders, name)
    finally:
        db.close()


class _McpBackend:
    async def search(
        self, query: str, within: str | None, limit: int, offset: int, max_chars: int
    ) -> rag.SearchResult:
        if not query:
            raise AgentError("The query is empty.")
        config = cfg.load_config(check_folders=False)
        scopes = None
        if within and within.strip():
            scopes = [paths.scope(*await asyncio.to_thread(_resolve, config, within))]

        def finish(response, _db, _seconds):
            return rag.build(
                response,
                offset=offset,
                max_chars=max_chars,
                file_name=lambda r: paths.full(r.folder, r.path),
                view_link=lambda r, chunk: _show_url(r.folder, r.path, chunk, "", absolute=True),
            )

        try:
            result = await run_search(query, offset + limit, None, finish, scopes=scopes)
        except SearchUnavailable as e:
            raise AgentError(str(e)) from None
        if result is None:
            return rag.empty(query, "Nothing is indexed yet. Add folders in Findling's web app.")
        if note := _indexing_note():
            result.note = f"{result.note} {note}" if result.note else note
        return result

    async def read(
        self, file: str, at: int | str | None, section: str | None, before: int, after: int
    ) -> reading.ReadResult:
        config = cfg.load_config(check_folders=False)
        if section is not None and at is not None:
            raise AgentError("Pass `at` or `section`, not both.")

        def run() -> reading.ReadResult:
            db = reading.open_index(config.db_path)
            try:
                folder, rel = paths.resolve(db, config.folders, file)
                chunks = reading.load_chunks(db, folder, rel) if rel else []
            finally:
                db.close()
            if not chunks:
                raise AgentError(f"{file!r} is a folder, not a file. `browse` lists its files.")
            doc = reading.Doc(chunks)
            stop = None
            if section is not None:
                first, stop, _ = reading.find_section(doc, section)
                start = (first, 0)
            elif at is not None:
                start = doc.parse(at)
            else:
                start = (0, 0)
            link = _link(folder, rel)
            text, first_pos, end_pos = reading.read(doc, start, before=before, after=after, stop=stop, link=link)
            chunk = doc.chunks[start[0]]
            return reading.ReadResult(
                file=paths.full(folder, rel),
                type=_file_type(rel),
                modified=reading.modified(os.path.join(folder, rel))[0],
                chunks=len(doc),
                section=chunk.headings or None,
                page=chunk.page or None,
                url=link(chunk.index),
                text=text,
                prev=None if first_pos == (0, 0) else doc.cursor(*first_pos),
                next=doc.cursor(*end_pos) if end_pos[0] < len(doc) else None,
            )

        return await asyncio.to_thread(run)

    async def browse(
        self, path: str | None, filter_: str | None, sort: reading.Sort, offset: int, limit: int
    ) -> reading.BrowseResult:
        config = cfg.load_config(check_folders=False)
        if path is None or not path.strip():
            info = await status()
            return reading.BrowseResult(
                folders=[
                    reading.FolderEntry(
                        path=f["path"],
                        files=f["files"],
                        available=f["problem"] is None,
                        problem=f["problem"],
                    )
                    for f in info["folders"]
                ],
                indexing=info["indexing"]["phase"],
                search_ready=info["model_ready"],
            )

        def run() -> reading.BrowseResult:
            db = reading.open_index(config.db_path)
            try:
                folder, rel = paths.resolve(db, config.folders, path)
                chunks = reading.load_chunks(db, folder, rel) if rel else []
                if chunks:  # a file: its sections
                    doc = reading.Doc(chunks)
                    found_sections = reading.sections(doc, filter_)
                    shown, next_offset = _page(found_sections, offset, limit)
                    entry = reading.FileEntry(
                        file=paths.full(folder, rel),
                        type=_file_type(rel),
                        modified=reading.modified(os.path.join(folder, rel))[0],
                        chunks=len(doc),
                    )
                    return reading.BrowseResult(
                        file=entry, sections=shown, total=len(found_sections), next_offset=next_offset
                    )
                rows = reading.indexed_files(db, [paths.scope(folder, rel)])
                dirs, files = reading.list_dir(rows, folder, rel, sort, filter_)
                entries, next_offset = _page([*dirs, *files], offset, limit)
                return reading.BrowseResult(
                    path=paths.full(folder, rel),
                    dirs=[e for e in entries if isinstance(e, reading.DirEntry)],
                    files=[e for e in entries if isinstance(e, reading.FileEntry)],
                    total=len(dirs) + len(files),
                    next_offset=next_offset,
                )
            finally:
                db.close()

        return await asyncio.to_thread(run)

    async def open(self, file: str, chunk: int | None, query: str) -> mcp_server.Opened:
        folder, rel = await asyncio.to_thread(_resolve, cfg.load_config(check_folders=False), file)
        try:
            target = _indexed_file(folder, rel)
        except HTTPException as e:
            raise AgentError(f"{file}: {e.detail}") from None
        if not target.is_file():
            raise AgentError(f"{file} isn't a file.")
        url = _show_url(folder, rel, chunk, query, absolute=True)
        # Only Office files open in their app: other indexed types include scripts
        # (.sh, .bat, ...) that `open` would run.
        await _open(url or str(target))
        return mcp_server.Opened(file=paths.full(folder, rel), opened_in="browser" if url else "app", url=url)


async def _open(target: str) -> None:
    """macOS `open`: URLs in the default browser, files in their app."""
    proc = await asyncio.create_subprocess_exec("open", target, stderr=subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AgentError(stderr.decode().strip() or f"Could not open {target}.")


# Stateless with plain JSON responses: clients only POST. local_only() turns away the
# rest (a GET would open an event stream that is never used). The SDK checks Host and
# Origin too.
_mcp = mcp_server.create_server(_McpBackend())
app.router.routes.extend(
    _mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host=HOST
    ).routes
)


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


class FolderBody(BaseModel):
    path: str


@app.post("/api/folders/pick")
async def pick_folder() -> dict:
    """Open the native macOS "Choose Folder" dialog on this Mac."""
    script = [
        "-e", "activate",
        "-e", 'POSIX path of (choose folder with prompt "Choose a folder to index")',
    ]
    proc = await asyncio.create_subprocess_exec(
        "osascript", *script, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        if b"-128" in stderr or b"User canceled" in stderr:
            return {"cancelled": True}
        raise HTTPException(500, f"Folder dialog failed: {stderr.decode().strip()}")
    return {"path": stdout.decode().strip().rstrip("/") or "/"}


def _require_indexer():
    if state.indexer is None:
        raise HTTPException(503, "Still starting up. Try again in a few seconds.")
    return state.indexer


@app.post("/api/folders")
async def add_folder(body: FolderBody) -> dict:
    indexer = _require_indexer()
    folder = pathlib.Path(body.path.strip()).expanduser()
    if not folder.is_absolute():
        raise HTTPException(400, "Please give a full path, e.g. /Users/you/Documents.")
    folder = folder.resolve()
    if problem := cfg.folder_problem(folder):
        raise HTTPException(400, problem)
    folders = list(cfg.load_config(check_folders=False).folders)
    if str(folder) in folders:
        raise HTTPException(409, f"{folder} is already indexed.")
    if problem := cfg.nesting_problem([*folders, str(folder)]):
        raise HTTPException(400, problem)
    cfg.save_folders([*folders, str(folder)])
    await indexer.restart()
    return {"ok": True, "path": str(folder)}


@app.post("/api/folders/remove")
async def remove_folder(body: FolderBody) -> dict:
    indexer = _require_indexer()
    folders = list(cfg.load_config(check_folders=False).folders)
    if body.path not in folders:
        raise HTTPException(404, "That folder isn't indexed.")
    folders.remove(body.path)
    cfg.save_folders(folders)
    await indexer.restart()  # the restarted run deletes the folder's chunks
    return {"ok": True}


@app.post("/api/indexing/restart")
async def restart_indexing() -> dict:
    """E.g. after reconnecting a drive that paused indexing."""
    await _require_indexer().restart()
    return {"ok": True}


@app.post("/api/indexing/pause")
async def pause_indexing() -> dict:
    await _require_indexer().pause()
    return {"ok": True}


@app.post("/api/indexing/resume")
async def resume_indexing() -> dict:
    await _require_indexer().resume()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Opening and viewing files
# ---------------------------------------------------------------------------


def _indexed_file(folder: str, path: str) -> pathlib.Path:
    """`path` inside the indexed `folder`. Only files in indexed folders, nothing else
    on disk."""
    if folder not in cfg.load_config(check_folders=False).folders:
        raise HTTPException(403, "Not an indexed folder.")
    root = pathlib.Path(folder)
    # Check the path as written, without following symlinks: indexing follows them,
    # so a symlinked file inside the folder is a valid result. normpath removes "..",
    # so nothing outside the folder can be named.
    target = pathlib.Path(os.path.normpath(root / path))
    if not target.is_relative_to(root) or target == root or not target.exists():
        raise HTTPException(404, "File not found.")
    return target


@functools.cache
def _url_key() -> bytes:
    """Secret behind the folder ids in file links (/files/<id>/..., /show/<id>/...), so
    other websites can't guess the links and load your files. Kept next to the index
    (findling.urlkey), so links stay valid when Findling restarts; delete the file to
    make all old links stop working."""
    try:
        path = cfg.load_config(check_folders=False).db_path.with_suffix(".urlkey")
        try:
            key = bytes.fromhex(path.read_text().strip())
            if len(key) == 32:
                return key
        except (FileNotFoundError, ValueError):
            pass
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # only you can read it
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
        os.replace(tmp, path)
        return key
    except (OSError, cfg.ConfigError) as e:
        log.warning("Can't keep the link key (%s); file links stop working when Findling restarts.", e)
        return secrets.token_bytes(32)


def _folder_id(folder: str) -> str:
    return hmac.new(_url_key(), folder.encode(), "sha256").hexdigest()[:16]


def _folder_for_id(fid: str, folders) -> str | None:
    return next((f for f in folders if hmac.compare_digest(_folder_id(f), fid)), None)


def _file_url(folder: str, path: str) -> str:
    """URL of the raw file. A path (not a query string), so links, images and styles
    relative to an HTML page resolve to the files next to it."""
    return f"/files/{_folder_id(folder)}/{urllib.parse.quote(path)}"


BASE_URL = f"http://{HOST}:{PORT}"
# Office files only open in their apps; the browser can show everything else.
NO_VIEW_TYPES = {"docx", "pptx", "pptm", "xlsx", "xlsm"}


def _file_type(path: str) -> str:
    return pathlib.PurePath(path).suffix.lower().lstrip(".")


def _show_url(folder: str, path: str, chunk: int | None, q: str, *, absolute: bool = False) -> str | None:
    """The link that shows a passage highlighted in its file, the same for every file
    type: /show/<folder id>/<path>?chunk=N&q=… (show_file picks the viewer). It stays
    valid across restarts. None for Office files, which only open in their apps."""
    if _file_type(path) in NO_VIEW_TYPES:
        return None
    mark = urllib.parse.urlencode({
        **({"chunk": str(chunk)} if chunk is not None else {}),
        **({"q": q} if q else {}),
    })
    url = f"/show/{_folder_id(folder)}/{urllib.parse.quote(path)}" + (f"?{mark}" if mark else "")
    return BASE_URL + url if absolute else url


def _viewer_url(folder: str, path: str, chunk: int | None, page: int | None, q: str) -> str:
    """Where /show/ sends the browser: PDFs to pdf.js (at the chunk's page), HTML
    pages to themselves, text and code to Findling's viewer."""
    mark = {"chunk": "" if chunk is None else str(chunk), "q": q}
    file_type = _file_type(path)
    if file_type == "pdf":
        params = urllib.parse.urlencode({"file": _file_url(folder, path), "folder": folder, "path": path, **mark})
        return f"/static/pdfjs/web/viewer.html?{params}" + (f"#page={page}" if page else "")
    if file_type in HTML_EXTS:
        return f"{_file_url(folder, path)}?{urllib.parse.urlencode(mark)}"
    return f"/view?{urllib.parse.urlencode({'folder': folder, 'path': path, **mark})}"


def _chunk_index(value: str) -> int | None:
    value = value.strip()
    # isdigit() alone also accepts "²"; 9 digits keep it within SQLite's integers.
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else None


def _chunk_info(db_path: pathlib.Path, folder: str, path: str, chunk: int | None, q: str) -> dict:
    """The chunk's text and page, and the query's words to highlight (normalized as
    the search compares them)."""
    from . import searcher  # light; doesn't need the model

    text, page = None, 0
    if chunk is not None and db_path.exists():
        db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            row = db.execute(
                f"SELECT text, page FROM {cfg.TABLE_NAME} WHERE folder = ? AND path = ? AND chunk_index = ?",
                (folder, path, chunk),
            ).fetchone()
        except sqlite3.OperationalError:  # table not created yet
            row = None
        finally:
            db.close()
        if row:
            text, page = row
    needles = searcher.parse_query(q).needles if q.strip() else []
    return {"text": text, "page": page or 0, "needles": needles}


@app.get("/api/chunk")
async def chunk_info(folder: str, path: str, chunk: str = "", q: str = "") -> dict:
    """What the viewers highlight: the result's chunk and the query words."""
    _indexed_file(folder, path)  # same rule as opening the file
    db_path = cfg.load_config(check_folders=False).db_path
    return await asyncio.to_thread(_chunk_info, db_path, folder, path, _chunk_index(chunk), q)


@app.get("/api/text")
async def file_text(folder: str, path: str) -> dict:
    """A text or code file's content, decoded the way the indexer reads it."""
    target = _indexed_file(folder, path)
    if not target.is_file():
        raise HTTPException(404, "File not found.")
    if state.pipeline is None:
        raise HTTPException(503, "Still starting up. Try again in a few seconds.")
    data = await asyncio.to_thread(target.read_bytes)
    text = state.pipeline._decode_text(data)
    if text is None:
        raise HTTPException(415, "This file isn't text.")
    return {"name": target.name, "full_path": str(target), "text": text}


_BODY_END = re.compile(rb"</body\s*>", re.IGNORECASE)


def _with_highlighter(page: bytes, info: dict) -> bytes:
    """The HTML page plus static/highlight.js and the data it needs, before </body>."""
    if page.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return page  # can't add ASCII markup to UTF-16
    data = json.dumps({"chunk": info["text"], "needles": info["needles"]}, ensure_ascii=True)
    script = (
        '<script type="application/json" id="findling-data">'
        + data.replace("<", "\\u003c")
        + "</script><script>"
        + (STATIC_DIR / "highlight.js").read_text("utf-8")
        + "</script>"
    )
    # ASCII only, so it fits into the page whatever its encoding (UTF-8, Windows-1252, ...).
    insert = script.encode("ascii", "backslashreplace")
    ends = list(_BODY_END.finditer(page))
    at = ends[-1].start() if ends else len(page)
    return page[:at] + insert + page[at:]


@app.get("/files/{fid}/{path:path}")
async def raw_file(fid: str, path: str, chunk: str = "", q: str = "") -> Response:
    """A file from an indexed folder, for the browser (PDFs via static/pdfjs). HTML
    pages with ?chunk=N&q=… get the result highlighted."""
    config = cfg.load_config(check_folders=False)
    folder = _folder_for_id(fid, config.folders)
    if folder is None:
        raise HTTPException(404, "File not found. (Is its folder still indexed?)")
    target = _indexed_file(folder, path)
    if not target.is_file():
        raise HTTPException(404, "File not found.")
    headers = {
        # The page runs in a unique origin: its scripts can't use Findling's API.
        "Content-Security-Policy": "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
    if target.suffix.lower().lstrip(".") not in HTML_EXTS:
        return FileResponse(target, headers=headers)
    page = await asyncio.to_thread(target.read_bytes)
    index = _chunk_index(chunk)
    if index is not None or q.strip():
        info = await asyncio.to_thread(_chunk_info, config.db_path, folder, path, index, q)
        page = _with_highlighter(page, info)
    # No charset: the page's own <meta charset> decides (many are Windows-1252).
    return Response(page, headers={**headers, "Content-Type": "text/html"})


@app.get("/show/{fid}/{path:path}")
async def show_file(fid: str, path: str, chunk: str = "", q: str = "") -> RedirectResponse:
    """A passage highlighted in its file: sends the browser to the right viewer. The
    links search results carry (web page and MCP) all have this one form."""
    config = cfg.load_config(check_folders=False)
    folder = _folder_for_id(fid, config.folders)
    if folder is None:
        raise HTTPException(404, "File not found. (Is its folder still indexed?)")
    if not _indexed_file(folder, path).is_file():
        raise HTTPException(404, "File not found.")
    if _file_type(path) in NO_VIEW_TYPES:
        raise HTTPException(415, "Office files can't be shown in the browser. Open them from Findling's results.")
    index = _chunk_index(chunk)
    page = None
    if index is not None and _file_type(path) == "pdf":
        info = await asyncio.to_thread(_chunk_info, config.db_path, folder, path, index, "")
        page = info["page"] or None
    return RedirectResponse(_viewer_url(folder, path, index, page, q), status_code=302)


class OpenBody(BaseModel):
    folder: str
    path: str
    reveal: bool = False  # True: show in Finder instead of opening


@app.post("/api/open")
async def open_file(body: OpenBody) -> dict:
    target = _indexed_file(body.folder, body.path)
    args = ["open", "-R", str(target)] if body.reveal else ["open", str(target)]
    proc = await asyncio.create_subprocess_exec(*args, stderr=subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(500, stderr.decode().strip() or "Could not open the file.")
    return {"ok": True}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Take the port before uvicorn starts the app: it runs the lifespan (model load,
    # then indexing) before binding, so a second Findling would start indexing too.
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, PORT))
    except OSError as e:
        sys.exit(f"Findling: can't use port {PORT} ({e.strerror}). Is Findling already running? http://{HOST}:{PORT}")
    print(f"Findling: http://{HOST}:{PORT}", flush=True)
    uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()

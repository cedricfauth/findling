"""Runs the CocoIndex pipeline in live mode inside the web server and reports status."""

from __future__ import annotations

import asyncio
import collections
import logging
import pathlib
import sqlite3
import time

from cocoindex._internal import core as _coco_core
from cocoindex._internal.update_stats import UpdateStatus

from . import config as cfg
from . import fts
from . import pipeline

log = logging.getLogger("findling.indexer")


class _RecentErrors(logging.Handler):
    """Keeps the last CocoIndex errors (e.g. a file that failed to process)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.items: collections.deque[dict] = collections.deque(maxlen=30)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("cocoindex", "findling")):
            message = record.getMessage().splitlines()[0]
            self.items.appendleft({"time": record.created, "message": message})


class Indexer:
    """Owns the single live CocoIndex run. Changing the folder list means
    cancelling the run and starting it again (the app re-reads folders.toml);
    finished files stay cached."""

    def __init__(self) -> None:
        self.phase = "starting"  # starting | indexing | paused | user_paused | error
        self.message = ""
        # Paused with the Pause button (not saved: indexing runs again after a
        # server restart). While set, restart() doesn't start a run.
        self.user_paused = False
        self.started_at: float | None = None
        self.caught_up_at: float | None = None
        # CocoIndex rescans every folder hourly and adds each rescan to its
        # counters. After the first scan, "checked" is that scan's count plus
        # files added and minus files removed since (each counted once).
        # (checked, adds, deletes) when the first scan finished:
        self._first_scan: tuple[int, int, int] | None = None
        self._stats = None
        self._task: asyncio.Task | None = None
        self._cancelling = False
        self._lock = asyncio.Lock()
        self._db_path = None
        # Each run gets a number. A cancelled run that is slow to stop must not
        # overwrite the status of the run that replaced it.
        self._run = 0
        self.total_files: int | None = None  # files in all folders, counted per run
        self._run_start_files: int | None = None  # the first count of this run
        # For the time-left estimate: files already in the index when the run
        # started, and (time, files processed) samples of this run.
        self._indexed_at_start: int | None = None
        self._processed: collections.deque[tuple[float, int]] = collections.deque()
        self.errors = _RecentErrors()
        logging.getLogger().addHandler(self.errors)

    # -- control ------------------------------------------------------------

    async def restart(self) -> None:
        """(Re)start live indexing with the folders currently in folders.toml."""
        async with self._lock:
            await self._stop_locked()
            if self.user_paused:
                # E.g. a folder was added or removed while paused: apply it on resume.
                self._set("user_paused", self._PAUSED_MESSAGE)
                return
            self._stats = self._first_scan = None
            self.started_at = self.caught_up_at = None
            try:
                config = cfg.load_config(check_folders=False)
            except cfg.ConfigError as e:  # invalid folders.toml
                self._set("error", str(e))
                return
            # Never index while a folder is missing or unreadable: it would be
            # treated as empty and its whole index deleted.
            problems = [
                p for f in config.folders if (p := cfg.folder_problem(pathlib.Path(f)))
            ]
            if problems:
                self._set("paused", " ".join(problems) + " Indexing is paused; search still works.")
                return
            self._db_path = config.db_path
            config.db_path.parent.mkdir(parents=True, exist_ok=True)  # first start
            self._run += 1
            handle = pipeline.APP.update(live=True)
            self.started_at = time.time()
            self._set("indexing", "")
            self._task = asyncio.create_task(self._follow(handle, self._run))
            self.total_files = self._run_start_files = self._indexed_at_start = None
            self._processed = collections.deque([(self.started_at, 0)])
            asyncio.create_task(self._count_files(config, self._run))
            asyncio.create_task(self._count_indexed(config, self._run))
            asyncio.create_task(self._watch_for_many_new_files(config, self._run))

    _PAUSED_MESSAGE = (
        "Search still works. Finished files are kept; folder changes and edited "
        "files are picked up when you resume."
    )

    async def pause(self) -> None:
        """Stop indexing until resume(). Files already indexed stay in the index;
        files that were half-done start over on resume."""
        async with self._lock:
            self.user_paused = True
            await self._stop_locked()
            self._set("user_paused", self._PAUSED_MESSAGE)

    async def resume(self) -> None:
        self.user_paused = False
        await self.restart()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._task is not None and not self._task.done():
            # CocoIndex only offers a global cancel; nothing else in this
            # process uses CocoIndex, so that's fine.
            self._cancelling = True
            _coco_core.cancel_all()
            _, pending = await asyncio.wait({self._task}, timeout=60)
            if pending:
                log.warning("previous indexing run is slow to stop; starting the new one anyway")
            _coco_core.reset_global_cancellation()
            self._cancelling = False
        self._task = None

    async def _follow(self, handle, run: int) -> None:
        try:
            async for snapshot in handle.watch():
                if run != self._run:
                    return  # replaced by a newer run
                self._stats = snapshot.stats
                self._note_processed()
                if snapshot.status == UpdateStatus.READY and self.caught_up_at is None:
                    self.caught_up_at = time.time()
                    files = self._file_stats()
                    self._first_scan = (
                        (files.num_finished, files.num_adds, files.num_deletes) if files else (0, 0, 0)
                    )
                    # The chunks table may have been created during this run, before
                    # the full-text triggers existed: make sure the index is complete.
                    await asyncio.to_thread(fts.ensure_fts, self._db_path)
        except Exception as e:
            if run == self._run and not self._cancelling:
                log.exception("indexing stopped")
                self._set("error", f"Indexing stopped: {e}")

    async def _count_files(self, config, run: int) -> None:
        try:
            total = await asyncio.to_thread(pipeline.count_files, config)
        except Exception:
            log.exception("could not count files")
            return
        if run == self._run:
            self.total_files = total
            if self._run_start_files is None:
                self._run_start_files = total

    async def _count_indexed(self, config, run: int) -> None:
        try:
            indexed = await asyncio.to_thread(_indexed_files, config)
        except Exception:
            log.exception("could not count indexed files")
            return
        if run == self._run:
            files = self._file_stats()
            # Files added since the run started are in the index already.
            self._indexed_at_start = max(0, indexed - (files.num_adds if files else 0))

    def _file_stats(self):
        return self._stats.by_component.get("process_file") if self._stats else None

    def _note_processed(self) -> None:
        files = self._file_stats()
        if files is None:
            return
        done = _processed(files)
        if done != self._processed[-1][1]:
            self._processed.append((time.time(), done))
        # Keep one sample older than the window, so the window stays full.
        cutoff = time.time() - ETA_WINDOW_SECONDS
        while len(self._processed) > 2 and self._processed[1][0] < cutoff:
            self._processed.popleft()

    # After the first scan, CocoIndex's file watcher handles changed files one at a
    # time: it waits for each to finish before taking the next. That's fine for a few
    # edits, but thousands of new files (a copied folder, a big export) would take
    # hours. A new run scans everything again with MAX_PARALLEL_FILES at once and
    # shows progress; finished files are skipped quickly, files half-done start over.
    MANY_NEW_FILES = 20
    RECOUNT_SECONDS = 15.0

    def _live_files_in_progress(self) -> int:
        files = self._file_stats()
        return files.num_in_progress if files else 0

    async def _watch_for_many_new_files(self, config, run: int) -> None:
        while run == self._run:
            await asyncio.sleep(self.RECOUNT_SECONDS)
            if run != self._run:
                return
            # Only while the watcher works through changes after the first scan.
            if (self.phase != "indexing" or self.caught_up_at is None
                    or self._run_start_files is None or not self._live_files_in_progress()):
                continue
            try:
                total = await asyncio.to_thread(pipeline.count_files, config)
            except Exception:
                log.exception("could not count files")
                continue
            if run != self._run:
                return
            self.total_files = total
            new = total - self._run_start_files
            if new >= self.MANY_NEW_FILES:
                log.info("%d new files since the last scan; scanning again to index them in parallel", new)
                asyncio.create_task(self.restart())
                return

    def _checked(self, files) -> int:
        if files is None:
            return 0
        if self._first_scan is None:
            return files.num_finished
        checked, adds, deletes = self._first_scan
        return max(0, checked + (files.num_adds - adds) - (files.num_deletes - deletes))

    def _set(self, phase: str, message: str) -> None:
        self.phase = phase
        self.message = message

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        files = self._file_stats()
        in_progress = files.num_in_progress if files else 0
        phase = self.phase
        if phase == "indexing":
            if self.caught_up_at is None:
                phase = "catching_up"
            else:
                phase = "updating" if in_progress else "up_to_date"
        now = time.time()
        eta = None
        if phase == "catching_up" and files and self.total_files is not None:
            eta = seconds_left(
                total=self.total_files,
                indexed_at_start=self._indexed_at_start,
                unchanged=files.num_unchanged,
                updated=files.num_reprocesses,
                added=files.num_adds,
                failed=files.num_errors,
                samples=list(self._processed),
                now=now,
            )
        return {
            "phase": phase,
            "message": self.message,
            "started_at": self.started_at,
            "caught_up_at": self.caught_up_at,
            "files": {
                "checked": self._checked(files),
                "unchanged": files.num_unchanged if files else 0,
                "added": files.num_adds if files else 0,
                "updated": files.num_reprocesses if files else 0,
                "removed": files.num_deletes if files else 0,
                "failed": files.num_errors if files else 0,
                "in_progress": in_progress,
                "total": self.total_files,
            },
            "active": sorted(
                (
                    {"path": path, "seconds": round(now - started)}
                    for path, started in list(pipeline.ACTIVE_FILES.items())
                ),
                key=lambda a: -a["seconds"],
            )[:10],
            "errors": list(self.errors.items)[:10],
            "seconds_left": eta,
        }


# -- time-left estimate ---------------------------------------------------------

ETA_WINDOW_SECONDS = 600  # speed is measured over the last 10 minutes
ETA_MIN_SECONDS = 30  # no estimate before this much of a run has passed
# ...nor before one batch of files has finished: until then the files still in
# progress make the speed look slower than it is.
ETA_MIN_PROCESSED = cfg.MAX_PARALLEL_FILES


def _processed(files) -> int:
    """Files that needed work (not just a quick 'unchanged' check)."""
    return files.num_adds + files.num_reprocesses + files.num_errors


def _indexed_files(config) -> int:
    """Files in the index that are still on disk, including skipped ones (those
    are also only checked, not processed again). Files deleted since they were
    indexed don't count: the scan won't pass them."""
    indexed = _indexed_paths(config.db_path, config.folders)
    return len(indexed & pipeline.list_files(config)) if indexed else 0


def _indexed_paths(db_path: pathlib.Path, folders: list[str]) -> set[tuple[str, str]]:
    if not db_path.exists() or not folders:
        return set()
    marks = ",".join("?" * len(folders))
    db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        return set(db.execute(
            f"SELECT folder, path FROM {cfg.TABLE_NAME} WHERE folder IN ({marks})"
            f" UNION SELECT folder, path FROM {cfg.ISSUES_TABLE_NAME} WHERE folder IN ({marks})",
            [*folders, *folders],
        ))
    except sqlite3.OperationalError:  # tables not created yet
        return set()
    finally:
        db.close()


def seconds_left(
    *, total: int, indexed_at_start: int | None, unchanged: int, updated: int, added: int,
    failed: int, samples: list[tuple[float, int]], now: float,
) -> float | None:
    """Estimated seconds until the scan is done, or None while unknown.

    Files already indexed and unchanged take a moment each; files that need work
    take seconds to minutes. So: files still needing work, divided by how many
    files were processed per second recently. Old files not reached yet are those
    indexed at the start (and still on disk) minus the ones the scan already passed;
    of these, the same share needs work as among the old files passed so far (all of
    them after a change to the chunking code, almost none on a normal restart)."""
    if indexed_at_start is None or not samples:
        return None
    not_reached = max(0, total - (unchanged + updated + added + failed))
    old_reached = unchanged + updated
    old_not_reached = max(0, indexed_at_start - old_reached)
    new_not_reached = max(0, not_reached - old_not_reached)
    to_process = new_not_reached + (old_not_reached * updated / old_reached if old_reached else 0)
    if to_process == 0:
        return 0.0
    start_time, start_done = samples[0]
    elapsed = now - start_time
    done = samples[-1][1] - start_done
    if elapsed < ETA_MIN_SECONDS or done <= 0 or samples[-1][1] < ETA_MIN_PROCESSED:
        return None
    return to_process * elapsed / done

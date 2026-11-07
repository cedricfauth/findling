"""Tests for indexer.py: switching to a full scan when many new files appear.
No model or real index needed:

    uv run --with pytest pytest tests
"""

import asyncio
import time
from types import SimpleNamespace


from findling import indexer
from findling import pipeline


def live_indexer(start_files: int, in_progress: int = 1) -> indexer.Indexer:
    """An Indexer in run 1, past its first scan, with the watcher busy."""
    ix = indexer.Indexer()
    ix.RECOUNT_SECONDS = 0.01
    ix._run = 1
    ix.phase = "indexing"
    ix.caught_up_at = time.time()
    ix.total_files = ix._run_start_files = start_files
    stats = SimpleNamespace(num_in_progress=in_progress)
    ix._stats = SimpleNamespace(by_component={"process_file": stats})
    return ix


def watch(ix: indexer.Indexer, counts: list[int], monkeypatch) -> list[str]:
    """Run the watch loop against the given file counts; return what it did
    ("count" per recount, "restart" per restart), in order."""
    counts = iter(counts)
    events = []

    def count_files(config):
        events.append("count")
        return next(counts)

    monkeypatch.setattr(pipeline, "count_files", count_files)

    async def restart():
        events.append("restart")
        ix._run += 1  # what restart() does; stops the old loop

    ix.restart = restart

    async def main():
        task = asyncio.create_task(ix._watch_for_many_new_files(None, 1))
        await asyncio.sleep(0.2)
        ix._run += 1  # end the loop if it didn't stop by itself
        await asyncio.wait_for(task, 1)
        await asyncio.sleep(0)  # let a scheduled restart run

    asyncio.run(main())
    return events


def without_counts(events: list[str]) -> list[str]:
    return [e for e in events if e != "count"]


def test_many_new_files_start_a_full_scan(monkeypatch):
    ix = live_indexer(1000)
    events = watch(ix, [1005, 1012, 9996] + [9996] * 100, monkeypatch)
    assert events == ["count", "count", "count", "restart"]
    assert ix.total_files == 9996


def test_files_arriving_slowly_still_add_up(monkeypatch):
    # The baseline is the run's first count, not the previous check.
    ix = live_indexer(1000)
    events = watch(ix, [1008, 1016, 1024] + [1024] * 100, monkeypatch)
    assert without_counts(events) == ["restart"]


def test_a_few_new_files_stay_with_the_watcher(monkeypatch):
    ix = live_indexer(1000)
    events = watch(ix, [1003] * 100, monkeypatch)
    assert "count" in events and without_counts(events) == []
    assert ix.total_files == 1003  # the shown total stays current


def test_no_recount_while_idle(monkeypatch):
    ix = live_indexer(1000, in_progress=0)
    assert watch(ix, [], monkeypatch) == []
    assert ix.total_files == 1000


# -- time-left estimate ---------------------------------------------------------

def eta(**kw):
    args = dict(total=10_000, indexed_at_start=1_000, unchanged=0, updated=0, added=0,
                failed=0, samples=[(0.0, 0)], now=600.0)
    return indexer.seconds_left(**{**args, **kw})


def test_old_files_not_reached_yet_dont_count_as_work():
    # 100 new files done in 10 min, 100 of the 1000 old ones passed already:
    # 10000 - 200 not reached, of which 900 old -> 8900 to do at 1 file per 6 s.
    assert eta(unchanged=100, added=100, samples=[(0.0, 0), (600.0, 100)]) == 8900 * 6


def test_speed_comes_from_the_recent_window():
    # The oldest kept sample is the window start, not the run start.
    assert eta(added=500, samples=[(300.0, 400), (600.0, 500)], now=600.0) == 8500 * 3


def test_a_stuck_file_slows_the_estimate_down():
    fast = eta(added=100, samples=[(0.0, 0), (300.0, 100)], now=300.0)
    stuck = eta(added=100, samples=[(0.0, 0), (300.0, 100)], now=600.0)
    assert stuck == 2 * fast


def test_no_estimate_until_something_was_processed():
    assert eta(samples=[(0.0, 0)], now=600.0) is None
    assert eta(added=5, samples=[(0.0, 0), (10.0, 5)], now=10.0) is None  # too early
    # Fewer than one batch done: the files still in progress would make it look slow.
    assert eta(added=5, samples=[(0.0, 0), (300.0, 5)], now=600.0) is None
    assert eta(indexed_at_start=None) is None


def test_old_files_count_as_work_when_they_get_reprocessed():
    # Full reindex: 300 of the first 400 old files needed work, so 3/4 of the 600 old
    # files not reached will too: 450 to do at 1 file per 2 s.
    assert eta(total=1_000, unchanged=100, updated=300,
               samples=[(0.0, 0), (600.0, 300)]) == 450 * 2


def test_only_old_files_left_is_almost_done():
    assert eta(total=1_000, unchanged=200, samples=[(0.0, 0)]) == 0.0


def test_indexed_files_counts_files_still_on_disk_once(tmp_path):
    import sqlite3
    from findling import config as cfg
    folder = tmp_path / "docs"
    folder.mkdir()
    for name in ("x.html", "y.html", "big.pdf", "new.html"):
        (folder / name).write_text("<p>Text</p>")
    a = str(folder)
    db_path = tmp_path / "index.db"
    db = sqlite3.connect(db_path)
    db.execute(f"CREATE TABLE {cfg.TABLE_NAME} (folder TEXT, path TEXT)")
    db.execute(f"CREATE TABLE {cfg.ISSUES_TABLE_NAME} (folder TEXT, path TEXT, reason TEXT)")
    db.executemany(f"INSERT INTO {cfg.TABLE_NAME} VALUES (?, ?)",
                   [(a, "x.html"), (a, "x.html"), (a, "y.html"), (a, "deleted.html"), ("/gone", "z.html")])
    db.execute(f"INSERT INTO {cfg.ISSUES_TABLE_NAME} VALUES (?, 'big.pdf', 'too large')", (a,))
    db.commit()
    db.close()
    config = SimpleNamespace(db_path=db_path, folders=[a], exclude=())
    # x, y and the skipped big.pdf; not deleted.html (gone), not new.html (not indexed yet)
    assert indexer._indexed_files(config) == 3
    assert indexer._indexed_files(SimpleNamespace(db_path=tmp_path / "missing.db", folders=[a], exclude=())) == 0


def test_checked_follows_new_and_removed_files_not_rescans():
    ix = live_indexer(2009, in_progress=0)
    ix._first_scan = (2009, 0, 0)
    # After the hourly rescan went over every file again; meanwhile 3 files were
    # added and 1 removed.
    ix._stats.by_component["process_file"] = SimpleNamespace(
        num_in_progress=0, num_finished=4022, num_unchanged=4018, num_adds=3,
        num_reprocesses=0, num_deletes=1, num_errors=0,
    )
    assert ix.status()["files"]["checked"] == 2011

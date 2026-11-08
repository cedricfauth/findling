"""Tests for mcp_stdio.py: starting the web app and waiting for it. No server needed:

    uv run --with pytest pytest tests
"""

from findling import mcp_stdio

LOADING = {"model_ready": False, "indexing": {"phase": "loading_model"}}
READY = {"model_ready": True, "indexing": {"phase": "indexing"}}
BROKEN = {"model_ready": False, "indexing": {"phase": "error", "message": "bad config"}}


def fake(monkeypatch, up: bool, statuses: list):
    started = []
    statuses = iter(statuses)
    monkeypatch.setattr(mcp_stdio, "_server_up", lambda: up)
    monkeypatch.setattr(mcp_stdio, "_start_server", lambda: started.append(1) or True)
    monkeypatch.setattr(mcp_stdio, "_status", lambda: next(statuses))
    monkeypatch.setattr(mcp_stdio.time, "sleep", lambda _s: None)
    return started


def test_starts_server_and_waits_for_model(monkeypatch):
    started = fake(monkeypatch, up=False, statuses=[None, LOADING, READY])
    assert mcp_stdio._wait_until_ready() is None
    assert started == [1]


def test_running_server_is_not_started_again(monkeypatch):
    started = fake(monkeypatch, up=True, statuses=[READY])
    assert mcp_stdio._wait_until_ready() is None
    assert started == []


def test_errors_are_left_to_the_server(monkeypatch):
    fake(monkeypatch, up=True, statuses=[BROKEN])
    assert mcp_stdio._wait_until_ready() is None


def test_gives_up_with_a_message(monkeypatch):
    fake(monkeypatch, up=True, statuses=[LOADING] * 1000)
    monkeypatch.setattr(mcp_stdio, "STARTUP_TIMEOUT", -1)
    assert mcp_stdio._wait_until_ready() == mcp_stdio.STARTING


def fake_restart(monkeypatch, tmp_path, status: dict, command: str):
    """Fakes for _restart_if_outdated; returns the list of what it did."""
    did = []
    alive = [True]
    monkeypatch.setattr(mcp_stdio, "LOCK_PATH", tmp_path / "findling.lock")
    monkeypatch.setattr(mcp_stdio, "VERSION", "0.5.0")
    monkeypatch.setattr(mcp_stdio, "_status", lambda: status)
    monkeypatch.setattr(mcp_stdio, "_command", lambda pid: command if alive[0] else "")
    monkeypatch.setattr(mcp_stdio, "_server_up", lambda: not alive[0])
    monkeypatch.setattr(mcp_stdio, "_spawn", lambda: did.append("spawn"))
    monkeypatch.setattr(mcp_stdio.time, "sleep", lambda _s: None)

    def kill(pid, sig):
        did.append(("kill", pid))
        alive[0] = False

    monkeypatch.setattr(mcp_stdio.os, "kill", kill)
    return did


OURS = "/x/python3.12 -m findling.server"


def test_restarts_outdated_server_started_by_a_bridge(monkeypatch, tmp_path):
    did = fake_restart(monkeypatch, tmp_path, {"version": "0.4.0", "pid": 42}, OURS)
    mcp_stdio._restart_if_outdated()
    assert did == [("kill", 42), "spawn"]


def test_keeps_current_unknown_or_foreign_servers(monkeypatch, tmp_path):
    for status, command in [
        ({"version": "0.5.0", "pid": 42}, OURS),  # same version
        ({"model_ready": True}, OURS),  # older servers: no version, no pid
        ({"version": "0.4.0", "pid": 42}, "/x/.venv/bin/findling"),  # started in a terminal
        ({"version": "0.4.0", "pid": 42}, "/usr/bin/something-else"),
    ]:
        did = fake_restart(monkeypatch, tmp_path, status, command)
        mcp_stdio._restart_if_outdated()
        assert did == [], status

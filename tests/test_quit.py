"""The Quit button's endpoint. No model or index needed."""

import asyncio
import os
import signal

from fastapi.testclient import TestClient

from findling import server


def test_quit_answers_then_stops_like_ctrl_c(monkeypatch):
    killed = []
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    async def run():
        assert await server.quit_app() == {"ok": True}
        assert killed == []  # not before the response is sent
        await asyncio.sleep(0.3)

    asyncio.run(run())
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_other_websites_cannot_quit():
    c = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    assert c.post("/api/quit", json={}, headers={"Origin": "https://example.com"}).status_code == 403
    assert c.post("/api/quit", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415

"""Tests for the View links (/show/…) and the link key in server.py. No model or real index needed:

    uv run --with pytest pytest tests
"""

import sqlite3
import urllib.parse

import pytest
from fastapi.testclient import TestClient


from findling import config as cfg
from findling import server


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """An indexed folder with a few files, an index with one PDF chunk on page 12,
    and a fresh link key."""
    folder = tmp_path / "Docs"
    (folder / "sub").mkdir(parents=True)
    for name in ["sub/a b.md", "x.pdf", "page.html", "w.docx"]:
        (folder / name).write_text("content")
    db_path = tmp_path / "findling.db"
    db = sqlite3.connect(db_path)
    db.execute(f"CREATE TABLE {cfg.TABLE_NAME} (folder TEXT, path TEXT, chunk_index INTEGER, text TEXT, page INTEGER)")
    db.execute(f"INSERT INTO {cfg.TABLE_NAME} VALUES (?, 'x.pdf', 7, 'chunk', 12)", (str(folder),))
    db.commit()
    db.close()
    config = cfg.Config(folders=(str(folder),), exclude=(), max_file_bytes=1 << 20, db_path=db_path)
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: config)
    server._url_key.cache_clear()
    yield str(folder), db_path
    server._url_key.cache_clear()


def client() -> TestClient:
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}", follow_redirects=False)


def params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True))


def test_show_url_has_one_form_for_all_types(setup):
    folder, _ = setup
    fid = server._folder_id(folder)
    url = server._show_url(folder, "sub/a b.md", 3, "Kündigungs frist", absolute=True)
    assert url == f"http://127.0.0.1:{server.PORT}/show/{fid}/sub/a%20b.md?chunk=3&q=K%C3%BCndigungs+frist"
    assert server._show_url(folder, "x.pdf", 7, "q").startswith(f"/show/{fid}/x.pdf?")
    assert server._show_url(folder, "w.docx", 0, "q") is None
    # Agents' links leave out what's empty, to stay short.
    assert server._show_url(folder, "x.pdf", 7, "") == f"/show/{fid}/x.pdf?chunk=7"
    assert server._show_url(folder, "x.pdf", None, "") == f"/show/{fid}/x.pdf"


def test_show_redirects_to_the_right_viewer(setup):
    folder, _ = setup
    c = client()

    text = c.get(server._show_url(folder, "sub/a b.md", 3, "frist"))
    assert text.status_code == 302 and text.headers["location"].startswith("/view?")
    assert params(text.headers["location"]) == {"folder": folder, "path": "sub/a b.md", "chunk": "3", "q": "frist"}

    pdf = c.get(server._show_url(folder, "x.pdf", 7, "q")).headers["location"]
    assert pdf.startswith("/static/pdfjs/web/viewer.html?") and pdf.endswith("#page=12")  # page from the index
    assert params(pdf.split("#")[0])["file"] == server._file_url(folder, "x.pdf")

    html = c.get(server._show_url(folder, "page.html", None, "")).headers["location"]
    assert html == server._file_url(folder, "page.html") + "?chunk=&q="


def test_show_refuses_what_it_cant_show(setup):
    folder, _ = setup
    fid = server._folder_id(folder)
    c = client()
    assert c.get(f"/show/{fid}/w.docx?chunk=0").status_code == 415
    assert c.get(f"/show/{fid}/missing.md").status_code == 404
    assert c.get(f"/show/{fid}/sub/..%2F..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert c.get("/show/0000000000000000/sub/a%20b.md").status_code == 404


def test_link_key_survives_restarts(setup):
    folder, db_path = setup
    fid = server._folder_id(folder)
    key_file = db_path.with_suffix(".urlkey")
    assert key_file.stat().st_mode & 0o777 == 0o600
    server._url_key.cache_clear()  # as after a restart
    assert server._folder_id(folder) == fid
    key_file.unlink()  # deleting the key makes old links stop working
    server._url_key.cache_clear()
    assert server._folder_id(folder) != fid


@pytest.mark.parametrize("opener, reveal, expected", [
    ("open", False, ["open", "{f}/x.pdf"]),
    ("open", True, ["open", "-R", "{f}/x.pdf"]),
    ("xdg-open", False, ["xdg-open", "{f}/x.pdf"]),
    ("xdg-open", True, ["xdg-open", "{f}"]),
])
def test_open_and_reveal_commands(setup, monkeypatch, opener, reveal, expected):
    folder, _ = setup
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        class Done:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return Done()

    monkeypatch.setattr(server, "OPEN", opener)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    r = client().post("/api/open", json={"folder": folder, "path": "x.pdf", "reveal": reveal})
    assert r.status_code == 200
    assert calls == [[a.format(f=folder) for a in expected]]

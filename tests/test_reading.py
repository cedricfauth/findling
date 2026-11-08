"""Tests for paths.py (full paths, `within`) and reading.py (`read`, sections,
listings). No model or real index needed:

    uv run --with pytest pytest tests
"""

import sqlite3
import unicodedata

import pytest


from findling import paths
from findling import reading
from findling import searcher
from findling.paths import AgentError

# ---------------------------------------------------------------------------
# Full paths and `within`
# ---------------------------------------------------------------------------

FOLDERS = ["/x/urteile", "/x/gesetze", "/y/Work", "/z/Work/Docs"]
ROWS = [
    ("/x/urteile", "ag-aachen/1992 Urteil.html"),
    ("/x/urteile", "bgh/2023-01 Beschluss.pdf"),
    ("/x/urteile", "bgh/sub/2023-x.html"),
    ("/x/urteile", "bghz/a.html"),
    ("/x/gesetze", "BGB.html"),
    ("/x/gesetze", unicodedata.normalize("NFD", "Übungen/Kündigung.txt")),  # as macOS may store it
    ("/y/Work", "Docs/report.pdf"),
    ("/z/Work/Docs", "report.pdf"),
]


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE chunks (folder TEXT, path TEXT)")
    db.executemany("INSERT INTO chunks VALUES (?, ?)", ROWS)
    return db


def resolve(db, name):
    return paths.resolve(db, FOLDERS, name)


def within(db, name) -> list[str]:
    where, params = searcher.scope_clause([paths.scope(*resolve(db, name))])
    return sorted(path for _, path in db.execute(f"SELECT * FROM chunks WHERE {where}", params))


def test_resolve(db):
    assert resolve(db, "/x/gesetze/BGB.html") == ("/x/gesetze", "BGB.html")
    assert resolve(db, "/x/gesetze/") == ("/x/gesetze", "")
    assert resolve(db, "/X/Urteile/BGH") == ("/x/urteile", "bgh")  # case ignored, also below the folder
    assert resolve(db, "/x/gesetze/übungen/KÜNDIGUNG.TXT") == ("/x/gesetze", ROWS[5][1])  # NFC finds NFD
    # Folders that the old short names mixed up ("Work/Docs/report.pdf") stay apart.
    assert resolve(db, "/y/Work/Docs/report.pdf") == ("/y/Work", "Docs/report.pdf")
    assert resolve(db, "/z/Work/Docs/report.pdf") == ("/z/Work/Docs", "report.pdf")
    for bad in ["gesetze/BGB.html", "/etc/passwd", "/x/gesetze/../urteile", "/x/urteile/BGH-nope"]:
        with pytest.raises(AgentError):
            resolve(db, bad)


def test_within(db):
    assert within(db, "/x/gesetze") == ["BGB.html", ROWS[5][1]]
    assert within(db, "/x/gesetze/BGB.html") == ["BGB.html"]
    assert within(db, "/x/urteile/BGH") == ["bgh/2023-01 Beschluss.pdf", "bgh/sub/2023-x.html"]  # not bghz/


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def doc(*chunks: tuple[str, str] | tuple[str, str, int]) -> reading.Doc:
    """chunks: (headings, text[, page]) in file order."""
    return reading.Doc([
        reading.Chunk(index=i, headings=c[0], page=c[2] if len(c) > 2 else 0, text=c[1])
        for i, c in enumerate(chunks)
    ])


def link(chunk: int) -> str:
    return f"u{chunk}"


LAW = doc(
    ("BGB > § 1598", "Rule 1598."),
    ("BGB > § 1599 Anfechtbarkeit", "Rule 1599 part one."),
    ("BGB > § 1599 Anfechtbarkeit", "Rule 1599 part two."),
    ("BGB > § 1600 Anfechtungsberechtigte", "Rule 1600."),
    ("BGB > § 15990 Other", "Rule 15990."),
)


def test_read_marks_sections_with_links():
    text, first, end = reading.read(LAW, (0, 0), before=0, after=10_000, link=link)
    assert text == (
        "[BGB > § 1598](u0)\nRule 1598.\n\n"
        "[§ 1599 Anfechtbarkeit](u1)\nRule 1599 part one.\n\nRule 1599 part two.\n\n"
        "[§ 1600 Anfechtungsberechtigte](u3)\nRule 1600.\n\n"
        "[§ 15990 Other](u4)\nRule 15990."
    )
    assert first == (0, 0) and end == (5, 0)


def test_find_section():
    assert reading.find_section(LAW, "§ 1599")[:2] == (1, 3)  # both chunks, not § 15990
    assert reading.find_section(LAW, "§1600")[2] == "BGB > § 1600 Anfechtungsberechtigte"
    assert reading.find_section(LAW, "anfechtungsberechtigte")[0] == 3
    assert reading.find_section(LAW, "BGB")[:2] == (0, 5)  # the whole file
    with pytest.raises(AgentError, match="Several sections"):
        reading.find_section(LAW, "Anfecht")
    with pytest.raises(AgentError, match="No section"):
        reading.find_section(LAW, "§ 2000")


def test_section_includes_subsections():
    d = doc(("Doc > A", "a"), ("Doc > A > 1", "a1"), ("Doc > A > 2", "a2"), ("Doc > B", "b"))
    assert reading.find_section(d, "A")[:2] == (0, 3)
    d = doc(("Doc > Kapitel 1 > § 1", "x"), ("Doc > Kapitel 1 > § 2", "y"), ("Doc > Kapitel 2 > § 3", "z"))
    assert reading.find_section(d, "Kapitel 1")[:3] == (0, 2, "Doc > Kapitel 1")


def test_read_section_stops_at_its_end():
    first, stop, _ = reading.find_section(LAW, "§ 1599")
    text, _, end = reading.read(LAW, (first, 0), before=0, after=10_000, stop=stop, link=link)
    assert text == "[BGB > § 1599 Anfechtbarkeit](u1)\nRule 1599 part one.\n\nRule 1599 part two."
    assert end == (3, 0)
    assert LAW.cursor(*end) == "3"


def test_read_cuts_at_sentences_and_continues():
    d = doc(("", "One two three. Four five six. Seven eight nine."))
    text, _, end = reading.read(d, (0, 0), before=0, after=35, link=link)
    assert text == "One two three. Four five six. "[:len(text)] and text.endswith("six. ")
    assert d.cursor(*end) == "0:30"
    rest, first, end = reading.read(d, d.parse("0:30"), before=0, after=1000, link=link)
    assert rest == "Seven eight nine." and end == (1, 0)


def test_read_never_cuts_a_word_at_a_chunk_boundary():
    d = doc(("", "First chunk ends here."), ("", "Eigenbedarfskündigung follows."))
    text, _, end = reading.read(d, (0, 0), before=0, after=30, link=link)
    assert text == "First chunk ends here." and end == (1, 0)  # stops before the long word
    text, _, _ = reading.read(d, (1, 0), before=0, after=10, link=link)
    assert text == "Eigenbedar"  # a single word longer than `after` is still cut


def test_read_before_goes_back_across_chunks():
    text, first, _ = reading.read(LAW, (3, 0), before=25, after=10, link=link)
    # Back: all of chunk 2 (19 chars), then the start of chunk 1 doesn't fit whole.
    assert text.startswith("[BGB > § 1599 Anfechtbarkeit](u1)\n")
    assert "Rule 1599 part two." in text and text.endswith("Rule 1600.")
    assert first[0] == 1 and LAW.cursor(*first).startswith("1:")


def test_overlapping_chunks_are_not_repeated():
    tail = "this sentence is the shared overlap. "
    d = doc(("", "First part, then " + tail), ("", tail + "And the second part."))
    assert d.skip == [0, len(tail)]
    text, _, _ = reading.read(d, (0, 0), before=0, after=10_000, link=link)
    assert text == "First part, then " + tail + "And the second part."
    # Starting at chunk 1 alone shows its whole text; with `before`, the overlap comes from chunk 0.
    assert reading.read(d, (1, 0), before=0, after=10_000, link=link)[0] == tail + "And the second part."
    text, first, _ = reading.read(d, (1, 0), before=10_000, after=10_000, link=link)
    assert text == "First part, then " + tail + "And the second part." and first == (0, 0)


def test_pages_get_link_lines():
    d = doc(("Report", "Page one.", 1), ("Report", "Page two.", 2))
    text, _, _ = reading.read(d, (0, 0), before=0, after=10_000, link=link)
    assert text == "[Report, p. 1](u0)\nPage one.\n\n[Report, p. 2](u1)\nPage two."


def test_parse_cursor():
    assert LAW.parse("3") == (3, 0) and LAW.parse(3) == (3, 0) and LAW.parse("2:5") == (2, 5)
    for bad in ["x", "1:2:3", "-1"]:
        with pytest.raises(AgentError):
            LAW.parse(bad)
    with pytest.raises(AgentError, match="no chunk 99"):
        LAW.parse("99")


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def test_sections_listing():
    entries = reading.sections(LAW, None)
    assert [(e.chunk, e.section) for e in entries][:2] == [(0, "BGB > § 1598"), (1, "BGB > § 1599 Anfechtbarkeit")]
    assert len(entries) == 4  # § 1599's two chunks are one section
    assert [e.chunk for e in reading.sections(LAW, "anfecht")] == [1, 3]


def test_list_dir():
    rows = [("/x/urteile", p, "html", 1) for p in ["bgh/a.html", "bgh/b.html", "ag/c.html", "top.html"]]
    dirs, files = reading.list_dir(rows, "/x/urteile", "", "name", None)
    assert [(d.name, d.files) for d in dirs] == [("/x/urteile/ag", 1), ("/x/urteile/bgh", 2)]
    assert [f.file for f in files] == ["/x/urteile/top.html"]
    dirs, files = reading.list_dir(rows[:2], "/x/urteile", "bgh", "name", "B.HTML")
    assert dirs == [] and [f.file for f in files] == ["/x/urteile/bgh/b.html"]


def test_open_index_in_folder_with_url_characters(tmp_path):
    db_path = tmp_path / "a#b?c" / "findling.db"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as setup:
        setup.execute("CREATE TABLE chunks (folder TEXT, path TEXT)")
        setup.executemany("INSERT INTO chunks VALUES (?, ?)", ROWS)
    db = reading.open_index(db_path)
    assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == len(ROWS)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM chunks")

"""Tests for rag.py (search results for AI agents). No model or real index needed:

    uv run --with pytest pytest tests
"""



from findling import rag
from findling import searcher

FOLDER = "/nonexistent/docs"


def match(
    index: int, text: str, headings: str = "", page: int = 0, words: bool = True, snippet: tuple[int, int] | None = None
) -> searcher.Match:
    return searcher.Match(
        chunk_index=index, headings=headings, page=page, text=text, snippet=snippet or (0, len(text)),
        highlights=[], heading_highlights=[], similarity=0.8, bm25_rank=1 if words else None, vec_rank=1,
    )


def response(query: str, results: list[searcher.FileResult], weak: bool = False, total: int = 0) -> searcher.SearchResponse:
    return searcher.SearchResponse(
        query=searcher.parse_query(query), results=results, weak=weak, total=total or len(results)
    )


def file_result(path: str, matches: list[searcher.Match]) -> searcher.FileResult:
    return searcher.FileResult(
        folder=FOLDER, path=path, file_type=path.rsplit(".", 1)[-1], score=0.1,
        word_match=True, path_highlights=[], matches=matches,
    )


def test_overlap():
    a = "The nightly backup job copies all files. " + "It runs at two in the morning."
    b = "It runs at two in the morning. Restores use the restore script."
    assert rag.overlap(a, b) == len("It runs at two in the morning.")
    assert rag.overlap("First part.", "Second part.") == 0


def test_files_and_passages():
    r = response("backup", [
        file_result("guide.md", [match(1, "Backups run nightly.", "Guide > Backups")]),
        file_result("scan.pdf", [match(0, "Restore notes.", page=3, words=False)]),
    ])
    out = rag.build(r, file_name=lambda f: "docs/" + f.path, view_link=lambda f, c: f"http://x/{f.path}/{c}")
    assert out.match_quality == "words_and_meaning"
    assert out.total == 2 and out.next_offset is None
    guide, scan = out.files
    assert (guide.file, guide.type, guide.modified) == ("docs/guide.md", "md", None)  # file doesn't exist
    (p,) = guide.passages
    assert (p.chunk, p.section, p.page, p.match, p.text) == (1, "Guide > Backups", None, "words", "Backups run nightly.")
    assert p.url == "http://x/guide.md/1" and p.excerpt is None
    (q,) = scan.passages
    assert (q.section, q.page, q.match) == (None, 3, "meaning")


def test_default_file_name_is_full_path():
    out = rag.build(response("x", [file_result("a.txt", [match(0, "x")])]))
    assert out.files[0].file == f"{FOLDER}/a.txt"


def test_match_is_meaning_when_fulltext_is_stale():
    r = response("backup", [file_result("a.txt", [match(0, "backup")])])
    r.fulltext_stale = True
    out = rag.build(r)
    assert out.files[0].passages[0].match == "meaning" and out.match_quality == "meaning_only"


def test_passage_that_doesnt_fit_becomes_excerpt():
    long = "x" * 900 + " the match is here " + "y" * 900
    window = (880, 940)
    r = response("match", [
        file_result("a.txt", [match(0, "a" * 1000)]),
        file_result("b.txt", [match(0, long, snippet=window)]),
    ])
    out = rag.build(r, max_chars=1200)
    a, b = out.files
    assert a.passages[0].excerpt is None
    (p,) = b.passages
    assert p.excerpt and p.text.startswith("… ") and p.text.endswith(" …") and "the match is here" in p.text


def test_best_passage_of_each_file_first():
    r = response("x", [
        file_result("a.txt", [match(0, "a" * 1000), match(2, "c" * 1000)]),
        file_result("b.txt", [match(0, "b" * 1000)]),
    ])
    out = rag.build(r, max_chars=2500)
    # b's best beats a's second, which becomes an excerpt (its snippet is whole: left out)
    assert [len(f.passages) for f in out.files] == [1, 1]


def test_files_that_dont_fit_go_to_next_page():
    r = response("x", [
        file_result("a.txt", [match(0, "a" * 1000)]),
        file_result("b.txt", [match(0, "b" * 1000)]),
        file_result("c.txt", [match(0, "c" * 10)]),
    ], total=5)
    out = rag.build(r, max_chars=1500)
    # b doesn't fit (its snippet is the whole text): b and c wait for the next page,
    # even though c would fit, so the pages stay in rank order.
    assert [f.file.rsplit("/", 1)[-1] for f in out.files] == ["a.txt"]
    assert out.total == 5 and out.next_offset == 1


def test_offset_pages_on():
    r = response("x", [file_result(f"{n}.txt", [match(0, n)]) for n in "abcd"], total=4)
    out = rag.build(r, offset=2)
    assert [f.file.rsplit("/", 1)[-1] for f in out.files] == ["c.txt", "d.txt"]
    assert out.next_offset is None
    first = rag.build(response("x", r.results[:2], total=4))
    assert first.next_offset == 2


def test_first_passage_cut_to_budget():
    r = response("word", [file_result("a.txt", [match(0, "word " * 1000)])])
    out = rag.build(r, max_chars=1000)
    (passage,) = out.files[0].passages
    assert len(passage.text) <= 1000 and passage.text.endswith(" …") and passage.excerpt


def test_filler_word_query_note():
    out = rag.build(response("the and", [file_result("a.txt", [match(0, "something", words=False)])], weak=True))
    assert out.match_quality == "meaning_only" and "no words to match" in out.note
    assert out.files[0].passages[0].match == "meaning"


def test_weak_and_empty():
    weak = rag.build(response("nothing", [file_result("a.txt", [match(0, "something", words=False)])], weak=True))
    assert weak.match_quality == "meaning_only" and "closest by meaning" in weak.note
    empty = rag.build(response("nothing", []))
    assert empty.match_quality == "no_results" and empty.files == [] and empty.total == 0


def test_normalize_matches_char_by_char_folding():
    import unicodedata

    text = "Kündigung KÜNDIGUNG " + unicodedata.normalize("NFD", "Übung é") + " Straße ﬁle 한국 Ἀθῆναι İ"
    assert searcher.normalize(text) == "".join(searcher._fold_char(c) for c in text)
    assert searcher.normalize("Kündigung") == searcher.normalize(unicodedata.normalize("NFD", "KÜNDIGUNG")) == "kundigung"

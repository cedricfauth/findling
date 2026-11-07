"""Generic HTML handling around docling, for real-world pages.

prepare_html(): parse the page, let enabled extensions rewrite it, unwrap layout
tables, and hand docling clean UTF-8 bytes.

dropped_text(): the safety net. Compares the page's visible text with what docling
produced and returns the text docling dropped (with its heading path), so it can be
indexed as plain text instead of silently disappearing.
"""

from __future__ import annotations

import re
from types import ModuleType

import lxml.html
from bs4.dammit import EncodingDetector, UnicodeDammit

HTML_EXTS = {"html", "htm", "xhtml"}

# Elements whose text is never shown, so docling rightly ignores it.
_INVISIBLE = {"head", "script", "style", "noscript", "template", "svg", "math", "iframe", "object"}
# Elements that start a new block of text (anything else is treated as inline).
_BLOCKS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "br",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

# Layout tables: at most this many columns, with this share of their text in long cells.
_LAYOUT_MAX_COLS = 3  # e.g. heading | § | text of a quoted regulation
_LAYOUT_LONG_CELL = 150  # characters
_LAYOUT_PROSE_SHARE = 0.6


def parse(data: bytes) -> lxml.html.HtmlElement:
    if not data.strip():
        return lxml.html.document_fromstring("<html></html>")
    # Byte order mark, then the declared charset, then UTF-8, then Windows-1252 (what
    # browsers assume for Western pages without a declaration); statistical guessing only
    # if all of those fail. Guessing first mis-decodes short German pages (ü -> ³).
    # (bs4 would try user_encodings before the declared charset, so pass both as "known".)
    _, bom = EncodingDetector.strip_byte_order_mark(data)
    declared = EncodingDetector.find_declared_encoding(data, is_html=True)
    text = UnicodeDammit(
        data,
        known_definite_encodings=[e for e in (bom, declared, "utf-8", "windows-1252") if e],
        is_html=True,
    ).unicode_markup or ""
    text = re.sub(r"^\s*<\?xml[^>]*>", "", text)  # lxml rejects str input with an XML declaration
    return lxml.html.document_fromstring(text if text.strip() else "<html></html>")


def prepare_html(data: bytes, extensions: list[ModuleType]) -> tuple[bytes, lxml.html.HtmlElement]:
    """Returns (bytes for docling, the parsed and rewritten page)."""
    root = parse(data)
    for ext in extensions:
        hook = getattr(ext, "prepare_html", None)
        if hook is not None:
            hook(root)
    unwrap_layout_tables(root)
    _set_utf8(root)
    return lxml.html.tostring(root, encoding="utf-8", doctype="<!DOCTYPE html>"), root


def _set_utf8(root: lxml.html.HtmlElement) -> None:
    head = root.find("head")
    if head is None:
        head = lxml.html.Element("head")
        root.insert(0, head)
    for meta in head.findall("meta"):
        if "charset" in meta.attrib or meta.get("http-equiv", "").lower() == "content-type":
            meta.drop_tree()
    head.insert(0, lxml.html.Element("meta", charset="utf-8"))


# ---------------------------------------------------------------------------
# Layout tables
# ---------------------------------------------------------------------------


def _rows(table: lxml.html.HtmlElement) -> list[lxml.html.HtmlElement]:
    """The table's own rows (not those of nested tables)."""
    return [
        tr for tr in table.iter("tr")
        if next(tr.iterancestors("table"), None) is table
    ]


def _cells(tr: lxml.html.HtmlElement) -> list[lxml.html.HtmlElement]:
    return [c for c in tr if c.tag in ("td", "th")]


def _is_layout_table(table: lxml.html.HtmlElement) -> bool:
    rows = _rows(table)
    if not rows:
        return True  # no rows at all: just a wrapper
    widths = [len(_cells(tr)) for tr in rows]
    if max(widths) <= 1:
        return True  # a single column of blocks is never a data table
    # Header cells mark a data table, but only the table's own: a layout table often
    # wraps real data tables.
    if max(widths) > _LAYOUT_MAX_COLS or any(c.tag == "th" for tr in rows for c in _cells(tr)):
        return False
    lengths = [len(c.text_content().strip()) for tr in rows for c in _cells(tr)]
    total = sum(lengths)
    long = sum(n for n in lengths if n >= _LAYOUT_LONG_CELL)
    return total > 0 and long / total >= _LAYOUT_PROSE_SHARE


def unwrap_layout_tables(root: lxml.html.HtmlElement) -> None:
    """Turn tables used for page layout (e.g. a margin-number column next to the text)
    into plain blocks. docling would otherwise serialize their prose as table cells
    ("1, 1 = ..."), which pollutes every chunk. Innermost tables go first, so nested
    wrapper tables collapse before their parent is judged."""
    for table in reversed(list(root.iter("table"))):
        if not _is_layout_table(table):
            continue
        rows = _rows(table)
        for el in [c for c in table if c.tag in ("thead", "tbody", "tfoot", "colgroup", "col", "caption")]:
            if el.tag in ("colgroup", "col"):
                el.drop_tree()
            elif el.tag == "caption":
                el.tag = "p"
            else:
                el.drop_tag()
        for tr in rows:
            tr.tag = "div"
            for cell in _cells(tr):
                has_blocks = any(
                    isinstance(d.tag, str) and d.tag in _BLOCKS for d in cell.iterdescendants()
                )
                cell.tag = "div" if has_blocks else "p"
                for attr in ("colspan", "rowspan", "valign", "align", "width"):
                    cell.attrib.pop(attr, None)
        table.tag = "div"


# ---------------------------------------------------------------------------
# Safety net: text docling dropped
# ---------------------------------------------------------------------------

_NGRAM = 3
_MIN_WORDS = 3  # shorter fragments (margin numbers, labels) aren't worth recovering


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _segments(root: lxml.html.HtmlElement) -> list[tuple[tuple[str, ...], str]]:
    """Visible text split at block boundaries, each with its heading path."""
    out: list[tuple[tuple[str, ...], str]] = []
    heads: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        text = re.sub(r"\s+", " ", "".join(buf)).strip()
        buf.clear()
        if text:
            out.append((tuple(h for _, h in heads), text))

    def walk(el) -> None:
        tag = el.tag if isinstance(el.tag, str) else None  # comments, PIs: tag is a function
        if tag is None or tag in _INVISIBLE:
            buf.append(el.tail or "")
            return
        if tag in _BLOCKS:
            flush()
        if tag in _HEADINGS:
            text = re.sub(r"\s+", " ", el.text_content()).strip()
            while heads and heads[-1][0] >= _HEADINGS[tag]:
                heads.pop()
            if text:
                heads.append((_HEADINGS[tag], text))
                out.append((tuple(h for _, h in heads[:-1]), text))
        else:
            buf.append(el.text or "")
            for child in el:
                walk(child)
        if tag in _BLOCKS:
            flush()
        buf.append(el.tail or "")

    body = root.find("body")
    walk(body if body is not None else root)
    flush()
    return out


def _ngrams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + _NGRAM]) for i in range(len(words) - _NGRAM + 1)}


def dropped_text(root: lxml.html.HtmlElement, produced: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """Blocks of the page's text that are (mostly) missing from `produced` (docling's
    chunk texts incl. headings), merged per heading path, in page order."""
    seen = set()
    for text in produced:
        seen |= _ngrams(_words(text))
    missing: list[tuple[tuple[str, ...], str]] = []
    for heads, text in _segments(root):
        words = _words(text)
        if len(words) < _MIN_WORDS:
            continue
        grams = _ngrams(words)
        if sum(g in seen for g in grams) / len(grams) >= 0.5:
            continue
        if missing and missing[-1][0] == heads:
            missing[-1] = (heads, missing[-1][1] + "\n" + text)
        else:
            missing.append((heads, text))
    return missing

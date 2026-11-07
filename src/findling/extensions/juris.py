"""juris markup, as used in German federal law texts (gesetze-im-internet.de and its
dumps, e.g. Open Legal Data).

Law texts embed juris/SGML-style elements in their HTML. Most of it is harmless
(unknown inline tags like <LA> keep their text), but tables use the CALS table model
(<table><tgroup><colspec/><tbody><row><entry>) which HTML parsers don't recognize as
a table: docling drops their text completely (tables of contents, but also annexes
and tables inside sections). This rewrites them into plain HTML tables.
"""

from __future__ import annotations

import lxml.html

# CALS table elements -> HTML equivalents (thead/tbody/tfoot are the same in both).
_RENAME = {"row": "tr", "entry": "td"}


def prepare_html(root: lxml.html.HtmlElement) -> None:
    # Column/span definitions: layout hints without text (drop_tree keeps tail text).
    for el in list(root.iter("colspec", "spanspec")):
        el.drop_tree()
    # <tgroup> sits between <table> and <tbody>; keep its children, drop the wrapper.
    for el in list(root.iter("tgroup")):
        el.drop_tag()
    for el in root.iter("row", "entry"):
        el.tag = _RENAME[el.tag]
        if el.tag == "td":
            # CALS spans (namest/nameend, morerows) have no direct HTML meaning here;
            # leaving them would only confuse the table parser.
            for attr in ("namest", "nameend", "morerows", "colname"):
                el.attrib.pop(attr, None)
    # juris puts <title> (part/chapter names) inside tables; outside <head> that tag
    # means nothing to HTML parsers.
    body = root.find("body")
    for el in list((body if body is not None else root).iter("title")):
        el.tag = "span"

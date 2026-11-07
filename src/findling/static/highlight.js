/* Findling: find a search result's chunk in a page and highlight it.

   Used by the text viewer (view.html), the PDF viewer add-on (pdf-highlight.js) and
   HTML pages opened from the results (the server inlines this file into them).

   The chunk text comes from the index, which isn't always the page's text verbatim:
   docling reformats documents (markdown syntax, table cells, hyphenation, spacing).
   So the chunk is located on letters and digits only, case- and accent-insensitive,
   by its first and last words if it doesn't match as a whole.
*/
(function (global) {
  "use strict";

  const ACCENT = /[̀-ͯ]/; // Latin accents after NFD (same as searcher.py)
  const ALNUM = /[\p{L}\p{N}]/u;
  const ANCHOR = 40; // folded chars used to find the chunk's start / end

  // Lower-case, accent-free form of one character (as searcher.normalize).
  function foldChar(c) {
    if (c.length === 1 && c.charCodeAt(0) < 128) return c.toLowerCase();
    if (ACCENT.test(c)) return "";
    const d = c.normalize("NFD");
    if (d.length > 1 && [...d.slice(1)].every((x) => ACCENT.test(x))) c = d[0];
    return c.normalize("NFKC").toLowerCase(); // NFKC: ligatures from PDFs (ﬁ -> fi)
  }

  // Folded text plus, for each folded char, the [start, end) of its source char.
  function fold(text, alnumOnly) {
    let s = "";
    const from = [], to = [];
    for (let i = 0; i < text.length; ) {
      const ch = String.fromCodePoint(text.codePointAt(i));
      const end = i + ch.length;
      for (const f of foldChar(ch)) {
        if (alnumOnly && !ALNUM.test(f)) continue;
        s += f;
        for (let k = 0; k < f.length; k++) { from.push(i); to.push(end); }
      }
      i = end;
    }
    return { s, from, to };
  }

  // [start, end) of the chunk in text, or null.
  function locate(text, chunk) {
    if (!chunk) return null;
    const exact = text.indexOf(chunk);
    if (exact !== -1) return [exact, exact + chunk.length];
    const h = fold(text, true);
    const c = fold(chunk, true).s;
    if (!c.length || !h.s.length) return null;
    // Letters and digits only were compared: take in the "(" of "(1)" or a final ".".
    const OPENING = /[(\[{"'„“‚‘«§¿¡]/, CLOSING = /[)\]}"'“”’».,;:!?]/;
    const span = (a, b) => {
      let s = h.from[a], e = h.to[b - 1];
      for (let i = 0; i < 2 && s > 0 && OPENING.test(text[s - 1]); i++) s--;
      for (let i = 0; i < 2 && e < text.length && CLOSING.test(text[e]); i++) e++;
      return [s, e];
    };
    const whole = h.s.indexOf(c);
    if (whole !== -1) return span(whole, whole + c.length);

    const k = Math.min(ANCHOR, c.length);
    // Start: the first anchor (from the chunk's beginning on) that occurs in text.
    let start = -1, startOff = 0;
    for (let off = 0; off + k <= c.length && off <= 10 * k; off += k) {
      const i = h.s.indexOf(c.substr(off, k));
      if (i !== -1) { start = i; startOff = off; break; }
    }
    if (start === -1) return null;
    // End: the last anchor (from the chunk's end back) after the start, not too far.
    const limit = start + Math.ceil((c.length - startOff) * 1.5) + 200;
    for (let off = 0; c.length - off - k >= startOff && off <= 10 * k; off += k) {
      const j = h.s.indexOf(c.substr(c.length - off - k, k), start);
      if (j !== -1 && j + k <= limit) return span(start, j + k);
    }
    // End not found (e.g. the chunk continues on the next PDF page): estimate it.
    return span(start, Math.min(h.s.length, start + c.length - startOff));
  }

  // [start, end) ranges of the needles (already normalized, from the search API)
  // inside text[lo, hi).
  function findWords(text, needles, lo = 0, hi = text.length) {
    const h = fold(text.slice(lo, hi), false);
    const found = [];
    for (const n of needles || []) {
      if (!n) continue;
      for (let i = h.s.indexOf(n); i !== -1; i = h.s.indexOf(n, i + 1))
        found.push([lo + h.from[i], lo + h.to[i + n.length - 1]]);
    }
    found.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const r of found) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push(r.slice());
    }
    return merged;
  }

  // What to mark in text: the chunk, the query words inside it, or (chunk not
  // found) the query words anywhere.
  function ranges(text, chunk, needles) {
    const range = locate(text, chunk);
    const words = range ? findWords(text, needles, range[0], range[1]) : findWords(text, needles);
    return { chunk: range, words };
  }

  // Pieces of text[lo, hi) as {start, end, cls}; cls is "" for unmarked text.
  function pieces(lo, hi, marks) {
    const cuts = new Set([lo, hi]);
    const add = (r) => { if (r && r[1] > lo && r[0] < hi) { cuts.add(Math.max(lo, r[0])); cuts.add(Math.min(hi, r[1])); } };
    add(marks.chunk);
    marks.words.forEach(add);
    const points = [...cuts].sort((a, b) => a - b);
    const out = [];
    for (let i = 0; i + 1 < points.length; i++) {
      const a = points[i], b = points[i + 1];
      const inside = (r) => r && r[0] <= a && b <= r[1];
      const cls = marks.words.some(inside) ? "findling-word" : inside(marks.chunk) ? "findling-chunk" : "";
      out.push({ start: a, end: b, cls });
    }
    return out;
  }

  // Text nodes under root, in document order, with their offsets in the joined text.
  function textNodes(root) {
    const skip = "script,style,noscript,template,textarea";
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest(skip)
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    let text = "";
    for (let n; (n = walker.nextNode()); ) {
      nodes.push({ node: n, start: text.length });
      text += n.data;
    }
    return { nodes, text };
  }

  // Wrap the marked parts of root's text in <mark class="findling-…">.
  // Returns the first chunk (or word) mark, or null if nothing was found.
  function highlightDom(root, chunk, needles) {
    const { nodes, text } = textNodes(root);
    const marks = ranges(text, chunk, needles);
    if (!marks.chunk && !marks.words.length) return null;
    let first = null;
    for (const { node, start } of nodes) {
      const end = start + node.data.length;
      const parts = pieces(start, end, marks);
      if (!parts.some((p) => p.cls)) continue;
      const frag = document.createDocumentFragment();
      for (const p of parts) {
        const t = node.data.slice(p.start - start, p.end - start);
        if (!p.cls) { frag.append(t); continue; }
        const m = document.createElement("mark");
        m.className = p.cls;
        m.textContent = t;
        frag.append(m);
        first = first || m;
      }
      node.replaceWith(frag);
    }
    return first;
  }

  function scrollTo(el) {
    if (el) el.scrollIntoView({ block: "center", inline: "nearest" });
  }

  global.FindlingHighlight = { fold, locate, findWords, ranges, pieces, textNodes, highlightDom, scrollTo };

  // HTML pages: the server adds a <script type="application/json" id="findling-data">.
  const data = document.getElementById("findling-data");
  if (data) {
    const { chunk, needles } = JSON.parse(data.textContent);
    const style = document.createElement("style");
    style.textContent =
      "mark.findling-chunk{background:#fff1a8;color:inherit;box-shadow:0 0 0 2px #fff1a8}" +
      "mark.findling-word{background:#ffc94d;color:inherit}";
    document.head.append(style);
    const run = () => {
      const first = highlightDom(document.body, chunk, needles);
      scrollTo(first);
      // Images loading later move the text; scroll again unless the user already did.
      let moved = false;
      addEventListener("wheel", () => (moved = true), { once: true });
      addEventListener("keydown", () => (moved = true), { once: true });
      addEventListener("load", () => { if (!moved) scrollTo(first); });
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
    else run();
  }
})(window);

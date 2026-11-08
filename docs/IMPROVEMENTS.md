# Possible improvements

Ideas collected while working on search quality, performance and the result viewer
(Nov 2026). Rough order: most value for the effort first. Nothing here is implemented.

Running example: the query `definition von vater` returns the right answer first
(BGB § 1592 Vaterschaft), but most of the other 14 files are noise:

- "Definition der Vollrente" and HGB "§ 311 Definition": only the word *definition* matches.
- "Vaterland", "Großvater": substring matches of *vater*.
- HGB §§ 84–92c (Handelsvertreter): the meaning model likes legal definitions
  ("Handelsvertreter ist, wer …"), whatever they define.
- A meme about a tortoise and a World of Warcraft decision: kept as "close to the best
  meaning match", because all similarities lie between 0.79 and 0.83.

---

## 0. First: a small evaluation set

Every ranking change below is a trade-off. Without measurements it is guessing.

- 20–50 queries with the files/passages that should be found (German legal questions,
  exact terms like `§ 626 BGB`, file names, code).
- A script that runs them through `searcher.search` and reports e.g. hit@1, hit@5 and
  MRR, plus how many results are noise.
- Run it before and after each change.

Effort: small. Prerequisite for most of section 1.

## 1. Search quality

### 1.1 Reranker over the top candidates (biggest expected win)

Embedding search compares a query summary with passage summaries computed separately.
A cross-encoder reranker reads query and passage together and scores how well the
passage answers the query.

- Rerank the best ~30–50 passages after the current hybrid search, then group into files.
- Use a small multilingual cross-encoder (CPU on an Intel Mac: check the latency).
- Expected effect on the example: § 1592 BGB, § 153 ZPO and the BGH paternity decision
  on top; Handelsvertreter, Vaterland and the meme down.

Effort: medium. No reindexing. Costs some latency per search.

### 1.2 Query rewriting / expansion (RAG-style)

Queries mix the topic with what the user wants ("definition von", "was ist", "wer
zahlt …"). Those words are searched too.

- Cheap version without an LLM: treat question and meta words (definition, bedeutung,
  was ist, wer, wann, …) as filler words for the full-text part.
- With an LLM: rewrite into one or more search queries (topic words and synonyms), or
  write a hypothetical answer passage and embed that (HyDE; it fits laws, which define
  terms as "X ist, wer …"). Search each and merge.
- Trade-off: a local LLM is slow on older machines; an API sends queries off the machine.

Effort: small (filler words) to large (LLM).

### 1.3 Stricter rules for weak matches

- **2-word queries:** today one of the two words is enough (`short_query_terms = 2`).
  Requiring both would drop the "Definition der Vollrente" kind of result, but can also
  drop good answers that don't use the word literally. Decide with the evaluation set.
- **Files found only by meaning:** kept if within 0.02 similarity of the best
  (`rel_keep`), up to 5. With e5-small all scores are close, so noise gets in. Use a
  tighter value, or none when there are enough word matches.
- **Passages inside a word-matched file:** a file listed because of its words can show
  passages that only the meaning search found (HGB showed 7 Handelsvertreter sections
  while only § 311 contained a query word). Option: show meaning-only passages only when
  the file has no word match, or label them.

Effort: small each. Needs the evaluation set.

### 1.4 Whole words above substrings

The trigram index matches substrings: *vater* finds *Großvater*, *Vaterland*,
*privater*. Useful for compounds ("frist" → "Kündigungsfrist"), noisy for short words.

- Add a second FTS5 index with a word tokenizer (unicode61, optionally with a German
  stemmer) and give whole-word hits a bonus in the full-text order.

Effort: medium. Larger database; reindex of the FTS part (not the embeddings).

### 1.5 Better embedding model

`multilingual-e5-small` is fast but separates topics weakly (all 15 results of the
example between 0.79 and 0.83).

- Candidates: `multilingual-e5-base` / `-large`, `bge-m3` (multilingual, also has a
  sparse/lexical mode).
- Costs: full re-embed of ~28k passages, more memory, slower indexing and queries on
  the Intel Mac. Try the reranker (1.1) first: it may make this unnecessary.

Effort: medium (code), large (compute).

### 1.6 Long documents dominate the candidate pool

Each search takes the top 300 passages per method. Big laws (BGB, SGB V) can fill much
of that pool, pushing shorter documents out, and often show the 10-passage maximum.

- Cap passages per file inside the candidate lists (e.g. 20), or take more candidates.

Effort: small. Needs the evaluation set.

### 1.7 More than 15 results

The page asks for 15 files (`k=15`), without paging and without the total count.

- A "Show more" button (next 15) and/or "15 of 230 files".

Effort: small.

## 2. Performance and robustness

### 2.1 Searches while indexing

Indexing used ~7 of 8 cores; searches got slower and the machine had a load of ~17.

- Lower the indexer's priority (`os.nice`) or fewer parallel files while a search runs.

### 2.2 Time limit for searches

Searches stop when the client goes away. A search nobody abandons can still run long
(e.g. very common trigrams). Add a time budget (e.g. 10 s) via the same SQLite progress
handler and return what is there, or an error.

### 2.3 Full-text index maintenance

After large indexing runs, FTS5 segments can pile up. Run
`INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')` when indexing goes idle, and
measure whether query times improve.

### 2.4 Shutdown

Ctrl+C/SIGTERM should stop the server promptly even while searches and the indexer are
busy. Add a forced exit after a few seconds in case the indexer doesn't stop in time.

## 3. Result viewer (View)

- **PDF passages spanning pages:** the highlight stops at the end of the first page;
  continue on the next page.
- **PDF tables/charts:** docling's table text doesn't occur on the page (1 of 15 test
  passages). Fall back to the passage's first sentence, or the query words, on that page.
- **Office files:** open in their app without highlighting. An in-app preview from
  docling's HTML export could highlight the passage.
- **Markdown:** shown as raw source. Optional rendered view (with the fuzzy highlight).
- **Text viewer:** optional syntax highlighting. It still has its own Open button
  (removed from the result list); decide whether to keep it.
- **UTF-16 HTML pages** get no highlighting (the script can't be inserted as ASCII).

## 4. Housekeeping

- **Tests:** the browser side (highlight matching in `highlight.js` and
  `pdf-highlight.js`, the viewers, the search page's typing debounce) has no automated
  tests yet. Add some, e.g. with jsdom and pdf.js in Node.
- **pdf.js updates:** `src/findling/static/pdfjs` is a vendored copy (v6.4.299, legacy build) with
  three added lines in `web/viewer.html`, trimmed as described in `development.md`. A small
  script to update it would redo both.

# How Findling works

[← Back to the README](../README.md)

## Overview

Private local search over several folders (by meaning and by exact words), built with
[CocoIndex](https://cocoindex.io).

- **Documents** (PDF, DOCX, PPTX/PPTM, XLSX/XLSM, HTML, Markdown, AsciiDoc, LaTeX) are
  parsed with docling and chunked with its `HybridChunker`. Every chunk is embedded
  together with its heading path (e.g. `Handbook > Backups > Nightly job`).
- **Source code and plain text** are split with CocoIndex's syntax-aware `RecursiveSplitter`.
- **Embeddings**: `intfloat/multilingual-e5-small` (local, multilingual). Indexing uses
  the GPU when there is one, searches the CPU (see [GPU](configuration.md#gpu)).
- **Storage**: one SQLite file with sqlite-vec vectors and an FTS5 full-text index.
- **Search**: hybrid, see [How search ranks results](#how-search-ranks-results).
- **Incremental**: only changed files are reprocessed; deleted files and folders removed
  from the config disappear from the index. Changes to the chunking code or its settings
  in `src/findling/config.py` reprocess all files automatically (embeddings of unchanged
  chunks are reused).

## How search ranks results

Every query runs two searches over the chunks and merges them with
[Reciprocal Rank Fusion](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion):

1. **By meaning**: cosine similarity of the query embedding (sqlite-vec), top 300 chunks.
2. **By words**: SQLite FTS5 with the trigram tokenizer, so parts of words match
   (`frist` finds `Kündigungsfrist`), case and accents don't matter (`kundigung` finds
   `Kündigung`), and IDs like `INV-2024-0042` work. File path counts more than headings,
   headings more than text. Filler words (the, how, wie, ich, …) are ignored. A chunk only
   counts as a word match if it contains any query word (1–2 word queries) or at least half
   of them (longer queries); chunks with more of the words rank first.
3. Both lists are reduced to files (best chunk per file) and merged:
   `score = 1/(20 + rank by meaning) + 1/(20 + rank by words)`.
4. Shown: files with a word match, plus up to 5 files whose meaning score is close to the
   best one. If no file contains the words, the page says so: the results are then only the
   closest by meaning.

The full-text index is kept in sync with triggers and rebuilt automatically if it ever
falls behind (`src/findling/fts.py`).

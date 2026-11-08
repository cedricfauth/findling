# Configuration

[← Back to the README](../README.md)

## Where settings live

Settings, the index and its state live in `~/Library/Application Support/Findling/` on macOS
and in `~/.local/share/findling/` (or `$XDG_DATA_HOME/findling/`) on Linux.

## Folders and the config file

Folders are listed in `folders.toml` in the settings folder (see
[Where settings live](#where-settings-live); the web app edits it for you). Use another
config file with `FINDLING_CONFIG=path/to/config.toml`. Relative paths in it (folders,
`db_path`; default `findling.db`) are relative to the config file.

Folders must exist and be readable when indexing; otherwise the indexer stops instead of
deleting that folder's index (e.g. an unplugged drive, or macOS privacy settings blocking
the app that started Findling: your terminal, or Claude). Searching still works while a
folder is unavailable. Folders must not be nested inside each other.

Files that can't be parsed (corrupt, encrypted) are logged and skipped until they change.
Other errors (e.g. a file that can't be read right now) keep the file's previous index
and are retried on the next run.

To rebuild the index from scratch: stop the server, delete the index and start the
server again. On macOS:

```bash
rm ~/Library/Application\ Support/Findling/findling.db*
```

## OCR

Scanned PDFs yield no text unless OCR is on. Click **Edit settings file** in the web app
(it adds the setting if it's missing), set

```toml
ocr = true
```

and restart Findling. Indexing PDFs gets much slower; switching OCR on or off reprocesses
only PDFs. docling picks the OCR engine: Apple's Vision framework on macOS, RapidOCR on
Linux. Images (.png, .jpg, …) are not indexed either way.

## Linux

Install with the same command, run `findling` and open http://127.0.0.1:8765. There is
no folder dialog: paste the folder's path. Claude Desktop isn't available on Linux;
connect Claude Code with the stdio bridge (the same command works in other MCP clients):

```bash
claude mcp add findling -- findling-mcp
```

or over HTTP at `http://127.0.0.1:8765/mcp` (see [AI agents](usage.md#ai-agents-mcp)). The
bridge logs to `~/.local/state/findling/findling.log` (or `$XDG_STATE_HOME/findling/`).

## GPU

Indexing computes embeddings on the GPU when it can (searches always use the CPU, ~15 ms
per query):

- Apple Silicon Mac: the GPU (MPS), nothing to install.
- Intel Mac: the CPU.
- Linux with an NVIDIA GPU: the default install is CPU-only (small). For the GPU, install
  PyPI's CUDA build of PyTorch instead (about 3 GB to download, needs NVIDIA driver 580 or newer):

  ```bash
  uv tool install --python 3.12 --force --no-sources git+https://github.com/cedricfauth/findling
  ```

The log and `/api/status` show the device (`indexing on mps`). To choose it yourself, set
`FINDLING_DEVICE` (`cpu`, `mps`, `cuda`) where `findling` starts (for Claude Desktop, in the
bridge's `env`). Switching devices doesn't reindex. GPU use hasn't been tested on real Apple
Silicon or NVIDIA hardware yet: if indexing crashes or gives odd results, start with
`FINDLING_DEVICE=cpu` and open a GitHub issue with the log.

## HTML pages

Real-world HTML is prepared before docling sees it (`src/findling/html_prep.py`):

- **Layout tables** (one or two columns holding mostly prose, e.g. a margin-number
  column next to the text, often nested) become plain paragraphs. Otherwise docling
  serializes them as table cells (`1, 1 = …`), which pollutes every chunk.
- **Safety net**: after docling, the page's visible text is compared with the chunks.
  Text docling dropped (e.g. markup it doesn't understand) is still indexed as plain text
  under its heading path, and a warning is logged:
  `docling dropped N words of page.html; indexing them as plain text`.

## Extensions

Quirks of specific data sources live in `src/findling/extensions/`, outside the core
pipeline, and are switched on per config file:

```toml
extensions = ["juris"]
```

- **`juris`**: German federal law texts (gesetze-im-internet.de, Open Legal Data). Their
  tables use the CALS model (`<tgroup>`, `<row>`, `<entry>`), which HTML parsers don't
  know; without the extension the safety net still keeps their text, but only as plain
  text.

An extension is a module in `src/findling/extensions/` with a `prepare_html(root)` hook
that rewrites the parsed page (lxml) in place. Enabling, disabling or changing an
extension reprocesses the affected files.

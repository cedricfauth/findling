# Using Findling

[← Back to the README](../README.md)

## Web app

```bash
findling        # then open http://127.0.0.1:8765
```

With Claude Desktop you don't need to start it yourself: the extension starts it (see
[AI agents](#ai-agents-mcp)).

- The page is up within ~2 s; the search model loads in the background (~20 s on an
  older Intel Mac), then live indexing starts and keeps watching for changes.
- **Add folder…** opens the macOS folder dialog (or paste a path). Removing a folder
  deletes its index. Changes are saved to `folders.toml`.
- The status panel shows the indexing phase, file counters, files being processed,
  skipped files and recent errors.
- Search results are grouped per file with the best snippets (query words highlighted),
  heading path and page. A file shows every passage at least as good as the best
  passage of the weakest file listed (at least 1, at most 10), best first.
  **Show in Finder** reveals the file. Searches are linkable: `/?q=your+query`.
  Add `&debug=1` to `/api/search` to see ranks and scores.
- **View** (on each passage) opens the file in a browser tab with that passage
  highlighted and scrolled into view, query words marked inside it:
  - PDFs in the bundled [pdf.js](https://mozilla.github.io/pdf.js/) viewer, on the
    passage's page.
  - HTML pages as themselves, with a small highlight script added to the page.
  - Code, plain text and Markdown in Findling's viewer (`/view`, raw source with line numbers).
  - Office files (Word, PowerPoint, Excel) open in their app instead.

  The index doesn't always hold the file's text verbatim (docling reformats documents),
  so the passage is found by its letters and digits, by its first and last words if
  needed. If it can't be found (e.g. the file changed since indexing), only the query
  words are marked. Pages run sandboxed: their scripts can't use Findling's API.

  Every View link has the same form, `/show/<folder id>/<path>?chunk=N&q=…`; the server
  sends the browser on to the right viewer. The folder id (also in the raw file URLs,
  `/files/<folder id>/…`) comes from a secret key, so other websites can't guess the
  links and load your files. The key is kept in `findling.urlkey` next to the index, so
  links stay valid when Findling restarts. Delete that file to make all old links stop
  working.
- Listens on 127.0.0.1 only; other websites can't call its API. Port: `FINDLING_PORT`.

## Command line search

Searches the index the web app builds (also while the server is running):

```bash
findling-search "how do I restore a backup"
findling-search "Kündigungsfrist" -k 5 --folder ~/Documents
findling-search "INV-2024-0042" --debug   # with ranks and scores
findling-search "restore a backup" --json  # what AI agents get (see below)
```

## AI agents (MCP)

The web app is also an [MCP](https://modelcontextprotocol.io) server, so AI agents can
use the search for retrieval (RAG). It offers four tools. Files and folders are named by
their full path; case and Unicode form (ü as one or two characters) are ignored.

- **`search`** `(query, within=None, limit=8, offset=0, max_chars=24000)`: the same
  hybrid search and ranking as the web page. Results are grouped per file, best first:
  the file's path, type and modified date, and its best passages with text, heading
  path (`section`), page, `chunk` number, and `match` (`words` if the passage contains
  the query words, `meaning` if it's only similar). Each passage has a `url` that opens
  the file in your browser with the passage highlighted, like the View button (none for
  Office files). Agents are told to put it into their citations, so you can check a
  source with one click. The links keep working after restarts.
  - `within`: only search an indexed folder, a folder inside one, or one file (full
    path). A path with nothing indexed is an error, not an empty result.
  - `max_chars`: limit on the total passage text. Each file's best passage comes first,
    then the others. A passage that doesn't fit becomes a short excerpt around the match
    (`excerpt: true`). Files whose best passage doesn't fit even as an excerpt move to the
    next page: `total` says how many files were found, `next_offset` where to continue.
  - `match_quality`: `meaning_only` means no file contains the query words, so the
    results are only the closest by meaning.
- **`read`** `(file, at=None, section=None, before=0, after=6000)`: a file's indexed text
  around a starting point: a passage's `chunk`, a cursor (`812:340`), or a `section` by
  heading (`§ 1599`, `Tenor`; then it reads to the section's end). `before` and `after`
  are in characters; the text is cut at paragraph, sentence or word ends, and `next` /
  `prev` are cursors to read on or further back. Each new section in the text starts
  with a Markdown link line, `[§ 1600 …](url)`, that agents can copy as a citation.
- **`browse`** `(path=None, filter=None, sort="name", offset=0, limit=100)`: without
  `path`, the indexed folders and whether indexing is still catching up. A folder: its
  subfolders and files (`sort="modified"` for the newest first). A file: its sections
  with their chunk numbers (my copy of the BGB has about 2,500).
- **`open`** `(file, chunk=None, query="")`: opens a file on your computer with the passage
  highlighted: PDFs, web pages, text and code in your default browser,
  Word/PowerPoint/Excel files in their app. Only files in the indexed folders. Agents
  are told to use it only when you ask to see a passage ("open that in the browser").
  It changes nothing; it's the only tool that isn't read-only, because it opens a window.

`read` and `browse` work from the index alone, so they also work while the search model
is still loading.

Agents search the web app's live index and share its loaded model.

**Claude Code** connects over HTTP (the web app must be running: `findling`):

```bash
claude mcp add --transport http findling http://127.0.0.1:8765/mcp
```

**Claude Desktop** (and other apps that start MCP servers as a command) use the stdio
bridge `findling-mcp`. It starts in about a second, loads no model and forwards to the
web app. If the web app isn't running, the bridge starts it in the background (log on
macOS: `~/Library/Logs/Findling/findling.log`, Linux: see
[Configuration](configuration.md#linux)); it keeps running after Claude quits, until
you log out. The first call waits until the search model is loaded. After an update, the
bridge restarts a web app that it started with the old version; one you started in
Terminal keeps running until you restart it.

The easiest way to set it up is the extension bundle: download `findling.mcpb` from the
[latest release](https://github.com/cedricfauth/findling/releases/latest) (or build it:
`npx @anthropic-ai/mcpb pack mcpb dist/findling.mcpb`) and double-click it (or drag it
into Claude Desktop under Settings → Extensions). It only holds a manifest, an icon and
`launch.sh`, which runs the installed `findling-mcp`. Optional setting: the port.

Without the bundle, add it to `~/Library/Application Support/Claude/claude_desktop_config.json`.
Give the **full path**, because apps don't see your shell's PATH (`which findling-mcp`
shows it):

```json
{
  "mcpServers": {
    "findling": {
      "command": "/Users/you/.local/bin/findling-mcp"
    }
  }
}
```

With another port, set `FINDLING_PORT` in the bridge's `env` (or `FINDLING_MCP_URL` for
a web app the bridge shouldn't start). claude.ai on the web can't reach your computer:
its connectors need a public https URL, and the endpoint only listens on 127.0.0.1.

The endpoint is stateless and only answers on 127.0.0.1, with the same Host and Origin
checks as the rest of the API. Passage text comes from your files, and the tools tell
agents to treat it as data, not as instructions.


### Skill for German law

`examples/findling-recht/` is an example Claude skill (in German) for legal questions under German
law. It makes Claude ground its answers in the indexed laws and court decisions: check
which folders are indexed (and say what's missing), read the exact wording of each §,
read the relevant part of a judgment before relying on it, and link every source. It
also makes Claude mark what it couldn't find in your files. It knows how the files
are laid out: one file per law, a subfolder
per court, `-` for `/` in case numbers in file names, and Randnummern to cite. Indexed
decisions change and are never all case law, so no hit doesn't mean none exists.

Package it with skill-creator's `package_skill.py` (output: `dist/findling-recht.skill`),
then add it in Claude Desktop under Settings → Capabilities → Skills. For Claude Code,
copy the folder to `~/.claude/skills/`. It needs the Findling tools above, and folders
with laws (e.g. from gesetze-im-internet.de) and court decisions (e.g. from
openlegaldata.io) indexed.

### Skill creator

`examples/findling-skill-creator/` makes a skill like this for any field you index
(medicine, research papers, company docs …). It looks at the indexed folders, file
names and section headings with `browse` and `read`, asks what it can't see (citation
style, answer format, web policy), and writes `findling-<field>/SKILL.md`. With folders
for several fields, it offers one skill per field. Install it like the skill above,
then ask: "Create a Findling skill for my <field> folders."

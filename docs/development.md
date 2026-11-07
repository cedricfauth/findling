# Development

[← Back to the README](../README.md)

## Working on Findling

Install your checkout as the tool, so `findling`, `findling-search` and `findling-mcp`
run your code directly (run it again with `--force` after dependency changes):

```bash
uv tool install --python 3.12 --editable /path/to/findling
```

Run the tests with `uv run --with pytest pytest tests`. GitHub runs them on Linux for
every push to `main` and every pull request (`.github/workflows/test.yml`).

## The viewers

The View links (see [Using Findling](usage.md#web-app)) use these files:

- PDFs: the bundled pdf.js in `src/findling/static/pdfjs` (legacy build v6.4.299).
  Findling's add-on is `src/findling/static/pdf-highlight.js`, loaded by three added lines
  in `pdfjs/web/viewer.html`; keep them when updating pdf.js.
- HTML pages: the server adds `src/findling/static/highlight.js` to the page.
- Code and text: `src/findling/static/view.html`.

## Releasing a new version

1. Raise `version` in `pyproject.toml` and `mcpb/manifest.json` (both the same; Claude
   Desktop only updates an extension with a higher version), run the tests, commit and push.
2. Tag and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.

The Release workflow (`.github/workflows/release.yml`) then checks that the tag and both
versions match, runs the tests, builds `findling.mcpb` and publishes a GitHub release with
it. To try it without releasing: Actions → Release → Run workflow.

Users update with

```bash
uv tool install --python 3.12 --force git+https://github.com/cedricfauth/findling@vX.Y.Z
```

and double-click the new `findling.mcpb`. A web app started by the extension restarts
with the new version the next time Claude starts the extension. Changes to the
processing code (`pipeline.py`, chunking settings, the model) reindex all files once.

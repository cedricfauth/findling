"""Hybrid (meaning + exact words) search over the Findling index from the command line.

    uv run findling-search "how do I configure the backup job"
    uv run findling-search "Kündigungsfrist" -k 5 --folder ~/Documents

The web app (uv run findling) does the same without reloading the model each time.
"""

import argparse
import pathlib
import textwrap

from sentence_transformers import SentenceTransformer

from . import config as cfg
from . import searcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=10, help="number of files")
    parser.add_argument("--folder", help="only search inside this indexed folder")
    parser.add_argument("--debug", action="store_true", help="show ranks and scores")
    parser.add_argument("--json", action="store_true", help="print what AI agents get from the MCP search tool")
    parser.add_argument("--max-chars", type=int, default=None, help="with --json: budget for all passage texts")
    args = parser.parse_args()

    try:
        conf = cfg.load_config(check_folders=False)
    except cfg.ConfigError as e:
        raise SystemExit(str(e)) from None
    db = searcher.open_db(conf.db_path)
    if db is None:
        raise SystemExit(f"No index at {conf.db_path}. Start the web app (uv run findling) to build it.")

    model = SentenceTransformer(cfg.EMBED_MODEL, device="cpu")
    folder = str(pathlib.Path(args.folder).expanduser().resolve()) if args.folder else None
    query_vec = searcher.embed_query(model, args.query)
    with searcher.snapshot(db):
        response = searcher.search(
            db, args.query, query_vec, folders=list(conf.folders), k=args.k, folder=folder,
        )
        if args.json:
            from . import rag

            result = rag.build(response, max_chars=args.max_chars or rag.DEFAULT_MAX_CHARS)
            print(result.model_dump_json(indent=2, exclude_none=True))
            return

    if not response.results:
        print("No results.")
        return
    if response.weak:
        print("No good matches. Closest files:\n")
    if args.debug:
        print(response.debug, "\n")
    for rank, result in enumerate(response.results, 1):
        best = result.matches[0]
        location = result.full_path + (f"  (p. {best.page})" if best.page else "")
        print(f"{rank:>2}. {location}")
        if args.debug:
            print(f"    {result.debug}")
        if best.headings:
            print(f"    § {best.headings}")
        start, end = best.snippet
        snippet = " ".join(best.text[start:end].split())
        print(textwrap.indent(textwrap.fill(snippet, 100), "    "))
        if len(result.matches) > 1:
            print(f"    (+{len(result.matches) - 1} more matches in this file)")
        print()


if __name__ == "__main__":
    main()

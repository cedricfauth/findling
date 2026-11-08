"""Shared settings for the web server (server.py) and the search CLI (search.py)."""

import os
import pathlib
import sys
import tomllib
from dataclasses import dataclass

import tomlkit

from . import extensions

CONFIG_PATH = pathlib.Path(
    os.environ.get("FINDLING_CONFIG")
    or (
        pathlib.Path.home() / "Library/Application Support/Findling/folders.toml"
        if sys.platform == "darwin"
        else pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local/share")
        / "findling/folders.toml"
    )
)

# Changing the model (or any chunking setting below) reprocesses all files automatically.
EMBED_MODEL = "intfloat/multilingual-e5-small"
# e5 models expect these prefixes on documents vs. queries.
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "
# Hard input limit of the embedding model (tokens, incl. special tokens).
# Longer inputs would be silently truncated, so every chunk is fitted below it.
MODEL_MAX_TOKENS = 512
# Target size for docling's HybridChunker; leaves room for the prefix and file path line.
MAX_TOKENS = 460

# Files processed at the same time. CocoIndex's default (1024) starts nearly every
# file at once; docling converts one at a time and the embedding model already uses
# all cores, so the rest only wait while holding memory. 16 keeps embedding batches full.
MAX_PARALLEL_FILES = 16

TABLE_NAME = "chunks"
ISSUES_TABLE_NAME = "issues"  # skipped files and why

# Parsed by docling (layout-aware, chunked with HybridChunker + heading context).
# Only formats docling can actually read: python-docx/python-pptx reject templates
# and macro variants (.dotx, .docm, .potx, .ppsx, ...).
DOCLING_EXTS = {
    "pdf",
    "docx",
    "pptx", "pptm",
    "xlsx", "xlsm",
    "html", "htm", "xhtml",
    "md", "markdown",
    "adoc", "asciidoc",
    "tex", "latex",
}
# Extensions docling doesn't recognize by name but can parse as another format.
DOCLING_ALIASES = {"markdown": "md"}

# Read as plain text and chunked with CocoIndex's RecursiveSplitter
# (syntax-aware for source code).
TEXT_EXTS = {
    # prose / data
    "txt", "text", "rst", "org", "csv", "tsv", "log",
    "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml",
    # source code
    "py", "pyi", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "svelte",
    "java", "kt", "kts", "scala", "groovy", "go", "rs", "c", "h", "cc", "cpp", "cxx",
    "hpp", "hh", "cs", "fs", "swift", "m", "mm", "rb", "php", "pl", "lua", "r", "jl",
    "dart", "ex", "exs", "erl", "hs", "ml", "clj", "zig", "nim",
    "sh", "bash", "zsh", "fish", "ps1", "bat",
    "sql", "graphql", "proto", "css", "scss", "sass", "less",
    "cmake", "gradle", "tf", "hcl",
}

# Extension-less files that are still worth indexing (matched case-insensitively).
TEXT_FILENAMES = {"makefile", "dockerfile", "justfile", "readme", "license"}

# A pattern matching a directory also excludes everything inside it (see excluded_patterns).
DEFAULT_EXCLUDES = [
    "**/.*",  # hidden files and directories (.git, .venv, .DS_Store, ...)
    "**/node_modules",
    "**/__pycache__",
    "**/venv",
    "**/dist",
    "**/build",
    "**/target",
    "**/*.min.js",
    "**/*.lock",
    "**/package-lock.json",
]


def _case_insensitive(text: str) -> str:
    """Glob that matches text in any letter case: "pdf" -> "[pP][dD][fF]"."""
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in text)


def included_patterns() -> list[str]:
    exts = sorted(DOCLING_EXTS | TEXT_EXTS)
    return [f"**/*.{_case_insensitive(e)}" for e in exts] + [
        f"**/{_case_insensitive(name)}" for name in sorted(TEXT_FILENAMES)
    ]


def excluded_patterns(extra: tuple[str, ...] = ()) -> list[str]:
    """Each pattern also excludes the contents of matching directories. Needed for
    watch mode, which checks changed files individually instead of pruning dirs."""
    patterns: list[str] = []
    for pattern in [*DEFAULT_EXCLUDES, *extra]:
        patterns.append(pattern)
        if not pattern.endswith("/**"):
            patterns.append(pattern.rstrip("/") + "/**")
    return patterns


@dataclass(frozen=True)
class Config:
    folders: tuple[str, ...]
    exclude: tuple[str, ...]
    max_file_bytes: int
    db_path: pathlib.Path
    extensions: tuple[str, ...] = ()  # source-specific add-ons, see extensions/__init__.py


def folder_problem(folder: pathlib.Path) -> str | None:
    """Why a folder can't be indexed right now, or None if it's fine.

    Indexing must not run while a configured folder has a problem: the walker
    treats a missing or unreadable folder as empty, which would delete its whole
    index (unplugged drive, macOS privacy settings blocking the terminal, ...)."""
    if not folder.is_dir():
        return f"Folder does not exist: {folder}"
    try:
        with os.scandir(folder) as entries:
            next(entries, None)
    except PermissionError:
        return (
            f"No permission to read {folder}. On macOS, grant your terminal access "
            "under System Settings > Privacy & Security > Files and Folders "
            "(or Full Disk Access)."
        )
    return None


def nesting_problem(folders: list[str]) -> str | None:
    """Nested folders would index (and return) the same files twice."""
    for a in folders:
        for b in folders:
            if a != b and pathlib.Path(a).is_relative_to(b):
                return f"Folder {a} is inside {b}; only one of them can be indexed."
    return None


class ConfigError(ValueError):
    """The config file is missing, invalid, or lists unusable folders. A normal
    exception (not SystemExit), so the web server can report it instead of exiting."""


def load_config(path: pathlib.Path = CONFIG_PATH, *, check_folders: bool = True) -> Config:
    """check_folders=False skips the disk checks (for searching while a drive is away).
    Raises ConfigError."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}  # first start: no folders yet (adding one in the web app creates the file)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from None
    folders: list[str] = []
    for raw in data.get("folders", []):
        folder = pathlib.Path(raw).expanduser()
        if not folder.is_absolute():  # relative to the config file, like db_path
            folder = path.parent / folder
        folder = folder.resolve()
        if check_folders and (problem := folder_problem(folder)):
            raise ConfigError(problem)
        if str(folder) not in folders:
            folders.append(str(folder))
    if problem := nesting_problem(folders):
        raise ConfigError(f"{problem} Fix {path}.")
    db_path = pathlib.Path(data.get("db_path", "findling.db")).expanduser()
    if not db_path.is_absolute():
        db_path = (path.parent / db_path).resolve()
    names = tuple(dict.fromkeys(data.get("extensions", [])))
    try:
        extensions.load(names)
    except ValueError as e:
        raise ConfigError(f"{e}. Fix {path}.") from None
    return Config(
        folders=tuple(folders),
        exclude=tuple(data.get("exclude", [])),
        max_file_bytes=int(float(data.get("max_file_mb", 50)) * 1024 * 1024),
        db_path=db_path,
        extensions=names,
    )


def state_path(db_path: pathlib.Path) -> pathlib.Path:
    """CocoIndex's own change-tracking state, stored next to the index it describes."""
    return db_path.with_name(db_path.stem + ".cocoindex")


def save_folders(folders: list[str], path: pathlib.Path = CONFIG_PATH) -> None:
    """Replace the `folders` list in the config file, keeping everything else
    (comments, other settings). Any characters in paths are fine (e.g. "]")."""
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    array = tomlkit.array()
    for folder in folders:
        array.add_line(folder)
    array.add_line(indent="")  # closing bracket on its own line
    if "folders" in doc:
        doc["folders"] = array
    else:  # put it first, where people look for it
        new_doc = tomlkit.document()
        new_doc["folders"] = array
        new_doc.add(tomlkit.nl())
        for key, value in doc.body:
            if key is None:
                new_doc.add(value)
            else:
                new_doc[key.key] = value
        doc = new_doc
    new_text = tomlkit.dumps(doc)
    if tomllib.loads(new_text).get("folders") != folders:  # never write a broken config
        raise ConfigError("Could not update the folder list in the config file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)

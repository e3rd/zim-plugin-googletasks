#!/usr/bin/env python3
"""Insert a line of text into a Zim wiki page, right after the first blank line
in its content, then ask an already-running Zim instance to reload that page.

Goes through Zim's own notebook/page API (build_notebook / Page.dump / Notebook.store_page)
instead of editing the page file's raw text, the same way GoogletasksController.fetch()
does in googletasks.py - see zim_page_content.insert_after_first_blank_line, shared by
both so the "where does new content go" rule is defined in exactly one place. Using
Zim's own parser means the page's file header (Content-Type / Wiki-Format) is already
stripped out as metadata before we ever see the content lines, so there is no need to
special-case it the way a raw-text version would.

Reloading the running instance works because `zim NOTEBOOK PAGE` is dispatched over
Zim's IPC socket (zim/main/ipc.py) to the already-running process, which calls
MainWindow.open_page() -> Notebook.get_page() -> Page.check_source_changed()
(zim/notebook/page.py). That reload is safe: if the page's textbuffer has unsaved
edits, check_source_changed() leaves it alone instead of overwriting them.

IMPORTANT: Zim matches an already-open window to a notebook by comparing the
*exact* notebook URI string (see GuiCommand.run() in zim/main/__init__.py:
`if window.notebook.uri == notebook.uri`), not the resolved/realpath location.
If the page file's path goes through a symlink that differs from how the
notebook is registered in ~/.config/zim/notebooks.list, passing the resolved
path makes Zim think it's a different, not-yet-open notebook and it opens a
second MainWindow instead of reusing/reloading the existing one. So we look up
the notebook by matching realpaths against notebooks.list, and use the
*registered* (unresolved) URI/name, exactly as Zim itself would recognize it -
both to build the Notebook object and to trigger the reload.

Usage:
    insert_note.py PAGE_FILE TEXT
"""
import configparser
import subprocess
import sys
import warnings
from pathlib import Path

# Zim 0.75.2's own source (e.g. the tasklist plugin, pulled in transitively via
# zim.notebook) predates Python 3.12's stricter check for invalid escape
# sequences like '\d' or '\w' in plain (non-raw) strings, so importing it here
# floods stderr with unrelated SyntaxWarnings on every run.
warnings.filterwarnings("ignore", category=SyntaxWarning)

from zim.newfs import FilePath
from zim.notebook import build_notebook, Path as ZimPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from zim_page_content import insert_after_first_blank_line

NOTEBOOKS_LIST = Path.home() / ".config" / "zim" / "notebooks.list"


def find_registered_notebook(page_file: Path) -> tuple[str, Path] | None:
    """Match page_file against ~/.config/zim/notebooks.list by realpath, but
    return the notebook's *registered* URI (as Zim itself knows it) together
    with its resolved root folder (used to compute the page name)."""
    config = configparser.ConfigParser()
    config.read(NOTEBOOKS_LIST)
    resolved_file = page_file.resolve()

    best = None
    for section in config.sections():
        if section == "NotebookList" or "uri" not in config[section]:
            continue
        uri = config[section]["uri"]
        root = Path(uri).expanduser().resolve()
        if resolved_file == root or root in resolved_file.parents:
            if best is None or len(root.parts) > len(best[1].parts):
                best = (uri, root)
    return best


def page_name_from_file(page_file: Path, notebook_root: Path) -> str:
    rel = page_file.resolve().relative_to(notebook_root).with_suffix("")
    return ":".join(rel.parts)


def insert_into_page(notebook_uri: str, page_name: str, text: str) -> None:
    notebook, _ = build_notebook(FilePath(notebook_uri))
    page = notebook.get_page(ZimPath(page_name))
    contents = insert_after_first_blank_line(page.dump("wiki"), text + "\n")
    page.parse("wiki", "".join(contents))
    notebook.store_page(page)


def reload_in_running_zim(notebook_uri: str, page_name: str) -> None:
    # Fire-and-forget: if no Zim happens to be running yet, this "zim NOTEBOOK
    # PAGE" call becomes a brand new *primary* GUI process itself (instead of
    # just dispatching to one) and only returns once its window is closed - a
    # blocking subprocess.run() here would then hang insert_note.py until the
    # user closes that window. Popen + no wait avoids that, and dropping its
    # stdout/stderr also hides the unrelated SyntaxWarning noise Zim's own
    # (pre-3.12) source triggers on every plugin it loads.
    #
    # A missing `zim` binary must not look like a failure of the whole script: by the time we get
    # here the note is already written to the page. Cron - the main user of this script - runs with
    # a bare PATH that often misses ~/.local/bin or a snap shim, so warn and keep the exit code 0.
    try:
        subprocess.Popen(["zim", notebook_uri, page_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"Warning: note inserted, but running Zim could not be notified: {e}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} PAGE_FILE TEXT", file=sys.stderr)
        sys.exit(1)

    page_file = Path(sys.argv[1])
    text = sys.argv[2]

    found = find_registered_notebook(page_file)
    if found is None:
        print(f"Error: {page_file} is not under any notebook in {NOTEBOOKS_LIST}", file=sys.stderr)
        sys.exit(1)
    notebook_uri, notebook_root = found
    page_name = page_name_from_file(page_file, notebook_root)

    insert_into_page(notebook_uri, page_name, text)
    reload_in_running_zim(notebook_uri, page_name)


if __name__ == "__main__":
    main()

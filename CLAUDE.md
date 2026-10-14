# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file [Zim Wiki](https://zim-wiki.org/) plugin ([googletasks.py](googletasks.py)) that syncs Google Tasks into a Zim notebook page. It imports due tasks as checkbox lines, and propagates checkbox completion (in either direction) back to the Google Tasks server.

[_lib/zim_page_content.py](_lib/zim_page_content.py) holds one small helper (`insert_after_first_blank_line`) factored out so it's shared between the plugin and [bin/insert_note.py](bin/insert_note.py), a standalone CLI script (not a Zim plugin) that lets any system process/cron job insert an arbitrary line into a Zim page and then ask an already-running Zim to reload it, using Zim's own notebook/page API rather than raw text edits.

There is no build system, test suite, or Python package structure — just a plugin file, a small shared-code directory, a standalone CLI tool, and the OAuth client secret ([googletasks_client_id.json](googletasks_client_id.json)) that gets copied into Zim's plugins directory. Layout:
- `googletasks.py` — the Zim plugin (see below).
- `_lib/` — modules shared between the plugin and the standalone tools; not a Python package itself, just a folder both sides know how to reach.
- `bin/` — standalone entrypoints usable outside the Zim GUI (currently just `insert_note.py`).

## Running / installing

- Install deps: `pip install -r requirements.txt --break-system-packages` (needs Python 3.6+)
- Deploy by copying `googletasks.py`, `_lib/zim_page_content.py` and `googletasks_client_id.json` into Zim's plugin folder (`~/.local/share/zim/plugins/` on Linux, `%appdata%\zim\data\zim\plugins` on Windows), with `zim_page_content.py` directly alongside `googletasks.py` (not in a `_lib` subfolder there)
    - **Gotcha:** Zim loads plugins as submodules of the `zim.plugins` package — it adds the plugins folder to `zim.plugins.__path__`, not to `sys.path` — so `googletasks.py` must import the helper as `from zim.plugins.zim_page_content import insert_after_first_blank_line`, not a plain `import zim_page_content`. A plain top-level import silently fails as `ModuleNotFoundError`, which Zim's `PluginManager.load_plugins_from_preferences()` swallows into a bare `logger.info('No such plugin: %s', name)` — the *entire plugin* (including unrelated features like the checkbox→server sync) then silently doesn't load, with no traceback, and Zim even drops the plugin from `preferences['plugins']` when this happens. If task import/checkbox-sync mysteriously stops doing anything, check for this log line first.
- `bin/insert_note.py PAGE_FILE TEXT` can be run from anywhere Zim's own Python environment is importable (it uses `zim.notebook`/`zim.newfs` directly, and adds `_lib/` to `sys.path` itself to reach `zim_page_content.py`); it resolves which registered notebook (`~/.config/zim/notebooks.list`) a page file belongs to before touching it — see the module docstring for why the exact registered URI (not a resolved symlink path) matters for reloading an already-open window without spawning a duplicate one. It reloads the running Zim by launching `zim NOTEBOOK PAGE` via `Popen` (not waited on) — if launched when no Zim instance happens to be running yet, that call becomes a new *primary* GUI process instead of dispatching to one, so it must never be waited on synchronously.
- Enable via `Zim / Edit / Preferences / Plugins`, configure via `Zim / File / Properties / Google Tasks`
- Can also be invoked headless: `zim --plugin googletasks [NOTEBOOK]` (handled by `GoogletasksCommand`), useful for cron/anacron-driven auto-sync
- No automated tests exist in this repo; verification is manual, through a running Zim instance
- After editing `googletasks.py` or `_lib/zim_page_content.py`, Zim must be restarted (or the plugin toggled off/on in Preferences) to pick up the change — Python already has the old module loaded in memory

## Architecture

Everything lives in [googletasks.py](googletasks.py), organized around a few classes:

- **`GoogletasksPlugin`** (`PluginClass`): plugin registration and preference schema (`plugin_notebook_properties`) — these are the Notebook Properties surfaced in Zim's UI. Also monkeypatches `TextBuffer.set_bullet` at init so that ticking/unticking a checkbox in the editor routes through `GoogletasksController.task_checked` before Zim's own bullet logic runs.
- **`GoogletasksWindow`** (`MainWindowExtension`): per-window UI glue — builds the `Tools / Google Tasks` menu (`_add_actions`), and wires menu actions (import, sync, add task, the `Task lists` check items, claim OAuth permissions) to controller methods. One instance exists per open Zim window; each owns its own `GoogletasksController`.
- **`GoogletasksController`**: the actual logic — fetching/importing tasks (`fetch`), writing tasks into the page (`_store_to_page`), pushing local edits to the server (`submit_task`, `task_checked`), pulling status back down (`sync_bullets_from_server`), and task-list management (`tasklists`, `list_id`, `chosen_lists`, `toggle_list`, `refresh_task_lists`). This is the class to read first when tracing any feature.
- **`GoogleCalendarApi`**: thin wrapper around Google's `discovery.build('tasks', 'v1', ...)`, handling the OAuth2 flow (`oauth2client`) and two separate credential/scope pairs — read-only vs. write access are stored and requested as distinct credential files (`googletasks_oauth.json` / `googletasks_oauth_write.json`), so a user can grant read access without also granting write.
- **`Cache`**: a `jsonpickle`-serialized sidecar file (`googletasks.cache`, stored in the notebook's cache dir) tracking already-imported task etags (`items_ids`), known task lists (`lists`), icons of every list ever chosen (`icons`, so that unchecking and re-checking a list in the menu keeps its icon), and the last fetch timestamp — this is what prevents re-importing the same task twice and drives the "only fetch since last sync" window in `fetch()`.
- **`GoogletasksNewTaskDialog`** (`Dialog`): the "create/update task" popup, including the relative postponing-date buttons (Monday, next month, +3/6 months, etc.) configured via preferences.

### Task representation in Zim pages

Imported tasks are Zim checkbox lines with an embedded link back to the Google list and task ID, e.g.:
```
[ ] [[gtasks://<list_id>/<task_id>|💼]] Task title
```
The link label is the icon of the list (`parse_tasklists`: the `tasklist` preference `My Tasks, Work=💼` → `[(title, icon)]`, missing icons filled from `LIST_ICONS`, ✋ = `TASK_ANCHOR_SYMBOL` first). `taskAnchorTreeRe` parses it back (groups: checkbox, list ID, task ID, icon, text); links written before multi-list support have no `<list_id>/` part and are treated as the primary (first chosen) list. `get_task_id` finds the link by its `gtasks://` href, not by the icon. Optional inline start-date markup (`_{//>2021-01-01//}`, parsed via `start_date_in_title`) records a "not before this date" hint the plugin also uses to prefill the date field in the new-task dialog.

### Key data flow

1. **Import** (`fetch` → `_read_chosen_lists` → `_read_task_list`, paged via `nextPageToken`): read tasks of every chosen list due since last sync (or since midnight, on first run) → skip ones already in `cache.items_ids` → format new ones via `get_task_text` → insert after the first blank line of the target page (home page by default, or the page set in preferences).
2. **Checkbox → server** (via the `set_bullet` monkeypatch): ticking/unticking a task's checkbox in the editor calls `task_checked`, which patches (complete) or fully re-updates (un-complete, since Google auto-adds a `completed` field that must be stripped) the task on the server.
3. **Server → checkbox** (`sync_bullets_from_server`): fetches recent+completed tasks in one batch call (last 14 days) to build a status cache, falls back to per-task lookups for anything outside that window, then rewrites checkbox state for every matching line on the page.

### Gotchas worth knowing

- The `Dialog` import is wrapped in a `try/except RuntimeError` fallback to a stub class — this works around a GTK clipboard init crash seen when invoking the plugin from the command line (headless, no display).
- `monkeypatch_method` is idempotent (checks for an existing `_original` attribute) because `TextBuffer.set_bullet` is a *class*-level method shared across all open notebooks/windows — the current controller is looked up per-call via `self.notebook.plugin_googletasks`, not via closure, precisely to support multiple simultaneous Zim windows/notebooks.
- `list_id(title)` resolves a human-readable list *title* (from preferences) to the server-side list *ID* via `cache.lists`, refreshing the cache once if the title isn't found yet. `@default` is passed to the API as is; `refresh_task_lists` keeps the default list first in `cache.lists`, which `chosen_lists()` relies on to show it under its real title in the menu.
- If one of several lists cannot be read, `fetch` imports the others but keeps the old `items_ids` and does not move `last_fetched` on, so the failed list is neither re-imported nor skipped next time.
- The `Task lists` menu is built dynamically (`build_list_menu`, `Gtk.ToggleAction`s inside a UIManager `<placeholder>`), rebuilt after refresh/toggle; its merge ids go to `_uimanager_ids` so the parent's `teardown()` removes them.

## Open work / decisions

Open ideas and tasks live as GitHub issues: https://github.com/e3rd/edvard-hub/issues?q=label:"project: googletasks2zim"

Decided *not* to do (don't re-propose without a new reason):

- **Periodic sync inside the plugin** (GLib timeout instead of cron) — cron/anacron has its place: it runs even when Zim isn't running, is visible in the system and can be run manually. A timer inside the GUI would only duplicate that.
- **Removing `googletasks_client_id.json` from the repo** — rarely used project; it stays in the repo so anyone can run it without creating their own Google project. (Optional own client secret is tracked as an issue.)
- **Description/notes of a task as a separate feature** — already works (`get_task_text` appends notes as extra lines, the dialog can edit them); the remaining gaps (notes not indented, edits not pushed) belong to the "sync title/due to server" issue.

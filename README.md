# Google tasks plugin for Zim Wiki
Connects [Zim Wiki](https://zim-wiki.org/) to your Google Tasks lists – the default one, or several at once, each marked by its own icon. Append today's tasks to your Home file. Every task should be imported only once. If you change the tasks, you can upload it back to Google server. If you complete it, the completion should propagate back as well.

## Installation
Same as for the other plugins, but needs some Google Tasks API dependencies. Python3.6+ required.
* Run `python3 install.py` — installs the pip dependencies and copies `googletasks.py`, `_lib/zim_page_content.py` and `googletasks_client_id.json` into Zim's plugins folder for you.
    * `python3 install.py --link` symlinks the files instead of copying them (for development: edits apply after restarting Zim)
    * `python3 install.py --uninstall` removes the installed files again (OAuth tokens, caches and pip packages stay)
* Or do it by hand:
  * Run `pip install -r requirements.txt --break-system-packages`
  * Put the `googletasks.py`, `_lib/zim_page_content.py` and `googletasks_client_id.json` into the plugins folder
    * something like `%appdata%\zim\data\zim\plugins` in Win, or `~/.local/share/zim/plugins/` in Linux
    * **all three files go directly into that folder** – `zim_page_content.py` must end up next to `googletasks.py`, *not* in a `_lib` subfolder. Zim loads plugins as submodules of `zim.plugins`, so a misplaced helper makes the import fail – and Zim swallows that error, silently disabling the whole plugin (no tasks imported, no checkbox sync, no traceback; look for `No such plugin: googletasks` in the log)
* You enable the plugin in `Zim / Edit / Preferences / Plugins` : check mark Google tasks.
* Choose your preferences at `Zim / File / Properties / Google Tasks`
    * Import new tasks on Zim startup?
    * Sync current tasks status before import automatically? (May take a long time.)
    * Page to be updated (empty = homepage)
    * Task lists to import (empty = default task list) – comma separated titles, each optionally with an icon: `My Tasks, Work=💼`. Lists without an icon get one from the palette ✋ 💼 🏠 🛒 📚 ⭐ 🔧 🎯. The first list is where new tasks go by default. A single title (as in older versions) still works.
        * You can also check the lists in `Tools / Google Tasks / Task lists` (`Refresh task lists` loads them from the server); that edits this field for you, and the menu follows whatever you write here. Use the field to reorder the lists or pick the icons.
    * Import tasks without a due date (default on) – each one only once, when it first appears; when it gets a date later, it is not imported again
    * Import tasks assigned to me from Google Docs or Chat (default on)
    * A task gets a link before its title leading to where it comes from: 📄↗ document, 💬↗ Chat space, 📧↗ e-mail, 📝↗ Keep note (put the caret between the icon and the arrow and press Enter to open it)
    * Include start date – if true, when imported, tasks receive little **[start date](https://www.zim-wiki.org/manual/Plugins/Task_List.html)** string, ex: ">2021-01-01", and when you create a task from cursor or selection, this start time is pre-filled
* Go to `Tools / Google Tasks / Import new tasks` which imports current tasks that have not been yet imported before
    * When run for the first time, only tasks with today's due date are imported. Other times, all the tasks between current today and the day of the last import are fetched. If you need to import all history (e.g. after deleting the tasks from the page by mistake), use `Tools / Google Tasks / Import all non-completed even already imported tasks from the past` – it imports again every non-completed task, already imported ones too.
* Enjoy

### Own Google client secret (optional)
By default the plugin uses the bundled `googletasks_client_id.json`. If it hits the Google API quota, create your own OAuth client (type "Desktop app", Tasks API enabled) and point the plugin to its json – the bundled file stays untouched:
* set the environment variable `GOOGLETASKS_CLIENT_SECRET_FILE=/path/to/client_secret.json` (must be visible to the `zim` process, also in cron), or
* save it as `googletasks_client_secret.json` into Zim's config dir (`~/.config/zim/` on Linux).

The env var wins over the file, the file wins over the bundled secret. Already granted OAuth tokens belong to the client they were issued for, so after switching, re-grant them via `Tools / Google Tasks / Claim ... access`.

### Inbox sheet: tasks from Claude (optional, off by default)
A Google Sheet whose rows get imported as tasks 📥 at every sync (`Import new tasks`, on startup or from cron) and then deleted from it – so it stays empty. Meant for adding tasks from places that can write to a Google Sheet but not to Google Tasks, e.g. Claude in the mobile app ("make me a task …"), through its Google Drive / Sheets connector. You can add rows by hand, too.
* `Tools / Google Tasks / Create inbox sheet...` – asks for one more permission in the browser (access to the files the plugin creates itself, nothing else), creates `Zim inbox (Google Tasks plugin)` in your Google Drive and sets it in `File / Properties / Google Tasks / Inbox sheet`. No token to copy anywhere.
* The dialog then shows the sheet and a ready-made instruction to paste into Claude (`claude.ai / Settings / Profile`, or a project's instructions); `Copy instructions for Claude` puts it into the clipboard. When the sheet is set already, the menu item just shows the dialog again.
* Rows 1-2 (a banner saying the sheet is managed by the plugin and the column header `text | due | note`) are frozen and protected by a warning. Columns are found by the header, so if it gets changed, nothing is imported or deleted until it is fixed.
* An imported row is a plain Zim task (not a Google task), `due` becomes a Zim due date (`<2026-10-28`), `note` the next line.
* To turn it off, empty the `Inbox sheet` property.
* Needs the **Google Sheets API** enabled in the Google Cloud project of the client secret (else the plugin says so, with a link to enable it).

### Optional
If you want to synchronise automatically, I recommend using `anacron`. This line helps me get synchronised at 7:30 AM or 5 minutes after computer launch (Ubuntu 17.04) even without restarting Zim:
```bash
echo "1   5   googletasks2zim sudo su YOUR-USERNAME bash -c 'export DISPLAY=:0 && zim --plugin googletasks'" | sudo tee -a /etc/anacrontab
```

## Zim version compatibility
The plugin touches Zim internals, so the exact Zim version matters. The one difference in the API used is `XDG_DATA_HOME.subdir(...)` (older Zim) vs. `.folder(...)` (newer Zim), see the `WORKDIR` line in `googletasks.py`.

| Zim version | Plugin version | Notes |
|---|---|---|
| 0.68 and older | 1.0 (tag `0.68`) | Last version for the old Zim; current master does not work with it. |
| 0.74 | 1.1 (tag `1.1`) | Plugin options became Notebook properties. Uses `XDG_DATA_HOME.subdir`. |
| 0.74.2 (develop) | commit `9a6c741` | Switched to `XDG_DATA_HOME.folder`. |
| 0.74.3 | commit `5c5e12d` | Switched back to `XDG_DATA_HOME.subdir`. **Current master is not tested with it** – if it fails on `WORKDIR`, change `.folder` to `.subdir`. |
| 0.75 and newer | current master | Uses `XDG_DATA_HOME.folder`. |

## How the propagation works?
 * When importing, the task will preserve it's Google-ID (and the ID of its list) in a small link to the task in Google Tasks, ex: `[[https://tasks.google.com/task/<task>?list=<list>|💼]]` – click it to open the task in the browser; the icon tells the lists apart. Links written by older versions (`gtasks://<list>/<task>`, or just `gtasks://<task>` belonging to the first chosen list) still work.
 * When creating or updating a task, the dialog lets you choose its list (if more are chosen); changing it for an existing task moves it on the server.
 * If you complete the checkbox of zim-task, the plugin searches for this link and marks the task completed on server.
 * If you synchronize tasks status from server, every task on the task page will become un/checked according to the server status.

### Multiple Zim instances
Every Zim notebook can use independent task list. If they use the same list, any task will be imported into both.

## One image for hundred words
![Demonstration](example.png?raw=true)

## Missing feature?
Feel free to tell me in the issues what you'd like it to do.

## Other tools in this repository
* `bin/insert_note.py PAGE_FILE TEXT` — inserts an arbitrary line of text into a Zim page from any system script or cron job (not just from Google Tasks), then asks an already-running Zim to pick up the change. See the script's own docstring for how it finds the right notebook/page and avoids duplicating an already-open window.

## Development
Tests and lint need no Zim, GTK or network (they are stubbed in `tests/conftest.py`):
```bash
pip install pytest ruff python-dateutil
ruff check .
pytest
```

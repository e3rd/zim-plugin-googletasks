# Google tasks plugin for Zim Wiki
Connects [Zim Wiki](https://zim-wiki.org/) to your default Google Tasks lists. Append today's tasks to your Home file. Every task should be imported only once. If you change the tasks, you can upload it back to Google server. If you complete it, the completion should propagate back as well.

## Installation
Same as for the other plugins, but needs some Google Tasks API dependencies. Python3.6+ required.
* Run `python3 install.py` — installs the pip dependencies and copies `googletasks.py`, `_lib/zim_page_content.py` and `googletasks_client_id.json` into Zim's plugins folder for you.
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
    * Task list name on server (empty = default task list)
    * Include start date – if true, when imported, tasks receive little **[start date](https://www.zim-wiki.org/manual/Plugins/Task_List.html)** string, ex: ">2021-01-01", and when you create a task from cursor or selection, this start time is pre-filled
* Go to `Tools / Google Tasks / Import new tasks` which imports current tasks that have not been yet imported before
    * When run for the first time, only tasks with today's due date are imported. Other times, all the tasks between current today and the day of the last import are fetched. If you need to import all history, just go to the option in `Tools / Google Tasks`.
* Enjoy

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
 * When importing, the task will preserve it's Google-ID in a small link.
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

# Google tasks plugin for Zim Wiki
Connects to your default Google Tasks lists. Append today's tasks to your Home file. Every task should be imported only once. If you change the tasks, you can upload it back to Google server. If you complete it, the completion should propagate back as well.

## Installation
Same as for the other plugins, but needs some Google Tasks API dependencies. Python3.6+ required.
* Run `pip3 install --user dateutil google-api-python-client oauth2client==3.0.0 jsonpickle` (for current version, for the old Zim 0.68-, try `pip2 ...`)
* Put the `googletasks.py` and `googletasks_client_id.json` into the plugins folder
  * something like `%appdata%\zim\data\zim\plugins` in Win, or `~/.local/share/zim/plugins/` in Linux
* You enable the plugin in `Zim / Edit / Preferences / Plugins` : check mark Google tasks.
* Choose your preferences at `Zim / File / Properties / Google Tasks`
    * Import new tasks on Zim startup?
    * Sync current tasks status before import automatically? (May take a long time.)
    * Page to be updated (empty = homepage)
    * Task list name on server (empty = default task list)
    * Include start date – if true, when imported, tasks receive little **[start date](https://www.zim-wiki.org/manual/Plugins/Task_List.html)** string, ex: ">2021-01-01", and when you create a task from cursor or selection, this start time is pre-filled 
* Go to `Tools / Google Tasks / Import new tasks` which imports current tasks that have not been yet imported before
    * When run for the first time, only tasks with today's due date are imported. Other times, all the tasks between current today and the day of the last import are fetched. If you need to import all hisotyr XXX
* Enjoy

### Optional
If you want to synchronise automatically, I recommend using `anacron`. This line helps me get synchronised at 7:30 AM or 5 minutes after computer launch (Ubuntu 17.04) even without restarting Zim:
```bash
echo "1   5   googletasks2zim sudo su YOUR-USERNAME bash -c 'export DISPLAY=:0 && zim --plugin googletasks'" | sudo tee -a /etc/anacrontab
```

## How the propagation works?
 * When importing, the task will preserve it's Google-ID in a small link.
 * If you complete the checkbox of zim-task, the plugin searches for this link and marks the task completed on server.
 * If you synchronizes tasks status from server, every task on the task page will become un/checked according to the server status.

### Multiple Zim instances
Every Zim notebook can use independent task list. If they use the same list, any task will be imported into both.
      
## One image for hundred words
![Demonstration](example.png?raw=true)

## Missing feature?
Feel free to tell me in the issues what you'd like it to do.

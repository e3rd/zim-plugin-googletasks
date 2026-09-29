# Unreleased
* new: errors are reported (`controller.error`): status bar / error dialog in the GUI, stderr and a non-zero exit code for `zim --plugin googletasks`, instead of vanishing into the log
* changed: cache is plain versioned JSON instead of jsonpickle (old cache files are migrated once, so nothing is re-imported); `jsonpickle` dependency dropped
* new: optional own OAuth client secret (env var `GOOGLETASKS_CLIENT_SECRET_FILE` or `googletasks_client_secret.json` in Zim's config dir); bundled one stays the default
* new: `install.py --link` and `--uninstall`
* removed leftover debug menu items `_Test halo*`
* fix: tasks imported by the nightly cron run were imported again by the next manual import in an already running Zim (the window read the cache file only once, at startup)
* fix: tasks imported yesterday were imported again today (dedup cache keyed on the volatile etag and dropped past-due tasks that the next import still asks the server for)
* fix: view no longer jumps to the end of the page when an external process reloads it; the caret keeps its line even when text was inserted above it
* fix: silent ImportError was disabling the whole plugin (checkbox sync included)
* new: `bin/insert_note.py` standalone tool to insert arbitrary text into a Zim page from any script/cron job

# 1.1 (2021-11-02) 0.74 compatible
* CHANGED:
    * Plugin options become Notebook properties.
* multiple windows of Zim supported
* user can set up which task list will be used both in the notebook parameters and in the Tools menu
* tasks status sync (if you mark the task on the server, it will propagate to the notebook)
* Zim start date pre-filled
* explicitly download all non-complete history
* relative postponing buttons  

# 1.0 (2018-06-18) 0.68 compatible
Fully working version, last compatible with Zim 0.68 and older. Current master supports only newer versions. 
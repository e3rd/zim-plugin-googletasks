# Google tasks plugin for Zim Wiki
Connects to your default Google Tasks lists. Append today's tasks to your Home file. Every task should be imported only once.

## Installation
Same as for the other plugins.
* Put the googletasks.py and googletasks_client_id.json into the plugins folder
  * something like %appdata%\zim\data\zim\plugins in Win, or /~/.local/share/zim/plugins/ in Linux
* You enable the plugin in Zim/Edit/Preferences/Plugins/ check mark Google tasks.
* Go to Tools \ Update from Google tasks

### Optional
If you want to synchronise automatically, I recommend using anacron. By using this line, you should get synchronised at 7:30 AM or 5 minutes after computer launch: 
```gksudo echo "1	5	googletasks2zim	zim --plugin googletasks" >> /etc/anacrontab```


## One image for hundred words
[Demonstration](example.png)
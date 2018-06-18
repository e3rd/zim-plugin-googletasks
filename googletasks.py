#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import print_function

import datetime
from time import time
import logging
import os
import pickle
import re
import sys

import dateutil.parser
from gi.repository import Gtk
import httplib2
from apiclient import discovery
from oauth2client import client, tools
from oauth2client.file import Storage
from zim.actions import action
from zim.config import XDG_DATA_HOME, ConfigManager
from zim.formats import get_dumper
from zim.formats.wiki import Parser

try:
    from zim.gui.widgets import Dialog
    from zim.gui.widgets import InputEntry
    from zim.gui.pageview import TextBuffer
except RuntimeError:
    """ When invoking the plugin from commandline, I'm was getting this on Ubuntu 17.10.
    Defining blank Dialog class helps me to mitigate this strange issue.
    
     File "/usr/lib/python2.7/dist-packages/zim/gui/clipboard.py", line 477, in __init__
        self.clipboard = Gtk.Clipboard(selection=atom)
     RuntimeError: could not create GtkClipboard object
     """


    class Dialog():
        pass
from zim.formats import CHECKED_BOX, UNCHECKED_BOX
from zim.main import NotebookCommand
from zim.main import ZIM_APPLICATION
from zim.main.command import GtkCommand
from zim.notebook import build_notebook, Path
from zim.plugins import PluginClass
from zim.gui.mainwindow import MainWindowExtension
#from zim.gui.mainwindow import extends
#from zim.plugins import WindowExtension
#from zim.plugins import extends

try:
    import ipdb
except ImportError:
    class ipdb:
        def set_trace(cls):
            pass

logger = logging.getLogger('zim.plugins.googletasks')
WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CACHEFILE = WORKDIR + "/googletasks.cache"
CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
APPLICATION_NAME = 'googletasks2zim'
TASKANCHOR_SYMBOL = u"\u270b"
taskAnchorTreeRe = re.compile('(\[ \]\s)?\[\[gtasks://([^|]*)\|' + TASKANCHOR_SYMBOL + '\]\]\s?(.*)')
INVALID_DAY = "N/A"

# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(Googletasks2zimPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")


class GoogletasksPlugin(PluginClass):
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks to your Home file. At first run, it appends today's tasks; next, it'll append new tasks added since last synchronisation every Zim startup (and every day, when set in anacron). Every task should be imported only once, unless changed on the server.

It preserves most of Zim formatting (notably links).
You may create new task from inside Zim - just select some text, choose menu item Task from selection and set the date it should be restored in Zim from Google server.
See https://github.com/e3rd/zim-plugin-googletasks for more info.
(V1.0)
'''),
        'author': "Edvard Rejthar",
    }

    plugin_preferences = (
        # T: label for plugin preferences dialog
        ('startup_check', 'bool', _('Fetch new tasks on Zim startup'), True),
        ('page', 'string', _('What page should be used to be updated by plugin? If not set, homepage is used'), ""),
    )


class GoogletasksCommand(NotebookCommand, GtkCommand):
    '''Class to handle "zim --plugin googletasks" '''
    arguments = ('[NOTEBOOK]',)

    preferences = ConfigManager().get_config_dict('preferences.conf')['GoogletasksPlugin'].dump()

    def run(self):
        ntb_name = self.get_notebook_argument()[0] or self.get_default_or_only_notebook()
        """
        for window in ZIM_APPLICATION._windows:
            if window.ui.notebook.uri == ntb_name.uri:
                ui = window.ui;
                break
        else:
            ui = None
        """
        # not needed since 0.67rc1 reload = (lambda: ui.reload_page()) if ui else lambda: None # function exists only if zim is open
        ntb, _ = build_notebook(ntb_name)
        # reload() # save current user's work, if zim's open
        GoogletasksController(notebook=ntb, preferences=GoogletasksCommand.preferences).fetch()  # add new lines
    # reload() # save new lines


class GoogleCalendarApi(object):
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')

    @staticmethod
    def serviceObtainable(write_access=False):
        if write_access:
            return os.path.isfile(GoogleCalendarApi.permission_write_file)
        else:
            return os.path.isfile(GoogleCalendarApi.permission_read_file)

    def getService(self, write_access=False):
        if write_access:
            self.credential_path = GoogleCalendarApi.permission_write_file
            self.scope = 'https://www.googleapis.com/auth/tasks'
        else:
            self.credential_path = GoogleCalendarApi.permission_read_file
            self.scope = 'https://www.googleapis.com/auth/tasks.readonly'
        credentials = self.get_credentials()
        http = credentials.authorize(httplib2.Http())
        return discovery.build('tasks', 'v1', http=http)

    def get_credentials(self):
        """Gets valid user credentials from storage.

        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials.

        Returns:
            Credentials, the obtained credential.
        """
        store = Storage(self.credential_path)
        credentials = store.get()
        if not credentials or credentials.invalid:
            flow = client.flow_from_clientsecrets(CLIENT_SECRET_FILE, self.scope)
            flow.user_agent = APPLICATION_NAME  # Googletasks2zimPlugin.plugin_info["name"]
            argv = sys.argv
            sys.argv = sys.argv[:1]  # tools.run_flow unfortunately parses arguments and would die from any zim args
            credentials = tools.run_flow(flow, store)
            self.info('Storing credentials to ' + self.credential_path)
            sys.argv = argv
        return credentials


def monkeypatch_method(cls):
    """ decorator used for extend features of a method"""

    def decorator(func):
        setattr(cls, func.__name__ + "_original", getattr(cls, func.__name__))
        setattr(cls, func.__name__, func)
        return func

    return decorator


#@extends('MainWindow')
class GoogletasksWindow(MainWindowExtension):
    s = ""
    if not GoogleCalendarApi.serviceObtainable():
        s += "<menuitem action='permission_readonly'/>"
    if not GoogleCalendarApi.serviceObtainable(write_access=True):
        s += "<menuitem action='permission_write'/>"

    uimanager_xml = '''
    <ui>
    <menubar name='menubar'>
            <menu action='tools_menu'>
                <menu action='googletasks_menu'>
                    <placeholder name='plugin_items'>
                            <menuitem action='import_tasks'/>
                            <menuitem action='add_new_task'/>
                            <menuitem action='send_as_task'/>
                            ''' + s + '''
                    </placeholder>
                </menu>
            </menu>
    </menubar>
    </ui>
    '''

    gui = "";

    @action(_('Google Tasks'))  # T: menu item
    def googletasks_menu(self):
        pass

    def __init__(self, *args, **kwargs):
        MainWindowExtension.__init__(self, *args, **kwargs)  # super(WindowExtension, self).__init__(*args, **kwargs)
        if self.plugin.preferences['startup_check']:
            GoogletasksCommand("--plugin googletasks").run()
        controller = self.controller = GoogletasksController(window=self.window, preferences=self.plugin.preferences)

        # self.import_tasks()

        @monkeypatch_method(TextBuffer)
        def set_bullet(self, row, bullet):
            """ overriding of pageview.py/TextBuffer/set_bullet """
            if bullet in [CHECKED_BOX, UNCHECKED_BOX]:
                taskid = controller.getTaskId(row)
                if taskid:
                    controller.task_checked(taskid, bullet)
            self.set_bullet_original(row, bullet)

    @action(_('_Task from cursor or selection...'), accelerator='<ctrl><alt><shift>g')  # T: menu item
    def send_as_task(self):
        """ cut current text and call send to tasks dialog """
        buffer = self.window.pageview.textview.get_buffer()

        if not buffer.get_selection_bounds():
            # no text is selected yet - we try to detect and autoselect a task
            lineI = buffer.get_insert_iter().get_line()
            startiter = buffer.get_iter_at_line(lineI)

            while True:
                lineI += 1
                s = None
                try:
                    s = self.controller.readline(lineI)
                finally:
                    if (not s or not s.strip() or s.startswith("\n") or
                            s.startswith("\xef\xbf\xbc ") or  # begins with a checkbox
                            TASKANCHOR_SYMBOL in s):  # X.encode("utf-8")
                        lineI -= 1
                        break

            enditer = buffer.get_iter_at_line(lineI)
            enditer.forward_to_line_end()
            buffer.select_range(startiter, enditer)

        if buffer.get_selection_bounds():  # a task is selected
            task = self.controller.readTaskFromSelection()
            self.add_new_task(task=task)

    @action(_('_Add new task...'))  # T: menu item
    def add_new_task(self, task={}):
        # gui window
        self.gui = GoogletasksNewtaskDialog(self.window, _('Search'), defaultwindowsize=(300, -1)) # Xself.window.ui
        self.gui.setController(self.controller)
        self.gui.resize(300, 100)  # reset size
        self.gui.task = task

        # title
        self.labelObject = Gtk.Label(label=('Update Google task') if task.get("id", "") else ('Create Google tasks'))
        self.labelObject.set_size_request(300, -1) #  set_usize
        self.gui.vbox.pack_start(self.labelObject, expand=False, fill=True, padding=0)

        # editable text fields (title and notes)
        self.gui.inputTitle = InputEntry(allow_empty=False, placeholder_text="task title")
        self.gui.vbox.pack_start(self.gui.inputTitle, expand=False, fill=True, padding=0)
        self.gui.inputNotes = Gtk.TextView()

        # date field
        self.gui.inputDue = InputEntry(allow_empty=False)
        self.gui.inputDue.set_text(self.controller.get_time(addDays=1, mode="dateonly"))
        self.gui.inputDue.connect('changed', self.update_date)
        self.gui.labelDue = Gtk.Label(self.controller.get_time(addDays=1, mode="day"))
        hbox = Gtk.HBox(spacing=1)
        hbox.pack_start(self.gui.inputDue, expand=True, fill=True, padding=0)
        hbox.pack_start(self.gui.labelDue, expand=True, fill=True, padding=0)
        # self.gui.vbox.pack_start(self.gui.inputDue, False)
        self.gui.vbox.pack_start(hbox, expand=False, fill=True, padding=0)

        # we cant tab out from notes textarea field, hence its placed under date
        self.gui.vbox.pack_start(self.gui.inputNotes, expand=False, fill=True, padding=0)

        # 9 postponing buttons
        hbox = Gtk.HBox()
        self.gui.vbox.add(hbox)
        hbox.pack_start(Gtk.Label(label='Days: '), expand=False, fill=True, padding=0)
        for i in range(1, 10):
            button = Gtk.Button.new_with_mnemonic(label="_{} {}".format(i, self.controller.get_time(addDays=i, mode="day")))
            button.connect("clicked", self.gui.postpone, i)
            hbox.pack_start(button, expand=False, fill=True, padding=0)

        # predefined fields
        if "title" in task:
            self.gui.inputTitle.set_text(task["title"])
        if "notes" in task:
            self.gui.inputNotes.get_buffer().set_text(task["notes"])
        if "due" in task:
            logger.error("Not yet implemented")  # XX

        # display window
        self.gui.show_all()

    @action(_('_Claim read only access'))  # T: menu item
    def permission_readonly(self):
        GoogleCalendarApi().getService()

    @action(_('_Claim write access'))  # T: menu item
    def permission_write(self):
        GoogleCalendarApi().getService(write_access=True)

    @action(_('_Import new tasks'), accelerator='<ctrl><alt>g')  # T: menu item
    def import_tasks(self):
        self.controller.fetch(force=True)

    def update_date(self, _):
        try:
            day = self.controller.get_time(fromString=self.gui.inputDue.get_text(), mode="day", pastDates=False)
        except ValueError:
            day = INVALID_DAY
        self.gui.labelDue.set_text(day)


class GoogletasksNewtaskDialog(Dialog):
    def _load_task(self):
        try:
            o = self.inputNotes.get_buffer()
            self.task["notes"] = o.get_text(*o.get_bounds(), include_hidden_chars=True)
            self.task["title"] = self.inputTitle.get_text()
            if self.inputDue.get_text():
                try:
                    self.task["due"] = self.controller.get_time(fromString=self.inputDue.get_text(), mode="morning")
                except:
                    logger.error("Failed due date parsing")
                    raise
        except:
            pass

    def do_response(self, id):
        """ we cant use parent function because Esc is not handled as cancel """
        if id == Gtk.ResponseType.OK:
            self.do_response_ok()
        else:
            self.do_response_cancel()

    def do_response_ok(self):
        if self.labelDue.get_text() == INVALID_DAY:
            return False
        self._load_task()
        self.destroy()  # immediately close (so that we wont hit Ok twice)
        if not self.controller.submit_task(task=self.task):
            self.do_response_cancel()
        return True

    def do_response_cancel(self):
        """ something failed, restore original text in the zim-page """
        text = self.controller.getTaskText(self.task)
        if text:
            buffer = self.controller.window.pageview.textview.get_buffer()
            buffer.insert_parsetree_at_cursor(Parser().parse(text))
        self.destroy()

    def setController(self, controller):
        self.controller = controller

    def postpone(self, _, number):
        self.inputDue.set_text(self.controller.get_time(addDays=number, mode="dateonly"))
        self.do_response_ok()


class GoogletasksController(object):

    def __init__(self, window=None, notebook=None, preferences=None):
        self.window = window
        self.notebook = notebook
        self.preferences = preferences
        if not self.notebook and self.window:
            self.notebook = self.window.notebook #ui.notebook
        self.page = self.notebook.get_page(Path(self.preferences["page"])) if self.preferences[
            "page"] else self.notebook.get_home_page()
        logger.debug("Google tasks page: {} ".format(self.page))

    def task_checked(self, taskid, bullet):
        """ un/mark task on Google server """
        service = GoogleCalendarApi().getService(write_access=True)
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}
        try:
            if bullet is CHECKED_BOX:  # small patch will be sufficient
                result = service.tasks().patch(tasklist='@default', task=taskid, body=task).execute()
            else:  # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist='@default', task=taskid).execute()
                del task["completed"]
                task["status"] = "needsAction"
                result = service.tasks().update(tasklist='@default', task=taskid, body=task).execute()
        except:
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))
            return False
        self.info('Marked as {}'.format(task["status"]))
        return True

    def submit_task(self, task={}):
        """ Upload task to Google server """
        if "due" not in task:  # fallback - default is to postpone the task by a day
            task["due"] = self.get_time(addDays=1, mode="morning")

        service = GoogleCalendarApi().getService(write_access=True)

        try:
            if "id" in task:
                result = service.tasks().patch(tasklist='@default', task=task["id"], body=task).execute()
                self.info("Task '{}' updated.".format(task["title"]))
            else:
                result = service.tasks().insert(tasklist='@default', body=task).execute()
                self.info("Task '{}' created.".format(task["title"]))
        except:
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))
            return False

        # with open(CACHEFILE, "rb+") as f: #15
        #    recentItemIds = pickle.load(f)
        #    recentItemIds.remove(result["etag"])
        #    f.seek(0)
        #    pickle.dump(recentItemIds, f)
        #    f.write(output)
        #    f.truncate()                                

        return True

    def get_time(self, addDays=0, mode=None, fromString=None, usedate=None, pastDates=True):
        """ Time formatting function
         mode =  object | dateonly | morning | midnight | lastsec | day
        """
        dtnow = usedate if usedate else dateutil.parser.parse(fromString) if fromString else datetime.datetime.now()
        if addDays:
            dtnow += datetime.timedelta(addDays, 0)
        if not pastDates and dtnow.isoformat()[:10] < datetime.datetime.now().isoformat()[
                                                      :10]:  # why isoformat? something with tzinfo
            raise ValueError
        if mode:
            try:
                return {
                    "object": lambda: dtnow,
                    "dateonly": lambda: dtnow.isoformat()[:10],
                    "midnight": lambda: dtnow.isoformat()[:11] + "00:00:00.000Z",
                    "morning": lambda: dtnow.isoformat()[:11] + "08:00:00.000Z",
                    "lastsec": lambda: dtnow.isoformat()[:11] + "23:59:59.999Z",
                    "day": lambda: dtnow.strftime("%A").lower()[:2] # X.decode("utf-8")
                }[mode]()
            except KeyError:
                logger.error("Wrong time mode {}!".format(mode))
        return dtnow.isoformat()

    def info(self, text):
        """ Echo to the console and status bar. """
        text = "Googletasks \ " + text
        logger.info(text)
        if self.window:
            self.window.statusbar.push(0, text)

    def _get_new_items(self, dueMin):
        """ Download new tasks from google server. """
        service = GoogleCalendarApi().getService()
        try:
            results = service.tasks().list(maxResults=999,
                                           tasklist="@default",
                                           showCompleted=False,
                                           dueMin=dueMin,
                                           dueMax=self.get_time(mode="lastsec")).execute()
        except httplib2.ServerNotFoundError:
            return ""
        items = results.get('items', [])
        if not items:
            self.info('No task lists found.')
            return
        else:
            text = ""
            c = 0
            for item in items:
                if item["etag"] in self.recentItemIds:
                    if self.get_time(fromString=item["due"], mode="object").date() >= self.get_time(mode="object").date():
                        self.itemIds.add(item["etag"])
                    logger.debug('Skipping {}.'.format(item['title']))
                    continue
                self.itemIds.add(item["etag"])
                logger.info(item)
                c += 1
                text += self.getTaskText(item) + "\n"
            self.info("New tasks: {}, page: {}".format(c, self.page.get_title()))
        return text

    def getTaskText(self, task):
        """ formats task object to zim markup """
        s = "[ ] "
        if task.get("id", ""):
            s += "[[gtasks://{}|{}]] ".format(task["id"], TASKANCHOR_SYMBOL)
        if "title" not in task:
            logger.error("Task text is missing")
            return False
        s += task['title'][4:] if task['title'].startswith("[ ] ") else task[
            'title']  # we dont want to have the task started with: "[ ] [ ] "
        if task.get("notes", ""):
            s += "\n\t" + task['notes']
        # s+="\n"
        return s

    def readline(self, lineI):
        """ this crazy construct just reads a line in a page """
        buffer = self.window.pageview.textview.get_buffer()
        textiter = buffer.get_iter_at_line(lineI)
        start = buffer.get_iter_at_offset(textiter.get_offset())
        if textiter.forward_to_line_end():
            end = buffer.get_iter_at_offset(textiter.get_offset())
        else:
            end = start
        return buffer.get_slice(start, end, include_hidden_chars=True)

    def getTaskId(self, lineI):
        buffer = self.window.pageview.textview.get_buffer()
        task_anchor_pos = self.readline(lineI).find(TASKANCHOR_SYMBOL) # .encode("utf-8")
        if task_anchor_pos > -1:
            offset = task_anchor_pos + buffer.get_iter_at_line(lineI).get_offset()  # X- 2 the -2 is because of a charset mystery
            try:
                linkdata = buffer.get_link_data(buffer.get_iter_at_offset(offset))
                return linkdata["href"].split("gtasks://")[1]  # linkdata["href"]
            except:
                pass
        return None

    def readTaskFromSelection(self):
        buffer = self.window.pageview.textview.get_buffer()
        task = {}

        # lineI = buffer.get_iter_at_offset(min([x.get_offset() for x in buffer.get_selection_bounds()])).get_line() # first line in selection
        # task["id"] = self.getTaskId(lineI)

        lines = get_dumper("wiki").dump(buffer.get_parsetree(buffer.get_selection_bounds()))
        match = taskAnchorTreeRe.match("".join(lines))
        if match:
            task["id"], task["title"] = match.group(2), match.group(3).split("\n", 1)[0]
        else:
            try:
                task["title"] = lines[0].strip()
            except IndexError:
                task["title"] = ""
        task["notes"] = "".join(lines[1:])

        # text = buffer.get_text(*buffer.get_selection_bounds())
        # text = taskAnchorRe.sub("", text).split("\n", 1) # XXX #5 musi to umet checknout stazeny task: text =

        # task["title"] = text[0]
        # task["notes"]="".join(text[1:])

        buffer.delete(*buffer.get_selection_bounds())  # cuts the task
        return task

    def fetch(self, force=False):
        """ Get the new tasks and insert them into page
        :type force: Cancels the action if False and cache file have been accessed recently.
        """

        # Load the list of recently seen tasks
        self.itemIds = set()
        if os.path.isfile(CACHEFILE):
            if not force:
                MAX_HOURS = 3
                now = time()
                last_time = os.path.getmtime(CACHEFILE)
                if now - last_time < MAX_HOURS * 3600 and \
                        datetime.datetime.now().day is datetime.datetime.fromtimestamp(last_time).day:
                    # if checked in last hours and there wasn't midnight (new tasks time) since then
                    return
            with open(CACHEFILE, "rb") as f:
                self.recentItemIds = pickle.load(f)
            dueMin = self.get_time(usedate=datetime.datetime.fromtimestamp(os.path.getmtime(CACHEFILE)), mode="midnight")
        else:
            self.recentItemIds = set()
            dueMin = self.get_time(mode="midnight")

        # Do internal fetching of new tasks text
        text = self._get_new_items(dueMin)
        if not text:
            os.utime(CACHEFILE, None)  # we touch the file
            return

        # Insert tasks string into page
        counter = 1
        contents = []
        for line in self.page.dump("wiki"):
            contents.append(line)
            if line.strip() == "":  # ignore first empty line
                counter -= 1
            if counter == 0:
                contents.append(text)
                counter = -1
        bounds = None
        if self.window and self.window.pageview.get_page().name is self.page.name:
            # HP is current page - we use GTK buffers, caret stays at position
            buffer = self.window.pageview.textview.get_buffer()
            bounds = [x.get_offset() for x in buffer.get_selection_bounds()]
            if not bounds:
                i = buffer.get_insert_iter()
                bounds = [i.get_offset()] * 2

        self.page.parse('wiki', "".join(contents))
        self.notebook.store_page(self.page)
        if bounds:
            buffer.select_range(buffer.get_iter_at_offset(bounds[0]), buffer.get_iter_at_offset(bounds[1]))

        # Save the list of successfully imported task
        with open(CACHEFILE, "wb") as f:
            pickle.dump(self.itemIds, f)

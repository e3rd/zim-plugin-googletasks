#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import print_function

import datetime
import logging
import os
import pickle
import re
import sys
from time import time

import dateutil.parser
import httplib2
from apiclient import discovery
from gi.repository import Gtk
from oauth2client import client, tools
from oauth2client.file import Storage
from zim.actions import action, get_gtk_actiongroup, ActionMethod
from zim.config import XDG_DATA_HOME, ConfigManager
from zim.formats import get_dumper
from zim.formats.wiki import Parser

try:
    from zim.gui.widgets import Dialog
    from zim.gui.widgets import InputEntry
    from zim.gui.pageview import TextBuffer
    from zim.gui.mainwindow import MainWindow
except RuntimeError:
    """ When invoking the plugin from commandline, I'm was getting this on Ubuntu 17.10.
    Defining blank Dialog class helps me to mitigate this strange issue.
    
     File "/usr/lib/python2.7/dist-packages/zim/gui/clipboard.py", line 477, in __init__
        self.clipboard = Gtk.Clipboard(selection=atom)
     RuntimeError: could not create GtkClipboard object
     """


    class Dialog:
        pass
from zim.formats import CHECKED_BOX, UNCHECKED_BOX
from zim.main import NotebookCommand
from zim.main.command import GtkCommand
from zim.notebook import build_notebook, Path
from zim.plugins import PluginClass
from zim.gui.mainwindow import MainWindowExtension

logger = logging.getLogger('zim.plugins.googletasks')
WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CACHEFILE = WORKDIR + "/googletasks.cache"
CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
APPLICATION_NAME = 'googletasks2zim'
TASKANCHOR_SYMBOL = u"\u270b"
taskAnchorTreeRe = re.compile(r'(\[ \]\s)?\[\[gtasks://([^|]*)\|' + TASKANCHOR_SYMBOL + r'\]\]\s?(.*)')
INVALID_DAY = "N/A"


class GoogletasksPlugin(PluginClass):
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks to your Home file. At first run, 
it appends today's tasks; next, it'll append new tasks added since last synchronisation every Zim startup
 (and every day, when set in anacron). Every task should be imported only once, unless changed on the server.

It preserves most of Zim formatting (notably links).
You may create new task from inside Zim - just select some text, choose menu item Task from selection and set the date
 it should be restored in Zim from Google server.
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
    """Class to handle "zim --plugin googletasks" """
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
        ntb, _ = build_notebook(ntb_name)
        GoogletasksController(notebook=ntb, preferences=GoogletasksCommand.preferences).fetch()  # add new lines


class GoogleCalendarApi:
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')

    def __init__(self):
        self.credential_path = None
        self.scope = None

    @staticmethod
    def service_obtainable(write_access=False):
        if write_access:
            return os.path.isfile(GoogleCalendarApi.permission_write_file)
        else:
            return os.path.isfile(GoogleCalendarApi.permission_read_file)

    def get_service(self, write_access=False):
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
            GoogletasksController.info('Storing credentials to ' + self.credential_path)
            sys.argv = argv
        return credentials


def monkeypatch_method(cls):
    """ decorator used for extend features of a method"""

    def decorator(func):
        setattr(cls, func.__name__ + "_original", getattr(cls, func.__name__))
        setattr(cls, func.__name__, func)
        return func

    return decorator


class GoogletasksWindow(MainWindowExtension):
    gui = ""

    # @action(_('Google Tasks'), menuhints="view")  # T: menu item
    # def googletasks_menu(self):
    #    pass

    def __init__(self, *args, **kwargs):
        self.label_object = None

        MainWindowExtension.__init__(self, *args, **kwargs)  # super(WindowExtension, self).__init__(*args, **kwargs)
        if self.plugin.preferences['startup_check']:
            # XX What it is good for? This means GoogletasksController launches twice which is probably not ideal
            GoogletasksCommand("--plugin googletasks").run()
        controller = self.controller = GoogletasksController(window=self.window, preferences=self.plugin.preferences)

        # group = get_gtk_actiongroup(self)
        # group.add_actions(MENU_ACTIONS)
        # self._uimanager.insert_action_group(group, 0)

        # noinspection PyShadowingNames
        @monkeypatch_method(TextBuffer)
        def set_bullet(self, row, bullet, indent=None):
            """ overriding of pageview.py/TextBuffer/set_bullet """
            if bullet in [CHECKED_BOX, UNCHECKED_BOX]:
                taskid = controller.get_task_id(row)
                if taskid:
                    controller.task_checked(taskid, bullet)
            self.set_bullet_original(row, bullet, indent=indent)

    def _add_actions(self, uimanager):
        """ Set up menu items.
            Here we override parent function that adds menu items.
            XX It would be much nicer if we could define those via @action menuhint decorator, not possible now.
            So that we must define XML ourselves.

            If we did not override, all the items would be placed directly in Tools, not in Tools / Google Tasks.
        """

        def get_actions(obj):
            import inspect
            return inspect.getmembers(obj.__class__, lambda m: isinstance(m, ActionMethod))

        actions = get_actions(self)
        if actions:
            self._uimanager = uimanager

            actiongroup = get_gtk_actiongroup(self)
            uimanager.insert_action_group(actiongroup, 0)

            # Set up menu items. Here we change parent function behaviour.
            s = []
            if GoogleCalendarApi.service_obtainable():
                s.append("<menuitem action='permission_readonly'/>")
            if GoogleCalendarApi.service_obtainable(write_access=True):
                s.append("<menuitem action='permission_write'/>")
            xml = '''
                    <ui>
                    <menubar name='menubar'>
                            <menu action='tools_menu'>
                                <menu action='googletasks_menu'>
                                    <menuitem action='import_tasks'/>
                                    <menuitem action='add_new_task'/>
                                    <menuitem action='send_as_task'/>
                                    ''' + "\n".join(s) + '''                            
                                </menu>
                            </menu>
                    </menubar>
                    </ui>
                    '''

            MENU_ACTIONS = (('googletasks_menu', None, _('_Google tasks')),)
            actiongroup.add_actions(MENU_ACTIONS)
            self._uimanager.add_ui_from_string(xml)

    @action(_('_Task from cursor or selection...'), accelerator='<ctrl><alt><shift>g')  # T: menu item
    def send_as_task(self):
        """ cut current text and call send to tasks dialog """
        buffer = self.window.pageview.textview.get_buffer()

        if not buffer.get_selection_bounds():
            # no text is selected yet - we try to detect and autoselect a task
            line_i = buffer.get_insert_iter().get_line()
            startiter = buffer.get_iter_at_line(line_i)

            while True:
                line_i += 1
                s = None
                try:
                    s = self.controller.readline(line_i)
                finally:
                    if (not s or not s.strip() or s.startswith("\n") or
                            s.startswith("\xef\xbf\xbc ") or  # begins with a checkbox
                            TASKANCHOR_SYMBOL in s):  # X.encode("utf-8")
                        line_i -= 1
                        break

            enditer = buffer.get_iter_at_line(line_i)
            enditer.forward_to_line_end()
            buffer.select_range(startiter, enditer)

        if buffer.get_selection_bounds():  # a task is selected
            task = self.controller.read_task_from_selection()
            self.add_new_task(task=task)

    # noinspection PyArgumentList
    @action(_('_Add new task...'))  # T: menu item
    def add_new_task(self, task=None):
        # gui window
        if task is None:
            task = {}
        self.gui = GoogletasksNewTaskDialog(self.window, _('Search'), defaultwindowsize=(300, -1))  # Xself.window.ui
        self.gui.set_controller(self.controller)
        self.gui.resize(300, 100)  # reset size
        self.gui.task = task

        # title
        self.label_object = Gtk.Label(label='Update Google task' if task.get("id", "") else 'Create Google tasks')
        self.label_object.set_size_request(300, -1)  # set_usize
        self.gui.vbox.pack_start(self.label_object, expand=False, fill=True, padding=0)

        # editable text fields (title and notes)
        self.gui.input_title = InputEntry(allow_empty=False, placeholder_text="task title")
        self.gui.vbox.pack_start(self.gui.input_title, expand=False, fill=True, padding=0)
        self.gui.input_notes = Gtk.TextView()

        # date field
        self.gui.input_due = InputEntry(allow_empty=False)
        self.gui.input_due.set_text(self.controller.get_time(add_days=1, mode="dateonly"))
        self.gui.input_due.connect('changed', self.update_date)
        self.gui.label_due = Gtk.Label(self.controller.get_time(add_days=1, mode="day"))
        hbox = Gtk.HBox(spacing=1)
        hbox.pack_start(self.gui.input_due, expand=True, fill=True, padding=0)
        hbox.pack_start(self.gui.label_due, expand=True, fill=True, padding=0)
        # self.gui.vbox.pack_start(self.gui.input_due, False)
        self.gui.vbox.pack_start(hbox, expand=False, fill=True, padding=0)

        # we cant tab out from notes textarea field, hence its placed under date
        self.gui.vbox.pack_start(self.gui.input_notes, expand=False, fill=True, padding=0)

        # 9 postponing buttons
        hbox = Gtk.HBox()
        self.gui.vbox.add(hbox)
        hbox.pack_start(Gtk.Label(label='Days: '), expand=False, fill=True, padding=0)
        for i in range(1, 10):
            button = Gtk.Button.new_with_mnemonic(
                label="_{} {}".format(i, self.controller.get_time(add_days=i, mode="day")))
            button.connect("clicked", self.gui.postpone, i)
            hbox.pack_start(button, expand=False, fill=True, padding=0)

        # predefined fields
        if "title" in task:
            self.gui.input_title.set_text(task["title"])
        if "notes" in task:
            self.gui.input_notes.get_buffer().set_text(task["notes"])
        if "due" in task:
            logger.error("Not yet implemented")  # XX

        # display window
        self.gui.show_all()

    @action(_('_Claim read only access'))  # T: menu item
    def permission_readonly(self):
        GoogleCalendarApi().get_service()

    @action(_('_Claim write access'))  # T: menu item
    def permission_write(self):
        GoogleCalendarApi().get_service(write_access=True)

    @action(_('_Import new tasks'), accelerator='<ctrl><alt>g')  # T: menu item
    def import_tasks(self):
        self.controller.fetch(force=True)

    def update_date(self, _):
        try:
            day = self.controller.get_time(from_string=self.gui.input_due.get_text(), mode="day", past_dates=False)
        except ValueError:
            day = INVALID_DAY
        self.gui.label_due.set_text(day)


class GoogletasksNewTaskDialog(Dialog):

    def __init__(self, *args, **kwargs):
        self.input_title = None
        self.input_due = None
        self.input_notes = None
        self.controller = None
        super().__init__(*args, **kwargs)

    def _load_task(self):
        # noinspection PyBroadException
        try:
            o = self.input_notes.get_buffer()
            self.task["notes"] = o.get_text(*o.get_bounds(), include_hidden_chars=True)
            self.task["title"] = self.input_title.get_text()
            if self.input_due.get_text():
                try:
                    self.task["due"] = self.controller.get_time(from_string=self.input_due.get_text(), mode="morning")
                except Exception:
                    logger.error("Failed due date parsing")
                    raise
        except Exception:
            pass

    def do_response(self, id_):
        """ we cant use parent function because Esc is not handled as cancel """
        if id_ == Gtk.ResponseType.OK:
            self.do_response_ok()
        else:
            self.do_response_cancel()

    def do_response_ok(self):
        if self.label_due.get_text() == INVALID_DAY:
            return False
        self._load_task()
        self.destroy()  # immediately close (so that we wont hit Ok twice)
        if not self.controller.submit_task(task=self.task):
            self.do_response_cancel()
        return True

    def do_response_cancel(self):
        """ something failed, restore original text in the zim-page """
        text = self.controller.get_task_text(self.task)
        if text:
            buffer = self.controller.window.pageview.textview.get_buffer()
            buffer.insert_parsetree_at_cursor(Parser().parse(text))
        self.destroy()

    def set_controller(self, controller):
        self.controller = controller

    def postpone(self, _, number):
        self.input_due.set_text(self.controller.get_time(add_days=number, mode="dateonly"))
        self.do_response_ok()


class GoogletasksController:
    window = None  # type: MainWindow

    def __init__(self, window=None, notebook=None, preferences=None):
        GoogletasksController.window = self.window = window
        self.notebook = notebook
        self.preferences = preferences
        if not self.notebook and self.window:
            self.notebook = self.window.notebook  # ui.notebook
        print("CON TROLLER INIT*******************")
        self._refresh_page()
        logger.debug("Google tasks page: {} ".format(self.page))

        self.item_ids = None
        self.recent_item_ids = None

    def _refresh_page(self):
        self.page = self.notebook.get_page(Path(self.preferences["page"])) if self.preferences["page"] \
            else self.notebook.get_home_page()

    def task_checked(self, taskid, bullet):
        """ un/mark task on Google server """
        service = GoogleCalendarApi().get_service(write_access=True)
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}
        # noinspection PyBroadException
        try:
            if bullet is CHECKED_BOX:  # small patch will be sufficient
                service.tasks().patch(tasklist='@default', task=taskid, body=task).execute()
            else:  # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist='@default', task=taskid).execute()
                del task["completed"]
                task["status"] = "needsAction"
                service.tasks().update(tasklist='@default', task=taskid, body=task).execute()
        except Exception:
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))
            return False
        self.info('Marked as {}'.format(task["status"]))
        return True

    def submit_task(self, task=None):
        """ Upload task to Google server """
        if task is None:
            task = {}
        if "due" not in task:  # fallback - default is to postpone the task by a day
            task["due"] = self.get_time(add_days=1, mode="morning")

        service = GoogleCalendarApi().get_service(write_access=True)

        # noinspection PyBroadException
        try:
            if "id" in task:
                service.tasks().patch(tasklist='@default', task=task["id"], body=task).execute()
                self.info("Task '{}' updated.".format(task["title"]))
            else:
                service.tasks().insert(tasklist='@default', body=task).execute()
                self.info("Task '{}' created.".format(task["title"]))
        except Exception:
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))
            return False

        # with open(CACHEFILE, "rb+") as f: #15
        #    recent_item_ids = pickle.load(f)
        #    recent_item_ids.remove(result["etag"])
        #    f.seek(0)
        #    pickle.dump(recent_item_ids, f)
        #    f.write(output)
        #    f.truncate()                                

        return True

    @staticmethod
    def get_time(add_days=0, mode=None, from_string=None, use_date=None, past_dates=True):
        """ Time formatting function
         mode =  object | dateonly | morning | midnight | lastsec | day
        """
        dtnow = use_date if use_date else dateutil.parser.parse(from_string) if from_string else datetime.datetime.now()
        if add_days:
            dtnow += datetime.timedelta(add_days, 0)
        if not past_dates and dtnow.isoformat()[:10] < datetime.datetime.now().isoformat()[:10]:
            # why isoformat? something with tzinfo
            raise ValueError
        if mode:
            try:
                return {
                    "object": lambda: dtnow,
                    "dateonly": lambda: dtnow.isoformat()[:10],
                    "midnight": lambda: dtnow.isoformat()[:11] + "00:00:00.000Z",
                    "morning": lambda: dtnow.isoformat()[:11] + "08:00:00.000Z",
                    "lastsec": lambda: dtnow.isoformat()[:11] + "23:59:59.999Z",
                    "day": lambda: dtnow.strftime("%A").lower()[:2]  # X.decode("utf-8")
                }[mode]()
            except KeyError:
                logger.error("Wrong time mode {}!".format(mode))
        return dtnow.isoformat()

    @staticmethod
    def info(text):
        """ Echo to the console and status bar. """
        text = "[Googletasks] " + text
        logger.info(text)
        if GoogletasksController.window:
            GoogletasksController.window.statusbar.push(0, text)

    def _get_new_items(self, due_min):
        """ Download new tasks from google server. """
        service = GoogleCalendarApi().get_service()
        try:
            results = service.tasks().list(maxResults=999,
                                           tasklist="@default",
                                           showCompleted=False,
                                           dueMin=due_min,
                                           dueMax=self.get_time(add_days=1, mode="midnight")  # for format see #17
                                           ).execute()
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
                if item["etag"] in self.recent_item_ids:
                    if self.get_time(from_string=item["due"], mode="object").date() \
                            >= self.get_time(mode="object").date():
                        self.item_ids.add(item["etag"])
                    logger.debug('Skipping {}.'.format(item['title']))
                    continue
                self.item_ids.add(item["etag"])
                logger.info("Appending {}.".format(item["title"]))
                logger.debug(item)
                c += 1
                text += self.get_task_text(item) + "\n"
            self.info("New tasks: {}, page: {}".format(c, self.page.get_title()))
        return text

    @staticmethod
    def get_task_text(task):
        """ formats task object to zim markup """
        s = "[ ] "
        if task.get("id", ""):
            s += "[[gtasks://{}|{}]] ".format(task["id"], TASKANCHOR_SYMBOL)
        if "title" not in task:
            logger.error("Task text is missing")
            return False
        # we dont want to have the task started with: "[ ] [ ] "
        s += task['title'][4:] if task['title'].startswith("[ ] ") else task['title']
        if task.get("notes", ""):
            s += "\n" + task['notes']
        return s

    def readline(self, line_i):
        """ this crazy construct just reads a line in a page """
        buffer = self.window.pageview.textview.get_buffer()
        textiter = buffer.get_iter_at_line(line_i)
        start = buffer.get_iter_at_offset(textiter.get_offset())
        if textiter.forward_to_line_end():
            end = buffer.get_iter_at_offset(textiter.get_offset())
        else:
            end = start
        return buffer.get_slice(start, end, include_hidden_chars=True)

    def get_task_id(self, line_i):
        buffer = self.window.pageview.textview.get_buffer()
        task_anchor_pos = self.readline(line_i).find(TASKANCHOR_SYMBOL)
        if task_anchor_pos > -1:
            offset = task_anchor_pos + buffer.get_iter_at_line(line_i).get_offset()
            # noinspection PyBroadException
            try:
                linkdata = buffer.get_link_data(buffer.get_iter_at_offset(offset))
                return linkdata["href"].split("gtasks://")[1]
            except Exception:
                pass
        return None

    def read_task_from_selection(self):
        buffer = self.window.pageview.textview.get_buffer()
        task = {}

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

        buffer.delete(*buffer.get_selection_bounds())  # cuts the task
        return task

    def fetch(self, force=False):
        """ Get the new tasks and insert them into page
        :type force: Cancels the action if False and cache file have been accessed recently.
        """

        # Load the list of recently seen tasks
        self._refresh_page()
        self.item_ids = set()
        cache_exists = os.path.isfile(CACHEFILE)
        if cache_exists:
            if not force:
                max_hours = 3
                now = time()
                last_time = os.path.getmtime(CACHEFILE)
                if now - last_time < max_hours * 3600 and \
                        datetime.datetime.now().day is datetime.datetime.fromtimestamp(last_time).day:
                    # if checked in last hours and there wasn't midnight (new tasks time) since then
                    return
            with open(CACHEFILE, "rb") as f:
                self.recent_item_ids = pickle.load(f)
            due_min = self.get_time(use_date=datetime.datetime.fromtimestamp(os.path.getmtime(CACHEFILE)),
                                    mode="midnight")
        else:
            self.recent_item_ids = set()
            due_min = self.get_time(mode="midnight")

        # Do internal fetching of new tasks text
        text = self._get_new_items(due_min)
        if not text:
            if cache_exists:
                os.utime(CACHEFILE, None)  # we touch the file
            return

        # Insert tasks string into page
        appended = False
        contents = []
        for line in self.page.dump("wiki"):
            contents.append(line)
            if line.strip() == "" and not appended:  # insert after first empty line
                appended = True
                contents.append(text)
        if not appended:  # or insert at the end of the page text
            contents.append(text)

        bounds = buffer = None
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
            pickle.dump(self.item_ids, f)


# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(GoogletasksPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")

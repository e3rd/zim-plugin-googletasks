#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import print_function

import datetime
import logging
import os
import re
import sys
from pathlib import Path
from time import time

import dateutil.parser
import httplib2
import jsonpickle
from apiclient import discovery
from gi.repository import Gtk
from googleapiclient.errors import Error
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
from zim.notebook import build_notebook, Path as ZimPath
from zim.plugins import PluginClass
from zim.gui.mainwindow import MainWindowExtension

logger = logging.getLogger('zim.plugins.googletasks')
CACHE_FILE = "googletasks.cache"
WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
APPLICATION_NAME = 'googletasks2zim'
TASK_ANCHOR_SYMBOL = u"\u270b"
taskAnchorTreeRe = re.compile(r'(\[.\]\s)?\[\[gtasks://([^|]*)\|' + TASK_ANCHOR_SYMBOL + r'\]\]\s?(.*)')
start_date_in_title = re.compile(r'(.*)>(\d{4}-\d{2}(-\d{2})?)(.*)')  # (_\{//)?  (//})?
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
See `File / Properties / Google Tasks` for options, `Tools / Google Tasks` for actions
 and https://github.com/e3rd/zim-plugin-googletasks for more info.
(V1.1)
'''),
        'author': "Edvard Rejthar",
    }

    plugin_notebook_properties = (
        # T: label for plugin preferences dialog
        ('startup_check', 'bool', _('Import new tasks on Zim startup'), True),
        ('auto_sync', 'bool', _('Sync tasks status before import'), False),
        ('page', 'page', _('Page to be updated (empty = homepage)'), ""),
        ('tasklist', 'string', _('Task list name on server (empty = default)'), ""),
        ('include_start_date', 'bool', _('Include start date'), False)
    )

    def __init__(self):
        super().__init__()

        # noinspection PyShadowingNames
        @monkeypatch_method(TextBuffer)
        def set_bullet(self, line, bullet, indent=None):
            """ Overriding of pageview.py/TextBuffer/set_bullet """
            # Note we cannot use any closure variable inherited from GoogletasksWindow here
            # because when using multiple Zim windows,
            # it would contains the first one always because TextBuffer.set_bullet exists only once.
            # However we can use previously set self.notebook.self.plugin_googletasks
            if bullet in [CHECKED_BOX, UNCHECKED_BOX]:
                controller = self.notebook.plugin_googletasks
                task_id = controller.get_task_id(line, buffer=self)
                if task_id:
                    controller.task_checked(task_id, bullet)
            self.set_bullet_original(line, bullet, indent=indent)


class GoogletasksCommand(NotebookCommand, GtkCommand):
    """Class to handle "zim --plugin googletasks" """
    arguments = ('[NOTEBOOK]',)

    preferences = ConfigManager().get_config_dict('preferences.conf')['GoogletasksPlugin'].dump()

    def run(self):
        ntb_name = self.get_notebook_argument()[0] or self.get_default_or_only_notebook()
        ntb, _ = build_notebook(ntb_name)
        GoogletasksController(notebook=ntb, preferences=GoogletasksCommand.preferences).fetch()  # add new lines


class GoogleCalendarApi:
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')

    def __init__(self, controller):
        self.credential_path = None
        self.scope = None
        self.controller = controller

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
            self.controller.info('Storing credentials to ' + self.credential_path)
            sys.argv = argv
        return credentials


def monkeypatch_method(cls):
    """ Decorator used for extend features of a method
        If it seems the methods has been already monkey patched (func.__name__ + "_original" exists), it does nothing.
    """

    def decorator(func):
        if not hasattr(cls, func.__name__ + "_original"):
            setattr(cls, func.__name__ + "_original", getattr(cls, func.__name__))
            setattr(cls, func.__name__, func)
        return func

    return decorator


class GoogletasksWindow(MainWindowExtension):

    def __init__(self, plugin, window):
        controller = self.controller = GoogletasksController(
            window=window,
            preferences=plugin.notebook_properties(window.notebook)
        )
        MainWindowExtension.__init__(self, plugin, window)  # super(WindowExtension, self).__init__(*args, **kwargs)

        if self.plugin.preferences['startup_check']:
            controller.fetch()

    def _add_actions(self, uimanager):
        """ Set up menu items.
            Here we override parent function that adds menu items.

            If we did not override, all the items would be placed directly in Tools, not in Tools / Google Tasks.
        """

        def get_actions(obj):
            import inspect
            return inspect.getmembers(obj.__class__, lambda m: isinstance(m, ActionMethod))

        actions = get_actions(self)
        if actions:
            self._uimanager = uimanager

            action_group = get_gtk_actiongroup(self)
            uimanager.insert_action_group(action_group, 0)

            # Set up menu items. Here we change parent function behaviour.
            permissions = []
            if GoogleCalendarApi.service_obtainable():
                permissions.append("<menuitem action='permission_readonly'/>")
            if GoogleCalendarApi.service_obtainable(write_access=True):
                permissions.append("<menuitem action='permission_write'/>")
            if permissions:
                permissions = ["<separator/>"] + permissions

            lists = []
            for title in self.controller.cache.lists:
                # Apostrophe suppressed; I was not able to reliably slash it, getting a strange error:
                # gi.repository.GLib.GError: g-markup-error-quark: Error on line 12 char 82:
                # Odd character “l”, expected a “=” after attribute name “s” of element “menuitem” (2)
                el = "change_list_{}".format(title.replace(r"'", r""))
                lists.append(f"<menuitem action='{el}'/>")
                action_group.add_actions((
                    (el, None, _(f'_{title}'), None, None, self.controller.change_task_list_closure(title)),))

            xml = '''
                    <ui>
                    <menubar name='menubar'>
                            <menu action='tools_menu'>
                                <menu action='googletasks_menu'>
                                    <menuitem action='import_tasks'/>
                                    <menuitem action='sync_status'/>
                                    <menuitem action='send_as_task'/>
                                    <menuitem action='add_new_task'/>
                                    <menuitem action='import_history'/>                                    
                                    ''' + "\n".join(permissions) + '''
                                    <menu action='choose_task_list'>                            
                                        ''' + "\n".join(lists) + '''
                                        <separator/>
                                        <menuitem action='refresh_task_lists'/>
                                    </menu>                                    
                                </menu>
                            </menu>
                    </menubar>
                    </ui>
                    '''

            # Add menu actions seen in XML
            action_group.add_actions((('googletasks_menu', None, _('_Google tasks')),))
            action_group.add_actions((('choose_task_list', None, _('_Choose task list')),))
            self._uimanager.add_ui_from_string(xml)

    @action(_('_Task from cursor or selection...'), accelerator='<ctrl><alt><shift>g')  # T: menu item
    def send_as_task(self):
        """ cut current text and call send to tasks dialog """
        buffer = self.window.pageview.textview.get_buffer()

        if not buffer.get_selection_bounds():
            # no text is selected yet - we try to detect and auto-select a task
            line_i = buffer.get_insert_iter().get_line()
            start_iter = buffer.get_iter_at_line(line_i)

            while True:
                line_i += 1
                s = None
                try:
                    s = self.controller.readline(line_i, buffer)
                finally:
                    if (not s or not s.strip() or s.startswith("\n") or
                            s.startswith("\xef\xbf\xbc ") or  # begins with a checkbox
                            TASK_ANCHOR_SYMBOL in s):
                        line_i -= 1
                        break

            end_iter = buffer.get_iter_at_line(line_i)
            end_iter.forward_to_line_end()
            buffer.select_range(start_iter, end_iter)

        if buffer.get_selection_bounds():  # a task is selected
            task = self.controller.read_task_from_selection(buffer)
            self.add_new_task(task=task)

    @action(_('_Add new task...'))  # T: menu item
    def add_new_task(self, task=None):
        # gui window
        if task is None:
            task = {}
        GoogletasksNewTaskDialog(self.window,
                                 _('Search'),
                                 task=task,
                                 controller=self.controller,
                                 defaultwindowsize=(300, -1)) \
            .setup()

    @action(_('_Claim read only access'))  # T: menu item
    def permission_readonly(self):
        self.controller.calendar_api.get_service()

    @action(_('_Claim write access'))  # T: menu item
    def permission_write(self):
        self.controller.calendar_api.get_service(write_access=True)

    @action(_('_Import new tasks'), accelerator='<ctrl><alt>g')  # T: menu item
    def import_tasks(self):
        if self.controller.preferences["auto_sync"]:
            self.sync_status()
        self.controller.fetch(force=True)

    @action(_('_Import all non-completed even already imported tasks'))  # T: menu item
    def import_history(self):
        if self.controller.preferences["auto_sync"]:
            self.sync_status()
        self.controller.fetch(all_history=True)

    @action(_('_Sync tasks status from server'))  # T: menu item
    def sync_status(self):
        self.controller.sync_bullets_from_server()

    @action(_('_Refresh task lists'))  # T: menu item
    def refresh_task_lists(self):
        self.controller.refresh_task_lists()


class GoogletasksNewTaskDialog(Dialog):

    def __init__(self, *args, task=None, controller=None, **kwargs):
        self.input_title = None
        self.input_due = None
        self.input_notes = None
        self.label_object = None
        self.label_due = None
        self.task = task
        self.controller = controller
        super().__init__(*args, **kwargs)

    # noinspection PyArgumentList
    def setup(self):
        self.resize(300, 100)  # reset size

        # title
        self.label_object = Gtk.Label(label='Update Google task' if self.task.get("id", "") else 'Create Google tasks')
        self.label_object.set_size_request(300, -1)
        self.vbox.pack_start(self.label_object, expand=False, fill=True, padding=0)

        # editable text fields (title and notes)
        self.input_title = InputEntry(allow_empty=False, placeholder_text="task title")
        self.vbox.pack_start(self.input_title, expand=False, fill=True, padding=0)
        self.input_notes = Gtk.TextView()

        # date field
        self.input_due = InputEntry(allow_empty=False)
        try:
            s = self.controller.get_time(mode="date-only", from_string=self.task["due"], past_dates=False)
        except (ValueError, KeyError):  # task["due"] is not set or is in past
            s = self.controller.get_time(add_days=1, mode="date-only")
        self.input_due.set_text(s)
        self.input_due.connect('changed', self.update_date)
        self.label_due = Gtk.Label(self.controller.get_time(add_days=1, mode="day"))
        hbox = Gtk.HBox(spacing=1)
        hbox.pack_start(self.input_due, expand=True, fill=True, padding=0)
        hbox.pack_start(self.label_due, expand=True, fill=True, padding=0)
        self.vbox.pack_start(hbox, expand=False, fill=True, padding=0)

        # we cant tab out from notes textarea field, hence its placed under date
        self.vbox.pack_start(self.input_notes, expand=False, fill=True, padding=0)

        # 9 postponing buttons
        hbox = Gtk.HBox()
        self.vbox.add(hbox)
        hbox.pack_start(Gtk.Label(label='Days: '), expand=False, fill=True, padding=0)
        for i in range(1, 10):
            button = Gtk.Button.new_with_mnemonic(
                label="_{} {}".format(i, self.controller.get_time(add_days=i, mode="day")))
            button.connect("clicked", self.postpone, i)
            hbox.pack_start(button, expand=False, fill=True, padding=0)

        # predefined fields
        if "title" in self.task:
            self.input_title.set_text(self.task["title"])
        if "notes" in self.task:
            self.input_notes.get_buffer().set_text(self.task["notes"])
        if "due" in self.task:
            logger.error("Not yet implemented")  # XX

        # display window
        self.show_all()
        return self

    def update_date(self, _):
        try:
            day = self.controller.get_time(from_string=self.input_due.get_text(), mode="day", past_dates=False)
        except ValueError:
            day = INVALID_DAY
        self.label_due.set_text(day)

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
        text = self.controller.get_task_text(self.task, self.controller.preferences["include_start_date"])
        if text:
            buffer = self.controller.window.pageview.textview.get_buffer()
            buffer.insert_parsetree_at_cursor(Parser().parse(text))
        self.destroy()

    def postpone(self, _, number):
        self.input_due.set_text(self.controller.get_time(add_days=number, mode="date-only"))
        self.do_response_ok()


class GoogletasksController:
    window = None  # type: MainWindow

    def __init__(self, window=None, notebook=None, preferences=None):
        GoogletasksController.window = self.window = window
        self.notebook = notebook
        self.preferences = preferences

        if not self.notebook and self.window:
            self.notebook = self.window.notebook
        self.notebook.plugin_googletasks = self

        self.cache = Cache(Path(str(self.notebook.cache_dir), CACHE_FILE)).load()
        self.calendar_api = GoogleCalendarApi(self)

    @property
    def tasklist(self):
        """ Returns task list ID from cache (or fetches new one).
        If task list ID cannot be fetched, raise LookupError."""
        # we have to pass ID or @default as task list
        list_title = self.preferences["tasklist"]
        if not list_title:
            # it is more user friendly to let the parameter empty for the default task list
            return "@default"
        if list_title not in self.cache.lists:
            self.refresh_task_lists()
            if list_title not in self.cache.lists:
                raise LookupError(f"Cannot identify task list {list_title}")
        return self.cache.lists[list_title]

    def change_task_list_closure(self, title):
        def _(_):
            self.preferences["tasklist"] = title
            self.info(f"Task list changed to {title}")

        return _

    def task_checked(self, task_id, bullet):
        """ un/mark task on Google server """
        service = self.calendar_api.get_service(write_access=True)
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}

        # noinspection PyBroadException
        try:
            if bullet is CHECKED_BOX:  # small patch will be sufficient
                service.tasks().patch(tasklist=self.tasklist, task=task_id, body=task).execute()
            else:  # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist=self.tasklist, task=task_id).execute()
                del task["completed"]
                task["status"] = "needsAction"
                service.tasks().update(tasklist=self.tasklist, task=task_id, body=task).execute()
        except LookupError as e:
            self.info(e)
            return False
        except Exception:
            self.info('Error in communication while un/checking: {}'.format(sys.exc_info()[1]))
            return False
        self.info('Marked as {}'.format(task["status"]))
        return True

    def submit_task(self, task=None):
        """ Upload task to Google server """
        if task is None:
            task = {}
        if "due" not in task:  # fallback - default is to postpone the task by a day
            task["due"] = self.get_time(add_days=1, mode="morning")

        service = self.calendar_api.get_service(write_access=True)

        # noinspection PyBroadException
        try:
            if "id" in task:
                service.tasks().patch(tasklist=self.tasklist, task=task["id"], body=task).execute()
                self.info("Task '{}' updated.".format(task["title"]))
            else:
                service.tasks().insert(tasklist=self.tasklist, body=task).execute()
                self.info("Task '{}' created.".format(task["title"]))
        except LookupError as e:
            self.info(e)
            return False
        except Exception:
            self.info('Error in communication while sumitting: {}'.format(sys.exc_info()[1]))
            return False
        return True

    @staticmethod
    def get_time(add_days=0, mode=None, from_string=None, use_date=None, past_dates=True):
        """ Time formatting function
         mode =  object | date-only | morning | midnight | last-sec | day

         We preferably use `use_date` object, else we parses from `from_string`. If `from_string` has missing info,
         it is taken from the default 2020-01-01, so ex: '2021-05' will be converted to '2021-05-01'
        """
        dt_now = use_date if use_date else \
            dateutil.parser.parse(from_string, default=datetime.datetime(2020, 1, 1)) if from_string else \
                datetime.datetime.now()
        if add_days:
            dt_now += datetime.timedelta(add_days, 0)
        if not past_dates and dt_now.isoformat()[:10] < datetime.datetime.now().isoformat()[:10]:
            # why isoformat? something with tzinfo
            raise ValueError
        if mode:
            try:
                return {
                    "object": lambda: dt_now,
                    "date-only": lambda: dt_now.isoformat()[:10],
                    "midnight": lambda: dt_now.isoformat()[:11] + "00:00:00.000Z",
                    "morning": lambda: dt_now.isoformat()[:11] + "08:00:00.000Z",
                    "last-sec": lambda: dt_now.isoformat()[:11] + "23:59:59.999Z",
                    "day": lambda: dt_now.strftime("%A").lower()[:2]
                }[mode]()
            except KeyError:
                logger.error("Wrong time mode {}!".format(mode))
        return dt_now.isoformat()

    def info(self, text):
        """ Echo to the console and status bar. """
        text = "[Googletasks] " + str(text)
        logger.info(text)
        if self.window:
            self.window.statusbar.push(0, text)

    def _read_task_list(self, due_min, show_completed=False, service=None):
        """ In case of an error or no tasks found, informs user in the status bar and raises LookupError. """
        if not service:
            service = self.calendar_api.get_service()
        try:
            results = service.tasks().list(maxResults=99,
                                           tasklist=self.tasklist,
                                           showCompleted=show_completed,
                                           dueMin=due_min,
                                           dueMax=self.get_time(add_days=1, mode="midnight")  # for format see #17
                                           ).execute()
        except (LookupError, httplib2.ServerNotFoundError, Error) as e:
            # self.tasklist property has problem, raises LookupError
            self.info(e)
            raise LookupError
        items = results.get('items', [])
        if not items:
            self.info('No tasks found.')
            raise LookupError
        return items

    @staticmethod
    def get_task_text(task, include_due=False):
        """ formats task object to zim markup
        :type task: dict, ex: {'id': '...', 'etag': '"..."', 'title': '...',
                               'updated': '2019-07-25T10:11:12.000Z',
                               'status': 'needsAction', 'due': '2018-12-14T00:00:00.000Z'
                               }
        :param include_due: If True, small start date string is included.
        """
        s = "[*] " if task.get("completed") else "[ ] "
        if task.get("id", ""):
            s += "[[gtasks://{}|{}]] ".format(task["id"], TASK_ANCHOR_SYMBOL)
        if "title" not in task:
            logger.error("Task text is missing")
            return False
        # we dont want to have the task started with: "[ ] [ ] "
        s += task['title'][4:] if task['title'].startswith("[ ] ") else task['title']
        if include_due and task["due"]:
            d = GoogletasksController.get_time(mode="date-only", from_string=task['due'])
            s += " _{//>" + d + "//}"
        if task.get("notes", ""):  # insert other lines
            s += "\n" + task['notes']
        return s

    @classmethod
    def readline(cls, line_i, buffer):
        """ this crazy construct just reads a line at position in a page """
        text_iter = buffer.get_iter_at_line(line_i)
        start = buffer.get_iter_at_offset(text_iter.get_offset())
        if text_iter.forward_to_line_end():
            end = buffer.get_iter_at_offset(text_iter.get_offset())
        else:
            end = start
        return buffer.get_slice(start, end, include_hidden_chars=True)

    @classmethod
    def get_task_id(cls, line_i, buffer):
        task_anchor_pos = cls.readline(line_i, buffer).find(TASK_ANCHOR_SYMBOL)
        if task_anchor_pos > -1:
            offset = task_anchor_pos + buffer.get_iter_at_line(line_i).get_offset()
            # noinspection PyBroadException
            try:
                link_data = buffer.get_link_data(buffer.get_iter_at_offset(offset))
                return link_data["href"].split("gtasks://")[1]
            except Exception:
                pass
        return None

    def read_task_from_selection(self, buffer=None):
        if not buffer:
            buffer = self.window.pageview.textview.get_buffer()
        task = {}

        lines = get_dumper("wiki").dump(buffer.get_parsetree(buffer.get_selection_bounds()))
        match = taskAnchorTreeRe.match("".join(lines))
        if match:
            task = {"id": match.group(2),
                    "title": match.group(3).split("\n", 1)[0],
                    "status": "completed" if match.group(1).startswith("[*]") else "needsAction"
                    }
        else:
            try:
                task["title"] = lines[0].strip()
            except IndexError:
                task["title"] = ""
        if self.preferences["include_start_date"]:
            m = start_date_in_title.match(task["title"])
            if m:
                # ex: 'test 13 _{//>2020-06-03//}' (start date in 'small' markup)
                task["due"] = m[2]  # → "2020-06-03"
                task["title"] = (m[1] + m[4]).replace("_{////}", "").rstrip()  # → "test 13"
        task["notes"] = "".join(lines[1:])

        buffer.delete(*buffer.get_selection_bounds())  # cuts the task
        return task

    def fetch(self, force=False, all_history=False):
        """ Get the new tasks and insert them into page
        :type force: Cancels the action if False and cache file have been accessed recently.
        :type all_history: If True, imports all non-completed tasks. `force` is implied
                Note: When importing `all_history`, I have spotted a task that should appear at 7 December 2020
                 (as confirmed at the Google Calendar web page), however this is what we got from the server:
                 {'updated': '2019-07-25T10:11:12.000Z', 'status': 'needsAction', 'due': '2017-12-22T00:00:00.000Z'}
                 I have to admit that this task has been postponed few times.
                 But we cannot rely on fetching past-tasks only feature, its December date is seen nowhere.

        XX Add plugin option "import even completed tasks" ?
        """
        if all_history:
            force = True

        # Set the date since that we fetch the tasks
        cache_exists = self.cache.exists()
        if cache_exists:  # recently seen tasks since last import
            if not force:
                max_hours = 3
                now = time()
                last_time = self.cache.last_time()
                if now - last_time < max_hours * 3600 and \
                        datetime.datetime.now().day is datetime.datetime.fromtimestamp(last_time).day:
                    # if checked in last hours and there wasn't midnight (new tasks time) since then
                    return
            self.cache.load()
            due_min = self.get_time(use_date=datetime.datetime.fromtimestamp(self.cache.last_time()),
                                    mode="midnight")
        else:  # load today's task (first run)
            due_min = self.get_time(mode="midnight")

        if all_history:  # all non-completed tasks
            self.cache.items_ids.clear()  # re-import everything
            due_min = None

        # Do internal fetching of new tasks text
        try:
            items = self._read_task_list(due_min)
        except LookupError:
            return
        texts = []
        items_ids = set()
        for item in items:
            if item["etag"] in self.cache.items_ids:
                if self.get_time(from_string=item["due"], mode="object").date() \
                        >= self.get_time(mode="object").date():
                    items_ids.add(item["etag"])
                logger.debug('Text already imported {}.'.format(item['title']))
                continue
            items_ids.add(item["etag"])
            logger.info("Appending {}.".format(item["title"]))
            logger.debug(item)
            texts.append(self.get_task_text(item, self.preferences["include_start_date"]))
        self.cache.items_ids = items_ids

        # Refreshes page from current configuration
        page = self._get_page()
        s = " from list " + self.preferences['tasklist'] if self.preferences['tasklist'] else ""
        self.info(f"New tasks{s}: {len(texts)}, page: {page.get_title()}")
        if not texts:
            if cache_exists:
                self.cache.touch()
            return
        text = "\n".join(texts) + "\n"

        # Insert tasks string into page
        appended = False
        contents = []
        for line in page.dump("wiki"):
            contents.append(line)
            if line.strip() == "" and not appended:  # insert after first empty line
                appended = True
                contents.append(text)
        if not appended:  # or insert at the end of the page text
            contents.append(text)

        self._store_to_page(contents, page)

        # Save the list of successfully imported task
        self.cache.save()
        # with open(CACHE_FILE, "wb") as f:
        #     pickle.dump(self.item_ids, f)

    def _store_to_page(self, contents, page):
        """

        :param contents: [] lines of the page
        :param page: ZimPage
        """
        bounds = buffer = None
        if self.window and self.window.pageview.get_page().name is page.name:
            # HP is current page - we use GTK buffers, caret stays at position
            buffer = self.window.pageview.textview.get_buffer()
            bounds = [x.get_offset() for x in buffer.get_selection_bounds()]
            if not bounds:
                i = buffer.get_insert_iter()
                bounds = [i.get_offset()] * 2
        page.parse('wiki', "".join(contents))
        self.notebook.store_page(page)
        if bounds:
            buffer.select_range(buffer.get_iter_at_offset(bounds[0]), buffer.get_iter_at_offset(bounds[1]))

    def _get_page(self):
        return self.notebook.get_page(ZimPath(self.preferences["page"])) if self.preferences["page"] \
            else self.notebook.get_home_page()

    def sync_bullets_from_server(self):
        """ Loops tasks found on the page and un/check them according to the Google server task status."""

        service = self.calendar_api.get_service()

        # build status cache from last 14 days so that we have to fetch one by one as little tasks as possible
        # XX maybe we may check the number of tasks to be fetched one-by-one after that and if it is still big,
        # perform more batch operation like this if there is a performance issue
        try:
            cache = {task["id"]: task["status"] == "completed" for task in
                     self._read_task_list(self.get_time(add_days=-14, mode="midnight"),
                                          show_completed=True,
                                          service=service)}
        except LookupError:
            return False

        allow_single_search = True
        unidentified_tasks = []
        page = self._get_page()
        contents = []
        for line in page.dump("wiki"):
            match = taskAnchorTreeRe.match(line)
            if match:
                task_id = match[2]
                task_text = match[3]
                completed = cache.get(task_id, None)

                # perform single search if needed, this task was not fetched within the cache
                if completed is None and allow_single_search:
                    try:
                        task = service.tasks().get(task=task_id, tasklist=self.tasklist).execute()
                    except Error as e:
                        self.info(e)
                        unidentified_tasks.append(task_text)
                    except (LookupError, httplib2.ServerNotFoundError) as e:
                        self.info(e)
                        allow_single_search = False
                    else:
                        completed = task["status"] == "completed"

                if completed is not None:  # we know the current status, replace the line
                    # strip "[.] " (a dot may be "*", "x", " ") from the beginning
                    task_s_without_bullet = match[0][(len(match[1]) if match[1] else 0):]
                    # put "[*]" or "[ ]" to the beginning
                    line = "[{0}] {1}".format(("*" if completed else " "), task_s_without_bullet) + "\n"

            contents.append(line)
        self._store_to_page(contents, page)
        if unidentified_tasks:
            self.info(f"Cannot identify {len(unidentified_tasks)} tasks: " + ", ".join(unidentified_tasks))

    def refresh_task_lists(self):
        self.cache.load()
        service = self.calendar_api.get_service()
        try:
            results = service.tasklists().list().execute()
        except httplib2.ServerNotFoundError:
            return False
        except Error as e:
            self.info(e)
            return False
        items = results.get('items', [])
        if not items:
            self.info('No task lists found.')
            return

        self.cache.lists.clear()
        for tasklist in items:
            self.cache.lists[tasklist["title"]] = tasklist["id"]
        self.info(f'{len(items)} task lists discovered you can change to in the menu.')
        self.cache.save()


class Cache:

    def __init__(self, path):
        """

        :type path: Path
        """
        self._path = path
        self._loaded = False
        self.items_ids = set()  # If user changes to another taks list, imports and changes back, items_ids are lost.
        self.lists = {}  # type: title: id

    def load(self):
        if not self._loaded and self.exists():
            self.__dict__.update(jsonpickle.decode(self._path.read_text()))
            self._loaded = True
        return self

    def save(self):
        self._path.write_text(jsonpickle.encode({k: v for k, v in vars(self).items() if not k.startswith('_')}))

    def exists(self):
        return os.path.isfile(self._path)

    def touch(self):
        return os.utime(self._path, None)  # we touch the file

    def last_time(self):
        return os.path.getmtime(self._path)


# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(GoogletasksPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")

#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import annotations, print_function

import datetime
import json
import logging
import os
import re
import sys
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

import dateutil.parser
import httplib2
from dateutil.relativedelta import relativedelta
from gi.repository import GLib, Gtk
from googleapiclient import discovery
from googleapiclient.errors import Error
from oauth2client import client, tools
from oauth2client.file import Storage
from zim.actions import action, get_gtk_actiongroup, ActionClassMethod
from zim.config import XDG_CONFIG_HOME, XDG_DATA_HOME
from zim.formats import get_dumper
from zim.formats.wiki import Parser

from zim.plugins.zim_page_content import insert_after_first_blank_line

try:
    from zim.gui.widgets import Dialog, ErrorDialog
    from zim.gui.widgets import InputEntry
    from zim.gui.pageview import TextBuffer
    from zim.gui.mainwindow import MainWindow
except RuntimeError:
    """ When invoking the plugin from commandline, I was getting this on Ubuntu 17.10.
    Defining blank Dialog class helps me to mitigate this strange issue.

     File "/usr/lib/python2.7/dist-packages/zim/gui/clipboard.py", line 477, in __init__
        self.clipboard = Gtk.Clipboard(selection=atom)
     RuntimeError: could not create GtkClipboard object
     """

    class Dialog:
        pass

    ErrorDialog = None
from zim.formats import CHECKED_BOX, UNCHECKED_BOX
from zim.main import NotebookCommand
from zim.main.command import GtkCommand
from zim.notebook import build_notebook, Path as ZimPath
from zim.notebook.page import Page
from zim.plugins import PluginClass
from zim.gui.mainwindow import MainWindowExtension

if TYPE_CHECKING:
    _ = str

logger = logging.getLogger('zim.plugins.googletasks')
CACHE_FILE = "googletasks.cache"
CACHE_SCHEMA_VERSION = 1
WORKDIR = str(XDG_DATA_HOME.folder(('zim', 'plugins')))
BUNDLED_CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
CLIENT_SECRET_ENV = 'GOOGLETASKS_CLIENT_SECRET_FILE'
OWN_CLIENT_SECRET_NAME = 'googletasks_client_secret.json'


def find_client_secret_file():
    """ Path to the OAuth client secret. Optional own secret (for those hitting the quota of the bundled one):
        1. env var GOOGLETASKS_CLIENT_SECRET_FILE (path to the json)
        2. googletasks_client_secret.json in Zim's config dir (~/.config/zim/ on Linux)
        3. the bundled googletasks_client_id.json next to the plugin (default)
    """
    env = os.environ.get(CLIENT_SECRET_ENV)
    if env:
        return env
    own = os.path.join(str(XDG_CONFIG_HOME), 'zim', OWN_CLIENT_SECRET_NAME)
    if os.path.isfile(own):
        return own
    return BUNDLED_CLIENT_SECRET_FILE


CLIENT_SECRET_FILE = find_client_secret_file()
APPLICATION_NAME = 'googletasks2zim'
TASK_ANCHOR_SYMBOL = u"\u270b"
LIST_ICONS = (TASK_ANCHOR_SYMBOL, "💼", "🏠", "🛒", "📚", "⭐", "🔧", "🎯")
"icons given in turn to the task lists that have no icon of their own in the preferences"
DEFAULT_LIST = "@default"
taskAnchorTreeRe = re.compile(r'(\[.\]\s)?\[\[gtasks://(?:([^/|\]]*)/)?([^/|\]]*)\|([^\]]*)\]\]\s?(.*)')
""" A task line: 1 checkbox, 2 list ID (missing in links written before multiple lists were supported),
    3 task ID, 4 icon, 5 task text. Ex: `[ ] [[gtasks://<list_id>/<task_id>|💼]] Task title` """
start_date_in_title = re.compile(r'(.*)>(\d{4}-\d{2}(-\d{2})?)(.*)')  # (_\{//)?  (//})?
INVALID_DAY = "N/A"


def parse_tasklists(pref):
    """ Preference string -> [(title, icon)] of the task lists to work with, the first one being primary.

    'My Tasks, Work=💼' -> [('My Tasks', '✋'), ('Work', '💼')]. Lists with no icon get the first unused one
    from LIST_ICONS. An empty string means just the default list - the only behaviour before
    multiple lists were supported, as was a single title.
    """
    entries = []
    for part in (pref or "").split(","):
        title, _sep, icon = part.partition("=")
        title, icon = title.strip(), re.sub(r"[|\]\[/]", "", icon).strip()  # the icon goes into a link label
        if title and title not in (t for t, _i in entries):
            entries.append([title, icon])
    if not entries:
        entries = [[DEFAULT_LIST, ""]]
    free = (i for i in LIST_ICONS if i not in {icon for _t, icon in entries})
    for entry in entries:
        if not entry[1]:
            entry[1] = next(free, TASK_ANCHOR_SYMBOL)
    return [tuple(e) for e in entries]


def format_tasklists(entries):
    """ [(title, icon)] -> preference string, the counterpart of parse_tasklists """
    return ", ".join(f"{title}={icon}" if icon else title for title, icon in entries)


class GoogletasksPlugin(PluginClass):
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your Google Tasks lists (the default one or the ones chosen, each marked by its own icon).
Append today's tasks to your Home file. At first run,
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
        ('startup_check', 'bool', _('Import new tasks on Zim startup'
                                    '\nIncludes today\'s or since last time due date tasks'), True),
        ('auto_sync', 'bool', _('Sync tasks status before import'
                                '\nMay take a long time, notably if tasks on the page are older than 14 days.'), False),
        ('include_start_date', 'bool', _('Include start date'
                                         '\nStandard Zim notation, ex: ">2021-01-01" means you want'
                                         ' the task to appear not sooner than in January.'), False),
        ('page', 'page', _('Page to be updated (empty = homepage)'), ""),
        ('tasklist', 'string', _('Task lists to import (empty = default)'
                                 '\nComma separated, optionally with an icon, ex: "My Tasks, Work=💼".'
                                 '\nThe first one is used for new tasks.'), ""),
        ('postponing_days', 'int', _('Submitting a task: how many day buttons'), 9, (0, 40)),
        ('button_monday', 'bool', _('Monday button'), True),
        ('button_next_monday', 'bool', _('Next Monday button (Monday in two weeks)'), True),
        ('button_next_month', 'bool', _('Next month button (1st next month)'), True),
        ('button_3_months', 'bool', _('Three months button (+3 months)'), True),
        ('button_6_months', 'bool', _('Half a year button (+6 months)'), True),
        ('button_next_year', 'bool', _('Next year button (1st Feb)'), True),
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
                controller: GoogletasksController = self.notebook.plugin_googletasks
                list_id, task_id = controller.get_task_id(line, buffer=self)
                if task_id and not controller.task_checked(list_id, task_id, bullet):
                    return
            self.set_bullet_original(line, bullet, indent=indent)

        # noinspection PyShadowingNames
        @monkeypatch_method(Page)
        def reload_textbuffer(self):
            """ Overriding of notebook/page.py/Page/reload_textbuffer

            The default implementation clears the textbuffer and re-inserts all
            content at the cursor, which leaves the cursor (and thus the scrolled
            view) at the very end of the page. That happens on every reload of a
            currently displayed page - e.g. after an external process appends
            text to the page file and asks the running Zim to pick it up (see
            insert_note.py) - and jumps the view to the bottom of the page even
            though nothing the user was looking at moved that far.

            We restore the cursor to where it was before the reload, same
            reasoning as the set_bullet patch above for finding the right
            controller/window across multiple notebooks.

            Note: unlike TextBuffer, a Page has no `.notebook` attribute of its
            own - it's the bound TextBuffer (self._textbuffer) that carries
            `.notebook` (see TextBuffer.__init__ in zim/gui/pageview), so we
            look up the controller through that instead. This patch sits on the
            shared Page class and therefore fires for *every* page reload in
            every notebook, including notebooks this plugin never extended, so
            each step of that lookup has to tolerate being absent - anything
            missing simply means "no cursor to preserve", never an exception
            that would abort the reload itself.
            """
            buffer = self._textbuffer
            notebook = getattr(buffer, "notebook", None) if buffer else None
            controller: GoogletasksController = getattr(notebook, "plugin_googletasks", None)
            window = getattr(controller, "window", None)
            pageview = window.pageview if (window and window.page is self) else None
            anchor = get_cursor_anchor(pageview) if pageview else None

            self.reload_textbuffer_original()

            if anchor:
                restore_cursor_anchor(pageview, anchor)


class GoogletasksCommand(NotebookCommand, GtkCommand):
    """Class to handle "zim --plugin googletasks" """
    arguments = ('[NOTEBOOK]',)

    def run(self):
        ntb_name = self.get_notebook_argument()[0] or self.get_default_or_only_notebook()
        ntb, _ = build_notebook(ntb_name)
        # the notebook properties, as the GUI uses them; preferences.conf holds only their defaults
        # (reading it imported just the default task list, not the ones chosen for the notebook)
        preferences = GoogletasksPlugin().notebook_properties(ntb)
        controller = GoogletasksController(notebook=ntb, preferences=preferences)
        controller.fetch(True)  # add new lines
        if controller.failed:
            sys.exit(1)  # let cron/anacron notice


class GoogleCalendarApi:
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')

    def __init__(self, controller):
        self.credential_path = None
        self.scope = None
        self.controller: GoogletasksController = controller

    @staticmethod
    def service_obtainable(write_access=False):
        if write_access:
            return os.path.isfile(GoogleCalendarApi.permission_write_file)
        else:
            return os.path.isfile(GoogleCalendarApi.permission_read_file)

    def get_service(self, info=None, write_access=False):
        if info:
            self.controller.info(info)  # strangely, this info is not seen in the status bar
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


def _line_text(buffer, line):
    """ Plain text of a single line of a Gtk.TextBuffer, without the trailing newline. """
    start = buffer.get_iter_at_line(line)
    end = start.copy()
    if not end.ends_line():
        end.forward_to_line_end()
    return buffer.get_text(start, end, True)


def get_cursor_anchor(pageview):
    """ Where the caret is, in a form that survives a reload: (line number, that line's text,
        column within the line). See restore_cursor_anchor() and the reload_textbuffer patch. """
    buffer = pageview.textview.get_buffer()
    it = buffer.get_iter_at_mark(buffer.get_insert())
    return it.get_line(), _line_text(buffer, it.get_line()), it.get_line_offset()


def restore_cursor_anchor(pageview, anchor):
    """ Put the caret back onto the line it was on, located by that line's text.

    Restoring a plain character (or even line) offset breaks as soon as something was inserted
    *above* the caret - which is exactly what this plugin and insert_note.py do, they insert
    after the first blank line near the top of the page - so the caret would land somewhere in
    or below the freshly inserted block instead of where the user was reading. Therefore we look
    the line up by its content and, if it occurs several times, take the occurrence closest to
    the original line number. Blank/vanished lines fall back to the plain line number.
    """
    line, text, column = anchor
    buffer = pageview.textview.get_buffer()
    last_line = buffer.get_line_count() - 1
    candidates = [i for i in range(last_line + 1) if _line_text(buffer, i) == text] if text else []
    target = min(candidates, key=lambda i: abs(i - line)) if candidates else min(line, last_line)
    it = buffer.get_iter_at_line(target)
    it.forward_chars(min(column, len(_line_text(buffer, target))))
    pageview.set_cursor_pos(it.get_offset())


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
            return inspect.getmembers(obj.__class__, lambda m: isinstance(m, ActionClassMethod))

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
                                        <placeholder name='googletasks_lists'/>
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
            action_group.add_actions((('choose_task_list', None, _('_Task lists')),))
            # (the ids are what the parent's teardown() removes when the plugin gets disabled)
            self._uimanager_ids = [self._uimanager.add_ui_from_string(xml)]
            self._list_actions = []
            self._lists_ui_id = None
            self._menu_tasklist = None
            self.build_list_menu()
            # the menu follows the `tasklist` property, whether edited here or in File / Properties
            self.connectto(self.controller.preferences, 'changed', self._preferences_changed)

    def _preferences_changed(self, _preferences):
        if self.controller.preferences["tasklist"] != self._menu_tasklist:
            # not from within the signal of the very action being replaced (when toggled in the menu)
            GLib.idle_add(self.build_list_menu)

    def build_list_menu(self):
        """ (Re)build the task lists in `Tools / Google Tasks / Task lists`: a check item per list known
            from the server. Checked lists are imported, toggling one edits the `tasklist` preference. """
        action_group = self.actiongroup
        if self._lists_ui_id is not None:
            self._uimanager.remove_ui(self._lists_ui_id)
            self._uimanager_ids.remove(self._lists_ui_id)
        for gtk_action in self._list_actions:
            action_group.remove_action(gtk_action)
        self._list_actions = []
        self._menu_tasklist = self.controller.preferences["tasklist"]

        chosen = self.controller.chosen_lists()
        icons = self.controller.remember_icons()
        items = []
        for title in self.controller.cache.lists:
            # Apostrophe suppressed; I was not able to reliably slash it, getting a strange error:
            # gi.repository.GLib.GError: g-markup-error-quark: Error on line 12 char 82:
            # Odd character “l”, expected a “=” after attribute name “s” of element “menuitem” (2)
            el = "change_list_{}".format(title.replace(r"'", r""))
            # menu labels are mnemonic, a single underscore in the title would vanish
            gtk_action = Gtk.ToggleAction(name=el, label=f"{icons.get(title, '   ')} {title.replace('_', '__')}")
            gtk_action.set_active(title in chosen)
            gtk_action.connect("toggled", self._list_toggled, title)
            action_group.add_action(gtk_action)
            self._list_actions.append(gtk_action)
            items.append(f"<menuitem action='{el}'/>")

        self._lists_ui_id = self._uimanager.add_ui_from_string('''
            <ui><menubar name='menubar'><menu action='tools_menu'><menu action='googletasks_menu'>
                <menu action='choose_task_list'><placeholder name='googletasks_lists'>
                ''' + "\n".join(items) + '''
                </placeholder></menu>
            </menu></menu></menubar></ui>''')
        self._uimanager_ids.append(self._lists_ui_id)

    def _list_toggled(self, gtk_action, title):
        self.controller.toggle_list(title, gtk_action.get_active())
        chosen = title in self.controller.chosen_lists()
        if gtk_action.get_active() != chosen:  # refused (the last list cannot be unchosen)
            gtk_action.set_active(chosen)

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

    @action(_('_Import all non-completed even already imported tasks from the past'))  # T: menu item
    def import_history(self):
        if self.controller.preferences["auto_sync"]:
            self.sync_status()
        self.controller.fetch(all_history=True)

    @action(_('_Sync tasks status from server'))  # T: menu item
    def sync_status(self):
        self.controller.sync_bullets_from_server()

    @action(_('_Refresh task lists'))  # T: menu item
    def refresh_task_lists(self):
        if self.controller.refresh_task_lists():
            self.build_list_menu()


class GoogletasksNewTaskDialog(Dialog):

    def __init__(self, *args, task=None, controller: "GoogletasksController" = None, **kwargs):
        self.input_title = None
        self.input_due = None
        self.input_notes = None
        self.label_object = None
        self.label_due = None
        self.input_list = None
        self.list_ids = []
        "IDs of the task lists offered in input_list, in the same order"
        self.initial_list = 0
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
        self.input_notes.set_accepts_tab(False)  # Tab / Shift+Tab move the focus, not insert a tab

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

        # notes; TextView has no placeholder in GTK 3, a dimmed label over it shows while it is empty
        placeholder = Gtk.Label(label="description", halign=Gtk.Align.START, valign=Gtk.Align.START)
        placeholder.get_style_context().add_class("dim-label")
        notes_buffer = self.input_notes.get_buffer()
        notes_buffer.connect("changed", lambda b: placeholder.set_visible(not b.get_char_count()))
        overlay = Gtk.Overlay()
        overlay.add(self.input_notes)
        overlay.add_overlay(placeholder)
        overlay.set_overlay_pass_through(placeholder, True)  # clicks reach the text view
        self.vbox.pack_start(overlay, expand=False, fill=True, padding=0)

        # task list (the primary one, or the one the edited task lives in)
        self._setup_list_choice()

        # arbitrary postponing buttons
        hbox = Gtk.HBox()

        def butt(label=None, date=None, days=None):
            """ Call either with label and date are with days."""
            if days:
                label = f"{'_' if days < 10 else ''}{days} {self.controller.get_time(add_days=days, mode='day')}"
                date = self.controller.get_time(add_days=days, mode="date-only")
            b = Gtk.Button.new_with_mnemonic(label=label)
            b.connect("clicked", self.postpone(date))
            b.set_tooltip_text(date)
            hbox.pack_start(b, expand=False, fill=True, padding=0)

        if self.controller.preferences["button_monday"]:
            butt("_Monday", self._slippy_date(next_monday=True))
        if self.controller.preferences["button_next_monday"]:
            butt("_Next Monday", self._slippy_date(next_monday=True, relative_delta={"days": 7}))
        if self.controller.preferences["button_next_month"]:
            butt("Mon_th", self._slippy_date(relative_delta={"months": 1, "day": 1}))
        if self.controller.preferences["button_3_months"]:
            butt("3 month_s", self._slippy_date(relative_delta={"months": 3}))
        if self.controller.preferences["button_6_months"]:
            butt("_Half a year", self._slippy_date(relative_delta={"months": 6}))
        if self.controller.preferences["button_next_year"]:
            butt("_Year", self._slippy_date(relative_delta={"years": 1, "month": 2, "day": 1}))

        if hbox.get_children():  # there were some buttons to display
            self.vbox.add(hbox)

        # 9 (or more) postponing buttons
        days = self.controller.preferences["postponing_days"]
        if days:
            hbox = Gtk.HBox()
            self.vbox.add(hbox)
            hbox.pack_start(Gtk.Label(label='Days: '), expand=False, fill=True, padding=0)
            for i in range(1, days + 1):
                butt(days=i)
                if not i % 9:  # put every 9 buttons to another line, do not expand the row to infinity
                    hbox = Gtk.HBox()
                    self.vbox.add(hbox)

        # predefined fields
        if "title" in self.task:
            self.input_title.set_text(self.task["title"])
        if "notes" in self.task:
            self.input_notes.get_buffer().set_text(self.task["notes"])
        if "due" in self.task:
            logger.error("Not yet implemented")  # XX

        # display window
        self.show_all()
        notes_buffer.emit("changed")  # hide the placeholder when the notes are prefilled
        return self

    def _setup_list_choice(self):
        """ Offer the chosen task lists; with a single one, there is nothing to choose. """
        self.input_list = Gtk.ComboBoxText()
        for title, icon in self.controller.chosen_lists().items():
            try:
                self.list_ids.append(self.controller.list_id(title))
            except LookupError as e:
                self.controller.error("Cannot offer the task list", e)
                continue
            self.input_list.append_text(f"{icon} {title}")
        if len(self.list_ids) < 2:
            return
        current = self.task.get("_list_id")
        if current == DEFAULT_LIST and self.controller.cache.lists:  # the default list is the first one cached
            current = next(iter(self.controller.cache.lists.values()))
        if self.task.get("id") and not current:  # link written before multiple lists were supported
            current = self.list_ids[0]
        self.initial_list = self.list_ids.index(current) if current in self.list_ids else 0
        self.input_list.set_active(self.initial_list)
        # Alt+L pops the lists up (a mnemonic would only focus the combo box)
        label = Gtk.Label.new_with_mnemonic("_List: ")
        label.set_mnemonic_widget(self.input_list)
        self.input_list.connect("mnemonic-activate", lambda combo, _: combo.popup() or True)
        hbox = Gtk.HBox()
        hbox.pack_start(label, expand=False, fill=True, padding=0)
        hbox.pack_start(self.input_list, expand=True, fill=True, padding=0)
        self.vbox.pack_start(hbox, expand=False, fill=True, padding=0)

    def _slippy_date(self, add_days=0, next_monday=False, relative_delta=None):

        def next_weekday(d, weekday):
            days_ahead = weekday - d.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            return d + datetime.timedelta(days_ahead)

        d = self.controller.get_time(add_days=add_days, mode="object")

        if next_monday:
            d = next_weekday(d, 0)  # 0 = Monday, 1=Tuesday, 2=Wednesday...
        if relative_delta:
            d += relativedelta(**relative_delta)
        return self.controller.get_time(use_date=d, mode="date-only")

    def update_date(self, _):
        try:
            day = self.controller.get_time(from_string=self.input_due.get_text(), mode="day", past_dates=False)
        except ValueError:
            day = INVALID_DAY
        self.label_due.set_text(day)

    def _load_task(self):
        try:
            o = self.input_notes.get_buffer()
            self.task["notes"] = o.get_text(*o.get_bounds(), include_hidden_chars=True)
            self.task["title"] = self.input_title.get_text()
            if self.input_due.get_text():
                self.task["due"] = self.controller.get_time(from_string=self.input_due.get_text(), mode="morning")
            chosen = self.input_list.get_active()
            if len(self.list_ids) > 1 and chosen not in (-1, self.initial_list):
                self.task["_target_list_id"] = self.list_ids[chosen]
        except Exception as e:
            self.controller.error("Failed reading the task form", e)

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
        text = self.controller.get_task_text(self.task, self.controller.preferences["include_start_date"],
                                             self.task.get("_list_id"),
                                             self.controller.list_icon(self.task.get("_list_id")))
        if text:
            buffer = self.controller.window.pageview.textview.get_buffer()
            buffer.insert_parsetree_at_cursor(Parser().parse(text))
        self.destroy()

    # def postpone(self, _, number):
    #     self.input_due.set_text(self.controller.get_time(add_days=number, mode="date-only"))
    #     self.do_response_ok()
    def postpone(self, date):

        def _(_):
            self.input_due.set_text(date)
            self.do_response_ok()

        return _


class GoogletasksController:

    def __init__(self, window: MainWindow | None = None, notebook=None, preferences=None):
        self.window = window
        self.notebook = notebook
        self.preferences = preferences

        if not self.notebook and self.window:
            self.notebook = self.window.notebook
        self.notebook.plugin_googletasks = self

        self.cache = Cache(Path(str(self.notebook.cache_dir), CACHE_FILE)).load()
        self.calendar_api = GoogleCalendarApi(self)
        self.failed = False
        "True once error() was called; the headless command turns it into a non-zero exit code"

    @property
    def tasklists(self):
        """ [(title, icon)] of the task lists chosen in the preferences, the first one being primary """
        return parse_tasklists(self.preferences["tasklist"])

    def list_id(self, title):
        """ Returns task list ID from cache (or fetches new one).
        If task list ID cannot be fetched, raise LookupError."""
        # we have to pass ID or @default as task list
        if title == DEFAULT_LIST:  # never in the cache
            return DEFAULT_LIST
        if title not in self.cache.lists:
            self.refresh_task_lists()
            if title not in self.cache.lists:
                raise LookupError(f"Cannot identify task list {title}")
        return self.cache.lists[title]

    @property
    def primary_list_id(self):
        """ The list new tasks go to and that the task links written before multiple lists were supported
            belong to (they could only point to the single list set in the preferences). """
        return self.list_id(self.tasklists[0][0])

    def list_icon(self, list_id):
        for title, icon in self.tasklists:
            if self.cache.lists.get(title, title) == list_id:
                return icon
        return TASK_ANCHOR_SYMBOL

    def chosen_lists(self):
        """ {title: icon} of the chosen lists, '@default' replaced by the real title of the default list
            when known (refresh_task_lists() keeps the default list first in the cache). """
        default_title = next(iter(self.cache.lists), DEFAULT_LIST)
        return {(default_title if title == DEFAULT_LIST else title): icon for title, icon in self.tasklists}

    def remember_icons(self):
        """ Store the icons of the chosen lists, so that a list unchosen and chosen again gets its icon back
            (the icon is part of the links already on the pages). Returns {title: icon} of all lists ever seen. """
        icons = self.cache.load().icons
        if not icons.items() >= self.chosen_lists().items():
            icons.update(self.chosen_lists())
            self.cache.save()
        return icons

    def toggle_list(self, title, active):
        """ Add the list to the imported ones or remove it from them, keeping the icons already given
            (written explicitly, so that the remaining lists do not swap their icons). """
        remembered = self.remember_icons()
        lists = self.chosen_lists()
        if active and title not in lists:
            icon = remembered.get(title, "")
            lists[title] = "" if icon in lists.values() else icon  # taken by another list meanwhile
            lists = dict(parse_tasklists(format_tasklists(lists.items())))
        elif not active and title in lists:
            if len(lists) == 1:
                self.info("At least one task list must stay chosen")
                return
            del lists[title]
        else:
            return
        self.preferences["tasklist"] = format_tasklists(lists.items())
        self.info(f"Task lists changed to {self.preferences['tasklist']}")

    def task_checked(self, list_id, task_id, bullet):
        """ un/mark task on Google server
        :param list_id: None for the links written before multiple lists were supported
        """
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}
        service = self.calendar_api.get_service(write_access=True,
                                                info=f"Marking task ID {task_id} as {task['status']}")

        try:
            list_id = list_id or self.primary_list_id
            if bullet is CHECKED_BOX:  # small patch will be sufficient
                service.tasks().patch(tasklist=list_id, task=task_id, body=task).execute()
            else:  # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist=list_id, task=task_id).execute()
                task.pop("completed", None)
                task["status"] = "needsAction"
                service.tasks().update(tasklist=list_id, task=task_id, body=task).execute()
        except LookupError as e:
            self.error("Cannot un/check the task", e)
            return False
        except Exception as e:
            self.error("Error in communication while un/checking", e)
            return False
        self.info(f'Marked as {task["status"]}')
        return True

    def submit_task(self, task=None):
        """ Upload task to Google server
        :param task: Besides the Google fields, `_list_id` is the list an existing task lives in
            (None for the links written before multiple lists were supported) and `_target_list_id`
            the list the task should go to (if differs, an existing task is moved there).
        """
        if task is None:
            task = {}
        if "due" not in task:  # fallback - default is to postpone the task by a day
            task["due"] = self.get_time(add_days=1, mode="morning")
        body = {k: v for k, v in task.items() if not k.startswith("_")}

        service = self.calendar_api.get_service(write_access=True, info=f"Submitting task {task.get('title')}")

        # noinspection PyBroadException
        try:
            source = task.get("_list_id") or self.primary_list_id
            target = task.get("_target_list_id") or source
            if "id" in task:
                if target != source:
                    service.tasks().move(tasklist=source, task=task["id"], destinationTasklist=target).execute()
                service.tasks().patch(tasklist=target, task=task["id"], body=body).execute()
                self.info("Task '{}' updated.".format(task["title"]))
            else:
                service.tasks().insert(tasklist=target, body=body).execute()
                self.info("Task '{}' created.".format(task["title"]))
        except LookupError as e:
            self.error("Cannot submit the task", e)
            return False
        except Exception as e:
            self.error("Error in communication while submitting", e)
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
        # As of Zim 0.74 (3.12.2020), the status bar has been removed with no substitute,
        # we do not output the text now.
        # if self.window:
        #     self.window.statusbar.push(0, text)

    def error(self, msg, exc=None, fatal=False):
        """ Report a problem so that somebody notices it (unlike a bare `logger.error`).

        Always logged. In the GUI it goes to the status bar if this Zim has one; a `fatal` error
        (or any error when there is no status bar, which Zim 0.74+ lacks) opens an ErrorDialog.
        Headless (`zim --plugin googletasks`) it is written to stderr and marks the controller
        as `failed`, which makes the command exit with a non-zero code.
        """
        text = f"{msg}: {exc}" if exc else str(msg)
        logger.error("[Googletasks] %s", text, exc_info=exc if isinstance(exc, BaseException) else None)
        self.failed = True
        if not self.window:
            print(f"[Googletasks] ERROR: {text}", file=sys.stderr)
            return
        statusbar = getattr(self.window, "statusbar", None)
        if statusbar is not None and not fatal:
            statusbar.push(0, "[Googletasks] " + text)
        elif ErrorDialog:
            ErrorDialog(self.window, ("Google Tasks: " + str(msg), str(exc) if exc else "")).run()

    def _read_service(self, info):
        """ Service for reading. In case of an error, informs user and raises LookupError. """
        try:
            return self.calendar_api.get_service(info=info)
        except Exception as e:  # OAuth flow, missing/invalid client secret, network...
            self.error("Cannot connect to Google Tasks", e, fatal=True)
            raise LookupError from e

    def _read_task_list(self, due_min, show_completed=False, service=None, list_id=DEFAULT_LIST):
        """ All the tasks of the list due since `due_min`, page by page (the server returns at most 100 at once).
            In case of an error, informs user and raises LookupError. """
        if not service:
            service = self._read_service("Reading task list")
        items = []
        page_token = None
        try:
            while True:
                results = service.tasks().list(maxResults=100,
                                               tasklist=list_id,
                                               showCompleted=show_completed,
                                               dueMin=due_min,
                                               dueMax=self.get_time(add_days=1, mode="midnight"),  # for format see #17
                                               pageToken=page_token
                                               ).execute()
                items.extend(results.get('items', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    return items
        except (httplib2.ServerNotFoundError, Error) as e:
            self.error("Cannot read the task list", e)
            raise LookupError from e

    def _read_chosen_lists(self, due_min, show_completed=False, service=None):
        """ [(item, list_id, icon)] from all the chosen lists.
            :return: (items, complete) - complete is False if some of the lists could not be read.
            Raises LookupError if none could be read. """
        if not service:
            service = self._read_service("Reading task lists")
        items = []
        failures = 0
        for title, icon in self.tasklists:
            try:
                list_id = self.list_id(title)
                items.extend((item, list_id, icon)
                             for item in self._read_task_list(due_min, show_completed, service, list_id))
            except LookupError as e:
                if e.args:  # list_id() failure, the others are reported already
                    self.error("Cannot read the task list", e)
                failures += 1
        if failures == len(self.tasklists):
            raise LookupError
        return items, not failures

    @staticmethod
    def get_task_text(task, include_due=False, list_id=None, icon=TASK_ANCHOR_SYMBOL):
        """ formats task object to zim markup
        :type task: dict, ex: {'id': '...', 'etag': '"..."', 'title': '...',
                               'updated': '2019-07-25T10:11:12.000Z',
                               'status': 'needsAction', 'due': '2018-12-14T00:00:00.000Z'
                               }
        :param include_due: If True, small start date string is included.
        :param list_id: The task list, written to the link so that we know where the task lives.
        :param icon: The link label, distinguishing the task lists on the page.
        """
        s = "[*] " if task.get("completed") else "[ ] "
        if task.get("id", ""):
            s += "[[gtasks://{}{}|{}]] ".format(f"{list_id}/" if list_id else "", task["id"], icon)
        if "title" not in task:
            logger.error("Task text is missing")
            return False
        # we dont want to have the task started with: "[ ] [ ] "
        s += task['title'][4:] if task['title'].startswith("[ ] ") else task['title']
        if include_due and task.get("due"):
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
        """ (list_id, task_id) of the gtasks:// link on the line, (None, None) if there is none.
            list_id is None for the links written before multiple lists were supported. """
        start = buffer.get_iter_at_line(line_i)
        # the icon is only the link label (and differs per list), the link itself is what we look for
        for offset in range(start.get_offset(), start.get_offset() + len(cls.readline(line_i, buffer))):
            # noinspection PyBroadException
            try:
                href = (buffer.get_link_data(buffer.get_iter_at_offset(offset)) or {}).get("href") or ""
            except Exception:
                continue
            if href.startswith("gtasks://"):
                list_id, _sep, task_id = href[len("gtasks://"):].rpartition("/")
                return list_id or None, task_id
        return None, None

    def read_task_from_selection(self, buffer=None):
        if not buffer:
            buffer = self.window.pageview.textview.get_buffer()
        task = {}

        lines = get_dumper("wiki").dump(buffer.get_parsetree(buffer.get_selection_bounds()))
        match = taskAnchorTreeRe.match("".join(lines))
        if match:
            task = {"id": match.group(3),
                    "_list_id": match.group(2),
                    "title": match.group(5).split("\n", 1)[0],
                    "status": "completed" if (match.group(1) or "").startswith("[*]") else "needsAction"
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
            self.cache.load()
            if not force:
                max_hours = 3
                now = time()
                last_time = self.cache.last_time()
                if now - last_time < max_hours * 3600 and \
                        datetime.datetime.now().day is datetime.datetime.fromtimestamp(last_time).day:
                    # if checked in last hours and there wasn't midnight (new tasks time) since then
                    return
            due_min = self.get_time(use_date=datetime.datetime.fromtimestamp(self.cache.last_time()),
                                    mode="midnight")
        else:  # load today's task (first run)
            due_min = self.get_time(mode="midnight")

        if all_history:  # all non-completed tasks
            self.cache.items_ids.clear()  # re-import everything
            due_min = None

        # Do internal fetching of new tasks text
        try:
            items, complete = self._read_chosen_lists(due_min)
        except LookupError:
            return
        if not items:
            self.info('No tasks found.')
            return

        texts = []
        icons = []
        items_ids = set()
        for item, list_id, icon in items:
            # We remember the task ID, not the etag. The etag is a version fingerprint that changes
            # whenever *anything* about the task changes - title, due date, even its position in the
            # list - so an already imported task that got touched on the server looked brand new and
            # was imported a second time. (`or item["etag"]` only keeps caches written by older
            # versions of the plugin working; it may be dropped in some future release.)
            already_imported = item["id"] in self.cache.items_ids or item["etag"] in self.cache.items_ids
            # Remember *every* task the server returned, not just those due today or later. `due_min`
            # above reaches back to the midnight of the previous import, so past-due tasks are handed
            # to us again on the next run - forgetting them here is what re-imported yesterday's
            # tasks today. The set stays bounded anyway: it is rebuilt from one response each time.
            items_ids.add(item["id"])
            if already_imported:
                logger.debug('Text already imported {}.'.format(item.get("title")))
                continue
            logger.info("Appending {}.".format(item.get("title")))
            logger.debug(item)
            text = self.get_task_text(item, self.preferences["include_start_date"], list_id, icon)
            if text:
                texts.append(text)
                icons.append(icon)
            else:
                self.error(f"Task {item['id']} has no title, skipped")
        if not complete:
            # The IDs of the list(s) that failed are not in this response, keep them so that their tasks
            # are not imported again. And do not move the time on, the next import has to read them again.
            items_ids |= self.cache.items_ids
        self.cache.items_ids = items_ids
        save = self.cache.touch_save if complete else self.cache.save

        # Refreshes page from current configuration
        page = self._get_page()
        per_list = ", ".join(f"{icon} {icons.count(icon)}" for icon in dict.fromkeys(icons))
        self.info(f"New tasks: {len(texts)}{f' ({per_list})' if len(self.tasklists) > 1 and texts else ''},"
                  f" page: {page.get_title()}")
        if not texts:
            if cache_exists:
                save()
            return
        text = "\n".join(texts) + "\n"

        # Insert tasks string into page
        contents = insert_after_first_blank_line(page.dump("wiki"), text)

        self._store_to_page(contents, page)

        # Save the list of successfully imported task
        save()
        # with open(CACHE_FILE, "wb") as f:
        #     pickle.dump(self.item_ids, f)

    def _store_to_page(self, contents, page):
        """

        :param contents: [] lines of the page
        :param page: ZimPage
        """
        bounds = buffer = None
        if self.window and self.window.pageview.page.name is page.name:
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

        service = self.calendar_api.get_service(info="Syncing tasks status")

        # build status cache from last 14 days so that we have to fetch one by one as little tasks as possible
        # XX maybe we may check the number of tasks to be fetched one-by-one after that and if it is still big,
        # perform more batch operation like this if there is a performance issue
        try:
            items, _complete = self._read_chosen_lists(self.get_time(add_days=-14, mode="midnight"),
                                                       show_completed=True, service=service)
        except LookupError:
            return False
        cache = {task["id"]: task["status"] == "completed" for task, _list_id, _icon in items}

        allow_single_search = True
        unidentified_tasks = []
        page = self._get_page()
        contents = []
        for line in page.dump("wiki"):
            match = taskAnchorTreeRe.match(line)
            if match:
                list_id = match[2]
                task_id = match[3]
                task_text = match[5]
                completed = cache.get(task_id, None)

                # perform single search if needed, this task was not fetched within the cache
                if completed is None and allow_single_search:
                    try:
                        task = service.tasks().get(task=task_id, tasklist=list_id or self.primary_list_id).execute()
                    except Error as e:
                        logger.warning("Task %s not found on the server: %s", task_id, e)
                        unidentified_tasks.append(task_text)
                    except (LookupError, httplib2.ServerNotFoundError) as e:
                        self.error("Cannot look up task status", e)
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
        service = self.calendar_api.get_service(info="Refreshing task lists")
        try:
            results = service.tasklists().list().execute()
            default_id = service.tasklists().get(tasklist=DEFAULT_LIST).execute()["id"]
        except (httplib2.ServerNotFoundError, Error) as e:
            self.error("Cannot refresh task lists", e)
            return False
        items = results.get('items', [])
        if not items:
            self.info('No task lists found.')
            return False

        self.cache.lists.clear()
        # the default list first, chosen_lists() relies on that to put a title to '@default'
        for tasklist in sorted(items, key=lambda tl: tl["id"] != default_id):
            self.cache.lists[tasklist["title"]] = tasklist["id"]
        self.info(f'{len(items)} task lists discovered you can choose in the menu.')
        self.cache.save()
        return True


class Cache:

    def __init__(self, path: Path):
        self._path = path
        self._mtime = None
        "mtime of the cache file as we last read it, so that we notice another process rewriting it"
        self.items_ids = set()
        """ IDs of the tasks returned by the last import, so that they are not imported twice.
            If user changes to another taks list, imports and changes back, items_ids are lost. """
        self.lists = {}
        "{title: id}"
        self.icons = {}
        "{title: icon} of every list ever chosen, kept when the list is unchosen"
        self.last_fetched = 0
        "get initial datetime"

    def load(self):
        """ (Re)read the cache file whenever it changed on disk since we last looked.

        We are not the only writer. The Zim GUI keeps a single Cache object alive for the whole
        life of the window, while `zim --plugin googletasks` (anacron, see README) imports from a
        separate process with a Cache of its own. Reading the file only once meant the running Zim
        never learned about the nightly import: the next manual "Import new tasks" still worked
        from the task IDs it had seen at startup, imported the very same tasks a second time and
        then overwrote the cron's cache file with its own outdated copy.

        Loading is tolerant: a file in the old jsonpickle format is migrated (and re-saved in the
        current format), an unknown or corrupt file yields an empty cache.
        """
        if self.exists() and os.path.getmtime(self._path) != self._mtime:
            mtime = os.path.getmtime(self._path)
            try:
                data, migrated = self.parse(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:  # ValueError: json errors, bad encoding
                logger.warning("Cannot read cache %s, starting with an empty one: %s", self._path, e)
                data, migrated = None, False
            data = data or {"items_ids": set(), "lists": {}, "icons": {}, "last_fetched": 0}
            self.__dict__.update(data)
            self._mtime = mtime
            if migrated:
                self.save()
        return self

    @staticmethod
    def parse(text):
        """ Cache file content -> ({items_ids, lists, last_fetched}, migrated) or (None, False) if unusable.

        Current format: {"version": 1, "items_ids": [...], "lists": {...}, "icons": {...}, "last_fetched": float}.
        Old format (no "version") was written by jsonpickle: plain JSON as well, only the set of IDs
        is wrapped as {"py/set": [...]}, and the timestamp may be called `date_at` in even older files.
        We read it as plain JSON so that already imported tasks are not imported again and
        jsonpickle is not needed.
        """
        raw = json.loads(text)
        if not isinstance(raw, dict):
            return None, False
        version = raw.get("version")
        migrated = version is None
        if migrated and not {"items_ids", "lists", "last_fetched", "date_at"} & raw.keys():
            return None, False
        if version is not None and version != CACHE_SCHEMA_VERSION:
            return None, False  # written by some future version, we do not know what it means
        ids = raw.get("items_ids", [])
        if isinstance(ids, dict):  # jsonpickle set
            ids = ids.get("py/set", [])
        lists = raw.get("lists", {})
        icons = raw.get("icons", {})
        if not isinstance(icons, dict):
            icons = {}
        last_fetched = raw.get("last_fetched", raw.get("date_at", 0))
        if not isinstance(ids, list) or not isinstance(lists, dict) or \
                not isinstance(last_fetched, (int, float)) or isinstance(last_fetched, bool):
            return None, False
        return {"items_ids": {i for i in ids if isinstance(i, str)},
                "lists": {k: v for k, v in lists.items() if isinstance(k, str) and isinstance(v, str)},
                "icons": {k: v for k, v in icons.items() if isinstance(k, str) and isinstance(v, str)},
                "last_fetched": last_fetched}, migrated

    def save(self):
        data = {"version": CACHE_SCHEMA_VERSION,
                "items_ids": sorted(self.items_ids),
                "lists": self.lists,
                "icons": self.icons,
                "last_fetched": self.last_fetched}
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self._path)  # atomic, so that a concurrent reader never sees half a file
        self._mtime = os.path.getmtime(self._path)

    def exists(self):
        return os.path.isfile(self._path)

    def touch_save(self):
        self.last_fetched = datetime.datetime.now().timestamp()
        self.save()

    def last_time(self):
        return self.last_fetched


# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(f"{GoogletasksPlugin.plugin_info['name']}: client secret file not found: {CLIENT_SECRET_FILE}")

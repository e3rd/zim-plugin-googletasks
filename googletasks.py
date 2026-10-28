#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import annotations, print_function

import base64
import binascii
import datetime
import html
import json
import logging
import os
import queue
import re
import sys
import threading
from pathlib import Path
from time import sleep, time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import dateutil.parser
import httplib2
from dateutil.relativedelta import relativedelta
from gi.repository import GLib, Gtk
from googleapiclient import discovery
from googleapiclient.errors import Error, HttpError
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
from zim.formats import CHECKED_BOX, UNCHECKED_BOX, XCHECKED_BOX, MIGRATED_BOX, TRANSMIGRATED_BOX
from zim.main import NotebookCommand
from zim.main.command import GtkCommand
from zim.notebook import build_notebook, Path as ZimPath
from zim.notebook.operations import NotebookOperationOngoing, ongoing_operation
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
TASK_URL = "https://tasks.google.com/task/"
taskAnchorTreeRe = re.compile(r'(\[.\]\s)?\[\[((?:gtasks://|' + re.escape(TASK_URL) + r')[^|\]]*)\|([^\]]*)\]\]\s?(.*)')
""" A task line: 1 checkbox, 2 href of the task anchor (see task_href), 3 icon, 4 task text.
    Ex: `[ ] [[https://tasks.google.com/task/<web_id>?list=<list_id>|💼]] Task title` """


def task_href(task_id, list_id=None, web_link=None):
    """ The href of the task anchor: the task's page in Google Tasks, so that clicking the anchor opens it.

    The task ID of the API is the URL-safe base64 (without padding) of the ID in the web link
    (`webViewLink`: https://tasks.google.com/task/<web_id>?sa=6), so the link keeps it, and the list ID
    goes to the `list` parameter. If the web ID cannot be had (it is computed when the task comes
    without `webViewLink`), the original `gtasks://<list_id>/<task_id>` is written instead.
    """
    if web_link and "/task/" in web_link:
        web_id = web_link.split("/task/", 1)[1].split("?", 1)[0].split("#", 1)[0]
    else:
        try:
            web_id = base64.urlsafe_b64decode(task_id + "=" * (-len(task_id) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            web_id = None
    if web_id and _api_task_id(web_id) == task_id:
        return TASK_URL + quote(web_id, safe=":") + (f"?{urlencode({'list': list_id})}" if list_id else "")
    return "gtasks://{}{}".format(f"{list_id}/" if list_id else "", task_id)


def parse_task_href(href):
    """ (list_id, task_id) of a task anchor href, None if it is not one. See task_href.
        list_id is None for the links written before multiple lists were supported. """
    if href.startswith("gtasks://"):
        list_id, _sep, task_id = href[len("gtasks://"):].rpartition("/")
        return list_id or None, task_id
    if href.startswith(TASK_URL):
        url = urlsplit(href)
        web_id = url.path.rsplit("/task/", 1)[1]
        return parse_qs(url.query).get("list", [None])[0], _api_task_id(web_id)
    return None


def _api_task_id(web_id):
    return base64.urlsafe_b64encode(web_id.encode()).decode().rstrip("=")
SOURCE_ICONS = {"DOCUMENT": "📄", "SPACE": "💬", "email": "📧", "keep_note": "📝"}
""" Labels of the links to where a task comes from: a Google Doc or a Chat space it was assigned from
    (assignmentInfo.surfaceType), an e-mail or a Keep note it was created from (links[].type).
    🔗 for an unknown one. """
SOURCE_ARROW = "↗"
""" Follows the icon in the link label. Zim opens a link on Enter only with the caret inside its label,
    which a one-character label has no room for (the caret is either before or after it). """
sourceLinkRe = re.compile(r'\[\[(https?://[^|\]]+)\|(📄|💬|📧|📝|🔗)' + SOURCE_ARROW + r'?\]\]\s*')
""" A link to where the task comes from, see SOURCE_ICONS: 1 URL, 2 icon. """
start_date_in_title = re.compile(r'(.*)>(\d{4}-\d{2}(-\d{2})?)(.*)')  # (_\{//)?  (//})?
INVALID_DAY = "N/A"
HTTP_TIMEOUT = 30
"seconds; without it, a dead network would keep a job (and the jobs queued after it) waiting forever"
BUSY_SUFFIX = " ⟳ Google Tasks"
"appended to the window title while the jobs talking to Google run"
INBOX_ICON = "📥"
"marks the tasks imported from the inbox sheet"
INBOX_TITLE = "Zim inbox (Google Tasks plugin)"
INBOX_SHEET = "Inbox"
INBOX_BANNER = ("📥 Managed by the Google Tasks plugin for Zim. The rows below are imported into Zim as tasks"
                " at every sync and then deleted from this sheet. Add rows freely, do not change rows 1-2.")
INBOX_COLUMNS = ("text", "due", "note")
"the inbox sheet columns, found by the first word of their header in row 2; `text` is required"
INBOX_HEADER = ("text", "due (YYYY-MM-DD, optional)", "note (optional)")
INBOX_HEADER_ROWS = 2
"banner + header, the tasks start below"
INBOX_MISSING = "Inbox sheet not accessible – run Tools / Google Tasks / Create inbox sheet again"
INBOX_CLAUDE_PROMPT = (
    "When I ask you to make a task for me (\"make a task\", \"udělej mi úkol\"…), append a row to the Google Sheet"
    " {url} (sheet \"Inbox\", columns A-C): text = the task, due = a date YYYY-MM-DD only if I mention one,"
    " note = optional details. Only append rows, never edit or delete existing ones.")
INBOX_CALENDAR_TITLE = "Zim inbox"
INBOX_CALENDAR_DESCRIPTION = ("📥 Managed by the Google Tasks plugin for Zim. Its events are imported into Zim as tasks"
                              " at every sync and then deleted from this calendar.")
INBOX_CALENDAR_MISSING = "Inbox calendar not accessible – run Tools / Google Tasks / Create inbox calendar again"
INBOX_CALENDAR_CLAUDE_PROMPT = (
    "When I ask you to make a task for me (\"make a task\", \"udělej mi úkol\"…), create an event in my Google"
    " Calendar \"Zim inbox\" (calendar ID {calendar_id}): summary = the task, an all-day event on the date I mention"
    " (today if none), description = optional details. Only create events there, never edit or delete them.")
INBOX_CALENDAR_BATCH = 50
"events deleted in one batch request (the Calendar API takes up to 50)"
sheetIdRe = re.compile(r'/spreadsheets/d/([\w-]+)')
htmlLinkRe = re.compile(r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
activationUrlRe = re.compile(r'https://console\.(?:developers|cloud)\.google\.com/\S+?(?=["\s]|$)')


def disabled_api_message(exc):
    """ A readable message if `exc` is the 403 Google returns when the API is not enabled in the Google Cloud
        project of the OAuth client secret (e.g. the Sheets API for the inbox), else None. """
    if not isinstance(exc, HttpError) or exc.resp.status != 403:
        return None
    content = exc.content.decode("utf-8", "replace")
    if "SERVICE_DISABLED" not in content and "accessNotConfigured" not in content:
        return None
    try:
        details = json.loads(content)["error"].get("details", [])
        meta = next(d["metadata"] for d in details if d.get("reason") == "SERVICE_DISABLED")
        api, url = meta.get("serviceTitle"), meta.get("activationUrl")
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        api = url = None
    if not url:
        url = (activationUrlRe.findall(content) or [None])[0]
    msg = f"{api or 'This API'} is not enabled in the Google Cloud project of the OAuth client secret."
    return f"{msg} Enable it at {url} and try again in a few minutes." if url else f"{msg} Enable it there."




def parse_sheet_id(value):
    """ The spreadsheet ID from the `inbox_sheet` preference, which may be its URL or the bare ID. None if empty. """
    value = (value or "").strip()
    match = sheetIdRe.search(value)
    return match[1] if match else value or None


def parse_inbox_rows(values):
    """ Rows of the inbox sheet (`values` as the Sheets API returns them, trailing blanks cut)
        -> [(row index, text, due, note)] of the rows below the header, blank ones included (with an empty text)
        so that they get deleted as well.
        Raises ValueError if the header (row 2) does not name the columns, see INBOX_COLUMNS. """
    header = values[INBOX_HEADER_ROWS - 1] if len(values) >= INBOX_HEADER_ROWS else []
    columns = {}
    for i, cell in enumerate(header):
        word = (cell.split() or [""])[0].lower()
        if word in INBOX_COLUMNS and word not in columns:
            columns[word] = i
    if "text" not in columns:
        raise ValueError("Inbox sheet header changed – fix row 2 or run Tools / Google Tasks / Create inbox sheet"
                         " again")

    def cell(row, name):
        i = columns.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    return [(index, cell(row, "text"), cell(row, "due"), cell(row, "note"))
            for index, row in enumerate(values[INBOX_HEADER_ROWS:], INBOX_HEADER_ROWS)]


def get_inbox_text(text, due="", note=""):
    """ Zim markup of a task from the inbox sheet: a plain Zim task (it does not live in Google Tasks),
        the due date in the Zim notation `<2026-10-28`, the note on the next line(s), as Google Tasks notes are.
        A row with only a note takes the note as its text. None for a blank row. """
    text, note = " ".join(text.split()), note.strip()
    if not text:
        text, note = " ".join(note.split()), ""
    if not text:
        return None
    s = f"[ ] {INBOX_ICON} {text}"
    if due:
        try:
            s += " _{//<" + dateutil.parser.parse(due, default=datetime.datetime(2020, 1, 1)).date().isoformat() \
                 + "//}"
        except (ValueError, OverflowError):
            s += f" (due: {due})"
    if note:
        s += "\n" + note
    return s


def html_to_text(s):
    """ Plain text of an event description, which Google Calendar may keep as HTML. A link keeps its URL. """
    s = htmlLinkRe.sub(lambda m: m[1] if m[2] in (m[1], "") else f"{m[2]} {m[1]}", s)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def get_event_task(event):
    """ (text, due, note) of an event of the inbox calendar, as get_inbox_text takes them. The due date is
        the day of the event, the time of a timed one follows the text; a repeated one is imported once. """
    start = event.get("start") or {}
    text = event.get("summary") or ""
    due = start.get("date") or ""
    if start.get("dateTime"):  # in the time zone of the calendar (UTC for the one the plugin created)
        local = dateutil.parser.isoparse(start["dateTime"]).astimezone()
        due = local.date().isoformat()
        if text:
            text += local.strftime(" %H:%M")
    note = html_to_text(event.get("description") or "")
    if event.get("recurrence"):
        note = "\n".join(filter(None, (note, "repeats: " + "; ".join(event["recurrence"]))))
    return text, due, note


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
        ('include_undated', 'bool', _('Import tasks without a due date'
                                      '\nEach one only once, when it first appears.'), True),
        ('include_assigned', 'bool', _('Import tasks assigned to me from Google Docs or Chat'), True),
        ('include_start_date', 'bool', _('Include start date'
                                         '\nStandard Zim notation, ex: ">2021-01-01" means you want'
                                         ' the task to appear not sooner than in January.'), False),
        ('page', 'page', _('Page to be updated (empty = homepage)'), ""),
        ('inbox_sheet', 'string', _('Inbox sheet (empty = off)'
                                    '\nFilled in by Tools / Google Tasks / Create inbox sheet. Its rows are imported'
                                    ' as tasks, then deleted from the sheet.'), ""),
        ('inbox_calendar', 'string', _('Inbox calendar (empty = off)'
                                       '\nFilled in by Tools / Google Tasks / Create inbox calendar. Its events are'
                                       ' imported as tasks, then deleted from the calendar.'), ""),
        ('inbox_calendar_tasklist', 'string', _('Inbox calendar: make Google Tasks of its events in this list'
                                                '\n(empty = plain Zim tasks 📥, "@default" = the default list)'), ""),
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
                if task_id:
                    # ticked at once, the server is told in the background; if it fails, the tick is undone
                    revert = (self.page.name, task_id, self.get_bullet(line), controller.line_markup(self, line),
                              controller.tick(task_id))
                    self.set_bullet_original(line, bullet, indent=indent)

                    def job():
                        if controller.task_checked(list_id, task_id, bullet) is False:
                            on_main(controller.revert_bullet, *revert)

                    controller.background(job)
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
        # With Zim running, this command is dispatched into its process and build_notebook() returns the
        # cached Notebook of the open window. Its controller knows the window, so the caret stays put;
        # a new windowless one would replace it in ntb.plugin_googletasks and throw the caret to the page end.
        controller = getattr(ntb, "plugin_googletasks", None)
        if controller and controller.window:
            controller.background(lambda: controller.fetch(True), "import")
            return  # we are inside the GUI process, no exit code reaches cron
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
    permission_inbox_file = os.path.join(WORKDIR, 'googletasks_oauth_inbox.json')
    inbox_scope = 'https://www.googleapis.com/auth/drive.file'
    """ Only the files the plugin created itself – the inbox sheet. Unlike the `spreadsheets` scope
        (every sheet of the user), it is not a sensitive one, Google needs no verification of the app for it. """
    permission_inbox_calendar_file = os.path.join(WORKDIR, 'googletasks_oauth_calendar.json')
    inbox_calendar_scope = 'https://www.googleapis.com/auth/calendar.app.created'
    """ Only the calendars the plugin created itself – the inbox calendar. The other calendars of the user
        do not exist for it: reading, changing or deleting them (or their events) ends with 404. """

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

    def get_service(self, info=None, write_access=False, claim=True):
        """ :param claim: Run the OAuth flow if not granted yet. Otherwise raise LookupError then. """
        if info:
            self.controller.info(info)  # strangely, this info is not seen in the status bar
        if write_access:
            self.credential_path = GoogleCalendarApi.permission_write_file
            self.scope = 'https://www.googleapis.com/auth/tasks'
        else:
            self.credential_path = GoogleCalendarApi.permission_read_file
            self.scope = 'https://www.googleapis.com/auth/tasks.readonly'
        credentials = self.get_credentials(claim)
        if not credentials:
            raise LookupError("Google Tasks access not granted yet – add a task from Zim once to grant it")
        http = credentials.authorize(httplib2.Http(timeout=HTTP_TIMEOUT))
        return discovery.build('tasks', 'v1', http=http)

    def get_inbox_services(self, claim=False):
        """ Sheets service for the inbox sheet, with their own credentials (see inbox_scope).
            :param claim: Run the OAuth flow if not granted yet. Otherwise raise LookupError then – the import
                          (maybe headless, from cron) must not stop and wait for the browser. """
        self.credential_path = GoogleCalendarApi.permission_inbox_file
        self.scope = GoogleCalendarApi.inbox_scope
        credentials = self.get_credentials(claim)
        if not credentials:
            raise LookupError(INBOX_MISSING)
        http = credentials.authorize(httplib2.Http(timeout=HTTP_TIMEOUT))
        return discovery.build('sheets', 'v4', http=http)

    def get_inbox_calendar_service(self, claim=False):
        """ Calendar service for the inbox calendar, with its own credentials (see inbox_calendar_scope).
            :param claim: see get_inbox_services """
        self.credential_path = GoogleCalendarApi.permission_inbox_calendar_file
        self.scope = GoogleCalendarApi.inbox_calendar_scope
        credentials = self.get_credentials(claim)
        if not credentials:
            raise LookupError(INBOX_CALENDAR_MISSING)
        http = credentials.authorize(httplib2.Http(timeout=HTTP_TIMEOUT))
        return discovery.build('calendar', 'v3', http=http)

    def get_credentials(self, claim=True):
        """Gets valid user credentials from storage.

        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials (if `claim`, else None is returned).

        Returns:
            Credentials, the obtained credential.
        """
        store = Storage(self.credential_path)
        credentials = store.get()
        if not credentials or credentials.invalid:
            if not claim:
                return None
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


def on_main(fn, *args):
    """ Call `fn(*args)` in the GTK main thread and return its result (or raise its exception).

    Anything touching GTK – a page, the buffer, a dialog, the preferences (their `changed` signal) – must
    run there; the jobs of Worker call it from their thread. Called in the main thread (always so when
    headless), it simply calls `fn`. The main thread never waits for the worker, so this cannot deadlock.
    """
    if threading.current_thread() is threading.main_thread():
        return fn(*args)
    done = threading.Event()
    result = {}

    def call():
        try:
            result["value"] = fn(*args)
        except BaseException as e:  # handed over to the waiting thread
            result["error"] = e
        finally:
            done.set()
        return False  # once, not again (GLib.idle_add)

    GLib.idle_add(call)
    done.wait()
    if "error" in result:
        raise result["error"]
    return result["value"]


def wait_for_notebook(notebook, timeout=300):
    """ Wait (in a Worker job) until Zim finishes its notebook operation, e.g. the index update after its start.
        Meanwhile it refuses to store pages. Headless, in the main thread, there is nothing to wait for. """
    if threading.current_thread() is threading.main_thread():
        return
    deadline = time() + timeout
    while on_main(ongoing_operation, notebook) and time() < deadline:
        sleep(1)


class Worker:
    """ Runs the jobs that talk to Google one by one in a background thread, so that Zim does not freeze.

    A job does the network part itself and hands the GTK part (the page, dialogs) over by on_main().
    One at a time, in order: they share the cache and the page, and a checkbox ticked during
    an import must not overtake it. A named job (an import) is not queued again while it is queued or running.
    While there are jobs, the window title gets BUSY_SUFFIX (Zim 0.75 has no status bar); when Zim rewrites
    the title (another page opened – also the very first one, after the startup import has been queued),
    the suffix is put back.
    """

    def __init__(self, window, on_error):
        self.window = window
        self.on_error = on_error
        "called (in the main thread) with an exception a job raised, a bug - expected failures are reported by the job"
        self.queue = queue.Queue()
        self.thread = None
        self.count = 0
        "jobs queued or running; changed in the main thread only"
        self.names = set()
        if hasattr(window, "connect"):
            window.connect("notify::title", lambda *_: self.count and self._busy(True))

    def submit(self, job, name=None):
        """ Queue the job. Returns False if a job of this name is queued or running already. """
        if name and name in self.names:
            logger.info("[Googletasks] %s is running already", name)
            return False
        if name:
            self.names.add(name)
        self.count += 1
        if self.count == 1:
            self._busy(True)
        self.queue.put((job, name))
        if not self.thread:
            self.thread = threading.Thread(target=self._run, name="googletasks", daemon=True)
            self.thread.start()
        return True

    def _run(self):
        while True:
            job, name = self.queue.get()
            # noinspection PyBroadException
            try:
                job()
            except Exception as e:
                logger.exception("[Googletasks] job failed")
                GLib.idle_add(self._report, e)
            GLib.idle_add(self._finished, name)

    def _report(self, exc):
        self.on_error(exc)
        return False

    def _finished(self, name):
        self.names.discard(name)
        self.count -= 1
        if not self.count:
            self._busy(False)
        return False

    def _busy(self, busy):
        try:
            current = self.window.get_title() or ""
            title = current.replace(BUSY_SUFFIX, "")
            if busy:
                title += BUSY_SUFFIX
            if title != current:  # (setting it again would notify again, see __init__)
                self.window.set_title(title)
        except Exception as e:  # a cosmetic thing, never worth failing a job for
            logger.debug("Cannot set the window title: %s", e)


CHECKBOXES = (UNCHECKED_BOX, CHECKED_BOX, XCHECKED_BOX, MIGRATED_BOX, TRANSMIGRATED_BOX)
BOX_MARKUP = {UNCHECKED_BOX: "[ ]", CHECKED_BOX: "[*]", XCHECKED_BOX: "[x]", MIGRATED_BOX: "[>]",
              TRANSMIGRATED_BOX: "[<]"}
"the checkboxes in the wiki markup"


def ends_task(buffer, line):
    """ True if the line no longer belongs to the task above it (see send_as_task): there is no such line,
        or it is blank, a heading or another task – any checkbox, ours (whatever its list icon) or Zim's own.
        A bulleted line is a part of the task (a note). """
    if line >= buffer.get_line_count():
        return True
    text = _line_text(buffer, line)
    return (not text.strip() or buffer.get_bullet(line) in CHECKBOXES or TASK_ANCHOR_SYMBOL in text
            or bool(buffer.get_heading_text(buffer.get_iter_at_line(line))))


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
            controller.background(controller.fetch, "import")

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
                                    <menuitem action='create_inbox'/>
                                    <menuitem action='create_inbox_calendar'/>
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
            # no text is selected yet - we try to detect and auto-select a task: its line and the ones below it
            line_i = buffer.get_insert_iter().get_line()
            start_iter = buffer.get_iter_at_line(line_i)
            while not ends_task(buffer, line_i + 1):
                line_i += 1

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
        self.controller.background(self.controller.calendar_api.get_service, "claim")

    @action(_('_Claim write access'))  # T: menu item
    def permission_write(self):
        self.controller.background(lambda: self.controller.calendar_api.get_service(write_access=True), "claim")

    @action(_('_Import new tasks'), accelerator='<ctrl><alt>g')  # T: menu item
    def import_tasks(self):
        if self.controller.preferences["auto_sync"]:
            self.sync_status()
        self.controller.background(lambda: self.controller.fetch(force=True), "import")

    @action(_('_Import all non-completed even already imported tasks from the past'))  # T: menu item
    def import_history(self):
        if self.controller.preferences["auto_sync"]:
            self.sync_status()
        self.controller.background(lambda: self.controller.fetch(all_history=True), "import")

    @action(_('Create _inbox sheet...'))  # T: menu item
    def create_inbox(self):
        """ Create the inbox sheet (or show the one already set) and how to tell Claude about it. """
        def job():
            url = self.controller.create_inbox()
            if url:  # not waited for, the dialog would hold the jobs queued after this one
                GLib.idle_add(self._show_inbox, url)
        self.controller.background(job, "inbox")

    def _show_inbox(self, url):
        self._show_inbox_dialog(
            _("Inbox sheet is ready"), url,
            f"{url}\n\nWhatever is added to its rows gets imported here as a task {INBOX_ICON} at every sync"
            " and deleted from the sheet. Its name, banner and header say so to anyone who opens it.\n\n"
            "To let Claude (e.g. in the mobile app, which reaches the sheet through its Google Drive / Sheets"
            " connector) add tasks there, paste this into its instructions"
            " (claude.ai / Settings / Profile, or a project's instructions):\n\n", INBOX_CLAUDE_PROMPT.format(url=url),
            _("_Open sheet"))
        return False  # once (GLib.idle_add)

    @action(_('Create inbox _calendar...'))  # T: menu item
    def create_inbox_calendar(self):
        """ Create the inbox calendar (or show the one already set) and how to tell Claude about it. """
        def job():
            calendar_id = self.controller.create_inbox_calendar()
            if calendar_id:  # not waited for, the dialog would hold the jobs queued after this one
                GLib.idle_add(self._show_inbox_calendar, calendar_id)
        self.controller.background(job, "inbox_calendar")

    def _show_inbox_calendar(self, calendar_id):
        tasklist = self.controller.preferences["inbox_calendar_tasklist"]
        self._show_inbox_dialog(
            _("Inbox calendar is ready"), "https://calendar.google.com/calendar/r",
            f"Calendar \"{INBOX_CALENDAR_TITLE}\" ({calendar_id}) is in your Google Calendar now. Its events get"
            " imported here at every sync – as "
            + (f"Google Tasks of the list {tasklist}" if tasklist else f"tasks {INBOX_ICON}")
            + " (see File / Properties / Google Tasks) – and deleted from the calendar. The plugin sees no other"
            " calendar of yours.\n\n"
            "To let Claude (e.g. in the mobile app, through its Google Calendar connector with the permission"
            " to create events) add tasks there, paste this into its instructions"
            " (claude.ai / Settings / Profile, or a project's instructions):\n\n",
            INBOX_CALENDAR_CLAUDE_PROMPT.format(calendar_id=calendar_id), _("_Open calendar"))
        return False  # once (GLib.idle_add)

    def _show_inbox_dialog(self, title, url, text, prompt, open_label):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.INFO,
                                   text=title)
        dialog.format_secondary_text(text + prompt)
        for label in dialog.get_message_area().get_children():
            label.set_selectable(True)
        dialog.add_buttons(_("_Copy instructions for Claude"), 1, open_label, 2, _("_Close"),
                           Gtk.ResponseType.CLOSE)
        while True:
            response = dialog.run()
            if response == 1:
                Gtk.Clipboard.get_default(dialog.get_display()).set_text(prompt, -1)
            elif response == 2:
                Gtk.show_uri_on_window(dialog, url, Gtk.get_current_event_time())
            else:
                break
        dialog.destroy()

    @action(_('_Sync tasks status from server'))  # T: menu item
    def sync_status(self):
        self.controller.background(self.controller.sync_bullets_from_server, "sync")

    @action(_('_Refresh task lists'))  # T: menu item
    def refresh_task_lists(self):
        self.controller.background(lambda: self.controller.refresh_task_lists() and on_main(self.build_list_menu),
                                   "lists")


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
        self.controller.background(lambda: self.controller.submit_task(task=self.task)
                                   or on_main(self.do_response_cancel))
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
        self.ticks = {}
        "{task_id: number of its last tick}, see tick()"
        self.worker = Worker(window, lambda e: self.error("Unexpected error", e, fatal=True)) if window else None

    def background(self, job, name=None):
        """ Run the job talking to Google in the background (see Worker), or right now when headless
            (nothing to freeze, and the command must know whether it failed). """
        if self.worker:
            return self.worker.submit(job, name)
        job()
        return True

    def tick(self, task_id):
        """ Number the tick of the task's checkbox (in the main thread, see set_bullet), so that revert_bullet
            knows whether the user has un/ticked the task again since. """
        self.ticks[task_id] = self.ticks.get(task_id, 0) + 1
        return self.ticks[task_id]

    @staticmethod
    def line_markup(buffer, line):
        """ Wiki markup of the line in the buffer, without the newline. """
        start = buffer.get_iter_at_line(line)
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        return "".join(get_dumper("wiki").dump(buffer.get_parsetree((start, end)))).rstrip("\n")

    def revert_bullet(self, page_name, task_id, bullet, markup, tick):
        """ The server refused the un/tick of the task: put its checkbox back to `bullet`.
            If the line is gone meanwhile (the task ticked as done and deleted), it is put back – its `markup`
            from before the tick – to the top of the page (after the first blank line, where new tasks go).

            The page is taken from the notebook, as the import does (see _store_to_page), never through the
            buffer of the tick: the user may have left the page meanwhile and a buffer of a page no longer shown
            is not saved, or worse, saved later over what has been written to the page file since (cron import).
            :param tick: see tick(); if the task has been un/ticked again since, the newest tick decides.
        """
        if self.ticks.get(task_id) != tick:
            return
        page = self.notebook.get_page(ZimPath(page_name))
        if not page.exists():  # deleted or moved meanwhile, not to be recreated
            page = self._get_page()
        lines = page.dump("wiki")
        for i, line in enumerate(lines):
            text = line.lstrip("\t")
            match = taskAnchorTreeRe.match(text)
            if match and parse_task_href(match[2])[1] == task_id:
                box = BOX_MARKUP.get(bullet)
                indent = line[:len(line) - len(text)]
                lines[i] = indent + (box + " " if box else "") + text[len(match[1] or ""):]
                message = f"Checkbox of task {task_id} reverted"
                break
        else:
            lines = insert_after_first_blank_line(lines, markup + "\n")
            message = f"Task {task_id} put back to the page {page.name}"
        try:
            self._store_to_page(lines, page)
        except NotebookOperationOngoing as e:
            self.error(f"Notebook busy, cannot revert the checkbox of task {task_id}: {markup}", e)
            return
        self.info(message)

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
        :return: True if done, False if failed, None if the task is not on the server any more
        """
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}
        try:
            service = self.calendar_api.get_service(write_access=True,
                                                    info=f"Marking task ID {task_id} as {task['status']}")
            list_id = list_id or self.primary_list_id
            if bullet is CHECKED_BOX:  # small patch will be sufficient
                service.tasks().patch(tasklist=list_id, task=task_id, body=task).execute()
            else:  # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist=list_id, task=task_id).execute()
                task.pop("completed", None)
                task["status"] = "needsAction"
                service.tasks().update(tasklist=list_id, task=task_id, body=task).execute()
        except HttpError as e:
            if e.resp.status in (404, 410):  # deleted on the server, nothing to revert to
                self.error(f"Task {task_id} is not on the server any more", e)
                return None
            self.error("Error in communication while un/checking", e)
            return False
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
        if threading.current_thread() is not threading.main_thread():
            return on_main(self.error, msg, exc, fatal)
        exc = disabled_api_message(exc) or exc
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

    def _read_task_list(self, due_min, show_completed=False, service=None, list_id=DEFAULT_LIST, undated=False):
        """ All the tasks of the list due since `due_min`, page by page (the server returns at most 100 at once).
            :param undated: Instead, all the tasks that have no due date. The server never returns them when
                            filtering by the due date, so we read the list without the filter and pick them.
            Tasks assigned to the user from Google Docs or Chat are returned only if `include_assigned` is set.
            In case of an error, informs user and raises LookupError. """
        if not service:
            service = self._read_service("Reading task list")
        due = {} if undated else {"dueMin": due_min,
                                  "dueMax": self.get_time(add_days=1, mode="midnight")}  # for format see #17
        items = []
        page_token = None
        try:
            while True:
                results = service.tasks().list(maxResults=100,
                                               tasklist=list_id,
                                               showCompleted=show_completed,
                                               showAssigned=self.preferences["include_assigned"],
                                               pageToken=page_token,
                                               **due
                                               ).execute()
                items.extend(results.get('items', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    return [item for item in items if not item.get("due")] if undated else items
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            self.error("Cannot read the task list", e)
            raise LookupError from e

    def _read_chosen_lists(self, due_min, show_completed=False, service=None, undated=False):
        """ [(item, list_id, icon)] from all the chosen lists.
            :param undated: Add the tasks that have no due date as well.
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
                if undated:
                    items.extend((item, list_id, icon)
                                 for item in self._read_task_list(due_min, show_completed, service, list_id, True))
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

        A task assigned from Google Docs or Chat or created from an e-mail or a Keep note gets a link there before
        its title, so that it is clear what it belongs to (its title is often just the comment text or the subject).
        Read back by read_task_from_selection.
        """
        s = "[*] " if task.get("completed") else "[ ] "
        # the icons go together, a single space separates them from the title
        meta = ""
        if task.get("id", ""):
            meta += f"[[{task_href(task['id'], list_id, task.get('webViewLink'))}|{icon}]]"
        if "title" not in task:
            logger.error("Task text is missing")
            return False
        for url, source_icon in task.get("_source_links") or GoogletasksController.source_links(task):
            meta += f"[[{url}|{source_icon}{SOURCE_ARROW}]]"
        if meta:
            s += meta + " "
        # we dont want to have the task started with: "[ ] [ ] "
        s += task['title'][4:] if task['title'].startswith("[ ] ") else task['title']
        if include_due and task.get("due"):
            d = GoogletasksController.get_time(mode="date-only", from_string=task['due'])
            s += " _{//>" + d + "//}"
        if task.get("notes", ""):  # insert other lines
            s += "\n" + task['notes']
        return s

    @staticmethod
    def source_links(task):
        """ [(URL, icon)] of where the task comes from, see SOURCE_ICONS. """
        links = []
        info = task.get("assignmentInfo") or {}
        if info.get("linkToTask"):
            links.append((info["linkToTask"], SOURCE_ICONS.get(info.get("surfaceType"), "🔗")))
        links.extend((link["link"], SOURCE_ICONS.get(link.get("type"), "🔗"))
                     for link in task.get("links") or () if link.get("link"))
        return links

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
        """ (list_id, task_id) of the task anchor on the line, (None, None) if there is none.
            list_id is None for the links written before multiple lists were supported. """
        start = buffer.get_iter_at_line(line_i)
        # the icon is only the link label (and differs per list), the link itself is what we look for
        for offset in range(start.get_offset(), start.get_offset() + len(cls.readline(line_i, buffer))):
            # noinspection PyBroadException
            try:
                href = (buffer.get_link_data(buffer.get_iter_at_offset(offset)) or {}).get("href") or ""
            except Exception:
                continue
            parsed = parse_task_href(href)
            if parsed:
                return parsed
        return None, None

    def read_task_from_selection(self, buffer=None):
        if not buffer:
            buffer = self.window.pageview.textview.get_buffer()
        task = {}

        lines = get_dumper("wiki").dump(buffer.get_parsetree(buffer.get_selection_bounds()))
        match = taskAnchorTreeRe.match("".join(lines))
        if match:
            list_id, task_id = parse_task_href(match.group(2))
            task = {"id": task_id,
                    "_list_id": list_id,
                    "title": match.group(4).split("\n", 1)[0],
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
        # keep the links out of the title on the server, but put them back to the page if the dialog is cancelled
        task["_source_links"] = [(m[1], m[2]) for m in sourceLinkRe.finditer(task["title"])]
        task["title"] = sourceLinkRe.sub("", task["title"]).strip()
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
        # independent of the tasks, they are not throttled: the inbox should not wait
        self.import_inbox()
        self.import_inbox_calendar()

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
            self.cache.undated_ids.clear()
            due_min = None

        # Do internal fetching of new tasks text
        try:
            items, complete = self._read_chosen_lists(due_min, undated=self.preferences["include_undated"])
        except LookupError:
            return
        if not items:
            self.info('No tasks found.')
            return

        texts = []
        icons = []
        items_ids = set()
        undated_ids = set()
        for item, list_id, icon in items:
            # We remember the task ID, not the etag. The etag is a version fingerprint that changes
            # whenever *anything* about the task changes - title, due date, even its position in the
            # list - so an already imported task that got touched on the server looked brand new and
            # was imported a second time. (`or item["etag"]` only keeps caches written by older
            # versions of the plugin working; it may be dropped in some future release.)
            already_imported = item["id"] in self.cache.items_ids or item["etag"] in self.cache.items_ids \
                or item["id"] in self.cache.undated_ids
            # Remember *every* task the server returned, not just those due today or later. `due_min`
            # above reaches back to the midnight of the previous import, so past-due tasks are handed
            # to us again on the next run - forgetting them here is what re-imported yesterday's
            # tasks today. The set stays bounded anyway: it is rebuilt from one response each time.
            (items_ids if item.get("due") else undated_ids).add(item["id"])
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
            undated_ids |= self.cache.undated_ids
        # A task without a date is remembered until it shows up dated. Had it got a date in the future, it would
        # vanish from both the undated and the dated (only up to tomorrow) response, so we cannot rebuild the set
        # from the response the way we do with items_ids - it would be imported a second time when it falls due.
        self.cache.undated_ids = undated_ids | (self.cache.undated_ids - items_ids)
        self.cache.items_ids = items_ids
        save = self.cache.touch_save if complete else self.cache.save
        wait_for_notebook(self.notebook)
        try:
            on_main(self._insert_tasks, texts, icons, save if texts or cache_exists else None)
        except NotebookOperationOngoing as e:  # the cache is not saved, the tasks are imported next time
            self.error("Notebook busy, tasks will be imported at the next sync", e)

    def _insert_tasks(self, texts, icons, save):
        """ Insert the imported tasks into the page (in the main thread, see on_main), then call `save`
            (the cache, which must not remember them as imported before they are on the page). """
        page = self._get_page()
        per_list = ", ".join(f"{icon} {icons.count(icon)}" for icon in dict.fromkeys(icons))
        self.info(f"New tasks: {len(texts)}{f' ({per_list})' if len(self.tasklists) > 1 and texts else ''},"
                  f" page: {page.get_title()}")
        if texts:
            self._store_to_page(insert_after_first_blank_line(page.dump("wiki"), "\n".join(texts) + "\n"), page)
        if save:
            save()
        # with open(CACHE_FILE, "wb") as f:
        #     pickle.dump(self.item_ids, f)

    def _inbox_sheet(self, sheets, sheet_id):
        """ (title, numeric sheetId) of the first sheet of the inbox spreadsheet """
        properties = sheets.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
        first = properties["sheets"][0]["properties"]
        return first["title"], first["sheetId"]

    def import_inbox(self):
        """ Import the rows of the inbox sheet (the `inbox_sheet` preference) as tasks and delete them from the sheet.
            Does nothing if no sheet is set. Returns the number of tasks imported (None on error). """
        sheet_id = parse_sheet_id(self.preferences["inbox_sheet"])
        if not sheet_id:
            return 0
        try:
            sheets = self.calendar_api.get_inbox_services()
            title, grid_id = self._inbox_sheet(sheets, sheet_id)
            values = sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id, range="'{}'!A:Z".format(title.replace("'", "''"))).execute().get("values", [])
        except LookupError as e:
            self.error(str(e))
            return None
        except (httplib2.ServerNotFoundError, OSError, Error, KeyError, IndexError) as e:
            self.error(INBOX_MISSING, e)
            return None
        try:
            rows = parse_inbox_rows(values)
        except ValueError as e:
            self.error(str(e))
            return None
        if not rows:
            return 0
        texts = [text for text in (get_inbox_text(*row[1:]) for row in rows) if text]
        if texts:
            wait_for_notebook(self.notebook)
            try:
                on_main(self._insert_inbox, texts)
            except NotebookOperationOngoing as e:  # the rows stay in the sheet, imported next time
                self.error("Notebook busy, the inbox sheet will be imported at the next sync", e)
                return None
        # bottom up, so that the indexes of the rows still to delete do not move; rows added meanwhile go below
        requests = [{"deleteDimension": {"range": {"sheetId": grid_id, "dimension": "ROWS",
                                                   "startIndex": index, "endIndex": index + 1}}}
                    for index, *_row in sorted(rows, reverse=True)]
        try:
            sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            self.error("Tasks imported from the inbox sheet, but not deleted from it – delete them by hand,"
                       " else they are imported again", e, fatal=True)
        return len(texts)

    def _insert_inbox(self, texts, source="inbox sheet"):
        page = self._get_page()
        self._store_to_page(insert_after_first_blank_line(page.dump("wiki"), "\n".join(texts) + "\n"), page)
        self.info(f"New tasks from the {source}: {len(texts)}, page: {page.get_title()}")

    def create_inbox(self):
        """ Create the inbox sheet and set it in the preferences. If one is set and accessible already,
            just return it. Returns its URL, None on error. """
        try:
            sheets = self.calendar_api.get_inbox_services(claim=True)
        except Exception as e:  # OAuth flow, missing/invalid client secret, network...
            self.error("Cannot connect to Google Sheets", e, fatal=True)
            return None
        sheet_id = parse_sheet_id(self.preferences["inbox_sheet"])
        if sheet_id:
            try:
                self._inbox_sheet(sheets, sheet_id)
                return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            except (httplib2.ServerNotFoundError, OSError, Error, KeyError, IndexError) as e:
                self.info(f"The inbox sheet set is not accessible, creating a new one: {e}")

        def cells(texts, bold, background):
            return {"values": [{"userEnteredValue": {"stringValue": text},
                                "userEnteredFormat": {"textFormat": {"bold": bold}, "wrapStrategy": "WRAP",
                                                      "backgroundColor": background}} for text in texts]}

        widths = (400, 180, 300)
        try:
            spreadsheet = sheets.spreadsheets().create(body={
                "properties": {"title": INBOX_TITLE},
                "sheets": [{"properties": {"title": INBOX_SHEET, "gridProperties": {"frozenRowCount": 2}},
                            "data": [{"rowData": [cells([INBOX_BANNER], True, {"red": 1, "green": .87, "blue": .6}),
                                                  cells(INBOX_HEADER, True, {"red": .9, "green": .9, "blue": .9})],
                                      "columnMetadata": [{"pixelSize": w} for w in widths]}]}]
            }).execute()
            sheet_id = spreadsheet["spreadsheetId"]
            grid_id = spreadsheet["sheets"][0]["properties"]["sheetId"]
            sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
                {"mergeCells": {"range": {"sheetId": grid_id, "startRowIndex": 0, "endRowIndex": 1,
                                          "startColumnIndex": 0, "endColumnIndex": len(widths)},
                                "mergeType": "MERGE_ALL"}},
                # editing the banner or header is possible, only after a warning
                {"addProtectedRange": {"protectedRange": {
                    "range": {"sheetId": grid_id, "startRowIndex": 0, "endRowIndex": INBOX_HEADER_ROWS},
                    "description": "Managed by the Google Tasks plugin for Zim", "warningOnly": True}}},
            ]}).execute()
        except (httplib2.ServerNotFoundError, OSError, Error, KeyError, IndexError) as e:
            self.error("Cannot create the inbox sheet", e, fatal=True)
            return None
        on_main(self._set_preference, "inbox_sheet", spreadsheet["spreadsheetUrl"])
        self.info(f"Inbox sheet created: {spreadsheet['spreadsheetUrl']}")
        return spreadsheet["spreadsheetUrl"]

    def _set_preference(self, name, value):
        """ Set the notebook property and save them – Zim itself writes notebook.zim only from its Properties
            dialog, the inbox sheet/calendar would be forgotten at the next start. """
        self.preferences[name] = value
        self.notebook.config.write()

    # --- Inbox calendar: events of a calendar the plugin created are imported as tasks and deleted from it ---

    def import_inbox_calendar(self):
        """ Import the events of the inbox calendar (the `inbox_calendar` preference) as tasks, all of them,
            the future ones too, and delete them from the calendar. As plain Zim tasks (see get_inbox_text),
            or as Google Tasks of the list in `inbox_calendar_tasklist`. Does nothing if no calendar is set.
            Returns the number of events imported (None on error). """
        calendar_id = (self.preferences["inbox_calendar"] or "").strip()
        if not calendar_id:
            return 0
        try:
            calendar = self.calendar_api.get_inbox_calendar_service()
            events, token = [], None
            while True:
                response = calendar.events().list(calendarId=calendar_id, pageToken=token,
                                                  maxResults=2500).execute()
                events += response.get("items", [])
                token = response.get("nextPageToken")
                if not token:
                    break
        except LookupError as e:
            self.error(str(e))
            return None
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            self.error(INBOX_CALENDAR_MISSING, e)
            return None
        if not events:
            return 0
        tasklist = (self.preferences["inbox_calendar_tasklist"] or "").strip()
        if tasklist:
            texts, done, task_ids = self._inbox_events_to_tasks(events, tasklist)
        else:
            texts = [text for text in (get_inbox_text(*get_event_task(e)) for e in events) if text]
            done, task_ids = events, []
        if texts:
            wait_for_notebook(self.notebook)
            try:
                on_main(self._insert_inbox, texts, "inbox calendar")
            except NotebookOperationOngoing as e:
                if not task_ids:  # the events stay in the calendar, imported next time
                    self.error("Notebook busy, the inbox calendar will be imported at the next sync", e)
                    return None
                # the Google Tasks exist already, the events go; the tasks come with the next import
                self.error("Notebook busy, the tasks from the inbox calendar will come with the next import", e)
                task_ids = []
        if task_ids:  # imported, the import of the task list must not bring them again (see fetch)
            self.cache.load()
            self.cache.undated_ids |= set(task_ids)
            self.cache.save()
        self._delete_inbox_events(calendar, calendar_id, done)
        return len(done)

    def _inbox_events_to_tasks(self, events, tasklist):
        """ Make a Google Task of each event in the task list. Stops at the first failure, the events left
            are imported next time. Returns (texts of the tasks for the page, events done, task IDs). """
        texts, done, task_ids = [], [], []
        try:
            list_id = self.list_id(tasklist)
            service = self.calendar_api.get_service(write_access=True, claim=False)
            for event in events:
                text, due, note = get_event_task(event)
                title, notes = " ".join(text.split()), note
                if not title:
                    title, notes = " ".join(note.split()), ""
                if title:
                    body = {"title": title, "notes": notes} if notes else {"title": title}
                    if due:
                        body["due"] = f"{due}T00:00:00.000Z"
                    task = service.tasks().insert(tasklist=list_id, body=body).execute()
                    texts.append(self.get_task_text(task, self.preferences["include_start_date"], list_id,
                                                    self.list_icon(list_id)))
                    task_ids.append(task["id"])
                done.append(event)  # (a blank one is just deleted)
        except LookupError as e:
            self.error("Cannot make Google Tasks of the inbox calendar", e)
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            self.error("Error in communication while making Google Tasks of the inbox calendar", e)
        return texts, done, task_ids

    def _delete_inbox_events(self, calendar, calendar_id, events):
        failed = []

        def deleted(_request_id, _response, exc):
            if exc and not (isinstance(exc, HttpError) and exc.resp.status in (404, 410)):  # (gone already)
                failed.append(exc)

        try:
            for i in range(0, len(events), INBOX_CALENDAR_BATCH):
                batch = calendar.new_batch_http_request(callback=deleted)
                for event in events[i:i + INBOX_CALENDAR_BATCH]:
                    batch.add(calendar.events().delete(calendarId=calendar_id, eventId=event["id"]))
                batch.execute()
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            failed.append(e)
        if failed:
            self.error(f"Tasks imported from the inbox calendar, but {len(failed)} not deleted from it – delete"
                       " them by hand, else they are imported again", failed[0], fatal=True)

    def create_inbox_calendar(self):
        """ Create the inbox calendar and set it in the preferences. If one is set and accessible already,
            just return it. Returns its ID, None on error. """
        try:
            calendar = self.calendar_api.get_inbox_calendar_service(claim=True)
        except Exception as e:  # OAuth flow, missing/invalid client secret, network...
            self.error("Cannot connect to Google Calendar", e, fatal=True)
            return None
        calendar_id = (self.preferences["inbox_calendar"] or "").strip()
        if calendar_id:
            try:
                calendar.calendars().get(calendarId=calendar_id).execute()
                return calendar_id
            except (httplib2.ServerNotFoundError, OSError, Error) as e:
                self.info(f"The inbox calendar set is not accessible, creating a new one: {e}")
        try:
            calendar_id = calendar.calendars().insert(body={"summary": INBOX_CALENDAR_TITLE,
                                                            "description": INBOX_CALENDAR_DESCRIPTION}).execute()["id"]
        except (httplib2.ServerNotFoundError, OSError, Error, KeyError) as e:
            self.error("Cannot create the inbox calendar", e, fatal=True)
            return None
        on_main(self._set_preference, "inbox_calendar", calendar_id)
        self.info(f"Inbox calendar created: {calendar_id}")
        return calendar_id

    # --- (end of inbox calendar) ---

    def _store_to_page(self, contents, page):
        """

        :param contents: [] lines of the page
        :param page: ZimPage

        page.parse() refills the whole textbuffer, leaving the caret at the end of the page. If the page
        is the one being displayed, we put the caret back onto its line (see restore_cursor_anchor).
        (The names used to be compared by `is`, which is False for two equal strings that are
        not the same object, so the caret was never restored and jumped to the end.)
        """
        operation = ongoing_operation(self.notebook)
        if operation:  # store_page() would be refused after the page had been changed already – and autosaved later
            raise NotebookOperationOngoing(operation.message)
        pageview = anchor = None
        if self.window and self.window.pageview.page and self.window.pageview.page.name == page.name:
            pageview = self.window.pageview
            anchor = get_cursor_anchor(pageview)
        page.parse('wiki', "".join(contents))
        self.notebook.store_page(page)
        if anchor:
            restore_cursor_anchor(pageview, anchor)

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

        # the page is read (and written) in the main thread, the server is asked here, in between
        tasks = []
        for line in on_main(lambda: self._get_page().dump("wiki")):
            match = taskAnchorTreeRe.match(line)
            if match:
                tasks.append((*parse_task_href(match[2]), match[4]))

        unidentified_tasks = []
        for list_id, task_id, task_text in tasks:
            if task_id in cache:
                continue
            # perform single search, this task was not fetched within the cache
            try:
                task = service.tasks().get(task=task_id, tasklist=list_id or self.primary_list_id).execute()
            except Error as e:
                logger.warning("Task %s not found on the server: %s", task_id, e)
                unidentified_tasks.append(task_text)
                cache[task_id] = None  # not asked again for the same task on the page
            except (LookupError, httplib2.ServerNotFoundError, OSError) as e:  # OSError: timeout, no network
                self.error("Cannot look up task status", e)
                break
            else:
                cache[task_id] = task["status"] == "completed"
        on_main(self._set_bullets, cache)
        if unidentified_tasks:
            self.info(f"Cannot identify {len(unidentified_tasks)} tasks: " + ", ".join(unidentified_tasks))

    def _set_bullets(self, statuses):
        """ Un/check the tasks on the page by {task_id: completed} (None = unknown, left as is).
            The page is read again, it may have changed while the server was asked. """
        page = self._get_page()
        contents = []
        for line in page.dump("wiki"):
            match = taskAnchorTreeRe.match(line)
            if match:
                _list_id, task_id = parse_task_href(match[2])
                completed = statuses.get(task_id)
                if completed is not None:  # we know the current status, replace the line
                    # strip "[.] " (a dot may be "*", "x", " ") from the beginning
                    task_s_without_bullet = match[0][(len(match[1]) if match[1] else 0):]
                    # put "[*]" or "[ ]" to the beginning
                    line = "[{0}] {1}".format(("*" if completed else " "), task_s_without_bullet) + "\n"
            contents.append(line)
        self._store_to_page(contents, page)

    def refresh_task_lists(self):
        self.cache.load()
        service = self.calendar_api.get_service(info="Refreshing task lists")
        try:
            results = service.tasklists().list().execute()
            default_id = service.tasklists().get(tasklist=DEFAULT_LIST).execute()["id"]
        except (httplib2.ServerNotFoundError, OSError, Error) as e:
            self.error("Cannot refresh task lists", e)
            return False
        items = results.get('items', [])
        if not items:
            self.info('No task lists found.')
            return False

        # the default list first, chosen_lists() relies on that to put a title to '@default';
        # replaced at once, not cleared and refilled: the menu may be reading it from the main thread
        self.cache.lists = {tasklist["title"]: tasklist["id"]
                            for tasklist in sorted(items, key=lambda tl: tl["id"] != default_id)}
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
        self.undated_ids = set()
        "IDs of the imported tasks without a due date, kept until they get one (see fetch)"
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
            data = data or {"items_ids": set(), "undated_ids": set(), "lists": {}, "icons": {}, "last_fetched": 0}
            self.__dict__.update(data)
            self._mtime = mtime
            if migrated:
                self.save()
        return self

    @staticmethod
    def parse(text):
        """ Cache file content -> ({items_ids, lists, last_fetched}, migrated) or (None, False) if unusable.

        Current format: {"version": 1, "items_ids": [...], "undated_ids": [...], "lists": {...}, "icons": {...},
         "last_fetched": float}. (`undated_ids` and `icons` were added later, a file without them is fine.)
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
        undated = raw.get("undated_ids", [])
        if not isinstance(undated, list):
            undated = []
        last_fetched = raw.get("last_fetched", raw.get("date_at", 0))
        if not isinstance(ids, list) or not isinstance(lists, dict) or \
                not isinstance(last_fetched, (int, float)) or isinstance(last_fetched, bool):
            return None, False
        return {"items_ids": {i for i in ids if isinstance(i, str)},
                "undated_ids": {i for i in undated if isinstance(i, str)},
                "lists": {k: v for k, v in lists.items() if isinstance(k, str) and isinstance(v, str)},
                "icons": {k: v for k, v in icons.items() if isinstance(k, str) and isinstance(v, str)},
                "last_fetched": last_fetched}, migrated

    def save(self):
        data = {"version": CACHE_SCHEMA_VERSION,
                "items_ids": sorted(self.items_ids),
                "undated_ids": sorted(self.undated_ids),
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

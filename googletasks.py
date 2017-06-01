#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import print_function
import httplib2
import os
import logging
import sys
import datetime
import pickle    
import dateutil.parser
import re
import gtk
from collections import defaultdict

from zim.actions import action
from zim.config import XDG_DATA_HOME
from zim.formats.wiki import Parser
from zim.notebook import Path
from zim.main import ZIM_APPLICATION, NotebookCommand
from zim.main.command import GtkCommand
from zim.plugins import PluginClass, WindowExtension, extends
from zim.gui.widgets import Dialog, InputEntry, PageEntry

from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage
try:
    import ipdb
except ImportError:
    pass

logger = logging.getLogger('zim.plugins.googletasks')
WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CACHEFILE = WORKDIR + "/googletasks.cache"
CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
APPLICATION_NAME = 'googletasks2zim'
TASKANCHOR_SYMBOL = u"\u270b"
#linkIconRe = re.compile('(.*)\[\[([^|]*)\|'+LINKICON+'\]\](.*)')
taskAnchorRe = re.compile(' {} '.format(TASKANCHOR_SYMBOL.encode("utf-8")))

# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(Googletasks2zimPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")


class GoogletasksPlugin(PluginClass):
    
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks in the middle of your Home file. At first run, it appends today's tasks; next, it'll append new tasks added since last synchronisation. Every task should be imported only once, unless it's changed.

See https://github.com/e3rd/zim-plugin-googletasks for more info.
(V1.0)
'''),
        'author': "Edvard Rejthar",    	  
    }          
        
    #plugin_preferences = ( I dont want to use that until I can access it from command line. (self.plugin doesnt exist there)
    #    # T: label for plugin preferences dialog
    #    ('page', 'string', _('What page should be used to be updated by plugin? If not set, homepage is used'), ""),
    #    )
     

class GoogletasksCommand(NotebookCommand, GtkCommand): #6 it used to be NotebookCommand... I dont know where is server proxy now
    '''Class to handle "zim --plugin googletasks" '''    
    arguments = ('[NOTEBOOK]',)
    
    def hej(self):
        print("ZDEE")
        with open("/tmp/text","w") as f:
            f.write("joo5")
  
    def run(self):                   
        for window in ZIM_APPLICATION._windows:
            if window.ui.notebook.uri == self.get_notebook_argument()[0].uri:
                notebook=window.ui.notebook;
                ui=window.ui;
                break;
        else:
            ui = None            
	
	reload = (lambda: ui.reload_page()) if ui else lambda: None # function exists only if zim is open
	ntb, _ = self.build_notebook()
	reload() # save current user's work, if zim's open
	GoogletasksController(notebook = ntb).fetch() # add new lines
	reload() # save new lines            

class GoogleCalendarApi(object):
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')
    
    @staticmethod
    def serviceObtainable(write_access = False):
        if write_access:
            return os.path.isfile(GoogleCalendarApi.permission_write_file)
        else:
            return os.path.isfile(GoogleCalendarApi.permission_read_file)
    
    def getService(self, write_access = False):        
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
            flow.user_agent = APPLICATION_NAME # Googletasks2zimPlugin.plugin_info["name"]                         
            argv = sys.argv 
            sys.argv = sys.argv[:1] # tools.run_flow unfortunately parses arguments and would die from any zim args
            credentials = tools.run_flow(flow, store)
            self.info('Storing credentials to ' + self.credential_path)
            sys.argv = argv 
        return credentials



def monkeypatch_method(cls):
    """ decorator used for extend features of a method"""
    def decorator(func):
        setattr(cls, func.__name__+"_original", getattr(cls, func.__name__))
        setattr(cls, func.__name__, func)       
        return func
    return decorator

from zim.gui.pageview import TextBuffer
from zim.formats import CHECKED_BOX, UNCHECKED_BOX


        

@extends('MainWindow')
class GoogletasksWindow(WindowExtension):
    
    s = ""
    if not GoogleCalendarApi.serviceObtainable():
        s += "<menuitem action='permission_readonly'/>" 
    if not GoogleCalendarApi.serviceObtainable(write_access = True):
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
    
    @action(_('Google Tasks')) # T: menu item
    def googletasks_menu(self):
        pass
        
    def __init__(self, *args, **kwargs):                
        WindowExtension.__init__(self,*args, **kwargs) #super(WindowExtension, self).__init__(*args, **kwargs)        
        controller = self.controller = GoogletasksController(window = self.window)
                
        @monkeypatch_method(TextBuffer)    
        def set_bullet(self, row, bullet): 
            """ overriding of pageview.py/TextBuffer/set_bullet """
            if bullet in [CHECKED_BOX, UNCHECKED_BOX]:
                taskid = controller.getTaskId(row)                
                if taskid:
                    controller.task_checked(taskid, bullet)
            self.set_bullet_original(row, bullet)         
    
        
                    
    @action(_('_Task from cursor or selection...'), accelerator='<ctrl><alt><shift>g') # T: menu item
    def send_as_task(self):
        """ cut current text and call send to tasks dialog """
        buffer = self.window.pageview.view.get_buffer()
        
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
                    if not s or not s.strip() or TASKANCHOR_SYMBOL.encode("utf-8") in s: # not s.startswith("\t") 
                        lineI -= 1
                        break                
            
            enditer = buffer.get_iter_at_line(lineI)
            enditer.forward_to_line_end()
            buffer.select_range(startiter, enditer)
            
        if buffer.get_selection_bounds(): # a task is selected
            task = self.controller.readTaskFromSelection()
            self.add_new_task(task = task)                    
        
    @action(_('_Add new task...')) # T: menu item
    def add_new_task(self, task = {}):
        self.gui = GoogletasksNewtaskDialog(self.window.ui, _('Search'),  defaultwindowsize=(300, -1))
        self.gui.setController(self.controller)
        self.gui.resize(300, 100) # reset size
        
        self.labelObject = gtk.Label(('Update Google task') if task.get("id","") else ('Create Google tasks'))
        self.labelObject.set_usize(300, -1)        
        self.gui.vbox.pack_start(self.labelObject, False)                
        
        self.controller.inputTitle = InputEntry(allow_empty=False, placeholder_text="task title")
        self.controller.inputNotes = gtk.TextView()
        self.gui.vbox.pack_start(self.controller.inputTitle, False)
        self.gui.vbox.pack_start(self.controller.inputNotes, False)

        self.gui.task = task
        
        if "title" in task:
            self.controller.inputTitle.set_text(task["title"])
            
        if "notes" in task:
            self.controller.inputNotes.get_buffer().set_text(task["notes"])
            
        if "due" in task:
            logger.error("Not yet implemented") ##
        
        self.gui.show_all()            
    
    @action(_('_Claim read only access')) # T: menu item
    def permission_readonly(self):        
        GoogleCalendarApi().getService()
    
    @action(_('_Claim write access')) # T: menu item
    def permission_write(self):        
        GoogleCalendarApi().getService(write_access = True)       

    @action(_('_Import new tasks'), accelerator='<ctrl><alt>g') # T: menu item
    def import_tasks(self):                
        self.controller.fetch()            

class GoogletasksNewtaskDialog(Dialog):
    def _loadTask(self):
        try:
            o = self.controller.inputNotes.get_buffer()
            self.task["notes"] = o.get_text(*o.get_bounds())
            self.task["title"] = self.controller.inputTitle.get_text()
        except:
            pass
            
    def do_response(self, id):
        """ we cant use parent function because Esc is not handled as cancel """
        if id == gtk.RESPONSE_OK:
            self.do_response_ok()
        else:
            self.do_response_cancel()        

    def do_response_ok(self):        
        self._loadTask()
        self.destroy() # immediately close (so that we wont hit Ok twice)        
        if not self.controller.submit_task(task = self.task):            
            self.do_response_cancel()
        return True

    def do_response_cancel(self):
        """ something failed, restore original text in the zim-page """                
        text = self.controller.getTaskText(self.task)
        buffer = self.controller.window.pageview.view.get_buffer()
        buffer.insert_parsetree_at_cursor(Parser().parse(text))        
        self.destroy()

    def setController(self,controller):
        self.controller = controller

class GoogletasksController(object):
  
    def __init__(self, window = None, notebook = None):      
        self.window = window
        self.notebook = notebook
        self.preferences = GoogletasksPlugin.plugin_preferences
        if not self.notebook and self.window:
                self.notebook = self.window.ui.notebook        
        #self.page = notebook.get_page(Path(self.preferences["page"])) if self.preferences["page"] else self.notebook.get_home_page()      
        self.page = self.notebook.get_home_page()
        logger.debug("Google tasks page: {} ".format(self.page))        
        
    def task_checked(self, taskid, bullet):
        service = GoogleCalendarApi().getService(write_access = True)
        task = {"status": "completed" if bullet is CHECKED_BOX else "needsAction"}        
        try:
            if bullet is CHECKED_BOX: # small patch will be sufficient
                result = service.tasks().patch(tasklist='@default', task = taskid, body=task).execute()
            else: # we need to delete the automatically generated completed field, need to do a whole update
                task = service.tasks().get(tasklist='@default', task = taskid).execute()
                del task["completed"]
                task["status"] = "needsAction"
                result = service.tasks().update(tasklist='@default', task = taskid, body=task).execute()
        except:                        
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))            
            return False
        self.info('Marked as {}'.format(task["status"]))
        return True        
    
    def submit_task(self, task = {}):
        if "due" not in task:
            task["due"] = self.getTime(addDays = 1, morning = True)
        
        service = GoogleCalendarApi().getService(write_access = True)

        try:
            if "id" in task:                                
                result = service.tasks().patch(tasklist='@default', task = task["id"], body=task).execute()
                self.info("Task '{}' updated.".format(task["title"]))                                
            else:
                result = service.tasks().insert(tasklist='@default', body=task).execute()
                self.info("Task '{}' created.".format(task["title"]))
        except:                        
            self.info('Error in communication with Google Tasks: {}'.format(sys.exc_info()[1]))            
            return False
        return True        
        
    def getTime(self, addDays = 0, now = False, dateonly = False, morning = False, midnight = False, lastsec = False):
        dtnow = datetime.datetime.now()        
        if now:
            return dtnow
        if addDays:
            dtnow += datetime.timedelta(addDays, 0)
        if dateonly:
            return dtnow.isoformat()[:11]
        if morning:
            return dtnow.isoformat()[:11]+"08:00:00.000Z"
        if midnight:
            return dtnow.isoformat()[:11]+"23:59:59.999Z"
        if lastsec:            
            return dtnow.isoformat()[:11]+"23:59:59.999Z"
        return dtnow.isoformat()

        
    def info(self, text):
        logger.info(text)
        if self.window:
            self.window.statusbar.push(0, text)
    
    

    def _get_new_items(self):        
        service = GoogleCalendarApi().getService()
                
        if os.path.isfile(CACHEFILE):
            with open(CACHEFILE, "rb") as f:
                self.recentItemIds = pickle.load(f)                
            dueMin = datetime.datetime.fromtimestamp(os.path.getmtime(CACHEFILE)).isoformat()[:11]
        else:
            self.recentItemIds = set()
            dueMin = self.getTime(dateonly = True)        
        
        results = service.tasks().list(maxResults=10, 
                                    tasklist = "@default",
                                    showCompleted = False,
                                    dueMin = dueMin + "00:00:00.000Z",
                                    dueMax = self.getTime(lastsec = True)).execute()
        items = results.get('items', [])
        if not items:
            self.info('No task lists found.')
            return
        else:
            text = ""
            logger.info('Task lists added.')            
            for item in items:
                if item["etag"] in self.recentItemIds:		 
                    if dateutil.parser.parse(item["due"]).date() >= self.getTime(now = True).date():                        
                        self.itemIds.add(item["etag"])
                    logger.debug('Skipping {}.'.format(item['title']))                    
                    continue #XXX
                self.itemIds.add(item["etag"])
                print(item)
                text += self.getTaskText(item) + "\n"
            #if text.strip() != "":
            #    text += "\n"
        return text

    def getTaskText(self, task):
        s = "[ ] "
        if task.get("id",""):
            s += "[[{}|{}]] ".format(task["id"], TASKANCHOR_SYMBOL)
        s+=task['title']
        if task.get("notes",""):
            s += "\n\t" + task['notes']
        #s+="\n"
        return s
    
    def readline(self, lineI): 
        """ this crazy construct just reads a line in page """
        buffer = self.window.pageview.view.get_buffer()
        textiter = buffer.get_iter_at_line(lineI)
        start = buffer.get_iter_at_offset(textiter.get_offset())
        if textiter.forward_to_line_end():
            end = buffer.get_iter_at_offset(textiter.get_offset())
        else:
            end = start                
        return buffer.get_slice(start,end)
    
    def getTaskId(self, lineI):
        buffer = self.window.pageview.view.get_buffer()
        task_anchor_pos = self.readline(lineI).find(TASKANCHOR_SYMBOL.encode("utf-8"))
        if task_anchor_pos > -1:
            offset = task_anchor_pos - 2 + buffer.get_iter_at_line(lineI).get_offset() # the -2 is because of a charset mystery
            linkdata = buffer.get_link_data(buffer.get_iter_at_offset(offset))            
            if linkdata:
                return linkdata["href"]
        return None

    def readTaskFromSelection(self):
        buffer = self.window.pageview.view.get_buffer()
        task = {}

        lineI = buffer.get_iter_at_offset(min([x.get_offset() for x in buffer.get_selection_bounds()])).get_line() # first line in selection
        task["id"] = self.getTaskId(lineI)

        text = buffer.get_text(*buffer.get_selection_bounds())        
        text = taskAnchorRe.sub("", text).split("\n", 1)
        task["title"] = text[0]
        task["notes"]="".join(text[1:])
        
        buffer.delete(*buffer.get_selection_bounds()) # cuts the task
        return task
    
                
    
    def fetch(self):
        self.itemIds = set()        
        
        text = self._get_new_items()
        if not text:
            return                    
            
        # Insert tasks string into page                                            
        counter = 1
        contents = []                   
        for line in self.page.dump("wiki"):
            contents.append(line)
            if line.strip() == "": # ignore first empty line
                counter -= 1
            if counter == 0:
                contents.append(text)
                counter = -1        
        bounds = None
        if self.window and self.window.pageview.get_page().name is self.page.name:
            # HP is current page - we use GTK buffers, caret stays at position            
            buffer = self.window.pageview.view.get_buffer()
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

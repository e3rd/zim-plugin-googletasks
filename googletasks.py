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
from collections import defaultdict

from zim.actions import action
from zim.plugins import PluginClass
from zim.plugins import WindowExtension
from zim.plugins import extends
from zim.config import XDG_DATA_HOME
from zim.notebook import Path
from zim.main import NotebookCommand
from zim.ipc import start_server_if_not_running, ServerProxy

from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

try:
    import ipdb
except ImportError:
    pass


import gtk
from zim.gui.widgets import Dialog
from zim.gui.widgets import InputEntry, PageEntry
import re


logger = logging.getLogger('zim.plugins.googletasks')


WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CACHEFILE = WORKDIR + "/googletasks.cache"
CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
APPLICATION_NAME = 'googletasks2zim'
LINKICON = u"\u270b"
linkIconRe = re.compile('(.*)\[\[([^|]*)\|'+LINKICON+'\]\](.*)')

# initial check
if not os.path.isfile(CLIENT_SECRET_FILE):
    quit(Googletasks2zimPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")




class GoogletasksPlugin(PluginClass):
    
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks in the middle of your Home file. At first run, it appends today's tasks; next, it'll append new tasks added since last synchronisation. Every task should be imported only once, unless it's changed.
(V0.9)
'''),
        'author': "Edvard Rejthar",    	  
    }          
        
    #plugin_preferences = ( I dont want to use that until I can access it from command line. (self.plugin doesnt exist there)
    #    # T: label for plugin preferences dialog
    #    ('page', 'string', _('What page should be used to be updated by plugin? If not set, homepage is used'), ""),
    #    )

        

class GoogletasksCommand(NotebookCommand):
    '''Class to handle "zim --plugin googletasks" '''    
    arguments = ('[NOTEBOOK]',)

    def run(self):                            
	start_server_if_not_running()
	server = ServerProxy()            
	ui = server.get_notebook(self.get_notebook_argument()[0].uri, False)
	reload = (lambda: ui.reload_page()) if ui else lambda: None
	ntb, _ = self.build_notebook()
	reload() # save current user's work, if zim's open
	Googletasks(notebook = ntb).fetch() # add new lines
	reload() # save new lines            

class GoogleCalendarApi(object):
    permission_write_file = os.path.join(WORKDIR, 'googletasks_oauth_write.json')
    permission_read_file = os.path.join(WORKDIR, 'googletasks_oauth.json')
    
    @staticmethod
    def serviceObtainable(write_access = False):
        #ipdb.set_trace()
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
            logger.info('Storing credentials to ' + self.credential_path)
            sys.argv = argv 
        return credentials


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
    
    @action(_('Google Tasks menu')) # T: menu item
    def googletasks_menu(self):
        pass
        
    def __init__(self, *args, **kwargs):                
        WindowExtension.__init__(self,*args, **kwargs) #super(WindowExtension, self).__init__(*args, **kwargs)        
        self.controller = Googletasks(window = self.window)
                    
    @action(_('_Task from selection...'), accelerator='<ctrl><alt><shift>g') # T: menu item    
    def send_as_task(self): # cut current text and send to tasks                
        buffer = self.window.pageview.view.get_buffer()
        
        
        # a text is selected
        if buffer.get_selection_bounds():
            #ipdb.set_trace()            
            text, selflink = self.controller.cutTask(buffer.get_text(*buffer.get_selection_bounds()))
            text = text.split("\n", 1)
            title = text[0]
            notes="".join(text[1:])             
            self.add_new_task(title = title, notes = notes, selflink = selflink)
            # XX possibility to safely cut the text, after successful posting to server. (Or it may be cut now and put back in case of an error?)
            return
            
        # XX
        logger.warning("Not yet implemented without selecting text")
        return                 
        # none text is selected - identify where the task begins and ends
        if not bounds:
            i = buffer.get_insert_iter() # XX how to get the line start of the current offset?
            bounds = [i.get_offset()] * 2                        
        #self.page.parse('wiki', "".join(contents))
        #self.notebook.store_page(self.page)
        #if bounds:            
        #    buffer.select_range(buffer.get_iter_at_offset(bounds[0]), buffer.get_iter_at_offset(bounds[1]))
                
            
        
    @action(_('_Add new task...')) # T: menu item
    def add_new_task(self, title = None, notes = None, due = None, selflink = None):        
        self.gui = GoogletasksNewtaskDialog(self.window.ui, _('Search'),  defaultwindowsize=(300, -1))
        self.gui.setController(self.controller)
        self.gui.resize(300, 100) # reset size
        
        self.labelObject = gtk.Label(('Send to Google tasks'))
        self.labelObject.set_usize(300, -1)        
        self.gui.vbox.pack_start(self.labelObject, False)                
        
        self.controller.inputTitle = InputEntry(allow_empty=False, placeholder_text="task title")
        self.controller.inputNotes = gtk.TextView()
        self.gui.vbox.pack_start(self.controller.inputTitle, False)
        self.gui.vbox.pack_start(self.controller.inputNotes, False)
        
        if selflink:
            # XXX WE ARE HERE:
            print("YESSSSSS, selflink is there!", selflink)
        
        if title:
            self.controller.inputTitle.set_text(title)
            
        if notes:
            self.controller.inputNotes.get_buffer().set_text(notes)
            
        if due:
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
        #import ipdb; ipdb.set_trace()
        self.controller.fetch()
      
      

class GoogletasksNewtaskDialog(Dialog):
    def do_response_ok(self):        
        o = self.controller.inputNotes.get_buffer()
        notes = o.get_text(*o.get_bounds())        
        title = self.controller.inputTitle.get_text()
        self.destroy() # immediately close (so that we wont hit Ok twice)        
        self.controller.add_new_task(title = title, notes = notes)
        return True
    
    def setController(self,controller):
        self.controller = controller

          

class Googletasks(object):    
  
    def __init__(self, window = None, notebook = None):      
        self.window = window
        self.notebook = notebook
        self.preferences = GoogletasksPlugin.plugin_preferences
        if not self.notebook and self.window:
                self.notebook = self.window.ui.notebook        
        #self.page = notebook.get_page(Path(self.preferences["page"])) if self.preferences["page"] else self.notebook.get_home_page()      
        self.page = self.notebook.get_home_page()
        print("settings page", self.page)
    
    def cutTask(self, text):
        """ The task selfLink previously inserted is found and cut from text. """        
        m = linkIconRe.search(text)
        #ipdb.set_trace()
        if m and len(m.groups()) == 3:            
            selflink = m.group(1)
            text = m.group(0) + m.group(2)
            return text, selflink
                    
        return text, None
    
    def add_new_task(self, title, notes = "", due = None):
        if not due:
            due = self.getTime(addDays = 1, morning = True)
        #ipdb.set_trace()
        service = GoogleCalendarApi().getService(write_access = True)        
        task = {'title': title,
            'notes': notes,
            'due': due
            }

        result = service.tasks().insert(tasklist='@default', body=task).execute()
        print(result['id'])
        
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
            logger.info('No task lists found.')
            return
        else:
            text = ""
            logger.info('Task lists added.')            
            for item in items:
	        #ipdb.set_trace()
                #print(item["title"], item["etag"], item["id"], "skipping: " + str(item["etag"] in self.recentItemIds),dateutil.parser.parse(item["due"]).date())
                if item["etag"] in self.recentItemIds:		 
                    if dateutil.parser.parse(item["due"]).date() >= self.getTime(now = True).date():                        
                        self.itemIds.add(item["etag"])
                    logger.debug('Skipping {}.'.format(item['title']))                    
                    continue #XXX
                self.itemIds.add(item["etag"])
                print(item)
                text += '[ ] [[{}|{}]] {}{}\n'.format(item["selfLink"], LINKICON, item['title'], ("\n\t" + item['notes']) if "notes" in item else "")                    
            if text.strip() != "":
                text += "\n"
        return text
    
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
            if line.strip() == "":
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
            
            
        # Save the success info
        with open(CACHEFILE, "wb") as f:
            pickle.dump(self.itemIds, f)                
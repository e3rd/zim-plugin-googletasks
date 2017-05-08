#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from __future__ import print_function
import httplib2
import os
import logging
import sys
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

import datetime

WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))
CACHEFILE = WORKDIR + "/googletasks.cache"

logger = logging.getLogger('zim.plugins.googletasks')
class GoogletasksPlugin(PluginClass):
    
    plugin_info = {
        'name': _('Google Tasks'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks in the middle of your Home file. At first run, it appends today's tasks; next, it'll append new tasks added since last synchronisation. Every task should be imported only once, unless it's changed.
(V0.8)
'''),
        'author': "Edvard Rejthar"        
    }
            

class GoogletasksCommand(NotebookCommand):
    '''Class to handle "zim --plugin trayicon" '''    
    arguments = ('[NOTEBOOK]',)

    def run(self):            
            start_server_if_not_running()
            server = ServerProxy()            
            ui = server.get_notebook(self.get_notebook_argument()[0].uri, False)
            reload = (lambda: ui.reload_page()) if ui else lambda: None
            ntb, _ = self.build_notebook()
            reload() # save current user's work, if zim's open
            Googletasks().fetch(notebook = ntb) # add new lines
            reload() # save new lines            

@extends('MainWindow')
class GoogletasksWindow(WindowExtension):

    uimanager_xml = '''
    <ui>
    <menubar name='menubar'>
            <menu action='tools_menu'>
                    <placeholder name='plugin_items'>
                            <menuitem action='update_tasks'/>
                    </placeholder>
            </menu>
    </menubar>
    </ui>
    '''


    gui = "";
        

    @action(_('_Update from Google Tasks'), accelerator='<ctrl><shift>g') # T: menu item
    def update_tasks(self):        
        #import ipdb; ipdb.set_trace()
        Googletasks().fetch(window = self.window)
        
import pickle    
import dateutil.parser

class Googletasks(object):    
 
    def _get_new_items(self):        
        credentials = self.get_credentials()
        http = credentials.authorize(httplib2.Http())
        service = discovery.build('tasks', 'v1', http=http)
        now = datetime.datetime.utcnow()
        today = now.isoformat()[:11]
        
        if os.path.isfile(CACHEFILE):
            with open(CACHEFILE, "rb") as f:
                self.recentItemIds = pickle.load(f)                
            dueMin = datetime.datetime.fromtimestamp(os.path.getmtime(CACHEFILE)).isoformat()[:11]
        else:
            self.recentItemIds = set()
            dueMin = today
        
        
        results = service.tasks().list(maxResults=10, 
                                    tasklist = "@default",
                                    showCompleted = False,
                                    dueMax = today + "23:59:59.999Z",
                                    dueMin = dueMin + "00:00:00.000Z").execute()
        items = results.get('items', [])
        if not items:
            logger.info('No task lists found.')
            return
        else:
            text = ""
            logger.info('Task lists added.')            
            for item in items:
                print(item["title"], item["etag"], "skipping: " + str(item["etag"] in self.recentItemIds),dateutil.parser.parse(item["due"]).date())
                if item["etag"] in self.recentItemIds:		 
                    if dateutil.parser.parse(item["due"]).date() >= now.date():
                        print("ADDED")
                        self.itemIds.add(item["etag"])
                    logger.debug('Skipping {}.'.format(item['title']))                    
                    continue                
                self.itemIds.add(item["etag"])
                text += '[ ] {0}{1}\n'.format(item['title'], ("\n\t" + item['notes']) if "notes" in item else "")                    
            if text.strip() != "":
                text += "\n"
        return text
    
    def fetch(self, window = None, notebook = None):
        self.itemIds = set()
        
        
        text = self._get_new_items()
        if not text:
            return
            
        if not notebook and window:
            notebook = window.ui.notebook
            
        # Insert tasks string into page        
        hp = notebook.get_home_page() # Xnotebook.get_page(Path("holuba"))
                    
        #import ipdb; ipdb.set_trace()
        counter = 1
        contents = []            
        for line in hp.dump("wiki"):
            contents.append(line)
            if line.strip() == "":
                counter -= 1
            if counter == 0:
                contents.append(text)
                counter = -1        
        bounds = None
        if window and window.pageview.get_page().name is hp.name:
            # HP is current page - we use GTK buffers, caret stays at position            
            buffer = window.pageview.view.get_buffer()                        
            bounds = [x.get_offset() for x in buffer.get_selection_bounds()]
            if not bounds:
                i = buffer.get_insert_iter()
                bounds = [i.get_offset()] * 2
            
        hp.parse('wiki', "".join(contents))
        notebook.store_page(hp)
        if bounds:            
            buffer.select_range(buffer.get_iter_at_offset(bounds[0]), buffer.get_iter_at_offset(bounds[1]))
            
            
        # Save the success info
        with open(CACHEFILE, "wb") as f:
            pickle.dump(self.itemIds, f)            

    def get_credentials(cls):
        """Gets valid user credentials from storage.

        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials.

        Returns:
            Credentials, the obtained credential.
        """                
        SCOPES = 'https://www.googleapis.com/auth/tasks.readonly' # If modifying these scopes, delete your previously saved credentials
        CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks_client_id.json')
        APPLICATION_NAME = 'googletasks2zim'

        if not os.path.isfile(CLIENT_SECRET_FILE):
            quit(Googletasks2zimPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")
            
        credential_path = os.path.join(WORKDIR, 'googletasks_oauth.json')    

        store = Storage(credential_path)
        credentials = store.get()
        if not credentials or credentials.invalid:
            flow = client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
            flow.user_agent = APPLICATION_NAME # Googletasks2zimPlugin.plugin_info["name"]                         
            argv = sys.argv 
            sys.argv = sys.argv[:1] # tools.run_flow unfortunately parses arguments and would die from any zim args
            credentials = tools.run_flow(flow, store)
            logger.info('Storing credentials to ' + credential_path)
            sys.argv = argv 
        return credentials
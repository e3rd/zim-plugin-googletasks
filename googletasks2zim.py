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
#from zim.command import Command

from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

import datetime

WORKDIR = str(XDG_DATA_HOME.subdir(('zim', 'plugins')))

logger = logging.getLogger('zim.plugins.instantsearch')
class Googletasks2zimPlugin(PluginClass):
    
    plugin_info = {
        'name': _('Google Tasks 2 Zim'),
        'description': _('''\
Connects to your default Google Tasks lists. Append today's tasks to your Home file. Every task should be imported only once.
(V0.8)
'''),
        'author': "Edvard Rejthar"        
    }
        
    #def __init__(self, config=None):
        #PluginClass.__init__(self, config)
        #self.preferences.connect('changed', self.on_preferences_changed)
        #self.on_preferences_changed(self.preferences)
        #klass = self.get_extension_class()
        #self.set_extension_class('MainWindow', klass)
        #print("HEEEEEEJ")
        #GoogletasksWindow().fetch(window = None)
    #    pass



class GoogletasksCommand(NotebookCommand):
    '''Class to handle "zim --plugin trayicon" '''    
    arguments = ('[NOTEBOOK]',)

    def run(self):
            #print("HEJ", self.arguments[0])
            import ipdb; ipdb.set_trace()
            notebook, _ = self.build_notebook()                                    
            Googletasks().fetch(notebook = notebook)


@extends('MainWindow')
class GoogletasksWindow(WindowExtension):

    uimanager_xml = '''
    <ui>
    <menubar name='menubar'>
            <menu action='tools_menu'>
                    <placeholder name='plugin_items'>
                            <menuitem action='googletasks'/>
                    </placeholder>
            </menu>
    </menubar>
    </ui>
    '''


    gui = "";
        

    @action(_('_Update from Google Tasks'), accelerator='<ctrl><shift>g') # T: menu item
    def googletasks(self):        
        #import ipdb; ipdb.set_trace()
        Googletasks().fetch(window = self.window)
        
    
class Googletasks(object):    
    def fetch(self, window = None, notebook = None):
        credentials = self.get_credentials()
        http = credentials.authorize(httplib2.Http())
        service = discovery.build('tasks', 'v1', http=http)
        
        now = datetime.datetime.utcnow().isoformat()[:11]     
        results = service.tasks().list(maxResults=10, 
                                    tasklist = "@default",
                                    showCompleted = False,
                                    dueMax = now + "23:59:59.999Z",
                                    dueMin = now + "00:00:00.000Z").execute()
        items = results.get('items', [])
        if not items:
            logger.info('No task lists found.')
            return
        else:
            text = ""
            logger.info('Task lists added.')
            for item in items:
                text += '[ ] {0}{1}\n'.format(item['title'], ("\n\t" + item['notes']) if "notes" in item else "")                    
            text += "\n"
            
        if not notebook and window:
            notebook = window.ui.notebook
            
        # Insert tasks string into page        
        hp = notebook.get_page(Path("holuba")) #notebook.get_home_page()
        if window and window.pageview.get_page().name is hp.name: # HP is current page - we use GTK buffers, caret stays at position            
            buffer = window.pageview.view.get_buffer()                        
            #bounds = buffer.get_selection_bounds()
            #if not bounds:
                #i = buffer.get_insert_iter().get_offset()
                #bounds = (i,i,)                        
            #buffer.select_range(buffer.get_iter_at_offset(100),buffer.get_iter_at_offset(200))
            
            counter = 1
            offset = 0
            for i in range(buffer.get_line_count()):            
                start, end = buffer.get_line_bounds(i)
                line = buffer.get_text(start, end)
                if line.strip() == "":
                        counter -= 1
                if counter == 0:
                    offset = end
                    break            
            
            if offset == 0:            
                offset = buffer.get_end_iter()        
                text = "\n" + text        
            cursor = buffer.get_iter_at_offset(offset.get_offset())        
            buffer.insert(cursor, text)
        else: # HP is not current page - we change the file directly
            counter = 1
            contents = []            
            for line in hp.dump("wiki"): #hp.source.readlines():
                contents.append(line)
                if line.strip() == "":
                    counter -= 1
                if counter == 0:
                    contents.append(text)
                    counter = -1        
            hp.parse('wiki', "".join(contents))
            #hp.source.writelines(contents)            
        #import ipdb; ipdb.set_trace()
            #reload_page
            #hp.parse("wiki"
            #page.parse('wiki', text
        notebook.store_page(hp)
        print("STORING PAGE")
        #hp.set_ui_object(None) # refresh zim cache
        #hp.set_parsetree(None)
        return



    def get_credentials(cls):
        """Gets valid user credentials from storage.

        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials.

        Returns:
            Credentials, the obtained credential.
        """                
        SCOPES = 'https://www.googleapis.com/auth/tasks.readonly' # If modifying these scopes, delete your previously saved credentials
        CLIENT_SECRET_FILE = os.path.join(WORKDIR, 'googletasks2zim_client_id.json')
        APPLICATION_NAME = 'googletasks2zim'  
        #import ipdb; ipdb.set_trace()        

        if not os.path.isfile(CLIENT_SECRET_FILE):            
            quit(Googletasks2zimPlugin.plugin_info["name"] + "CLIENT_SECRET_FILE not found")            
                    
            
        credential_path = os.path.join(WORKDIR, 'googletasks2zim.json')    

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
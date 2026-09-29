"""Test setup: stub everything that needs GTK, Zim or the network, then import googletasks.py.

googletasks.py imports Zim, GTK and the Google libraries at module level, none of which are
available (or wanted) in CI. `load_plugin_module()` fills sys.modules with minimal stand-ins first.
"""
import builtins
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _Folder:
    def __init__(self, path):
        self.path = path

    def __str__(self):
        return str(self.path)

    def folder(self, _parts):
        self.path.mkdir(parents=True, exist_ok=True)
        return self


class _Anything:
    """ Stand-in for any Zim/GTK class we only need to subclass or reference. """

    def __init__(self, *args, **kwargs):
        pass


def load_plugin_module(tmp_path, monkeypatch):
    """ Import googletasks.py against stubbed dependencies and return the module. """
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")
    monkeypatch.setenv("GOOGLETASKS_CLIENT_SECRET_FILE", str(secret))
    monkeypatch.setattr(builtins, "_", lambda s: s, raising=False)  # Zim installs gettext as `_`

    gi_repository = _module("gi.repository", Gtk=types.SimpleNamespace())
    _module("gi", repository=gi_repository)

    class GoogleError(Exception):
        pass

    _module("httplib2", Http=_Anything, ServerNotFoundError=type("ServerNotFoundError", (Exception,), {}))
    _module("googleapiclient")
    _module("googleapiclient.discovery", build=None)
    _module("googleapiclient.errors", Error=GoogleError)
    _module("oauth2client", client=None, tools=None)
    _module("oauth2client.file", Storage=_Anything)

    _module("zim")
    _module("zim.actions", action=lambda *a, **k: (lambda f: f), get_gtk_actiongroup=None, ActionClassMethod=_Anything)
    _module("zim.config", XDG_DATA_HOME=_Folder(tmp_path / "data"), XDG_CONFIG_HOME=_Folder(tmp_path / "config"),
            ConfigManager=_Anything)
    _module("zim.formats", get_dumper=None, CHECKED_BOX="checked", UNCHECKED_BOX="unchecked")
    _module("zim.formats.wiki", Parser=_Anything)
    _module("zim.gui")
    _module("zim.gui.widgets", Dialog=_Anything, InputEntry=_Anything, ErrorDialog=_Anything)
    _module("zim.gui.pageview", TextBuffer=type("TextBuffer", (), {"set_bullet": lambda *a, **k: None}))
    _module("zim.gui.mainwindow", MainWindow=_Anything, MainWindowExtension=_Anything)
    _module("zim.main", NotebookCommand=type("NotebookCommand", (), {}))
    _module("zim.main.command", GtkCommand=type("GtkCommand", (), {}))
    _module("zim.notebook", build_notebook=None, Path=_Anything)
    _module("zim.notebook.page", Page=type("Page", (), {"reload_textbuffer": lambda self: None}))
    _module("zim.plugins", PluginClass=_Anything)
    import zim_page_content  # from _lib/, see pyproject.toml pythonpath
    _module("zim.plugins.zim_page_content",
            insert_after_first_blank_line=zim_page_content.insert_after_first_blank_line)

    spec = importlib.util.spec_from_file_location("googletasks", ROOT / "googletasks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gt(tmp_path, monkeypatch):
    """ The googletasks module, imported with stubbed dependencies. """
    saved = dict(sys.modules)
    try:
        yield load_plugin_module(tmp_path, monkeypatch)
    finally:
        for name in set(sys.modules) - set(saved):
            del sys.modules[name]
        sys.modules.update(saved)

import pytest


@pytest.mark.parametrize("startup_check", [True, False])
def test_startup_import_follows_the_notebook_property(gt, monkeypatch, startup_check):
    """ Not the plugin-wide preference, which is only the default of the property. """
    imports = []

    class Controller:
        def __init__(self, window, preferences):
            self.preferences = preferences

        def import_and_sync(self):
            imports.append(True)

    class Plugin:
        preferences = {"startup_check": not startup_check}

        def notebook_properties(self, notebook):
            return {"startup_check": startup_check}

    monkeypatch.setattr(gt, "GoogletasksController", Controller)
    monkeypatch.setattr(gt.MainWindowExtension, "__init__",
                        lambda self, plugin, window: setattr(self, "plugin", plugin))
    gt.GoogletasksWindow(Plugin(), type("Window", (), {"notebook": None})())
    assert imports == [True] * startup_check

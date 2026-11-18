"""Working offline: no connection is not an error worth a dialog. It sets the offline state (in the window title),
a job without network does not ask Google at all, and one that finds no connection stops at the first request."""
import errno
import socket

import pytest

from test_background import Window
from test_fetch import FakeCache, item, make_controller as make_fetching


def make(gt, window=None):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = window
    controller.worker = None
    controller.failed = False
    return controller


@pytest.fixture
def dialogs(gt, monkeypatch):
    shown = []

    class Dialog:
        def __init__(self, parent, error):
            self.error = error

        def run(self):
            shown.append(self.error)

    monkeypatch.setattr(gt, "ErrorDialog", Dialog)
    return shown


def test_offline_errors_are_told_from_the_others(gt):
    assert gt.is_offline_error(gt.httplib2.ServerNotFoundError("Unable to find the server"))
    assert gt.is_offline_error(TimeoutError("timed out"))
    assert gt.is_offline_error(ConnectionRefusedError())
    assert gt.is_offline_error(socket.gaierror(-3, "Temporary failure in name resolution"))
    assert gt.is_offline_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    wrapped = LookupError()
    wrapped.__cause__ = TimeoutError()
    assert gt.is_offline_error(wrapped)
    assert not gt.is_offline_error(FileNotFoundError(errno.ENOENT, "client secret"))
    assert not gt.is_offline_error(gt.Error("403"))
    assert not gt.is_offline_error(None)


def test_headless_offline_is_quiet_and_not_a_failure(gt, capsys):
    """ cron: exit code 0 and nothing on stderr (cron would mail it) """
    c = make(gt)
    c.error("Cannot read the task list", gt.httplib2.ServerNotFoundError("Unable to find the server"))
    assert not c.failed
    assert c.offline and c.offline_errors == 1
    assert capsys.readouterr().err == ""


def test_gui_offline_opens_no_dialog_unless_the_user_just_acted(gt, dialogs):
    c = make(gt, object())
    c.error("Cannot read the task list", TimeoutError("timed out"), fatal=True)
    assert dialogs == []
    c.error("Cannot un/check the task", TimeoutError("timed out"), quiet_offline=False)
    assert dialogs == [("Google Tasks: Cannot un/check the task", "timed out")]


def test_no_second_dialog_while_one_is_open(gt, monkeypatch):
    shown = []
    c = make(gt, object())

    class Dialog:
        def __init__(self, parent, error):
            self.error = error

        def run(self):  # its own loop: another job reports meanwhile
            shown.append(self.error)
            c.error("Second")

    monkeypatch.setattr(gt, "ErrorDialog", Dialog)
    c.error("First")
    c.error("Third")
    assert [e[0] for e in shown] == ["Google Tasks: First", "Google Tasks: Third"]


def test_offline_import_stops_at_the_first_list(gt, monkeypatch):
    cache = FakeCache()
    c = make_fetching(gt, monkeypatch, None, cache, tasklist="My Tasks, Work",
                      lists={"My Tasks": "D", "Work": "W"}, pages={"D": [[item("a")]], "W": [[item("b")]]})
    c.worker = None

    def unreachable(tasklist, **kwargs):
        c.service.calls.append(tasklist)
        raise gt.httplib2.ServerNotFoundError("Unable to find the server")

    c.service.list = unreachable
    c.fetch(force=True)
    assert c.service.calls == ["D"]
    assert not c.failed and c.offline
    assert cache.saved == 0


def test_no_network_no_requests(gt, monkeypatch):
    c = make(gt)
    calls = []
    monkeypatch.setattr(gt, "network_available", lambda: False)
    monkeypatch.setattr(c, "page_tasks", lambda: {}, raising=False)
    monkeypatch.setattr(c, "fetch", lambda **kw: calls.append("fetch"), raising=False)
    monkeypatch.setattr(c, "_read_updated", lambda *a: calls.append("sync"), raising=False)
    c.import_and_sync()
    assert not c.queue_sync()
    assert calls == []
    assert c.offline and not c.failed


def test_sync_skipped_after_an_offline_import(gt, monkeypatch):
    c = make(gt)
    synced = []
    monkeypatch.setattr(c, "page_tasks", lambda: {}, raising=False)
    monkeypatch.setattr(c, "fetch", lambda **kw: c.error("x", TimeoutError()), raising=False)
    monkeypatch.setattr(c, "sync_bullets_from_server", lambda before: synced.append(True), raising=False)
    c.import_and_sync()
    assert synced == [] and not c.failed


def test_title_shows_offline_until_google_answers(gt, monkeypatch):
    window = Window()
    c = make(gt, window)
    c.worker = gt.Worker(window, None)
    c.error("x", TimeoutError())
    assert window.title == "Home - Notes" + gt.OFFLINE_SUFFIX
    monkeypatch.setattr(gt.OnlineHttp.__bases__[0], "request", lambda self, *a, **kw: "response", raising=False)
    assert gt.OnlineHttp(c).request("https://tasks.googleapis.com") == "response"
    assert window.title == "Home - Notes" and not c.offline

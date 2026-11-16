"""Status sync: _set_bullets un/checks the tasks by the server, but not a checkbox the user changed meanwhile;
sync_bullets_from_server asks for what changed since its cursor (incremental) or for every task in batches (full)."""
import logging
import types
from time import time

import pytest

from test_fetch import FakeNotebook, FakePage

HREF = "https://tasks.google.com/task/web1?list=L1"  # task "d2ViMQ"
HREF2 = "https://tasks.google.com/task/web2?list=L1"  # task "d2ViMg"


def make_controller(gt, lines):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"page": ""}
    controller.notebook = FakeNotebook(FakePage(lines))
    return controller


def test_checkbox_ticked_during_the_sync_stays(gt):
    c = make_controller(gt, [f"[ ] [[{HREF}|💼]] Call Bob\n", f"[ ] [[{HREF2}|💼]] Pay\n"])
    before = c.page_tasks()
    c.notebook.page.lines[0] = f"[*] [[{HREF}|💼]] Call Bob\n"  # ticked while the server was asked
    c._set_bullets({"d2ViMQ": False, "d2ViMg": True}, before)
    assert c.notebook.page.parsed == f"[*] [[{HREF}|💼]] Call Bob\n[*] [[{HREF2}|💼]] Pay\n"


def test_indented_task_keeps_its_indent(gt):
    c = make_controller(gt, ["Home\n", f"\t\t[ ] [[{HREF}|💼]] Call Bob\n"])
    before = c.page_tasks()
    assert before == {"d2ViMQ": ("L1", "[ ] ", "Call Bob")}
    c._set_bullets({"d2ViMQ": True}, before)
    assert c.notebook.page.parsed == f"Home\n\t\t[*] [[{HREF}|💼]] Call Bob\n"


def test_task_added_during_the_sync_is_left(gt):
    c = make_controller(gt, ["Home\n"])
    before = c.page_tasks()
    c.notebook.page.lines.append(f"[ ] [[{HREF}|💼]] Call Bob\n")
    assert not c._set_bullets({"d2ViMQ": True}, before)
    assert c.notebook.stored == []


def test_page_not_stored_when_nothing_changes(gt):
    c = make_controller(gt, [f"[*] [[{HREF}|💼]] Call Bob\n", f"[ ] [[{HREF2}|💼]] Pay"])  # no newline at the end
    assert not c._set_bullets({"d2ViMQ": True, "d2ViMg": False}, c.page_tasks())
    assert c.notebook.stored == [] and c.notebook.page.parsed is None
    assert c._set_bullets({"d2ViMg": True}, c.page_tasks())
    assert c.notebook.page.parsed == f"[*] [[{HREF}|💼]] Call Bob\n[*] [[{HREF2}|💼]] Pay"


# --- sync_bullets_from_server ---

class Request:
    def __init__(self, run):
        self.execute = run


class Batch:
    def __init__(self, service, callback):
        self.service, self.callback, self.requests = service, callback, []

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        if self.service.batch_error:
            raise self.service.batch_error
        self.service.batches.append([request_id for request_id, _r in self.requests])
        for request_id, request in self.requests:
            try:
                self.callback(request_id, request.execute(), None)
            except Exception as e:  # handed to the callback, as googleapiclient does
                self.callback(request_id, None, e)


class FakeTasks:
    """ tasks().list(updatedMin=...) serving `updated[list_id]` page by page (no date filtering, the test says
        what changed), tasks().get from `tasks[list_id]`, batches. `errors[list_id]` is raised by list(),
        `task_errors[task_id]` by get(). """

    def __init__(self, gt, updated=None, tasks=None, errors=None, task_errors=None):
        self.gt = gt
        self.task_errors = task_errors or {}
        self.updated = updated or {}
        self.all = tasks or {}
        self.errors = errors or {}
        self.list_calls = []
        self.batches = []
        self.batch_error = None

    def tasks(self):
        return self

    def list(self, tasklist, pageToken=None, **kwargs):
        self.list_calls.append((tasklist, pageToken, kwargs))

        def run():
            if tasklist in self.errors:
                raise self.errors[tasklist]
            pages = self.updated.get(tasklist, [[]])
            i = int(pageToken or 0)
            result = {"items": pages[i]}
            if i + 1 < len(pages):
                result["nextPageToken"] = str(i + 1)
            return result
        return Request(run)

    def get(self, tasklist, task):
        def run():
            if task in self.task_errors:
                raise self.task_errors[task]
            if task not in self.all.get(tasklist, {}):
                raise http_error(self.gt, 404)
            return self.all[tasklist][task]
        return Request(run)

    def new_batch_http_request(self, callback):
        return Batch(self, callback)


def http_error(gt, status):
    return gt.HttpError(types.SimpleNamespace(status=status), b"error")


def line(task_id, list_id="L1", box="[ ]", title=None):
    return f"{box} [[gtasks://{list_id + '/' if list_id else ''}{task_id}|💼]] {title or task_id}\n"


def task(task_id, completed=False):
    return {"id": task_id, "status": "completed" if completed else "needsAction"}


@pytest.fixture
def sync(gt, tmp_path):
    def make(lines, last_synced=1000.0, **service):
        c = make_controller(gt, ["Home\n", "\n", *lines])
        c.preferences["tasklist"] = ""
        c.failed = False
        c.cache = gt.Cache(tmp_path / "googletasks.cache")
        c.cache.last_synced = last_synced
        c.cache.save()
        c.service = FakeTasks(gt, **service)
        c.api = types.SimpleNamespace(get_service=lambda **_kw: c.service)
        return c
    return make


def test_incremental_asks_what_changed_in_every_list_of_the_page(gt, sync):
    c = sync([line("t1"), line("t2", box="[*]"), line("x", "L2"), line("p", None), line("same", box="[*]")],
             updated={"L1": [[task("t1", True), task("unrelated", True)], [task("t2"), task("same", True)]],
                      "@default": [[task("p", True)]]})
    start = time()
    assert c.sync_bullets_from_server(c.page_tasks()) is True
    assert c.notebook.page.parsed == "Home\n\n" + line("t1", box="[*]") + line("t2") + line("x", "L2") \
        + line("p", None, box="[*]") + line("same", box="[*]")
    calls = sorted((list_id, token or "") for list_id, token, _kw in c.service.list_calls)
    assert calls == [("@default", ""), ("L1", ""), ("L1", "1"), ("L2", "")]  # paged, the old link: primary
    kwargs = c.service.list_calls[0][2]
    assert kwargs["updatedMin"] == "1970-01-01T00:11:40.000Z"  # 1000 s minus the 5 minutes overlap
    assert kwargs["showCompleted"] and kwargs["showHidden"]
    assert c.service.batches == []
    assert gt.Cache(c.cache._path).load().last_synced >= start
    assert not c.failed


def test_incremental_nothing_changed_page_not_stored(gt, sync):
    c = sync([line("t1", box="[*]")], updated={"L1": [[task("t1", True)]]})
    start = time()
    assert c.sync_bullets_from_server(c.page_tasks())
    assert c.notebook.stored == []
    assert c.cache.load().last_synced >= start


def test_incremental_cursor_not_moved_when_a_list_fails(gt, sync, capsys):
    c = sync([line("t1"), line("x", "L2")], updated={"L1": [[task("t1", True)]]},
             errors={"L2": OSError("timed out")})
    assert c.sync_bullets_from_server(c.page_tasks()) is False
    assert c.notebook.page.parsed == "Home\n\n" + line("t1", box="[*]") + line("x", "L2")  # the rest applied
    assert gt.Cache(c.cache._path).load().last_synced == 1000.0
    assert c.failed
    assert "timed out" in capsys.readouterr().err


def test_incremental_list_gone_is_skipped(gt, sync):
    c = sync([line("t1"), line("x", "L2")], updated={"L1": [[task("t1", True)]]},
             errors={"L2": http_error(gt, 404)})
    assert c.sync_bullets_from_server(c.page_tasks()) is True
    assert c.cache.load().last_synced > 1000.0
    assert not c.failed


def test_cursor_not_moved_when_the_notebook_is_busy(gt, sync, capsys):
    c = sync([line("t1")], updated={"L1": [[task("t1", True)]]})
    c.notebook.operation = types.SimpleNamespace(message="Updating index")
    assert c.sync_bullets_from_server(c.page_tasks()) is False
    assert c.notebook.stored == []
    assert c.cache.load().last_synced == 1000.0
    assert c.failed


def test_no_cursor_means_full_sync(gt, sync):
    c = sync([line("t1"), line("t2", "L2", box="[*]")], last_synced=0,
             tasks={"L1": {"t1": task("t1", True)}, "L2": {"t2": task("t2")}})
    start = time()
    assert c.sync_bullets_from_server(c.page_tasks())
    assert c.service.list_calls == []
    assert c.service.batches == [["t1", "t2"]]
    assert c.notebook.page.parsed == "Home\n\n" + line("t1", box="[*]") + line("t2", "L2")
    assert c.cache.load().last_synced >= start


def test_full_sync_in_batches_reports_unknown_task(gt, sync, caplog):
    ids = [f"t{i}" for i in range(60)]
    c = sync([line(i) for i in ids] + [line("gone", title="Deleted one")],
             tasks={"L1": {i: task(i, True) for i in ids}})
    with caplog.at_level(logging.INFO, logger="zim.plugins.googletasks"):
        assert c.sync_bullets_from_server(c.page_tasks(), full=True)
    assert [len(b) for b in c.service.batches] == [50, 11]
    assert c.notebook.page.parsed == "Home\n\n" + "".join(line(i, box="[*]") for i in ids) \
        + line("gone", title="Deleted one")
    assert "Cannot identify 1 tasks: Deleted one" in caplog.text
    assert c.cache.load().last_synced > 1000.0
    assert not c.failed


def test_full_sync_network_failure_keeps_cursor(gt, sync):
    c = sync([line("t1")], tasks={"L1": {"t1": task("t1", True)}})
    c.service.batch_error = OSError("no network")
    assert c.sync_bullets_from_server(c.page_tasks(), full=True) is False
    assert c.notebook.stored == []
    assert c.cache.load().last_synced == 1000.0
    assert c.failed


def test_full_sync_server_error_of_a_task_keeps_cursor(gt, sync):
    c = sync([line("t1"), line("t2")], tasks={"L1": {"t1": task("t1", True)}},
             task_errors={"t2": http_error(gt, 500)})
    assert c.sync_bullets_from_server(c.page_tasks(), full=True) is False
    assert c.notebook.page.parsed == "Home\n\n" + line("t1", box="[*]") + line("t2")
    assert c.cache.load().last_synced == 1000.0


# --- the sync queued after every import ---

class FakeWorker:
    def __init__(self):
        self.jobs = []

    def submit(self, job, name=None):
        self.jobs.append((name, job))
        return True


def test_sync_queued_after_import_with_the_checkboxes_of_then(gt, sync, monkeypatch):
    c = sync([line("t1")])
    c.worker = FakeWorker()
    calls = []
    monkeypatch.setattr(c, "fetch", lambda **kw: calls.append(("fetch", kw)))
    monkeypatch.setattr(c, "sync_bullets_from_server", lambda before, full=False: calls.append(("sync", before, full)))
    c.import_and_sync(force=True)
    assert [name for name, _job in c.worker.jobs] == ["import", "sync"]
    c.notebook.page.lines.append(line("t2"))  # imported, or ticked, after the queuing
    for _name, job in c.worker.jobs:
        job()
    assert calls == [("fetch", {"force": True, "all_history": False}),
                     ("sync", {"t1": ("L1", "[ ] ", "t1")}, False)]


def test_headless_sync_runs_after_a_throttled_import_and_fails_the_command(gt, sync):
    c = sync([line("t1")], errors={"L1": OSError("timed out")})
    c.worker = None
    order = []
    c.fetch = lambda **kw: order.append("fetch")  # returns early, as when throttled
    c.import_and_sync()
    assert order == ["fetch"]
    assert c.service.list_calls  # the sync ran all the same
    assert c.failed


def test_sync_skipped_after_a_failed_import(gt, sync):
    c = sync([line("t1")])
    c.worker = None
    c.fetch = lambda **kw: c.error("Cannot read the task list")  # e.g. offline
    c.import_and_sync()
    assert c.service.list_calls == []  # would only report the same again
    assert c.failed


def test_menu_sync_is_full(gt, sync):
    c = sync([line("t1")])
    c.worker = FakeWorker()
    c.queue_sync(full=True)
    name, job = c.worker.jobs[0]
    assert name == "sync"
    c.service.all = {"L1": {"t1": task("t1", True)}}
    job()
    assert c.service.list_calls == [] and c.service.batches == [["t1"]]

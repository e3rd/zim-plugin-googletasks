import datetime

import pytest


class FakeCache:
    """ Same interface fetch() uses on Cache, without touching the disk. """

    def __init__(self, items_ids=(), exists=True):
        self.items_ids = set(items_ids)
        self.undated_ids = set()
        self.inbox_tasks = {}
        self._exists = exists
        self.saved = 0
        self.last_fetched = datetime.datetime.now().timestamp() - 86400

    def exists(self):
        return self._exists

    def load(self):
        return self

    def last_time(self):
        return self.last_fetched

    def touch_save(self):
        self.saved += 1

    def save(self):
        self.saved_without_time = getattr(self, "saved_without_time", 0) + 1


class FakePage:
    def __init__(self, lines):
        self.lines = lines
        self.parsed = None

    def dump(self, _format):
        return list(self.lines)

    def parse(self, _format, text):
        self.parsed = text

    def get_title(self):
        return "Home"


class FakeNotebook:
    def __init__(self, page):
        self.page = page
        self.stored = []

    def get_home_page(self):
        return self.page

    def store_page(self, page):
        self.stored.append(page)


class FakeService:
    """ tasks().list(...).execute() serving `pages[list_id]` one by one, linked with nextPageToken.
        A call without the due date filter (undated tasks wanted) is served from `undated[list_id]`
        (whole list: dated tasks too, the controller must pick the undated ones itself). """

    def __init__(self, pages, undated=None):
        self.pages = pages
        self.undated = undated or {}
        self.calls = []
        self.undated_calls = []
        self.kwargs = []

    def tasks(self):
        return self

    def list(self, tasklist, pageToken=None, **kwargs):
        self.kwargs.append(kwargs)
        if "dueMax" in kwargs:
            self.calls.append((tasklist, pageToken))
            source = self.pages
        else:
            self.undated_calls.append((tasklist, pageToken))
            source = self.undated
        if tasklist not in source:
            raise self.error("no such list")
        pages = source[tasklist]
        i = int(pageToken or 0)
        result = {"items": pages[i]} if pages else {}
        if i + 1 < len(pages):
            result["nextPageToken"] = str(i + 1)
        return type("Request", (), {"execute": lambda _self: result})()


def make_controller(gt, monkeypatch, items, cache, page_lines=("Home\n", "\n", "existing\n"),
                    tasklist="", pages=None, lists=None, undated=None):
    """ :param items: tasks of the default list (single page), unless `pages` {list_id: [[items]]} given
        :param undated: {list_id: [[items]]} served when reading without the due date filter;
                        if given, `include_undated` is on """
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"include_start_date": False, "tasklist": tasklist, "page": "",
                              "include_undated": undated is not None, "include_assigned": True,
                              "inbox_calendar": ""}
    controller.cache = cache
    cache.lists = lists or {}
    controller.failed = False
    controller.notebook = FakeNotebook(FakePage(list(page_lines)))
    controller.service = FakeService(pages if pages is not None else {"@default": [items]}, undated)
    controller.service.error = gt.Error
    monkeypatch.setattr(controller, "_read_service", lambda info: controller.service)
    return controller


DUE = "2026-10-23T00:00:00.000Z"


def item(task_id, title="t", etag=None, due=DUE):
    """ :param due: None for a task without a due date """
    task = {"id": task_id, "etag": etag or f'"etag-{task_id}"', "title": title}
    if due:
        task["due"] = due
    return task


def test_imports_new_tasks_after_first_blank_line(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [item("a", "Alpha"), item("b", "Beta")], cache)
    c.fetch(force=True)
    assert c.notebook.page.parsed == (
        "Home\n\n[ ] [[gtasks://@default/a|✋]] Alpha\n[ ] [[gtasks://@default/b|✋]] Beta\nexisting\n")
    assert cache.items_ids == {"a", "b"}
    assert cache.saved == 1


def test_skips_already_imported_by_id(gt, monkeypatch):
    cache = FakeCache(items_ids={"a"})
    c = make_controller(gt, monkeypatch, [item("a", "Alpha"), item("b", "Beta")], cache)
    c.fetch(force=True)
    assert "Alpha" not in c.notebook.page.parsed
    assert "Beta" in c.notebook.page.parsed
    assert cache.items_ids == {"a", "b"}


def test_skips_task_touched_on_server(gt, monkeypatch):
    """ Etag changes on any edit; the ID in the cache is what identifies the task. """
    cache = FakeCache(items_ids={"a"})
    c = make_controller(gt, monkeypatch, [item("a", "Alpha (edited)", etag='"new"')], cache)
    c.fetch(force=True)
    assert c.notebook.page.parsed is None  # nothing new -> page untouched
    assert c.notebook.stored == []
    assert cache.saved == 1  # timestamp still refreshed


def test_old_cache_with_etags_still_dedups(gt, monkeypatch):
    cache = FakeCache(items_ids={'"etag-a"'})
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache)
    c.fetch(force=True)
    assert c.notebook.page.parsed is None
    assert cache.items_ids == {"a"}  # migrated to IDs


def test_remembers_every_returned_task_so_past_due_ones_are_not_reimported(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [item("a")], cache)
    c.fetch(force=True)
    c.notebook.page.parsed = None
    c.fetch(force=True)  # server hands the same task again
    assert c.notebook.page.parsed is None


def test_all_history_reimports_everything(gt, monkeypatch):
    cache = FakeCache(items_ids={"a"})
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache)
    c.fetch(all_history=True)
    assert "Alpha" in c.notebook.page.parsed


def test_first_run_without_cache_file(gt, monkeypatch):
    cache = FakeCache(exists=False)
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache)
    c.fetch(force=True)
    assert "Alpha" in c.notebook.page.parsed
    assert cache.saved == 1


def test_cache_saved_by_the_inbox_calendar_before_the_first_import_is_a_first_run(gt, monkeypatch):
    """ Only today's tasks, not all since 1970 (the file exists, but no import has run yet). """
    cache = FakeCache()
    cache.last_fetched = 0
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache)
    c.fetch()  # not skipped as a recent fetch either
    assert c.service.kwargs[0]["dueMin"] == c.get_time(mode="midnight")
    assert "Alpha" in c.notebook.page.parsed


def test_recent_fetch_is_skipped_unless_forced(gt, monkeypatch):
    cache = FakeCache(items_ids=())
    cache.last_fetched = datetime.datetime.now().timestamp()
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache)
    c.fetch()
    assert c.notebook.page.parsed is None
    c.fetch(force=True)
    assert "Alpha" in c.notebook.page.parsed


def test_no_tasks_found_leaves_cache_alone(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [], cache)
    c.fetch(force=True)
    assert cache.saved == 0
    assert c.notebook.page.parsed is None


def test_unreadable_list_leaves_cache_alone(gt, monkeypatch, capsys):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [], cache, pages={})
    c.fetch(force=True)
    assert cache.saved == 0
    assert c.failed


def test_reads_all_pages(gt, monkeypatch):
    """ The server returns at most 100 tasks at once, the rest are on the next pages. """
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, None, cache,
                        pages={"@default": [[item("a", "Alpha")], [item("b", "Beta")], [item("c", "Gamma")]]})
    c.fetch(force=True)
    assert all(t in c.notebook.page.parsed for t in ("Alpha", "Beta", "Gamma"))
    assert c.service.calls == [("@default", None), ("@default", "1"), ("@default", "2")]
    assert cache.items_ids == {"a", "b", "c"}


def test_imports_from_multiple_lists_with_their_icons(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, None, cache, tasklist="My Tasks, Work=💼",
                        lists={"My Tasks": "D", "Work": "W"},
                        pages={"D": [[item("a", "Alpha")]], "W": [[item("b", "Beta")]]})
    c.fetch(force=True)
    assert c.notebook.page.parsed == (
        "Home\n\n[ ] [[gtasks://D/a|✋]] Alpha\n[ ] [[gtasks://W/b|💼]] Beta\nexisting\n")
    assert cache.items_ids == {"a", "b"}
    assert cache.saved == 1


def test_failing_list_keeps_its_ids_and_the_time(gt, monkeypatch):
    """ Work cannot be read: its tasks must not be imported again later, nor its time window skipped. """
    cache = FakeCache(items_ids={"w-old"})
    last = cache.last_fetched
    c = make_controller(gt, monkeypatch, None, cache, tasklist="My Tasks, Work",
                        lists={"My Tasks": "D", "Work": "W"},
                        pages={"D": [[item("a", "Alpha")]]})
    c.fetch(force=True)
    assert "Alpha" in c.notebook.page.parsed
    assert cache.items_ids == {"a", "w-old"}
    assert cache.saved == 0 and cache.saved_without_time == 1
    assert cache.last_fetched == last
    assert c.failed


@pytest.mark.parametrize("include_start_date", [True, False])
def test_untitled_task_is_skipped_and_reported(gt, monkeypatch, capsys, include_start_date):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [{"id": "x", "etag": "e"}, item("a", "Alpha")], cache)
    c.preferences["include_start_date"] = include_start_date
    c.fetch(force=True)
    assert "Alpha" in c.notebook.page.parsed
    assert "False" not in c.notebook.page.parsed
    assert c.failed
    assert "no title" in capsys.readouterr().err


def test_assigned_tasks_are_asked_for(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], FakeCache())
    c.fetch(force=True)
    assert all(kw["showAssigned"] is True for kw in c.service.kwargs)
    c.preferences["include_assigned"] = False
    c.fetch(force=True)
    assert c.service.kwargs[-1]["showAssigned"] is False


def test_undated_tasks_are_imported_once(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], cache,
                        undated={"@default": [[item("a", "Alpha"), item("u", "Undated", due=None)]]})
    c.fetch(force=True)
    assert c.notebook.page.parsed == "Home\n\n[ ] [[gtasks://@default/a|✋]] Alpha\n" \
                                     "[ ] [[gtasks://@default/u|✋]] Undated\nexisting\n"
    assert c.service.undated_calls == [("@default", None)]
    assert cache.items_ids == {"a"} and cache.undated_ids == {"u"}
    c.notebook.page.parsed = None
    c.fetch(force=True)
    assert c.notebook.page.parsed is None


def test_undated_task_getting_a_future_date_is_not_reimported(gt, monkeypatch):
    """ It vanishes from both responses for a while, then shows up when it falls due. """
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, None, cache,
                        pages={"@default": [[]]}, undated={"@default": [[item("u", "Undated", due=None)]]})
    c.fetch(force=True)
    c.service.undated["@default"] = [[item("u", "Undated", due="2026-12-24T00:00:00.000Z")]]
    c.notebook.page.parsed = None
    c.fetch(force=True)  # no task anywhere
    c.service.pages["@default"] = [[item("u", "Undated", due="2026-12-24T00:00:00.000Z")]]
    c.fetch(force=True)  # it is due now
    assert c.notebook.page.parsed is None
    assert cache.items_ids == {"u"} and cache.undated_ids == set()


def test_undated_tasks_not_read_when_disabled(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [item("a", "Alpha")], FakeCache())
    c.fetch(force=True)
    assert c.service.undated_calls == []


def test_all_history_reimports_undated_too(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [], cache, undated={"@default": [[item("u", "Undated", due=None)]]})
    cache.undated_ids = {"u"}
    c.fetch(all_history=True)
    assert "Undated" in c.notebook.page.parsed


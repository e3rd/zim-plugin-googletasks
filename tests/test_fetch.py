import datetime

import pytest


class FakeCache:
    """ Same interface fetch() uses on Cache, without touching the disk. """

    def __init__(self, items_ids=(), exists=True):
        self.items_ids = set(items_ids)
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


def make_controller(gt, monkeypatch, items, cache, page_lines=("Home\n", "\n", "existing\n")):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"include_start_date": False, "tasklist": "", "page": ""}
    controller.cache = cache
    controller.failed = False
    controller.notebook = FakeNotebook(FakePage(list(page_lines)))
    monkeypatch.setattr(controller, "_read_task_list", lambda due_min: items)
    return controller


def item(task_id, title="t", etag=None):
    return {"id": task_id, "etag": etag or f'"etag-{task_id}"', "title": title}


def test_imports_new_tasks_after_first_blank_line(gt, monkeypatch):
    cache = FakeCache()
    c = make_controller(gt, monkeypatch, [item("a", "Alpha"), item("b", "Beta")], cache)
    c.fetch(force=True)
    assert c.notebook.page.parsed == (
        "Home\n\n[ ] [[gtasks://a|✋]] Alpha\n[ ] [[gtasks://b|✋]] Beta\nexisting\n")
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

    def raise_lookup(due_min):
        raise LookupError

    monkeypatch.setattr(c, "_read_task_list", raise_lookup)
    c.fetch(force=True)
    assert cache.saved == 0
    assert c.notebook.page.parsed is None


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

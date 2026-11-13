"""revert_bullet: the server refused an un/tick, the checkbox goes back – or the deleted line comes back."""
import types

from test_fetch import FakeNotebook, FakePage

HREF = "https://tasks.google.com/task/web1?list=L1"  # task "d2ViMQ"
TICKED = f"[*] [[{HREF}|💼]] Call Bob\n"
MARKUP = f"[ ] [[{HREF}|💼]] Call Bob"


class Page(FakePage):
    def __init__(self, name, lines, exists=True):
        super().__init__(lines)
        self.name = name
        self._exists = exists

    def exists(self):
        return self._exists


class Notebook(FakeNotebook):
    def __init__(self, home, *pages):
        super().__init__(home)
        self.pages = {page.name: page for page in (home, *pages)}

    def get_page(self, path):
        return self.pages[path]


def make_controller(gt, monkeypatch, *pages):
    monkeypatch.setattr(gt, "ZimPath", lambda name: name)
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"page": ""}
    controller.failed = False
    controller.ticks = {}
    controller.notebook = Notebook(*pages)
    return controller


def revert(gt, controller, page="Journal", tick=None):
    tick = tick or controller.tick("d2ViMQ")
    controller.revert_bullet(page, "d2ViMQ", gt.UNCHECKED_BOX, MARKUP, tick)


def test_checkbox_reverted_where_the_line_is_now(gt, monkeypatch):
    journal = Page("Journal", ["Journal\n", "\n", "typed meanwhile\n", "\t" + TICKED.replace("Bob", "Bob!"), "end\n"])
    c = make_controller(gt, monkeypatch, Page("Home", ["Home\n"]), journal)
    revert(gt, c)
    assert journal.parsed == f"Journal\n\ntyped meanwhile\n\t[ ] [[{HREF}|💼]] Call Bob!\nend\n"
    assert c.notebook.stored == [journal]


def test_deleted_line_is_put_back_to_the_top_of_its_page(gt, monkeypatch):
    journal = Page("Journal", ["Journal\n", "\n", "typed meanwhile\n"])
    c = make_controller(gt, monkeypatch, Page("Home", ["Home\n"]), journal)
    revert(gt, c)
    assert journal.parsed == f"Journal\n\n{MARKUP}\ntyped meanwhile\n"


def test_deleted_page_puts_it_to_the_import_page(gt, monkeypatch):
    home = Page("Home", ["Home\n", "\n", "x\n"])
    c = make_controller(gt, monkeypatch, home, Page("Journal", [], exists=False))
    revert(gt, c)
    assert home.parsed == f"Home\n\n{MARKUP}\nx\n"


def test_ticked_again_since_the_newest_tick_decides(gt, monkeypatch):
    journal = Page("Journal", ["Journal\n", "\n", TICKED])
    c = make_controller(gt, monkeypatch, Page("Home", ["Home\n"]), journal)
    old = c.tick("d2ViMQ")
    c.tick("d2ViMQ")
    revert(gt, c, tick=old)
    assert journal.parsed is None


def test_busy_notebook_is_reported(gt, monkeypatch):
    journal = Page("Journal", ["Journal\n", "\n", TICKED])
    c = make_controller(gt, monkeypatch, Page("Home", ["Home\n"]), journal)
    c.notebook.operation = types.SimpleNamespace(message="Updating index")
    revert(gt, c)
    assert journal.parsed is None
    assert c.failed


def checked(gt, error):
    c = object.__new__(gt.GoogletasksController)
    c.window = None
    c.failed = False

    def get_service(**_):
        raise error

    c.api = types.SimpleNamespace(get_service=get_service)
    return c.task_checked("L1", "d2ViMQ", gt.CHECKED_BOX)


def test_task_gone_from_server_is_not_reverted(gt):
    assert checked(gt, gt.HttpError(types.SimpleNamespace(status=404), b"Not Found")) is None


def test_network_error_is_reverted(gt):
    assert checked(gt, OSError("timed out")) is False
    assert checked(gt, gt.HttpError(types.SimpleNamespace(status=500), b"")) is False

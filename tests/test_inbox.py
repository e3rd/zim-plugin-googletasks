import pytest

from test_fetch import FakeNotebook, FakePage

HEADER = ["text", "due (YYYY-MM-DD, optional)", "note (optional)"]
BANNER = ["📥 Managed by the Google Tasks plugin for Zim."]


class Request:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeSheets:
    """ spreadsheets().get / values().get / batchUpdate of the Sheets API over a single sheet of `values`. """

    def __init__(self, values, error=None, delete_error=None):
        self.values_ = values
        self.error = error
        self.delete_error = delete_error
        self.updates = []
        self.ranges = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range=None, fields=None):
        if range is None:
            return Request({"sheets": [{"properties": {"title": "Inbox", "sheetId": 7}}]}, self.error)
        self.ranges.append(range)
        return Request({"values": self.values_} if self.values_ else {}, self.error)

    def batchUpdate(self, spreadsheetId, body):
        self.updates.append(body["requests"])
        return Request({}, self.delete_error)


def make_controller(gt, monkeypatch, values, inbox_sheet="https://docs.google.com/spreadsheets/d/abc-1_x/edit",
                    **sheets_kwargs):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"inbox_sheet": inbox_sheet, "page": ""}
    controller.failed = False
    controller.notebook = FakeNotebook(FakePage(["Home\n", "\n", "existing\n"]))
    controller.sheets = FakeSheets(values, **sheets_kwargs)
    controller.calendar_api = type("Api", (), {"get_inbox_services": lambda _self: controller.sheets})()
    return controller


def deleted_rows(requests):
    return [r["deleteDimension"]["range"]["startIndex"] for r in requests]


@pytest.mark.parametrize("value, expected", [
    ("https://docs.google.com/spreadsheets/d/abc-1_x/edit#gid=0", "abc-1_x"),
    ("  abc-1_x ", "abc-1_x"),
    ("", None),
    (None, None),
])
def test_parse_sheet_id(gt, value, expected):
    assert gt.parse_sheet_id(value) == expected


def test_rows_found_by_header_whatever_the_column_order(gt):
    rows = gt.parse_inbox_rows([BANNER, ["note", "Text"], ["details", "Call Bob"], [], ["", "Buy milk"]])
    assert rows == [(2, "Call Bob", "", "details"), (3, "", "", ""), (4, "Buy milk", "", "")]


@pytest.mark.parametrize("values", [[], [BANNER], [BANNER, ["task", "due"]], [HEADER, ["Call Bob"]]])
def test_changed_header_is_refused(gt, values):
    with pytest.raises(ValueError):
        gt.parse_inbox_rows(values)


@pytest.mark.parametrize("args, expected", [
    (("  Call   Bob ",), "[ ] 📥 Call Bob"),
    (("Call Bob", "2026-10-28"), "[ ] 📥 Call Bob _{//<2026-10-28//}"),
    (("Call Bob", "28 Oct 2026"), "[ ] 📥 Call Bob _{//<2026-10-28//}"),
    (("Call Bob", "someday"), "[ ] 📥 Call Bob (due: someday)"),
    (("Call Bob", "", "about the roof\nand the bill"), "[ ] 📥 Call Bob\nabout the roof\nand the bill"),
    (("", "", "only a note"), "[ ] 📥 only a note"),
    (("", "", ""), None),
])
def test_inbox_text(gt, args, expected):
    assert gt.get_inbox_text(*args) == expected


def test_import_inserts_tasks_and_deletes_all_rows_bottom_up(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [BANNER, HEADER, ["Alpha"], [], ["Beta", "2026-10-28"]])
    assert c.import_inbox() == 2
    assert c.notebook.page.parsed == "Home\n\n[ ] 📥 Alpha\n[ ] 📥 Beta _{//<2026-10-28//}\nexisting\n"
    assert c.sheets.ranges == ["'Inbox'!A:Z"]
    assert len(c.sheets.updates) == 1
    assert deleted_rows(c.sheets.updates[0]) == [4, 3, 2]
    assert {r["deleteDimension"]["range"]["sheetId"] for r in c.sheets.updates[0]} == {7}
    assert not c.failed


def test_empty_inbox_touches_nothing(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [BANNER, HEADER])
    assert c.import_inbox() == 0
    assert c.notebook.page.parsed is None
    assert c.sheets.updates == []


def test_no_sheet_set_does_nothing(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [BANNER, HEADER, ["Alpha"]], inbox_sheet="")
    assert c.import_inbox() == 0
    assert c.sheets.ranges == []


def test_changed_header_neither_imports_nor_deletes(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [BANNER, ["task"], ["Alpha"]])
    assert c.import_inbox() is None
    assert c.notebook.page.parsed is None
    assert c.sheets.updates == []
    assert c.failed


def test_inaccessible_sheet_is_reported(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [], error=gt.Error("404"))
    assert c.import_inbox() is None
    assert c.failed


def test_not_granted_is_reported_without_oauth_flow(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [])

    def not_granted(_self):
        raise LookupError(gt.INBOX_MISSING)

    c.calendar_api = type("Api", (), {"get_inbox_services": not_granted})()
    assert c.import_inbox() is None
    assert c.failed


def test_failed_delete_is_reported_after_import(gt, monkeypatch):
    c = make_controller(gt, monkeypatch, [BANNER, HEADER, ["Alpha"]], delete_error=gt.Error("500"))
    assert c.import_inbox() == 1
    assert "Alpha" in c.notebook.page.parsed
    assert c.failed


def test_busy_notebook_leaves_page_and_rows(gt, monkeypatch):
    """ Zim updating its index refuses store_page(): the page must not be changed (it would be autosaved
        with the tasks) and the rows must stay in the sheet, to be imported next time. """
    c = make_controller(gt, monkeypatch, [BANNER, HEADER, ["Alpha"]])
    c.notebook.operation = type("Operation", (), {"message": "Updating index"})()
    assert c.import_inbox() is None
    assert c.notebook.page.parsed is None
    assert c.notebook.stored == []
    assert c.sheets.updates == []
    assert c.failed

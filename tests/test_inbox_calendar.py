"""Inbox calendar: its events are imported as tasks (plain Zim ones or Google Tasks) and deleted from it."""
import time
import types

import pytest

from test_fetch import FakeCache, FakeNotebook, FakePage
from test_inbox import Request

CAL = "c_abc@group.calendar.google.com"


def event(event_id, summary="", date="2026-10-28", **kwargs):
    start = {"dateTime": date} if "T" in date else {"date": date}
    return {"id": event_id, "summary": summary, "start": start, **kwargs}


class Batch:
    def __init__(self, calendar, callback):
        self.calendar, self.callback, self.requests = calendar, callback, []

    def add(self, request):
        self.requests.append(request)

    def execute(self):
        self.calendar.batches.append([r.event_id for r in self.requests])
        for r in self.requests:
            self.callback(r.event_id, None, self.calendar.delete_errors.get(r.event_id))


class FakeCalendar:
    """ events().list (paged by `pages`) / events().delete in batches / calendars().get, insert """

    def __init__(self, pages, error=None, delete_errors=None):
        self.pages, self.error = pages, error
        self.delete_errors = delete_errors or {}
        self.batches = []
        self.listed = []
        self.inserted = []

    def events(self):
        return self

    def calendars(self):
        return self

    def list(self, calendarId, pageToken=None, maxResults=None):
        self.listed.append(calendarId)
        i = int(pageToken or 0)
        result = {"items": self.pages[i]} if self.pages else {}
        if i + 1 < len(self.pages):
            result["nextPageToken"] = str(i + 1)
        return Request(result, self.error)

    def delete(self, calendarId, eventId):
        return types.SimpleNamespace(event_id=eventId)

    def new_batch_http_request(self, callback):
        return Batch(self, callback)

    def get(self, calendarId):
        return Request({"id": calendarId}, self.error)

    def insert(self, body):
        self.inserted.append(body)
        return Request({"id": "c_new@group.calendar.google.com"})


class FakeTasks:
    def __init__(self, error=None):
        self.created, self.error = [], error

    def tasks(self):
        return self

    def insert(self, tasklist, body):
        if self.error and len(self.created) == 1:
            return Request(error=self.error)
        task = {"id": f"t{len(self.created)}", **body}
        self.created.append((tasklist, body))
        return Request(task)


def make_controller(gt, pages, tasklist="", calendar_id=CAL, **calendar_kwargs):
    c = object.__new__(gt.GoogletasksController)
    c.window = None
    c.preferences = {"inbox_calendar": calendar_id, "inbox_calendar_tasklist": tasklist, "page": "",
                     "include_start_date": False, "tasklist": "My Tasks, Work=💼"}
    c.failed = False
    c.cache = FakeCache()
    c.cache.lists = {"My Tasks": "L0", "Work": "L1"}
    c.notebook = FakeNotebook(FakePage(["Home\n", "\n", "existing\n"]))
    c.calendar = FakeCalendar(pages, **calendar_kwargs)
    c.tasks_service = FakeTasks()
    c.calendar_api = types.SimpleNamespace(get_inbox_calendar_service=lambda claim=False: c.calendar,
                                           get_service=lambda **_: c.tasks_service)
    return c


@pytest.fixture
def prague(monkeypatch):
    """ The time of a timed event is shown in the local time zone, this one. """
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("ev, expected", [
    (event("1", "Call Bob"), ("Call Bob", "2026-10-28", "")),
    (event("1", "Call Bob", "2026-10-30T14:05:00+01:00"), ("Call Bob 14:05", "2026-10-30", "")),
    (event("1", "Call Bob", "2026-10-29T23:30:00Z"), ("Call Bob 00:30", "2026-10-30", "")),  # UTC calendar
    (event("1", "Call Bob", description='about <b>the roof</b><br>see <a href="https://x.cz/a">offer</a> &amp; bill'),
     ("Call Bob", "2026-10-28", "about the roof\nsee offer https://x.cz/a & bill")),
    (event("1", "Gym", recurrence=["RRULE:FREQ=WEEKLY"]), ("Gym", "2026-10-28", "repeats: RRULE:FREQ=WEEKLY")),
])
def test_event_task(gt, ev, expected, prague):
    assert gt.get_event_task(ev) == expected


def test_imports_all_events_as_zim_tasks_and_deletes_them(gt):
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "", description="only a note")],
                             [event("e3", "Beta", "2027-01-05")]])
    assert c.import_inbox_calendar() == 3
    assert c.notebook.page.parsed == ("Home\n\n[ ] 📥 Alpha _{//<2026-10-28//}\n[ ] 📥 only a note _{//<2026-10-28//}\n"
                                      "[ ] 📥 Beta _{//<2027-01-05//}\nexisting\n")
    assert c.calendar.batches == [["e1", "e2", "e3"]]
    assert not c.failed


def test_deletes_in_batches_of_50(gt):
    c = make_controller(gt, [[event(f"e{i}", f"t{i}") for i in range(120)]])
    assert c.import_inbox_calendar() == 120
    assert [len(b) for b in c.calendar.batches] == [50, 50, 20]


def test_events_become_google_tasks_of_the_list(gt):
    c = make_controller(gt, [[event("e1", "Alpha", description="note"), event("e2", "Beta", "2027-01-05")]],
                        tasklist="Work")
    assert c.import_inbox_calendar() == 2
    assert c.tasks_service.created == [("L1", {"title": "Alpha", "notes": "note", "due": "2026-10-28T00:00:00.000Z"}),
                                       ("L1", {"title": "Beta", "due": "2027-01-05T00:00:00.000Z"})]
    page = c.notebook.page.parsed
    assert "|💼]] Alpha\nnote\n" in page and "|💼]] Beta\n" in page
    assert c.cache.undated_ids == {"t0", "t1"}  # the import of the list will not bring them again
    assert c.calendar.batches == [["e1", "e2"]]


def test_failed_task_creation_keeps_the_events_left(gt):
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Beta"), event("e3", "Gamma")]], tasklist="Work")
    c.tasks_service.error = gt.Error("500")
    assert c.import_inbox_calendar() == 1
    assert c.calendar.batches == [["e1"]]
    assert "Alpha" in c.notebook.page.parsed and "Beta" not in c.notebook.page.parsed
    assert c.failed


def test_empty_calendar_touches_nothing(gt):
    c = make_controller(gt, [])
    assert c.import_inbox_calendar() == 0
    assert c.notebook.page.parsed is None
    assert c.calendar.batches == []


def test_no_calendar_set_does_nothing(gt):
    c = make_controller(gt, [[event("e1", "Alpha")]], calendar_id="")
    assert c.import_inbox_calendar() == 0
    assert c.calendar.listed == []


def test_inaccessible_calendar_is_reported(gt):
    c = make_controller(gt, [], error=gt.Error("404"))
    assert c.import_inbox_calendar() is None
    assert c.failed


def test_not_granted_is_reported_without_oauth_flow(gt):
    c = make_controller(gt, [])

    def not_granted(claim=False):
        assert not claim
        raise LookupError(gt.INBOX_CALENDAR_MISSING)

    c.calendar_api.get_inbox_calendar_service = not_granted
    assert c.import_inbox_calendar() is None
    assert c.failed


def test_failed_delete_is_reported_but_gone_ones_are_fine(gt):
    gone = gt.HttpError(types.SimpleNamespace(status=410), b"Gone")
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Beta")]], delete_errors={"e1": gone})
    assert c.import_inbox_calendar() == 2
    assert not c.failed
    c = make_controller(gt, [[event("e1", "Alpha")]], delete_errors={"e1": gt.Error("500")})
    c.import_inbox_calendar()
    assert c.failed


def test_busy_notebook_leaves_page_and_events(gt):
    c = make_controller(gt, [[event("e1", "Alpha")]])
    c.notebook.operation = types.SimpleNamespace(message="Updating index")
    assert c.import_inbox_calendar() is None
    assert c.notebook.page.parsed is None
    assert c.calendar.batches == []
    assert c.failed


def test_busy_notebook_with_google_tasks_still_deletes_events(gt):
    """ The tasks exist on the server already: kept events would make them twice, the import brings them. """
    c = make_controller(gt, [[event("e1", "Alpha")]], tasklist="Work")
    c.notebook.operation = types.SimpleNamespace(message="Updating index")
    c.import_inbox_calendar()
    assert c.calendar.batches == [["e1"]]
    assert c.cache.undated_ids == set()
    assert c.failed


def test_create_reuses_the_calendar_set(gt):
    c = make_controller(gt, [])
    assert c.create_inbox_calendar() == CAL
    assert c.calendar.inserted == []


def test_create_makes_a_new_one(gt):
    c = make_controller(gt, [], calendar_id="")
    c.notebook.config = types.SimpleNamespace(write=lambda: None)
    assert c.create_inbox_calendar() == "c_new@group.calendar.google.com"
    assert c.calendar.inserted[0]["summary"] == "Zim inbox"
    assert c.preferences["inbox_calendar"] == "c_new@group.calendar.google.com"

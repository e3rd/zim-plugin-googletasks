"""Inbox calendar: its events are imported as tasks (plain Zim ones or Google Tasks) and deleted from it."""
import datetime
import time
import types

import pytest

from test_fetch import FakeCache, FakeNotebook, FakePage

CAL = "c_abc@group.calendar.google.com"
TODAY = datetime.date.today().isoformat()
LATER = (datetime.date.today() + datetime.timedelta(days=14)).isoformat()
"a future event, the Google Task made of it is not put into the page"


class Request:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


def event(event_id, summary="", date=TODAY, **kwargs):
    start = {"dateTime": date} if "T" in date else {"date": date}
    return {"id": event_id, "summary": summary, "start": start, **kwargs}


class Batch:
    def __init__(self, calendar, callback):
        self.calendar, self.callback, self.requests = calendar, callback, []

    def add(self, request, request_id=None):
        assert request_id == request.event_id
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
    c.api = types.SimpleNamespace(get_inbox_calendar_service=lambda claim=False: c.calendar,
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
    (event("1", "Call Bob", "2026-10-28"), ("Call Bob", "2026-10-28", "")),
    (event("1", "Call Bob", "2026-10-30T14:05:00+01:00"), ("14:05 Call Bob", "2026-10-30", "")),
    (event("1", "Call Bob", "2026-10-29T23:30:00Z"), ("00:30 Call Bob", "2026-10-30", "")),  # UTC calendar
    (event("1", "Call Bob", "2026-10-30T14:05:00Z", end={"dateTime": "2026-10-30T15:30:00Z"}),
     ("15:05–16:30 Call Bob", "2026-10-30", "")),
    (event("1", "Trip", "2026-10-30T14:00:00Z", end={"dateTime": "2026-11-02T09:00:00Z"}),
     ("15:00 Trip (until 2026-11-02 10:00)", "2026-10-30", "")),
    (event("1", "Holiday", "2026-10-30", end={"date": "2026-11-03"}), ("Holiday (until 2026-11-02)", "2026-10-30", "")),
    (event("1", "Day off", "2026-10-30", end={"date": "2026-10-31"}), ("Day off", "2026-10-30", "")),
    (event("1", "Call Bob", "2026-10-28",
           description='about <b>the roof</b><br>see <a href="https://x.cz/a">offer</a> &amp; bill'),
     ("Call Bob", "2026-10-28", "about the roof\nsee offer https://x.cz/a & bill")),
    (event("1", "Meeting", "2026-10-28", location="Room 5", hangoutLink="https://meet.google.com/a-b-c"),
     ("Meeting", "2026-10-28", "Room 5\nhttps://meet.google.com/a-b-c")),
    (event("1", "Call", "2026-10-28", conferenceData={"entryPoints": [
        {"entryPointType": "phone", "uri": "tel:+420"}, {"entryPointType": "video", "uri": "https://zoom.us/j/1"}]}),
     ("Call", "2026-10-28", "https://zoom.us/j/1")),
    (event("1", "", "2026-10-30T14:05:00Z", description="only a note"), ("15:05 only a note", "2026-10-30", "")),
    (event("1", "", "2026-10-28"), ("(no title)", "2026-10-28", "")),
])
def test_event_task(gt, ev, expected, prague):
    assert gt.get_event_task(ev) == expected


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


def test_zim_task_of_a_future_event_starts_on_its_day(gt):
    """ The Zim Task List keeps it hidden till then. """
    assert gt.get_inbox_text("Alpha", TODAY, start_at_due=True) == f"[ ] 📥 Alpha _{{//<{TODAY}//}}"
    assert gt.get_inbox_text("Alpha", LATER, start_at_due=True) == f"[ ] 📥 Alpha _{{//>{LATER} <{LATER}//}}"
    assert gt.get_inbox_text("Alpha", LATER) == f"[ ] 📥 Alpha _{{//<{LATER}//}}"  # the inbox sheet


def test_imports_all_events_as_zim_tasks_and_deletes_them(gt):
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "", description="only a note")],
                             [event("e3", "Beta", LATER)]])
    assert c.import_inbox_calendar() == 3
    assert c.notebook.page.parsed == (f"Home\n\n[ ] 📥 Alpha _{{//<{TODAY}//}}\n[ ] 📥 only a note _{{//<{TODAY}//}}\n"
                                      f"[ ] 📥 Beta _{{//>{LATER} <{LATER}//}}\nexisting\n")
    assert c.calendar.batches == [["e1", "e2", "e3"]]
    assert not c.failed


def test_deletes_in_batches_of_50(gt):
    c = make_controller(gt, [[event(f"e{i}", f"t{i}") for i in range(120)]])
    assert c.import_inbox_calendar() == 120
    assert [len(b) for b in c.calendar.batches] == [50, 50, 20]


def test_events_become_google_tasks_of_the_list(gt):
    c = make_controller(gt, [[event("e1", "Alpha", description="note"), event("e2", "Beta", "2026-01-05")]],
                        tasklist="Work")
    assert c.import_inbox_calendar() == 2
    assert c.tasks_service.created == [
        ("L1", {"title": "Alpha", "notes": "note", "due": f"{TODAY}T00:00:00.000Z"}),
        ("L1", {"title": "Beta", "due": "2026-01-05T00:00:00.000Z"})]
    page = c.notebook.page.parsed
    # the date even when the dates are not included
    assert f"|💼]] Alpha _{{//<{TODAY}//}}\nnote\n" in page and "|💼]] Beta _{//<2026-01-05//}\n" in page
    assert c.cache.items_ids == {"t0", "t1"}  # the import of the list will not bring them again
    assert c.cache.undated_ids == set()
    assert c.calendar.batches == [["e1", "e2"]]
    assert c.cache.inbox_tasks == {}  # events deleted, nothing to remember


def test_future_event_just_passes_to_google_tasks(gt):
    """ Not put into the page now, the import of the list brings it when due. """
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Beta", LATER)]], tasklist="Work")
    assert c.import_inbox_calendar() == 2
    assert [body["due"] for _l, body in c.tasks_service.created] == [f"{TODAY}T00:00:00.000Z",
                                                                     f"{LATER}T00:00:00.000Z"]
    assert "Alpha" in c.notebook.page.parsed and "Beta" not in c.notebook.page.parsed
    assert c.cache.items_ids == {"t0"}
    assert c.calendar.batches == [["e1", "e2"]]


def test_future_event_of_a_list_not_imported_goes_to_the_page(gt):
    """ The import does not read the list, it would never bring it. """
    c = make_controller(gt, [[event("e1", "Beta", LATER)]], tasklist="Other")
    c.cache.lists["Other"] = "L2"
    assert c.import_inbox_calendar() == 1
    assert f"Beta _{{//<{LATER}//}}" in c.notebook.page.parsed
    assert c.cache.items_ids == {"t0"}


@pytest.mark.parametrize("chosen, target, passes", [
    ("@default", "@default", True),
    ("My Tasks", "@default", True),  # My Tasks is the default list, first in the cache
    ("@default", "My Tasks", True),
    ("Work", "@default", False),
])
def test_default_list_is_known_under_its_title(gt, chosen, target, passes):
    c = make_controller(gt, [[event("e1", "Beta", LATER)]], tasklist=target)
    c.preferences["tasklist"] = chosen
    c.import_inbox_calendar()
    assert ("Beta" not in (c.notebook.page.parsed or "")) == passes


def test_failed_delete_makes_neither_task_nor_line_twice(gt):
    """ The task is in the page already, its event stayed in the calendar. """
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Beta")]], tasklist="Work",
                        delete_errors={"e1": gt.Error("500")})
    assert c.import_inbox_calendar() == 2
    assert c.failed
    assert list(c.cache.inbox_tasks) == ["e1"] and c.cache.inbox_tasks["e1"]["in_page"]
    c.calendar.pages, c.calendar.delete_errors = [[event("e1", "Alpha")]], {}
    assert c.import_inbox_calendar() == 1
    assert len(c.tasks_service.created) == 2
    assert c.notebook.page.parsed.count("Alpha") == 1
    assert c.calendar.batches[-1] == ["e1"]
    assert c.cache.inbox_tasks == {}


def test_event_gone_meanwhile_is_forgotten(gt):
    c = make_controller(gt, [[event("e2", "Beta")]], tasklist="Work")
    c.cache.inbox_tasks = {"e1": {"id": "old", "title": "Alpha"}}
    assert c.import_inbox_calendar() == 1
    assert c.cache.inbox_tasks == {}


def test_repeated_events_are_left_in_the_calendar(gt):
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Gym", recurrence=["RRULE:FREQ=WEEKLY"]),
                              event("e3_20261030", "Gym moved", recurringEventId="e3")]])
    assert c.import_inbox_calendar() == 1
    assert "Gym" not in c.notebook.page.parsed
    assert c.calendar.batches == [["e1"]]
    assert c.failed  # reported


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

    c.api.get_inbox_calendar_service = not_granted
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


def test_busy_notebook_with_google_tasks_keeps_the_events_for_the_page(gt):
    """ Their tasks exist on the server already, they come into the page next time without being made again.
        The future ones go, the import brings them. """
    c = make_controller(gt, [[event("e1", "Alpha"), event("e2", "Beta", LATER)]], tasklist="Work")
    c.notebook.operation = types.SimpleNamespace(message="Updating index")
    assert c.import_inbox_calendar() is None
    assert c.calendar.batches == [["e2"]]
    assert c.cache.items_ids == set()
    assert list(c.cache.inbox_tasks) == ["e1"]
    assert c.failed

    c.notebook.operation = None
    c.calendar.pages = [[event("e1", "Alpha")]]
    assert c.import_inbox_calendar() == 1
    assert len(c.tasks_service.created) == 2  # Alpha once, Beta
    assert "Alpha" in c.notebook.page.parsed
    assert c.cache.items_ids == {"t0"}
    assert c.cache.inbox_tasks == {}


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

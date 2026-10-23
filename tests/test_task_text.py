import datetime

import pytest


def test_get_task_text_basic(gt):
    text = gt.GoogletasksController.get_task_text({"id": "abc", "title": "Buy milk"})
    assert text == "[ ] [[gtasks://abc|✋]] Buy milk"


def test_get_task_text_completed(gt):
    text = gt.GoogletasksController.get_task_text({"id": "abc", "title": "x", "completed": "2020-01-01T00:00:00.000Z"})
    assert text.startswith("[*] [[gtasks://abc|")


def test_get_task_text_without_id_has_no_anchor(gt):
    assert gt.GoogletasksController.get_task_text({"title": "x"}) == "[ ] x"


def test_get_task_text_keeps_markup(gt):
    title = "see [[Some:Page]] and **bold** //it// [[https://x.cz|link]]"
    assert gt.GoogletasksController.get_task_text({"id": "1", "title": title}).endswith(" " + title)


def test_get_task_text_strips_doubled_checkbox(gt):
    text = gt.GoogletasksController.get_task_text({"id": "1", "title": "[ ] real title"})
    assert text == "[ ] [[gtasks://1|✋]] real title"


def test_get_task_text_notes_on_following_lines(gt):
    text = gt.GoogletasksController.get_task_text({"id": "1", "title": "t", "notes": "line1\nline2"})
    assert text.split("\n") == ["[ ] [[gtasks://1|✋]] t", "line1", "line2"]


def test_get_task_text_missing_title(gt):
    assert gt.GoogletasksController.get_task_text({"id": "1"}) is False


def test_get_task_text_include_due(gt):
    task = {"id": "1", "title": "t", "due": "2021-03-04T00:00:00.000Z"}
    assert gt.GoogletasksController.get_task_text(task, include_due=True).endswith("t _{//>2021-03-04//}")
    assert ">" not in gt.GoogletasksController.get_task_text(task)


def test_get_task_text_include_due_without_due(gt):
    assert gt.GoogletasksController.get_task_text({"id": "1", "title": "t"}, include_due=True).endswith(" t")


def test_anchor_regex_roundtrip(gt):
    task = {"id": "Xy_12-3", "title": "Do [[a]] thing", "notes": "note"}
    line = gt.GoogletasksController.get_task_text(task).split("\n")[0]
    m = gt.taskAnchorTreeRe.match(line)
    assert m[1] == "[ ] "
    assert gt.parse_task_href(m[2]) == (None, task["id"])
    assert m[4] == task["title"]


def test_anchor_regex_completed_and_without_checkbox(gt):
    m = gt.taskAnchorTreeRe.match("[*] [[gtasks://a1|✋]] done")
    assert (m[1], gt.parse_task_href(m[2]), m[3], m[4]) == ("[*] ", (None, "a1"), "✋", "done")
    m = gt.taskAnchorTreeRe.match("[[https://tasks.google.com/task/IaMNd3-6tsOcoFVm|✋]] bare")
    assert (m[1], gt.parse_task_href(m[2]), m[4]) == (None, (None, "SWFNTmQzLTZ0c09jb0ZWbQ"), "bare")
    assert gt.taskAnchorTreeRe.match("[ ] ordinary checkbox") is None
    assert gt.taskAnchorTreeRe.match("[ ] [[https://example.com|✋]] other link") is None


def test_get_task_text_with_list_and_icon(gt):
    text = gt.GoogletasksController.get_task_text({"id": "abc", "title": "Report"}, list_id="L1", icon="💼")
    assert text == "[ ] [[gtasks://L1/abc|💼]] Report"  # "abc" is no real ID, no web link can be made of it
    m = gt.taskAnchorTreeRe.match(text)
    assert (gt.parse_task_href(m[2]), m[3], m[4]) == (("L1", "abc"), "💼", "Report")


def test_anchor_regex_default_list(gt):
    m = gt.taskAnchorTreeRe.match("[ ] [[gtasks://@default/abc|✋]] x")
    assert gt.parse_task_href(m[2]) == ("@default", "abc")


def test_start_date_in_title_roundtrip(gt):
    task = {"id": "1", "title": "test 13", "due": "2020-06-03T00:00:00.000Z"}
    line = gt.GoogletasksController.get_task_text(task, include_due=True)
    title = gt.taskAnchorTreeRe.match(line)[4]
    m = gt.start_date_in_title.match(title)
    assert m[2] == "2020-06-03"
    assert (m[1] + m[4]).replace("_{////}", "").rstrip() == "test 13"


def test_start_date_in_title_month_only_and_absent(gt):
    assert gt.start_date_in_title.match("x >2021-05 y")[2] == "2021-05"
    assert gt.start_date_in_title.match("no date here") is None


@pytest.fixture
def get_time_fn(gt):
    return gt.GoogletasksController.get_time


def test_get_time_modes_from_string(get_time_fn):
    kw = {"from_string": "2021-05-06T12:34:56"}
    assert get_time_fn(mode="date-only", **kw) == "2021-05-06"
    assert get_time_fn(mode="midnight", **kw) == "2021-05-06T00:00:00.000Z"
    assert get_time_fn(mode="morning", **kw) == "2021-05-06T08:00:00.000Z"
    assert get_time_fn(mode="last-sec", **kw) == "2021-05-06T23:59:59.999Z"
    assert get_time_fn(mode="day", **kw) == "th"  # Thursday
    assert get_time_fn(mode="object", **kw) == datetime.datetime(2021, 5, 6, 12, 34, 56)
    assert get_time_fn(**kw) == "2021-05-06T12:34:56"


def test_get_time_partial_date_defaults_to_first(get_time_fn):
    assert get_time_fn(mode="date-only", from_string="2021-05") == "2021-05-01"


def test_get_time_add_days(get_time_fn):
    assert get_time_fn(mode="date-only", from_string="2021-01-31", add_days=1) == "2021-02-01"
    assert get_time_fn(mode="date-only", from_string="2021-03-01", add_days=-1) == "2021-02-28"


def test_get_time_use_date_wins(get_time_fn):
    d = datetime.datetime(2030, 2, 3)
    assert get_time_fn(mode="date-only", use_date=d, from_string="2021-01-01") == "2030-02-03"


def test_get_time_relative_to_now(get_time_fn):
    today = datetime.date.today()
    assert get_time_fn(mode="date-only") == today.isoformat()
    assert get_time_fn(mode="date-only", add_days=3) == (today + datetime.timedelta(days=3)).isoformat()


def test_get_time_past_dates_false(get_time_fn):
    today = datetime.date.today()
    assert get_time_fn(mode="date-only", add_days=-1) == (today - datetime.timedelta(days=1)).isoformat()
    with pytest.raises(ValueError):
        get_time_fn(mode="date-only", add_days=-1, past_dates=False)
    with pytest.raises(ValueError):
        get_time_fn(mode="date-only", from_string="2001-01-01", past_dates=False)
    # today and the future are fine
    assert get_time_fn(mode="date-only", past_dates=False) == today.isoformat()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    assert get_time_fn(mode="date-only", add_days=1, past_dates=False) == tomorrow


def test_get_time_unknown_mode_falls_back_to_isoformat(get_time_fn):
    assert get_time_fn(mode="bogus", from_string="2021-05-06T01:02:03") == "2021-05-06T01:02:03"


@pytest.mark.parametrize("surface, icon", [("DOCUMENT", "📄"), ("SPACE", "💬"), ("OTHER", "🔗")])
def test_get_task_text_assigned_links_its_source(gt, surface, icon):
    task = {"id": "abc", "title": "Fix the table", "due": "2021-01-01T00:00:00.000Z",
            "assignmentInfo": {"linkToTask": "https://docs.google.com/document/d/D/edit?disco=X",
                               "surfaceType": surface}}
    text = gt.GoogletasksController.get_task_text(task, include_due=True, list_id="L1")
    assert text == f"[ ] [[gtasks://L1/abc|✋]][[https://docs.google.com/document/d/D/edit?disco=X|{icon}↗]]" \
                   " Fix the table _{//>2021-01-01//}"
    m = gt.taskAnchorTreeRe.match(text)
    assert (gt.parse_task_href(m[2]), m[3]) == (("L1", "abc"), "✋")
    assert m[4].startswith("[[https://docs.google.com/")


def test_get_task_text_links_email_and_keep(gt):
    task = {"id": "abc", "title": "Bill", "links": [
        {"type": "email", "description": "Bill", "link": "https://mail.google.com/mail/#all/1"},
        {"type": "keep_note", "link": "https://keep.google.com/u/0/#NOTE/2"},
        {"type": "future_thing", "link": "https://example.com/3"}]}
    assert gt.GoogletasksController.get_task_text(task) == (
        "[ ] [[gtasks://abc|✋]][[https://mail.google.com/mail/#all/1|📧↗]]"
        "[[https://keep.google.com/u/0/#NOTE/2|📝↗]][[https://example.com/3|🔗↗]] Bill")


@pytest.mark.parametrize("arrow", ["↗", ""])  # without the arrow: written by the first version of the feature
def test_source_links_are_cut_from_the_title(gt, arrow):
    title = f"[[https://docs.google.com/document/d/D/edit|📄{arrow}]]" \
            f"[[https://mail.google.com/mail/#all/1|📧{arrow}]] Fix the table [[https://example.com|web]]"
    links = [(m[1], m[2]) for m in gt.sourceLinkRe.finditer(title)]
    assert links == [("https://docs.google.com/document/d/D/edit", "📄"), ("https://mail.google.com/mail/#all/1", "📧")]
    assert gt.sourceLinkRe.sub("", title).strip() == "Fix the table [[https://example.com|web]]"
    text = gt.GoogletasksController.get_task_text({"title": "Fix the table", "_source_links": links})
    assert text == "[ ] [[https://docs.google.com/document/d/D/edit|📄↗]][[https://mail.google.com/mail/#all/1|📧↗]]" \
                   " Fix the table"


# real IDs: the API one is the URL-safe base64 of the one in the web link
NEW_ID, NEW_WEB = "SWFNTmQzLTZ0c09jb0ZWbQ", "IaMNd3-6tsOcoFVm"
OLD_ID, OLD_WEB = "MDY3ODI1MjUyNDk3ODIyNTAyODI6MDoyODgyNTYyODQxNDk4NTQ4", "06782525249782250282:0:2882562841498548"


@pytest.mark.parametrize("task_id, web_id", [(NEW_ID, NEW_WEB), (OLD_ID, OLD_WEB)])
@pytest.mark.parametrize("list_id", ["MDY3ODI1MjUyNDk3ODIyNTAyODI6MDow", "@default", None])
def test_task_href_links_to_google_tasks(gt, task_id, web_id, list_id):
    href = gt.task_href(task_id, list_id)
    assert href.startswith("https://tasks.google.com/task/" + web_id)
    assert ("?list=" in href) == bool(list_id)
    assert gt.parse_task_href(href) == (list_id, task_id)
    # webViewLink given by the server leads to the same
    assert gt.task_href(task_id, list_id, f"https://tasks.google.com/task/{web_id}?sa=6") == href


@pytest.mark.parametrize("task_id", ["abc", "a1", "1", "x"])
def test_task_href_falls_back_to_gtasks_for_odd_ids(gt, task_id):
    assert gt.task_href(task_id, "L") == f"gtasks://L/{task_id}"
    assert gt.parse_task_href(f"gtasks://L/{task_id}") == ("L", task_id)


def test_parse_task_href_ignores_other_links(gt):
    assert gt.parse_task_href("https://docs.google.com/document/d/D") is None
    assert gt.parse_task_href("gtasks://abc") == (None, "abc")


def test_get_task_text_anchor_is_a_web_link(gt):
    task = {"id": NEW_ID, "title": "Bill", "webViewLink": f"https://tasks.google.com/task/{NEW_WEB}?sa=6",
            "links": [{"type": "email", "link": "https://mail.google.com/mail/#all/1"}]}
    text = gt.GoogletasksController.get_task_text(task, list_id="L1", icon="💼")
    assert text == f"[ ] [[https://tasks.google.com/task/{NEW_WEB}?list=L1|💼]]" \
                   "[[https://mail.google.com/mail/#all/1|📧↗]] Bill"
    m = gt.taskAnchorTreeRe.match(text)
    assert (gt.parse_task_href(m[2]), m[3]) == (("L1", NEW_ID), "💼")
    assert gt.sourceLinkRe.sub("", m[4]).strip() == "Bill"

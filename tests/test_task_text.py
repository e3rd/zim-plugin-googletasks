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
    assert m[2] is None
    assert m[3] == task["id"]
    assert m[5] == task["title"]


def test_anchor_regex_completed_and_without_checkbox(gt):
    m = gt.taskAnchorTreeRe.match("[*] [[gtasks://a1|✋]] done")
    assert (m[1], m[2], m[3], m[4], m[5]) == ("[*] ", None, "a1", "✋", "done")
    m = gt.taskAnchorTreeRe.match("[[gtasks://a1|✋]] bare")
    assert (m[1], m[3], m[5]) == (None, "a1", "bare")
    assert gt.taskAnchorTreeRe.match("[ ] ordinary checkbox") is None


def test_get_task_text_with_list_and_icon(gt):
    text = gt.GoogletasksController.get_task_text({"id": "abc", "title": "Report"}, list_id="L1", icon="💼")
    assert text == "[ ] [[gtasks://L1/abc|💼]] Report"
    m = gt.taskAnchorTreeRe.match(text)
    assert (m[2], m[3], m[4], m[5]) == ("L1", "abc", "💼", "Report")


def test_anchor_regex_default_list(gt):
    m = gt.taskAnchorTreeRe.match("[ ] [[gtasks://@default/abc|✋]] x")
    assert (m[2], m[3]) == ("@default", "abc")


def test_start_date_in_title_roundtrip(gt):
    task = {"id": "1", "title": "test 13", "due": "2020-06-03T00:00:00.000Z"}
    line = gt.GoogletasksController.get_task_text(task, include_due=True)
    title = gt.taskAnchorTreeRe.match(line)[5]
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

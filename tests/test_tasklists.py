import pytest


@pytest.mark.parametrize("pref, expected", [
    ("", [("@default", "✋")]),
    (None, [("@default", "✋")]),
    (" , ", [("@default", "✋")]),
    ("Work", [("Work", "✋")]),  # a single title, as written before multiple lists were supported
    ("My Tasks, Work=💼", [("My Tasks", "✋"), ("Work", "💼")]),
    (" A ,B , C ", [("A", "✋"), ("B", "💼"), ("C", "🏠")]),
    ("A=💼, B", [("A", "💼"), ("B", "✋")]),  # the palette skips the icons already used
    ("A, A=💼", [("A", "✋")]),  # the first mention wins
    ("A=[|x]", [("A", "x")]),  # characters that would break the link label are dropped
])
def test_parse_tasklists(gt, pref, expected):
    assert gt.parse_tasklists(pref) == expected


def test_parse_tasklists_runs_out_of_icons(gt):
    lists = gt.parse_tasklists(",".join(str(i) for i in range(len(gt.LIST_ICONS) + 2)))
    assert [icon for _t, icon in lists][:len(gt.LIST_ICONS)] == list(gt.LIST_ICONS)
    assert lists[-1][1] == gt.TASK_ANCHOR_SYMBOL


def test_format_tasklists_roundtrip(gt):
    entries = [("My Tasks", "✋"), ("Work", "💼")]
    assert gt.format_tasklists(entries) == "My Tasks=✋, Work=💼"
    assert gt.parse_tasklists(gt.format_tasklists(entries)) == entries
    assert gt.format_tasklists([("A", "")]) == "A"


def make_controller(gt, pref, lists):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.preferences = {"tasklist": pref}
    controller.cache = type("C", (), {"lists": dict(lists), "icons": {}, "load": lambda self: self,
                                      "save": lambda self: None})()
    controller.failed = False
    return controller


def test_chosen_lists_names_the_default_list(gt):
    c = make_controller(gt, "", {"My Tasks": "D", "Work": "W"})
    assert c.chosen_lists() == {"My Tasks": "✋"}
    assert make_controller(gt, "", {}).chosen_lists() == {"@default": "✋"}


def test_toggle_list_adds_and_removes_keeping_icons(gt):
    c = make_controller(gt, "", {"My Tasks": "D", "Work": "W", "Home": "H"})
    c.toggle_list("Work", True)
    assert c.preferences["tasklist"] == "My Tasks=✋, Work=💼"
    c.toggle_list("Home", True)
    c.toggle_list("Work", False)
    assert c.preferences["tasklist"] == "My Tasks=✋, Home=🏠"  # Home does not take over Work's icon


def test_toggle_list_brings_the_icon_back(gt):
    c = make_controller(gt, "My Tasks, Work=🔧", {"My Tasks": "D", "Work": "W", "Home": "H"})
    c.toggle_list("Work", False)
    c.toggle_list("Work", True)
    assert c.preferences["tasklist"] == "My Tasks=✋, Work=🔧"
    c.toggle_list("Work", False)
    c.preferences["tasklist"] = "My Tasks=✋, Home=🔧"  # edited in the Properties meanwhile
    c.toggle_list("Work", True)
    assert c.preferences["tasklist"] == "My Tasks=✋, Home=🔧, Work=💼"  # the remembered icon is taken


def test_toggle_list_keeps_the_last_one(gt):
    c = make_controller(gt, "Work", {"Work": "W"})
    c.toggle_list("Work", False)
    assert c.preferences["tasklist"] == "Work"


def test_list_id_and_icon(gt):
    c = make_controller(gt, "My Tasks, Work=💼", {"My Tasks": "D", "Work": "W"})
    assert c.primary_list_id == "D"
    assert c.list_id("@default") == "@default"
    assert c.list_icon("W") == "💼"
    assert c.list_icon("unknown") == "✋"
    with pytest.raises(LookupError):
        c.refresh_task_lists = lambda: None
        c.list_id("Gone")

"""_set_bullets: the status sync un/checks the tasks by the server, but not a checkbox the user changed meanwhile."""
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
    c._set_bullets({"d2ViMQ": True}, before)
    assert c.notebook.page.parsed == f"Home\n[ ] [[{HREF}|💼]] Call Bob\n"

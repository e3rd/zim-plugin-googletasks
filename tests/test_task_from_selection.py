"""read_task_from_selection: a Zim line turned into a task for the dialog."""
import types

import pytest


def read(gt, monkeypatch, line, include_start_date=False):
    c = object.__new__(gt.GoogletasksController)
    c.preferences = {"include_start_date": include_start_date}
    buffer = types.SimpleNamespace(get_selection_bounds=lambda: (0, 1), get_parsetree=lambda bounds: None,
                                   delete=lambda *bounds: None)
    monkeypatch.setattr(gt, "get_dumper", lambda _format: types.SimpleNamespace(dump=lambda tree: [line]),
                        raising=False)
    return c.read_task_from_selection(buffer)


@pytest.mark.parametrize("line, title, due", [
    ("[ ] 📥 14:00 Dentist _{//<2026-11-11//}\n", "14:00 Dentist", "2026-11-11"),
    ("[ ] 📥 Dentist _{//>2026-11-11 <2026-11-12//}\n", "Dentist", "2026-11-12"),  # due rather than start
    ("[*] Dentist _{//>2026-11-11//}\n", "Dentist", "2026-11-11"),
    ("Dentist\n", "Dentist", None),
    ("Dentist -> clinic\n", "Dentist -> clinic", None),
])
def test_plain_line_gives_its_date_to_the_dialog(gt, monkeypatch, line, title, due):
    task = read(gt, monkeypatch, line)
    assert task["title"] == title
    assert task.get("due") == due


def test_google_task_line_gives_its_date_to_the_dialog(gt, monkeypatch):
    task = read(gt, monkeypatch, "[ ] [[https://tasks.google.com/task/YQ?list=L1|💼]] 14:00 Dentist"
                                 " _{//<2026-11-11//}\nnote\n")
    assert (task["_list_id"], task["title"], task["due"]) == ("L1", "14:00 Dentist", "2026-11-11")


def test_bare_start_date_needs_the_preference(gt, monkeypatch):
    assert "due" not in read(gt, monkeypatch, "Dentist >2026-11-11\n")
    task = read(gt, monkeypatch, "Dentist >2026-11-11\n", include_start_date=True)
    assert (task["title"], task["due"]) == ("Dentist", "2026-11-11")

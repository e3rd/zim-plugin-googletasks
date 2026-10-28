"""ends_task: where the task selected by Ctrl+Alt+Shift+G (send_as_task) ends."""
import pytest


class Buffer:
    """ Lines of (text, bullet, heading) """

    def __init__(self, lines):
        self.lines = lines

    def get_line_count(self):
        return len(self.lines)

    def get_bullet(self, line):
        return self.lines[line][1]

    def get_iter_at_line(self, line):
        return line

    def get_heading_text(self, line):
        return self.lines[line][0] if self.lines[line][2] else None


@pytest.fixture
def ends(gt, monkeypatch):
    monkeypatch.setattr(gt, "_line_text", lambda buffer, line: buffer.lines[line][0])
    return lambda line: gt.ends_task(Buffer([
        ("￼ 📥 Call Bob", "unchecked", False),  # 0
        ("about the roof", None, False),  # 1
        ("• and the bill", "*", False),  # 2 bullets are notes of the task
        ("￼ Zim's own task", "unchecked", False),  # 3
        ("￼ [[link|💼]] task of another list", "checked", False),  # 4
        ("Heading", None, True),  # 5
        ("", None, False),  # 6
        ("old task ✋ without a checkbox", None, False),  # 7
    ]), line)


@pytest.mark.parametrize("line, expected", [
    (1, False), (2, False), (3, True), (4, True), (5, True), (6, True), (7, True), (8, True),
])
def test_task_ends_at_checkbox_heading_or_blank_line(ends, line, expected):
    assert ends(line) is expected

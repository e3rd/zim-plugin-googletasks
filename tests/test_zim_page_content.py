from zim_page_content import insert_after_first_blank_line


def test_inserts_after_first_blank_line():
    lines = ["Title\n", "\n", "first\n", "\n", "second\n"]
    assert insert_after_first_blank_line(lines, "NEW\n") == ["Title\n", "\n", "NEW\n", "first\n", "\n", "second\n"]


def test_whitespace_only_line_counts_as_blank():
    assert insert_after_first_blank_line(["a\n", "  \n", "b\n"], "NEW\n") == ["a\n", "  \n", "NEW\n", "b\n"]


def test_appends_when_no_blank_line():
    assert insert_after_first_blank_line(["a\n", "b\n"], "NEW\n") == ["a\n", "b\n", "NEW\n"]


def test_empty_page():
    assert insert_after_first_blank_line([], "NEW\n") == ["NEW\n"]


def test_does_not_mutate_input():
    lines = ["a\n", "\n"]
    insert_after_first_blank_line(lines, "NEW\n")
    assert lines == ["a\n", "\n"]

def make(gt, window=None):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = window
    controller.failed = False
    return controller


def test_headless_error_goes_to_stderr_and_marks_failure(gt, capsys):
    c = make(gt)
    c.error("Boom", ValueError("detail"))
    assert c.failed
    assert "Boom: detail" in capsys.readouterr().err


def test_gui_error_uses_statusbar(gt, capsys):
    pushed = []
    window = type("W", (), {"statusbar": type("S", (), {"push": lambda self, i, t: pushed.append(t)})()})()
    c = make(gt, window)
    c.error("Boom")
    assert pushed == ["[Googletasks] Boom"]
    assert capsys.readouterr().err == ""


def test_gui_fatal_or_no_statusbar_opens_dialog(gt, monkeypatch):
    shown = []

    class Dialog:
        def __init__(self, parent, error):
            shown.append(error)

        def run(self):
            pass

    monkeypatch.setattr(gt, "ErrorDialog", Dialog)
    make(gt, object()).error("No bar")
    statusbar = type("S", (), {"push": lambda self, i, t: None})()
    make(gt, type("W", (), {"statusbar": statusbar})()).error("Fatal", RuntimeError("x"), fatal=True)
    assert shown == [("Google Tasks: No bar", ""), ("Google Tasks: Fatal", "x")]

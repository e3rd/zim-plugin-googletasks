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


def http_error(gt, status, body):
    resp = type("Resp", (), {"status": status, "reason": "Forbidden"})()
    return gt.HttpError(resp, body.encode())


def test_disabled_api_is_explained_with_the_activation_link(gt, capsys):
    url = "https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=123"
    body = ('{"error": {"code": 403, "message": "Google Calendar API has not been used in project 123 before or it is'
            ' disabled. Enable it by visiting ' + url + ' then retry.", "status": "PERMISSION_DENIED", "details": ['
            '{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",'
            ' "metadata": {"serviceTitle": "Google Calendar API", "activationUrl": "' + url + '"}}]}}')
    assert gt.disabled_api_message(http_error(gt, 403, body)) == (
        f"Google Calendar API is not enabled in the Google Cloud project of the OAuth client secret."
        f" Enable it at {url} and try again in a few minutes.")
    # older format, without details: the link is taken from the message
    old = ('{"error": {"errors": [{"reason": "accessNotConfigured"}],'
           ' "message": "Enable it by visiting ' + url + ' then"}}')
    assert url in gt.disabled_api_message(http_error(gt, 403, old))
    make(gt).error("Cannot create the inbox calendar", http_error(gt, 403, body))
    assert "Google Calendar API is not enabled" in capsys.readouterr().err


def test_other_errors_are_kept(gt):
    denied = '{"error": {"message": "The caller does not have permission"}}'
    assert gt.disabled_api_message(http_error(gt, 403, denied)) is None
    assert gt.disabled_api_message(http_error(gt, 500, '{"error": {"reason": "SERVICE_DISABLED"}}')) is None
    assert gt.disabled_api_message(ValueError("x")) is None

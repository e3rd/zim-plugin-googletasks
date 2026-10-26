"""Worker and on_main: the jobs talking to Google run in a thread, GTK is touched in the main thread only.

The GTK main loop is replaced by a queue that the test (being the main thread) pumps."""
import queue
import threading
import time

import pytest


class MainLoop:
    def __init__(self):
        self.calls = queue.Queue()

    def idle_add(self, fn, *args):
        self.calls.put((fn, args))

    def pump(self, until, timeout=5):
        """ Run the idle callbacks in this (the main) thread until `until()` holds. """
        deadline = time.monotonic() + timeout
        while not until():
            assert time.monotonic() < deadline, "timed out"
            try:
                fn, args = self.calls.get(timeout=.05)
            except queue.Empty:
                continue
            fn(*args)


class Window:
    def __init__(self):
        self.title = "Home - Notes"
        self.titles = []

    def get_title(self):
        return self.title

    def set_title(self, title):
        self.title = title
        self.titles.append(title)


@pytest.fixture
def loop(gt, monkeypatch):
    loop = MainLoop()
    monkeypatch.setattr(gt.GLib, "idle_add", loop.idle_add, raising=False)
    return loop


def test_on_main_in_main_thread_calls_directly(gt):
    assert gt.on_main(lambda a, b: a + b, 1, 2) == 3


def test_on_main_from_worker_runs_in_main_thread(gt, loop):
    seen = {}

    def job():
        seen["result"] = gt.on_main(lambda: threading.current_thread() is threading.main_thread())

    thread = threading.Thread(target=job)
    thread.start()
    loop.pump(lambda: "result" in seen)
    thread.join()
    assert seen["result"] is True


def test_on_main_hands_the_exception_over(gt, loop):
    seen = {}

    def boom():
        raise KeyError("x")

    def job():
        try:
            gt.on_main(boom)
        except KeyError as e:
            seen["error"] = e

    threading.Thread(target=job).start()
    loop.pump(lambda: "error" in seen)


def test_jobs_run_in_order_off_the_main_thread_and_title_shows_it(gt, loop):
    window = Window()
    worker = gt.Worker(window, on_error=None)
    ran = []
    release = threading.Event()
    worker.submit(lambda: (release.wait(5), ran.append(("a", threading.current_thread().name))))
    worker.submit(lambda: ran.append(("b", threading.current_thread().name)))
    assert window.title == "Home - Notes" + gt.BUSY_SUFFIX  # set at once, before the jobs run
    release.set()
    loop.pump(lambda: worker.count == 0)
    assert ran == [("a", "googletasks"), ("b", "googletasks")]
    assert window.title == "Home - Notes"


def test_named_job_is_not_queued_twice(gt, loop):
    worker = gt.Worker(Window(), on_error=None)
    release = threading.Event()
    ran = []
    assert worker.submit(lambda: (release.wait(5), ran.append(1)), "import")
    assert not worker.submit(lambda: ran.append(2), "import")
    assert worker.submit(lambda: ran.append(3))  # unnamed ones always queue
    release.set()
    loop.pump(lambda: worker.count == 0)
    assert ran == [1, 3]
    assert worker.submit(lambda: ran.append(4), "import")  # finished, may run again
    loop.pump(lambda: worker.count == 0)
    assert ran == [1, 3, 4]


def test_unexpected_exception_is_reported_and_worker_goes_on(gt, loop):
    errors = []
    worker = gt.Worker(Window(), on_error=errors.append)
    ran = []
    worker.submit(lambda: 1 / 0)
    worker.submit(lambda: ran.append(1))
    loop.pump(lambda: worker.count == 0)
    assert isinstance(errors[0], ZeroDivisionError)
    assert ran == [1]


def test_controller_without_window_runs_job_at_once(gt):
    controller = object.__new__(gt.GoogletasksController)
    controller.worker = None
    ran = []
    assert controller.background(lambda: ran.append(1), "import")
    assert ran == [1]


def test_error_from_worker_goes_to_main_thread(gt, loop):
    controller = object.__new__(gt.GoogletasksController)
    controller.window = None
    controller.failed = False
    seen = {}

    def job():
        controller.error("network down")
        seen["done"] = True

    threading.Thread(target=job).start()
    loop.pump(lambda: "done" in seen)
    assert controller.failed

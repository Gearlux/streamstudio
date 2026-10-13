"""One editor, many server threads: its requests take turns, and a host driving it from its own routes waits its turn.

A host serves the editor's page and its own routes from a THREADED server, so the panel's editor requests and the
host's own request (a history pick writing a tool's settings) reach the one :class:`streamstudio.app.Studio` at once.
Measured before 2026-10-13: a pick racing the panel's load died in the editor's graph list —
``RuntimeError: dictionary changed size during iteration`` at ``Studio._scan`` — about one run in fifteen of the
host's page test. Every editor request now holds :attr:`Studio.lock` from its first hook to its teardown; a host holds
the same lock across a read-change-save of its own; :meth:`Studio.stash` takes it too.

Each test proves an ABSENCE (a request that must not get in) by waiting :data:`ABSENT` for it — never a sleep.
"""

import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest
from test_host import PANE_TEXT, PREFIX, FakeHost, host_app

from streamstudio.app import SESSION_HEADER, studio_of

#: How long a test waits to see that a request does NOT get in: it either is let in (the test fails at once), or the
#: wait runs out — the editor's turn-taking holds it for as long as the other side keeps the lock.
ABSENT = 0.5
#: How long a request that SHOULD finish gets — far above any request here, so only a deadlock reaches it.
FINISH = 30.0


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """The host's folder, holding one graph."""
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "pane.yaml").write_text(PANE_TEXT)
    return folder


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A built page — the editor's page blueprint serves it beside the API."""
    built = tmp_path / "dist"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html><title>studio</title>")
    return built


class GatedHost(FakeHost):
    """A host whose ``shipped()`` — asked inside the editor's scan of its graph list — logs which thread is in it.
    The thread named ``first`` stays in until another thread comes in too, or :data:`ABSENT` runs out."""

    def __init__(self, folder: Path) -> None:
        super().__init__(folder)
        self.log: List[str] = []
        self.first_inside = threading.Event()
        self.other_inside = threading.Event()

    def shipped(self) -> Mapping[str, Path]:
        name = threading.current_thread().name
        self.log.append(name)
        if name == "first":
            self.first_inside.set()
            self.other_inside.wait(ABSENT)
        else:
            self.other_inside.set()
        self.log.append(name)
        return {}


def _request(app: Any, answers: Dict[str, int], url: str = f"{PREFIX}/api/graphs") -> None:
    answers[threading.current_thread().name] = app.test_client().get(url).status_code


def _thread(name: str, target: Any, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def _others_can_take(lock: Any) -> bool:
    """Whether ANOTHER thread can take ``lock`` now (the lock is reentrant: the holder itself always can)."""
    taken: List[bool] = []

    def attempt() -> None:
        got = lock.acquire(blocking=False)
        taken.append(got)
        if got:
            lock.release()

    _thread("probe", attempt).join(FINISH)
    return taken == [True]


class TestRequestsTakeTurns:
    def test_a_second_request_waits_until_the_first_is_answered(self, folder: Path, dist: Path) -> None:
        host = GatedHost(folder)
        app = host_app(host, dist)
        answers: Dict[str, int] = {}
        first = _thread("first", _request, app, answers)
        assert host.first_inside.wait(FINISH)
        second = _thread("second", _request, app, answers)
        first.join(FINISH)
        second.join(FINISH)
        assert answers == {"first": 200, "second": 200}
        assert not host.log[host.log.index("second") :].count("first"), f"interleaved: {host.log}"

    def test_CON_one_request_alone_is_answered_as_ever(self, folder: Path, dist: Path) -> None:
        host = GatedHost(folder)
        answers: Dict[str, int] = {}
        _request(host_app(host, dist), answers)
        assert answers == {"MainThread": 200}


class TestTheHostTakesItsTurn:
    def test_a_request_waits_while_the_host_holds_the_lock(self, folder: Path, dist: Path) -> None:
        host = GatedHost(folder)
        app = host_app(host, dist)
        answers: Dict[str, int] = {}
        with studio_of(app).lock:
            second = _thread("second", _request, app, answers)
            assert not host.other_inside.wait(ABSENT), "the request got in while the host held the editor"
        second.join(FINISH)
        assert answers == {"second": 200}

    def test_a_stash_waits_while_a_request_is_served(self, folder: Path, dist: Path) -> None:
        host = GatedHost(folder)
        app = host_app(host, dist)
        studio = studio_of(app)
        stashed: List[Tuple[str, List[str]]] = []
        with studio.lock:
            stash = _thread("stash", lambda: stashed.append(("stash", studio.stash())))
            stash.join(ABSENT)
            assert stash.is_alive() and not stashed, "the stash ran while the editor was held"
        stash.join(FINISH)
        assert stashed == [("stash", [])]


class TestTheLockIsLetGo:
    def test_after_an_answer(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        assert app.test_client().get(f"{PREFIX}/api/graphs").status_code == 200
        assert _others_can_take(studio_of(app).lock)

    def test_after_a_refusal_of_another_session(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        answer = app.test_client().get(f"{PREFIX}/api/graphs", headers={SESSION_HEADER: "an earlier editor"})
        assert answer.status_code == 409
        assert _others_can_take(studio_of(app).lock)

    def test_after_a_request_that_raised(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        assert app.test_client().get(f"{PREFIX}/api/graphs/nothing").status_code == 404
        assert _others_can_take(studio_of(app).lock)

    def test_CON_the_hosts_own_routes_never_take_it(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        with studio_of(app).lock:
            answers: Dict[str, int] = {}
            own = _thread("own", _request, app, answers, "/api/workspace")
            own.join(FINISH)
        assert answers == {"own": 200}

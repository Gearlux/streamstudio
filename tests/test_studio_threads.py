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

import streamstudio
import streamstudio.app as app_module
from streamstudio import catalogue as catalogue_module
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


#: How long a stalled call in the tests below stalls before it gives up — a request that waits behind it (the defect)
#: finishes only after this, by which time the stalled request has finished too.
STALL = 5.0


class Stall:
    """A stand-in for a slow call (the catalogue helper, a plugin's list, the host's record): it says it was entered,
    then waits for :attr:`release` (at most :data:`STALL`) and answers ``value``."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.entered.set()
        self.release.wait(STALL)
        return self.value


def _while_stalled(app: Any, stall: Stall, method: str, url: str) -> Tuple[bool, Dict[str, int]]:
    """Send ``method url`` and, while it stalls, ``GET /api/graphs``: answer whether the stalled request was STILL
    running when the list was answered, and every status."""
    answers: Dict[str, int] = {}

    def stalled() -> None:
        client = app.test_client()
        answer = client.post(url, json={}) if method == "POST" else client.get(url)
        answers["stalled"] = answer.status_code

    slow = _thread("stalled", stalled)
    assert stall.entered.wait(FINISH)
    _thread("list", _request, app, answers).join(FINISH)
    still_running = slow.is_alive()
    stall.release.set()
    slow.join(FINISH)
    return still_running, answers


def _trace(app: Any) -> int:
    status: int = app.test_client().post(f"{PREFIX}/api/graphs/pane/trace", json={}).status_code
    return status


class TestALongWaitHoldsNoTurn:
    """Measured 2026-10-13: the class catalogue's helper hung (a stale torch extension lock in a package it imports),
    and while ``GET /api/palette`` waited for it holding the editor, a host's settings panel, its knob saves, its tool
    runs and its history picks hung with it. A request that waits on something outside the editor takes no turn."""

    def test_the_catalogue(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        stall = Stall([])
        monkeypatch.setattr(catalogue_module.CATALOGUE, "entries", stall)
        still_running, answers = _while_stalled(host_app(FakeHost(folder), dist), stall, "GET", f"{PREFIX}/api/palette")
        assert still_running, "the list waited for the catalogue"
        assert answers == {"list": 200, "stalled": 200}

    def test_a_classs_settings(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        stall = Stall({})
        monkeypatch.setattr(catalogue_module.CATALOGUE, "spec", stall)
        url = f"{PREFIX}/api/palette/my_pkg.Thing"
        still_running, answers = _while_stalled(host_app(FakeHost(folder), dist), stall, "GET", url)
        assert still_running, "the list waited for a class's settings"
        assert answers == {"list": 200, "stalled": 200}

    def test_a_plugins_list(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        stall = Stall({"names": [], "reason": "stalled"})
        monkeypatch.setattr(app_module.lookups, "answer", stall)
        url = f"{PREFIX}/api/lookups/SOME_TAG"
        still_running, answers = _while_stalled(host_app(FakeHost(folder), dist), stall, "GET", url)
        assert still_running, "the list waited for a plugin's list"
        assert answers == {"list": 200, "stalled": 200}

    def test_a_datasets_splits(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        stall = Stall([])
        monkeypatch.setattr(streamstudio, "list_hf_dataset_splits", stall)
        url = f"{PREFIX}/api/hf/splits?path=some/dataset"
        still_running, answers = _while_stalled(host_app(FakeHost(folder), dist), stall, "GET", url)
        assert still_running, "the list waited for the Hub"
        assert answers == {"list": 200, "stalled": 200}

    def test_the_hosts_record_for_a_run(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        host = FakeHost(folder)
        stall = Stall(None)
        real = host.record

        def record(graph: str, path: Path) -> Any:
            stall(graph, path)
            return real(graph, path)

        monkeypatch.setattr(host, "record", record)
        url = f"{PREFIX}/api/graphs/pane/trace"
        still_running, answers = _while_stalled(host_app(host, dist), stall, "POST", url)
        assert still_running, "the list waited for the host's record"
        assert answers == {"list": 200, "stalled": 200}

    def test_a_run_itself(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        stall = Stall(None)
        real = app_module.Tracer.run

        def run(tracer: Any, record: Any, **kwargs: Any) -> Any:
            stall()
            return real(tracer, record, **kwargs)

        monkeypatch.setattr(app_module.Tracer, "run", run)
        url = f"{PREFIX}/api/graphs/pane/trace"
        still_running, answers = _while_stalled(host_app(FakeHost(folder), dist), stall, "POST", url)
        assert still_running, "the list waited for the run"
        assert answers == {"list": 200, "stalled": 200}

    def test_a_rerun(self, folder: Path, dist: Path, monkeypatch: Any) -> None:
        app = host_app(FakeHost(folder), dist)
        run_id = app.test_client().post(f"{PREFIX}/api/graphs/pane/trace", json={}).get_json()["run"]
        stall = Stall(None)
        real = app_module.Run.rerun

        def rerun(run: Any, node: str, params: Any) -> Any:
            stall()
            return real(run, node, params)

        monkeypatch.setattr(app_module.Run, "rerun", rerun)
        url = f"{PREFIX}/api/runs/{run_id}/rerun"

        def stalled() -> None:
            answer = app.test_client().post(url, json={"node": "gain", "params": {"gain": 3.0}})
            answers["stalled"] = answer.status_code

        answers: Dict[str, int] = {}
        slow = _thread("stalled", stalled)
        assert stall.entered.wait(FINISH)
        _thread("list", _request, app, answers).join(FINISH)
        still_running = slow.is_alive()
        stall.release.set()
        slow.join(FINISH)
        assert still_running, "the list waited for the rerun"
        assert answers == {"list": 200, "stalled": 200}

    def test_CON_a_run_still_reads_the_document_in_its_turn(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        answers: Dict[str, int] = {}
        with studio_of(app).lock:
            run = _thread("run", lambda: answers.update(run=_trace(app)))
            run.join(ABSENT)
            assert run.is_alive() and not answers, "the run read the document while the editor was held"
        run.join(FINISH)
        assert answers == {"run": 200}

    def test_CON_an_edit_still_takes_its_turn(self, folder: Path, dist: Path) -> None:
        app = host_app(FakeHost(folder), dist)
        answers: Dict[str, int] = {}
        url = f"{PREFIX}/api/graphs/pane/param"
        body = {"node": "gain", "param": "gain", "value": 3.0}
        with studio_of(app).lock:
            edit = _thread("edit", lambda: answers.update(edit=app.test_client().post(url, json=body).status_code))
            edit.join(ABSENT)
            assert edit.is_alive() and not answers, "the edit changed the document while the editor was held"
        edit.join(FINISH)
        assert answers == {"edit": 200}

    def test_every_turn_free_name_is_a_route_of_the_editor(self, folder: Path, dist: Path) -> None:
        """A route renamed without its entry here would take the turn again, silently."""
        endpoints = {rule.endpoint for rule in host_app(FakeHost(folder), dist).url_map.iter_rules()}
        assert {f"streamstudio.{name}" for name in app_module.TURN_FREE} <= endpoints

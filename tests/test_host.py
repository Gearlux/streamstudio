"""The HOST contract — what a Flask app that mounts the editor gives it, pinned through a tiny host app.

A consuming app mounts the editor's two blueprints under one prefix of its own (``/studio`` here) and passes a
HOST: any object with a ``name`` (``GET /api/workspace`` answers ``host: {name}``, how the page knows it is mounted)
and three methods (:class:`streamstudio.app.EditorHost`) —

* ``folder()`` names the folder the editor serves, asked at EACH request: a host that moves its state folder (the
  annotation workspace does on a workspace's first save) is followed, and a graph the editor holds unsaved follows
  it with its undo history;
* ``record(graph, path)`` gives the record a Run of a graph WITHOUT a source of its own runs
  (:class:`streamstudio.app.HostRecord`: the record, the values the host fills into the graph's ops — a window, a
  band — and the label the run panel names it by); the run goes through the text the editor holds, unsaved values
  included, on recordstream's tracer, so step / pause / rerun work as for a graph's own record;
* ``saved(paths)`` is told after every Save which document files were written, so the host re-runs what reads them.

A standalone editor has no host and refuses such a Run as before ("a host supplies the record …"). The host also
calls :meth:`Studio.stash` on its own stop (``studio_of(app).stash()``), so unsaved work survives it and the mounted
page asks the restore question.

Everything runs in-process over temporary folders through Flask's test client.
"""

import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pytest
from confluid import configurable
from confluid import load as confluid_load
from flask import Flask
from recordstream import Image, Record

from streamstudio.app import (
    BUILD_COMMAND,
    RECOVERY_FILE,
    EditorHost,
    HostRecord,
    create_app,
    editor_blueprint,
    page_blueprint,
    studio_of,
    web_dist,
)

MOD = __name__
PREFIX = "/studio"


# ------------------------------------------------------------------------------------------------
# synthetic ops — spelled !class:<MOD>.<Class> by the documents below
# ------------------------------------------------------------------------------------------------


@configurable(category="op", group="host-test")
class HostWindow:
    """Keep the record and write the window the host filled in — a stand-in for a signal window.

    Args:
        start_s: Where the window starts.
        stop_s: Where it stops.
    """

    consumes = {"image": "Image"}
    produces = {"span": "*"}

    def __init__(self, start_s: float = 0.0, stop_s: float = 0.0) -> None:
        self.start_s = float(start_s)
        self.stop_s = float(stop_s)

    def __call__(self, record: Record) -> Record:
        return {**record, "span": [self.start_s, self.stop_s]}


@configurable(category="op", group="host-test")
class HostBand:
    """Keep the record and write the band the host filled in — a stand-in for a tuner.

    Args:
        f0_hz: The band's lower edge.
        f1_hz: The band's upper edge.
    """

    consumes = {"image": "Image"}
    produces = {"band": "*"}

    def __init__(self, f0_hz: float = 0.0, f1_hz: float = 0.0) -> None:
        self.f0_hz = float(f0_hz)
        self.f1_hz = float(f1_hz)

    def __call__(self, record: Record) -> Record:
        return {**record, "band": [self.f0_hz, self.f1_hz]}


@configurable(category="op", group="host-test")
class HostStart:
    """Has a ``start_s`` but no ``stop_s`` — a fill group is set only on an op holding ALL its attributes.

    Args:
        start_s: A start of its own.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, start_s: float = 0.0) -> None:
        self.start_s = float(start_s)

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="host-test")
class HostGain:
    """Scale the image entry.

    Args:
        gain: The multiplier.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = float(gain)

    def __call__(self, record: Record) -> Record:
        return {**record, "image": record["image"] * self.gain}


@configurable(category="source", group="host-test")
class HostSource:
    """Two records of its own — a graph WITH a source never asks the host.

    Args:
        size: The side of the square image.
    """

    def __init__(self, size: int = 2) -> None:
        self.size = int(size)

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            yield {"image": Image(np.full((self.size, self.size), 0.1 * (index + 1), dtype=np.float32))}


# ------------------------------------------------------------------------------------------------
# the documents
# ------------------------------------------------------------------------------------------------

#: A pane graph: a flow with no source — the host supplies its record.
PANE_TEXT = f"""\
# a pane: the host supplies the record and the window
flow:
  window:
    op: !class:{MOD}.HostWindow {{}}
  band:
    op: !class:{MOD}.HostBand {{}}
    from: window
  gain:
    op: !class:{MOD}.HostGain {{gain: 2.0}}   # the knob
    from: band
outputs: gain
"""

#: A Stream with no source: its ops run the host's record.
CHAIN_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{MOD}.HostStart {{}}
    - !class:{MOD}.HostWindow {{}}
    - !class:{MOD}.HostGain {{gain: 3.0}}
"""

#: A Stream WITH a source: its own record runs, the host is never asked.
OWN_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.HostSource {{size: 2}}
  ops:
    - !class:{MOD}.HostGain {{gain: 2.0}}
"""

#: A flow with a subgraph step — Move to its own file… makes a steps file a Save writes beside the graph.
SUBGRAPH_TEXT = """\
flow:
  probe:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: probe}
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        mask:
          op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
    from: probe
outputs: prep
"""

#: A files graph: its source is wired but lists nothing — a host fills its files when IT runs the graph.
EMPTY_SOURCE_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource {}
  ops:
    - !class:recordstream.ops.debug.PrintRecordOp {label: files}
"""

WINDOW = {"start_s": 0.25, "stop_s": 0.75}
BAND = {"f0_hz": 1.0e6, "f1_hz": 2.0e6}
LABEL = "rec_7 · 0.25–0.75 s · 1–2 MHz"


class FakeHost:
    """The smallest host: a name, a folder, one selected record, and a log of what the editor told it."""

    name = "the test host"

    def __init__(self, folder: Path) -> None:
        self.where = folder
        self.fills: Tuple[Mapping[str, Any], ...] = (WINDOW, BAND)
        self.refusal: Optional[str] = None
        self.saved_error: Optional[str] = None
        self.asked: List[Tuple[str, Path]] = []
        self.saves: List[List[Path]] = []

    def folder(self) -> Path:
        return self.where

    def record(self, graph: str, path: Path) -> HostRecord:
        self.asked.append((graph, path))
        if self.refusal is not None:
            raise ValueError(self.refusal)
        return HostRecord(
            record={"image": Image(np.full((2, 2), 0.5, dtype=np.float32))}, fills=self.fills, label=LABEL
        )

    def saved(self, paths: Sequence[Path]) -> None:
        self.saves.append(list(paths))
        if self.saved_error is not None:
            raise RuntimeError(self.saved_error)


def host_app(host: FakeHost, dist: Optional[Path] = None) -> Flask:
    """A host app as a consuming workspace builds it: its OWN routes (an ``/api/workspace`` of its own among them)
    and the editor's API and page side by side under ``/studio``."""
    app = Flask("host")

    @app.get("/")
    def home() -> str:
        return "the host's own page"

    @app.get("/api/workspace")
    def workspace() -> Dict[str, str]:
        return {"owner": "the host"}

    app.register_blueprint(editor_blueprint(host=host), url_prefix=PREFIX)
    app.register_blueprint(page_blueprint(dist), url_prefix=PREFIX)
    return app


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    folder = tmp_path / "state"
    folder.mkdir()
    for name, text in [("pane", PANE_TEXT), ("chain", CHAIN_TEXT), ("own", OWN_TEXT), ("sub", SUBGRAPH_TEXT)]:
        (folder / f"{name}.yaml").write_text(text)
    return folder


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    built = tmp_path / "dist"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text("<!doctype html><title>studio</title><script src=./assets/app.js></script>")
    (built / "assets" / "app.js").write_text("console.log('built')")
    return built


@pytest.fixture
def host(folder: Path) -> FakeHost:
    return FakeHost(folder)


@pytest.fixture
def client(host: FakeHost, dist: Path) -> Any:  # flask.testing.FlaskClient
    return host_app(host, dist).test_client()


def _ok(response: Any) -> Dict[str, Any]:
    body = response.get_json()
    assert response.status_code == 200, body
    assert isinstance(body, dict)
    return body


def _error(response: Any) -> str:
    body = response.get_json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    return str(body["error"])


# ------------------------------------------------------------------------------------------------
# the mount
# ------------------------------------------------------------------------------------------------


class TestTheMount:
    def test_a_host_is_any_object_with_the_three_methods(self, host: FakeHost) -> None:
        assert isinstance(host, EditorHost)
        assert not isinstance(object(), EditorHost)

    def test_the_page_and_the_api_answer_side_by_side_under_the_hosts_prefix(self, client: Any) -> None:
        assert client.get("/").get_data(as_text=True) == "the host's own page"
        assert client.get(PREFIX).status_code == 308  # /studio → /studio/, as Flask redirects a blueprint root
        page = client.get(f"{PREFIX}/")
        assert page.status_code == 200 and page.get_data(as_text=True).startswith("<!doctype html><title>studio")
        assert client.get(f"{PREFIX}/assets/app.js").get_data(as_text=True) == "console.log('built')"
        listed = client.get(f"{PREFIX}/api/graphs")
        assert [entry["name"] for entry in listed.get_json()] == ["chain", "own", "pane", "sub"]

    def test_the_hosts_own_workspace_route_stays_the_hosts(self, client: Any, folder: Path) -> None:
        assert client.get("/api/workspace").get_json() == {"owner": "the host"}
        editor = _ok(client.get(f"{PREFIX}/api/workspace"))
        assert editor["folder"] == str(folder) and editor["recovery"] is None

    def test_the_page_learns_it_is_mounted_and_how_to_name_the_host(self, client: Any, folder: Path) -> None:
        """``GET /api/workspace`` answers ``host: {name}`` — what the page's run panel says ("the test host's
        selection") and what decides that Run sends no record number for a graph without a source; a standalone
        editor answers ``host: null``."""
        assert _ok(client.get(f"{PREFIX}/api/workspace"))["host"] == {"name": "the test host"}
        assert _ok(create_app(folder).test_client().get("/api/workspace"))["host"] is None

    def test_the_checkouts_built_page_and_every_asset_it_names_answer_under_the_prefix(self, host: FakeHost) -> None:
        """The REAL build (``streamstudio/canvas``), mounted under ``/studio``: the page names its assets relative to
        itself (``./assets/…``, vite's ``base: './'``), and each answers under the prefix — what a browser fetches
        next."""
        built = web_dist()
        if built is None:
            pytest.skip(f"the canvas page is not built — {BUILD_COMMAND}")
        client = host_app(host, built).test_client()
        page = client.get(f"{PREFIX}/").get_data(as_text=True)
        assets = re.findall(r'(?:src|href)="\./(assets/[^"]+)"', page)
        assert assets, page
        for asset in assets:
            assert client.get(f"{PREFIX}/{asset}").status_code == 200, asset

    def test_the_page_without_an_argument_is_the_checkouts_build(
        self, dist: Path, monkeypatch: pytest.MonkeyPatch, host: FakeHost
    ) -> None:
        """``page_blueprint()`` serves what :func:`web_dist` finds — before, it served the placeholder even when the
        page was built, so a host had to know to pass ``web_dist()``."""
        from streamstudio import app as app_module

        monkeypatch.setattr(app_module, "web_dist", lambda: dist)
        app = Flask("host")
        app.register_blueprint(editor_blueprint(host=host), url_prefix=PREFIX)
        app.register_blueprint(page_blueprint(), url_prefix=PREFIX)
        assert app.test_client().get(f"{PREFIX}/").get_data(as_text=True).startswith("<!doctype html><title>studio")

    def test_a_folder_and_a_host_together_or_neither_is_refused(self, folder: Path, host: FakeHost) -> None:
        with pytest.raises(ValueError, match="a graph folder or a host"):
            editor_blueprint()
        with pytest.raises(ValueError, match="not both"):
            editor_blueprint(folder, host=host)

    def test_the_editor_is_reached_from_the_hosts_app(self, host: FakeHost, dist: Path) -> None:
        app = host_app(host, dist)
        assert studio_of(app).graph_dir == host.folder()
        with pytest.raises(KeyError, match="no editor is mounted"):
            studio_of(Flask("bare"))


# ------------------------------------------------------------------------------------------------
# record: a graph with no source of its own runs the host's record
# ------------------------------------------------------------------------------------------------


class TestTheHostsRecord:
    def test_a_flow_runs_the_hosts_record_with_its_window_and_band_filled_in(
        self, client: Any, host: FakeHost, folder: Path
    ) -> None:
        body = _ok(client.post(f"{PREFIX}/api/graphs/pane/trace", json={}))
        assert host.asked == [("pane", folder / "pane.yaml")]
        assert body["record"] is None, "the record is the host's, not the graph's record #N"
        assert body["host"] == {"label": LABEL, "filled": {"window": WINDOW, "band": BAND}}
        assert body["statuses"] == {"window": "ok", "band": "ok", "gain": "ok"}
        rows = {row["node"]: row for row in body["trace"]["nodes"]}
        assert rows["window"]["params"] == WINDOW
        assert rows["band"]["params"] == BAND
        assert rows["gain"]["output"]["image"]["max"] == pytest.approx(1.0)

    def test_the_run_uses_the_unsaved_value_the_editor_shows(self, client: Any, folder: Path) -> None:
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        assert (folder / "pane.yaml").read_text() == PANE_TEXT
        body = _ok(client.post(f"{PREFIX}/api/graphs/pane/trace", json={}))
        rows = {row["node"]: row for row in body["trace"]["nodes"]}
        assert rows["gain"]["output"]["image"]["max"] == pytest.approx(1.5)

    def test_a_stream_without_a_source_runs_the_hosts_record_through_its_ops(self, client: Any) -> None:
        """PRO: ``stream.ops[1]`` holds ``start_s`` and ``stop_s`` and gets the window. CON: ``stream.ops[0]`` holds
        ``start_s`` alone, and a group is set only on an op holding every attribute of it — so it keeps its own."""
        body = _ok(client.post(f"{PREFIX}/api/graphs/chain/trace", json={}))
        assert body["host"]["filled"] == {"stream.ops[1]": WINDOW}
        rows = {row["node"]: row for row in body["trace"]["nodes"]}
        assert rows["stream.ops[0]"]["params"] == {"start_s": 0.0}
        assert rows["stream.ops[1]"]["params"] == WINDOW
        assert rows["stream.ops[2]"]["output"]["image"]["max"] == pytest.approx(1.5)

    def test_a_graph_with_its_own_source_never_asks_the_host(self, client: Any, host: FakeHost) -> None:
        body = _ok(client.post(f"{PREFIX}/api/graphs/own/trace", json={"record": 1}))
        assert host.asked == []
        assert body["record"] == 1 and body["host"] is None
        assert body["trace"]["nodes"][0]["output"]["image"]["max"] == pytest.approx(0.4)

    def test_a_pause_and_a_rerun_keep_what_the_host_filled_in(self, client: Any) -> None:
        paused = _ok(client.post(f"{PREFIX}/api/graphs/pane/trace", json={"until": "gain"}))
        assert paused["statuses"] == {"window": "ok", "band": "ok", "gain": "paused"}
        rerun = _ok(
            client.post(f"{PREFIX}/api/runs/{paused['run']}/rerun", json={"node": "window", "params": {"stop_s": 0.5}})
        )
        rows = {row["node"]: row for row in rerun["trace"]["nodes"]}
        assert rows["window"]["params"] == {"start_s": 0.25, "stop_s": 0.5}, "the rebuild starts from the filled op"
        assert rows["band"]["params"] == BAND
        assert rerun["host"] == paused["host"]
        assert _ok(client.get(f"{PREFIX}/api/runs/{paused['run']}")) == rerun

    def test_a_host_with_no_record_refuses_in_its_own_words_and_no_run_is_kept(
        self, client: Any, host: FakeHost
    ) -> None:
        host.refusal = "no record is selected in the page — open one there, then press Run"
        response = client.post(f"{PREFIX}/api/graphs/pane/trace", json={})
        assert response.status_code == 400
        assert _error(response) == host.refusal
        assert studio_of(client.application)._runs == {}

    @pytest.mark.parametrize(
        "failure, said",
        [
            (IndexError("list index out of range"), "IndexError: list index out of range"),
            (LookupError("no such capture"), "LookupError: no such capture"),
            (KeyError("rec_0042"), "KeyError: 'rec_0042'"),
        ],
    )
    def test_any_other_failure_of_the_host_is_a_refusal_in_words_naming_it(
        self, client: Any, host: FakeHost, folder: Path, failure: Exception, said: str
    ) -> None:
        """Measured before: a ``LookupError`` (or any failure but ``ValueError`` / ``RuntimeError`` / ``OSError`` /
        ``TypeError``) reached the page as werkzeug's HTML ``500``, and a ``KeyError`` as a ``404`` saying ``rec_0042``.
        """

        def broken(graph: str, path: Path) -> HostRecord:
            raise failure

        host.record = broken  # type: ignore[method-assign]
        response = client.post(f"{PREFIX}/api/graphs/pane/trace", json={})
        assert response.status_code == 400
        assert _error(response) == f"{folder / 'pane.yaml'}: the test host could not give a record: {said}"
        assert studio_of(client.application)._runs == {}

    @pytest.mark.parametrize(
        "answer, said",
        [
            ({"record": {}}, "the test host answered a dict, not a HostRecord"),
            (
                HostRecord(record=None),  # type: ignore[arg-type]
                "the record the test host gave is None, not a mapping of entries",
            ),
            (
                HostRecord(record={}, fills=[("start_s", 0.1)]),  # type: ignore[list-item]
                "the fills the test host gave hold a tuple, not a mapping of attribute → value",
            ),
        ],
    )
    def test_an_answer_that_is_not_a_host_record_is_refused_in_words(
        self, client: Any, host: FakeHost, folder: Path, answer: Any, said: str  # answer: whatever a host returns
    ) -> None:
        """Measured before: a dict was an HTML ``500`` (``AttributeError: 'dict' object has no attribute 'fills'``), a
        ``None`` record the bare ``'NoneType' object is not iterable``."""
        host.record = lambda graph, path: answer  # type: ignore[method-assign]
        response = client.post(f"{PREFIX}/api/graphs/pane/trace", json={})
        assert response.status_code == 400
        assert _error(response) == f"{folder / 'pane.yaml'}: {said}"

    def test_a_host_that_cannot_name_its_folder_is_a_refusal_in_words(self, client: Any, host: FakeHost) -> None:
        def lost() -> Path:
            raise LookupError("the workspace is closing")

        host.folder = lost  # type: ignore[method-assign]
        response = client.get(f"{PREFIX}/api/graphs")
        assert response.status_code == 400
        assert _error(response) == "the test host could not name its folder: LookupError: the workspace is closing"

    def test_a_source_that_yields_nothing_is_refused_saying_why(
        self, client: Any, host: FakeHost, folder: Path
    ) -> None:
        """A files graph whose ``FilesSource`` a host fills when IT runs the graph yields nothing in the editor.
        Measured before: ``the source yields only 0 records — there is no record #0``, which names no cause a person
        can act on; and the host was never asked."""
        (folder / "files.yaml").write_text(EMPTY_SOURCE_TEXT)
        response = client.post(f"{PREFIX}/api/graphs/files/trace", json={"record": 0})
        assert response.status_code == 400
        assert _error(response) == (
            "files.yaml: stream.source (FilesSource) yields no records, so there is no record #0 to run. The editor "
            "runs a source as the document spells it — what the test host fills into it when the test host runs "
            "this graph is not handed to the editor; spell what the source reads (a folder, files) to run it here"
        )
        assert host.asked == []
        standalone = create_app(folder).test_client().post("/api/graphs/files/trace", json={"record": 0})
        assert _error(standalone) == (
            "files.yaml: stream.source (FilesSource) yields no records, so there is no record #0 to run — spell what "
            "the source reads (a folder, files) to run it here"
        )
        short = client.post(f"{PREFIX}/api/graphs/own/trace", json={"record": 5})
        assert _error(short) == "own.yaml: the source yields only 2 records — there is no record #5"

    def test_without_a_host_the_refusal_is_the_standalone_editors(self, folder: Path) -> None:
        response = create_app(folder).test_client().post("/api/graphs/pane/trace", json={})
        assert response.status_code == 400
        assert _error(response) == (
            "pane.yaml: this graph has no source to draw a record from — "
            "a host supplies the record (mount the editor in one) or wire a source"
        )

    def test_the_shipped_psd_slice_pane_runs_a_hosts_signal_window(self, tmp_path: Path) -> None:
        """The shipped PSD slice pane (a Stream with no source, rooted by its host contract) through a host that
        hands it an IQ capture, a 2 ms window and a 400 kHz band: ``WindowSignal`` gets the window, ``PsdSlice`` the
        band, and the slice runs."""
        pytest.importorskip("waivefront")
        import waivefront.annotation as annotation
        from waivefront.items import Regions, Signal

        folder = tmp_path / "panes"
        folder.mkdir()
        shutil.copy(Path(annotation.__file__).with_name("psd_slice_view.yaml"), folder / "view_psd.yaml")

        class SignalHost(FakeHost):
            def record(self, graph: str, path: Path) -> HostRecord:
                noise = (np.random.default_rng(0).standard_normal(8192) * (1 + 1j)).astype(np.complex64)
                capture = Signal(data=noise, samplerate=1.0e6, center_freq=2.4e9)
                return HostRecord(
                    record={"input": capture, "regions": Regions()},
                    fills=({"start_s": 0.001, "stop_s": 0.003}, {"f0_hz": 2.3998e9, "f1_hz": 2.4002e9}),
                    label="capture · 1–3 ms",
                )

        client = host_app(SignalHost(folder)).test_client()
        body = _ok(client.post(f"{PREFIX}/api/graphs/view_psd/trace", json={}))
        assert body["host"]["filled"] == {
            "stream.ops[0]": {"start_s": 0.001, "stop_s": 0.003},  # WindowSignal: the window
            "stream.ops[1]": {"f0_hz": 2.3998e9, "f1_hz": 2.4002e9},  # PsdSlice: the band its threshold reads
        }
        assert body["statuses"] == {"stream.ops[0]": "ok", "stream.ops[1]": "ok"}


# ------------------------------------------------------------------------------------------------
# saved: the host is told which files a Save wrote
# ------------------------------------------------------------------------------------------------


class TestSaved:
    def test_a_save_tells_the_host_the_file_it_wrote(self, client: Any, host: FakeHost, folder: Path) -> None:
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        assert host.saves == []
        body = _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert body["dirty"] is False and "host_error" not in body
        assert host.saves == [[(folder / "pane.yaml").resolve()]]

    def test_a_held_steps_file_is_told_first_and_the_graph_last(
        self, client: Any, host: FakeHost, folder: Path
    ) -> None:
        _ok(client.post(f"{PREFIX}/api/graphs/sub/subgraph/prep/extract", json={}))
        _ok(client.post(f"{PREFIX}/api/graphs/sub/save"))
        assert host.saves == [[(folder / "prep.steps.yaml").resolve(), (folder / "sub.yaml").resolve()]]

    def test_a_save_of_positions_alone_writes_no_document_and_tells_nothing(
        self, client: Any, host: FakeHost, folder: Path
    ) -> None:
        _ok(client.put(f"{PREFIX}/api/graphs/pane/positions", json={"positions": {"gain": [10, 20]}}))
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert (folder / "pane.layout.yaml").is_file()
        assert host.saves == []

    def test_save_as_tells_the_host_the_new_file(self, client: Any, host: FakeHost, folder: Path) -> None:
        body = _ok(client.post(f"{PREFIX}/api/graphs/pane/save-as", json={"path": "pane2.yaml"}))
        assert body["name"] == "pane2"
        assert host.saves == [[(folder / "pane2.yaml").resolve()]]

    def test_a_host_that_fails_on_the_news_does_not_unsave_the_file(
        self, client: Any, host: FakeHost, folder: Path
    ) -> None:
        """The file IS written when the host hears of it: the answer stays the saved graph and carries the host's
        failure in words, for the page to show — never a refused Save."""
        host.saved_error = "the panes could not be re-run: the workspace is closing"
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        body = _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert body["dirty"] is False
        assert body["host_error"] == "RuntimeError: the panes could not be re-run: the workspace is closing"
        assert "gain: 3.0" in (folder / "pane.yaml").read_text()

    def test_a_standalone_save_answers_no_host_error(self, folder: Path) -> None:
        client = create_app(folder).test_client()
        client.post("/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0})
        assert "host_error" not in _ok(client.post("/api/graphs/pane/save"))

    def test_new_tells_the_host_the_file_it_wrote(self, client: Any, host: FakeHost, folder: Path) -> None:
        """File ▸ New… WRITES its starter at once — a document write like a Save. Measured before: the host heard
        nothing, so a host that learns of its graphs through ``saved`` never heard of a new ``view_*`` graph."""
        body = _ok(client.post(f"{PREFIX}/api/graphs", json={"name": "view_new", "kind": "flow"}))
        assert body["name"] == "view_new" and body["dirty"] is False and "host_error" not in body
        assert host.saves == [[folder / "view_new.yaml"]]
        host.saved_error = "the panes could not be re-run"
        failed = _ok(client.post(f"{PREFIX}/api/graphs", json={"name": "view_two", "kind": "pipeline"}))
        assert failed["host_error"] == "RuntimeError: the panes could not be re-run"
        assert (folder / "view_two.yaml").is_file()

    def test_every_path_the_host_is_handed_is_spelled_under_the_folder_it_gave(self, tmp_path: Path) -> None:
        """``folder()``, ``record(graph, path)`` and ``saved(paths)`` name a file ONE way: under the folder as the host
        spells it. Measured before, with the host's folder named through a symbolic link (a macOS temporary folder is
        ``/var/…`` for ``/private/var/…``): ``record`` got ``<folder>/view_x.yaml`` but ``saved`` the resolved
        ``/private/…/view_x.yaml``, which is not under ``folder()`` — a host matching the two missed every save."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "pane.yaml").write_text(PANE_TEXT)
        (real / "sub.yaml").write_text(SUBGRAPH_TEXT)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        host = FakeHost(link)
        client = host_app(host).test_client()
        _ok(client.post(f"{PREFIX}/api/graphs/pane/trace", json={}))
        assert host.asked == [("pane", link / "pane.yaml")]
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        _ok(client.post(f"{PREFIX}/api/graphs/sub/subgraph/prep/extract", json={}))
        _ok(client.post(f"{PREFIX}/api/graphs/sub/save"))
        _ok(client.post(f"{PREFIX}/api/graphs", json={"name": "view_new", "kind": "flow"}))
        assert host.saves == [
            [link / "pane.yaml"],
            [link / "prep.steps.yaml", link / "sub.yaml"],
            [link / "view_new.yaml"],
        ]
        assert all(path.parent == host.folder() for paths in host.saves for path in paths)


# ------------------------------------------------------------------------------------------------
# folder: asked at each request — a host that moves its folder is followed
# ------------------------------------------------------------------------------------------------


class TestTheHostsFolder:
    def test_a_moved_folder_is_followed_and_an_unsaved_graph_moves_with_its_history(
        self, client: Any, host: FakeHost, folder: Path, tmp_path: Path
    ) -> None:
        """A workspace's first save MOVES its state folder out of a temporary one: the list follows, a graph the
        editor holds unsaved keeps its edit and its undo history, and its Save writes into the NEW folder."""
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        moved = tmp_path / "workspace.yaml.d"
        shutil.move(str(folder), str(moved))
        host.where = moved

        listed = _ok(client.get(f"{PREFIX}/api/workspace"))
        assert listed["folder"] == str(moved)
        graphs = {entry["name"]: entry for entry in client.get(f"{PREFIX}/api/graphs").get_json()}
        assert set(graphs) == {"chain", "own", "pane", "sub"}
        assert graphs["pane"]["path"] == str(moved / "pane.yaml") and graphs["pane"]["dirty"] is True

        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert "gain: 3.0" in (moved / "pane.yaml").read_text()
        assert not folder.exists()
        assert host.saves == [[(moved / "pane.yaml").resolve()]]
        undone = _ok(client.post(f"{PREFIX}/api/graphs/pane/undo"))
        assert undone["changed"] is True and undone["dirty"] is True
        assert "gain: 2.0" in undone["text"]

    def test_a_copied_folder_is_followed_and_the_old_copy_is_left_alone(
        self, client: Any, host: FakeHost, folder: Path, tmp_path: Path
    ) -> None:
        """A save-as COPIES the state folder: the editor works in the new copy from then on."""
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        copy = tmp_path / "copy.yaml.d"
        shutil.copytree(folder, copy)
        host.where = copy
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert "gain: 3.0" in (copy / "pane.yaml").read_text()
        assert (folder / "pane.yaml").read_text() == PANE_TEXT

    def test_an_include_out_of_the_folder_still_names_its_file_after_the_folder_moves(
        self, host: FakeHost, folder: Path, tmp_path: Path
    ) -> None:
        """Insert steps file… writes the include RELATIVE to the graph's folder; the host then moves that folder (a
        workspace's first save: out of a temporary folder into one beside the workspace file). Measured before: the
        include still read ``../../home/gert/steps/prep.steps.yaml`` from the NEW folder, check said ``does not load:
        ConfigFileNotFoundError``, and Save wrote a graph that did not load — while Save As rebased the same include.
        Now following the folder rewrites the include as Save As does (the graph is then unsaved: the file on disk
        still names the old way, and Save writes the new one)."""
        temporary = tmp_path / "T" / "annotaide-abc123"
        temporary.parent.mkdir()
        shutil.move(str(folder), str(temporary))
        host.where = temporary
        steps = tmp_path / "home" / "gert" / "steps"
        steps.mkdir(parents=True)
        (steps / "prep.steps.yaml").write_text(
            "mask:\n  op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n"
        )
        client = host_app(host).test_client()
        inserted = _ok(
            client.post(
                f"{PREFIX}/api/graphs/pane/include", json={"path": str(steps / "prep.steps.yaml"), "after": "gain"}
            )
        )
        assert "steps: {include: ../../home/gert/steps/prep.steps.yaml}" in inserted["text"]

        moved = tmp_path / "home" / "gert" / "projects" / "ws.workspace.yaml.d"
        moved.parent.mkdir(parents=True)
        shutil.move(str(temporary), str(moved))
        host.where = moved
        assert _ok(client.get(f"{PREFIX}/api/graphs/pane/check")) == {"problems": []}
        shown = _ok(client.get(f"{PREFIX}/api/graphs/pane"))
        assert "steps: {include: ../../steps/prep.steps.yaml}" in shown["text"] and shown["dirty"] is True
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert "include: ../../steps/prep.steps.yaml" in (moved / "pane.yaml").read_text()
        assert "flow" in confluid_load(moved / "pane.yaml", until="document")
        undone = _ok(client.post(f"{PREFIX}/api/graphs/pane/undo"))  # the history moved with it: the insert undoes
        assert "include:" not in undone["text"] and _ok(client.get(f"{PREFIX}/api/graphs/pane/check"))["problems"] == []

    def test_an_include_inside_the_moved_folder_is_left_as_written(
        self, client: Any, host: FakeHost, folder: Path, tmp_path: Path
    ) -> None:
        """CON: a steps file that moves WITH the folder keeps its include word for word — and the graph stays clean."""
        _ok(client.post(f"{PREFIX}/api/graphs/sub/subgraph/prep/extract", json={}))
        _ok(client.post(f"{PREFIX}/api/graphs/sub/save"))
        before = _ok(client.get(f"{PREFIX}/api/graphs/sub"))["text"]
        moved = tmp_path / "moved.d"
        shutil.move(str(folder), str(moved))
        host.where = moved
        after = _ok(client.get(f"{PREFIX}/api/graphs/sub"))
        assert after["text"] == before and after["dirty"] is False
        assert _ok(client.get(f"{PREFIX}/api/graphs/sub/check")) == {"problems": []}

    def test_a_graph_opened_from_another_folder_stays_where_it_is(
        self, client: Any, host: FakeHost, folder: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "away.yaml").write_text(PANE_TEXT)
        _ok(client.post(f"{PREFIX}/api/graphs/open", json={"path": str(elsewhere / "away.yaml")}))
        moved = tmp_path / "moved"
        shutil.move(str(folder), str(moved))
        host.where = moved
        graphs = {entry["name"]: entry["path"] for entry in client.get(f"{PREFIX}/api/graphs").get_json()}
        assert graphs["away"] == str(elsewhere / "away.yaml")
        assert graphs["pane"] == str(moved / "pane.yaml")


# ------------------------------------------------------------------------------------------------
# a file the host rewrites: a graph holding nothing unsaved is read again
# ------------------------------------------------------------------------------------------------


class TestTheHostWritesAGraph:
    """A host writes graph files itself (removing a file from its source graph edits that graph; a conversion writes a
    pane). Measured before: the editor kept the text it read first for the life of the process — the panel, a reload
    and Run all showed and ran ``label: one`` after the file said ``label: two``, the page said nothing was unsaved, and
    only an edit's Save found out, refused as "changed on disk"."""

    def _label(self, body: Dict[str, Any], node: str = "gain") -> Any:  # a parameter value of any JSON type
        return next(n for n in body["model"]["nodes"] if n["name"] == node)["params"]["gain"]["value"]

    def test_a_graph_holding_nothing_unsaved_is_read_again_and_runs_what_the_file_says(
        self, client: Any, folder: Path
    ) -> None:
        assert self._label(_ok(client.get(f"{PREFIX}/api/graphs/pane"))) == 2.0
        (folder / "pane.yaml").write_text(PANE_TEXT.replace("gain: 2.0", "gain: 5.0"))  # the host writes the file

        shown = _ok(client.get(f"{PREFIX}/api/graphs/pane"))
        assert self._label(shown) == 5.0 and shown["dirty"] is False
        run = _ok(client.post(f"{PREFIX}/api/graphs/pane/trace", json={}))
        rows = {row["node"]: row for row in run["trace"]["nodes"]}
        assert rows["gain"]["params"] == {"gain": 5.0}
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 6.0}))
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        assert "gain: 6.0" in (folder / "pane.yaml").read_text()

    def test_a_graph_holding_unsaved_work_keeps_it_and_its_save_is_refused(self, client: Any, folder: Path) -> None:
        """CON: unsaved work is never dropped for the file — its Save still refuses, naming the remedies."""
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        (folder / "pane.yaml").write_text(PANE_TEXT.replace("gain: 2.0", "gain: 5.0"))
        shown = _ok(client.get(f"{PREFIX}/api/graphs/pane"))
        assert self._label(shown) == 3.0 and shown["dirty"] is True
        refused = client.post(f"{PREFIX}/api/graphs/pane/save")
        assert refused.status_code == 400 and "changed on disk since the editor read it" in _error(refused)

    def test_an_undo_history_does_not_survive_the_file_being_rewritten(self, client: Any, folder: Path) -> None:
        """A graph saved after an edit holds nothing unsaved; once the file is rewritten its old history names a text
        that is no longer there, so the graph is read afresh and an undo has nothing to walk back."""
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        _ok(client.post(f"{PREFIX}/api/graphs/pane/save"))
        (folder / "pane.yaml").write_text(PANE_TEXT.replace("gain: 2.0", "gain: 5.0"))
        undone = _ok(client.post(f"{PREFIX}/api/graphs/pane/undo"))
        assert undone["changed"] is False and self._label(undone) == 5.0


# ------------------------------------------------------------------------------------------------
# stash: the host keeps the unsaved work on its own stop
# ------------------------------------------------------------------------------------------------


class TestTheHostsStop:
    def test_the_host_stashes_on_its_stop_and_the_next_mount_offers_the_work(
        self, host: FakeHost, folder: Path, dist: Path
    ) -> None:
        first = host_app(host, dist)
        client = first.test_client()
        _ok(client.post(f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3.0}))
        assert studio_of(first).stash() == [str(folder / "pane.yaml")]
        assert (folder / RECOVERY_FILE).is_file()
        assert (folder / "pane.yaml").read_text() == PANE_TEXT

        again = host_app(FakeHost(folder), dist).test_client()
        recovery = _ok(again.get(f"{PREFIX}/api/workspace"))["recovery"]
        assert [row["name"] for row in recovery["graphs"]] == ["pane"]
        restored = again.post(f"{PREFIX}/api/workspace/recovery/restore").get_json()
        assert {entry["name"]: entry["dirty"] for entry in restored}["pane"] is True
        assert "gain: 3.0" in _ok(again.get(f"{PREFIX}/api/graphs/pane"))["text"]

    def test_a_stash_after_a_move_keeps_the_work_in_the_new_folder(
        self, host: FakeHost, folder: Path, tmp_path: Path, dist: Path
    ) -> None:
        app = host_app(host, dist)
        _ok(
            app.test_client().post(
                f"{PREFIX}/api/graphs/pane/param", json={"node": "gain", "param": "gain", "value": 3}
            )
        )
        moved = tmp_path / "moved"
        shutil.move(str(folder), str(moved))
        host.where = moved
        assert studio_of(app).stash() == [str(moved / "pane.yaml")]
        assert (moved / RECOVERY_FILE).is_file()

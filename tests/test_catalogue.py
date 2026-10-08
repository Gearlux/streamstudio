"""The catalogue the page lists, built in a HELPER PROCESS (``streamstudio.catalogue``).

Building the catalogue imports every ``confluid.configurables`` entry point — 12 s of a 21 s build, measured
2026-10-05 at load 23 — and describing each class's settings took 6 s more. A helper process does both, so the server
neither waits on those imports nor keeps them (1.2 GB). It answers twice: the LIST first (every class without its
settings, ``palette.listed``), then every class's full spec. What is pinned:

* a class described without its settings is its full description less ``params_schema``/``params_defs`` — the same
  kind, sockets and problem, the problem in confluid's own words (``TestTheListWithoutSettings``; the full installed
  catalogue in ``test_the_helper_lists_every_installed_class_as_its_full_spec_less_the_settings``);
* the helper answers on its own stdout, and whatever an imported package prints never reaches that answer;
* ONE helper per process: a second start, a second page, a second request wait for the same one; ``refresh`` starts a
  new one; a helper that stops before answering is a :class:`CatalogueUnavailable` naming its exit code, and the next
  request starts again;
* a class's settings come from the helper's second answer — at once when the class's module is already imported in
  the server, without waiting for that answer;
* a class only the server's registry holds (a test's, a host's) is in the list too, described in the server.

The helpers here are small scripts that answer what a test needs; a gate (a named pipe the script opens before its
second answer) holds that answer back until the test releases it — no test waits on time.
"""

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pytest
from confluid import configurable
from recordstream.ops.numpy import Threshold

from streamstudio import catalogue as catalogue_module
from streamstudio.catalogue import Catalogue, CatalogueUnavailable
from streamstudio.palette import describe, listed

THRESHOLD = "recordstream.ops.numpy.Threshold"


@configurable(category="op", group="catalogue-test")
class ServerOnlyGain:
    """A class only THIS process registered — no entry point names it, so no helper can list it.

    Args:
        gain: The multiplier.
    """

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = gain

    def __call__(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return record


class BrokenAnnotation:
    """A constructor naming a type that does not exist — the shape of torch's ``DataLoader`` here."""

    def __init__(self, x: "NoSuchType" = None) -> None:  # type: ignore[name-defined]  # noqa: F821
        self.x = x


SERVER_ONLY = f"{__name__}.ServerOnlyGain"


def make_widget(size: int = 3) -> object:
    """A builder function — described like a value producer."""
    return object()


# ------------------------------------------------------------------------------------------------
# a class without its settings
# ------------------------------------------------------------------------------------------------


class TestTheListWithoutSettings:
    @pytest.mark.parametrize("target", [Threshold, ServerOnlyGain, make_widget], ids=lambda t: t.__name__)
    def test_a_class_listed_is_its_full_description_less_the_settings(self, target: Any) -> None:
        full = asdict(describe(target))
        light = describe(target, settings=False)
        assert light.params_schema == {} and light.params_defs == {}
        assert listed(light) == {
            key: value for key, value in full.items() if key not in ("params_schema", "params_defs")
        }

    def test_the_list_entry_has_no_settings_keys(self) -> None:
        entry = listed(describe(Threshold))
        assert "params_schema" not in entry and "params_defs" not in entry
        assert entry["class_path"] == THRESHOLD and entry["kind"] == "op"

    def test_a_class_whose_settings_cannot_be_built_is_listed_with_confluids_own_problem(self) -> None:
        full = describe(BrokenAnnotation)
        light = describe(BrokenAnnotation, settings=False)
        assert full.problem.startswith("IntrospectionError: Cannot introspect BrokenAnnotation.__init__")
        assert (light.kind, light.inputs, light.outputs, light.problem) == (
            full.kind,
            full.inputs,
            full.outputs,
            full.problem,
        )


# ------------------------------------------------------------------------------------------------
# the real helper
# ------------------------------------------------------------------------------------------------


def run_helper(*prelude: str) -> List[Any]:
    """Run the helper as the server does (``python -P -m streamstudio.catalogue``), after ``prelude`` lines when given;
    its two answers, parsed."""
    if prelude:
        code = "\n".join([*prelude, "from streamstudio.catalogue import main", "main()"])
        command = [sys.executable, "-P", "-c", code]
    else:
        command = list(catalogue_module.HELPER)
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True)
    lines = done.stdout.splitlines()
    assert len(lines) == 2, f"two answers expected, got {len(lines)}: {done.stdout[:300]!r}"
    return [json.loads(line) for line in lines]


def test_what_an_imported_package_prints_never_reaches_the_answer() -> None:
    # The entry-point import is replaced by one that PRINTS, as some packages do on import; the answer stays two lines
    # of JSON, and the print lands on stderr — the server's log.
    noisy = (
        "import streamstudio.palette as p",
        "p.load_configurables = lambda: print('a package that prints on import') or {}",
    )
    entries, specs = run_helper(*noisy)
    assert all("params_schema" not in entry for entry in entries)
    by_path = {spec["class_path"]: spec for spec in specs}
    assert "low_level" in by_path[THRESHOLD]["params_schema"]
    assert entries == [listed_dict(spec) for spec in specs]


def test_the_helper_lists_every_installed_class_as_its_full_spec_less_the_settings() -> None:
    entries, specs = run_helper()
    assert len(entries) == len(specs)
    assert THRESHOLD in [spec["class_path"] for spec in specs]
    assert entries == [
        {key: value for key, value in spec.items() if key not in ("params_schema", "params_defs")} for spec in specs
    ]


# ------------------------------------------------------------------------------------------------
# the server's side, over helpers that answer what a test needs
# ------------------------------------------------------------------------------------------------


class Helper:
    """A stand-in helper: a script that notes each start in a file, then answers ``entries`` and ``specs`` as the real
    one does — or stops early (``answers`` < 2) with ``exit_code``. With ``gated``, the second answer waits until the
    test calls :meth:`release`."""

    def __init__(
        self,
        folder: Path,
        entries: List[Dict[str, Any]],
        specs: List[Dict[str, Any]],
        *,
        answers: int = 2,
        exit_code: int = 0,
        gated: bool = False,
        fail_first: bool = False,
    ) -> None:
        self.starts = folder / "starts"
        self.starts.write_text("")
        self.gate: Optional[Path] = folder / "gate" if gated else None
        if self.gate is not None:
            os.mkfifo(self.gate)
        script = folder / "helper.py"
        script.write_text(
            "import json, sys\n"
            f"starts = {str(self.starts)!r}\n"
            "with open(starts, 'a') as noted:\n"
            "    noted.write('started\\n')\n"
            "first = open(starts).read().count('started') == 1\n"
            f"if {fail_first!r} and first:\n"
            "    sys.exit(3)\n"
            f"answers = [{json.dumps(entries)!r}, {json.dumps(specs)!r}][:{answers}]\n"
            "for index, answer in enumerate(answers):\n"
            f"    if index == 1 and {str(self.gate) if self.gate else ''!r}:\n"
            f"        open({str(self.gate) if self.gate else ''!r}).read()\n"
            "    sys.stdout.write(answer + '\\n')\n"
            "    sys.stdout.flush()\n"
            f"sys.exit({exit_code})\n"
        )
        self.command: Sequence[str] = [sys.executable, "-P", str(script)]

    @property
    def started(self) -> int:
        return self.starts.read_text().count("started")

    def release(self) -> None:
        assert self.gate is not None
        with open(self.gate, "w") as gate:
            gate.write("go")


def spec_of(class_path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A full spec as the helper writes it — a minimal op."""
    return {
        "class_path": class_path,
        "name": class_path.rsplit(".", 1)[1],
        "category": "op",
        "group": "",
        "kind": "op",
        "params_schema": params or {},
        "params_defs": {},
        "inputs": [],
        "outputs": [],
        "consumes": {},
        "produces": {},
        "consumes_types": [],
        "produces_types": [],
        "flags": [],
        "reports": [],
        "doc": "",
        "tuple_form": False,
        "problem": "",
    }


#: A class no process imports: only the helper's answers can describe it.
ELSEWHERE = "somewhere.not_imported.Elsewhere"


@pytest.fixture
def specs() -> List[Dict[str, Any]]:
    return [spec_of(ELSEWHERE, params={"level": {"type": "number", "default": 1.0}})]


@pytest.fixture
def made(tmp_path: Path) -> Iterator[List[Catalogue]]:
    """Every catalogue a test makes is stopped after it — no helper outlives its test."""
    catalogues: List[Catalogue] = []
    yield catalogues
    for catalogue in catalogues:
        catalogue.stop()


def catalogue_over(made: List[Catalogue], helper: Helper) -> Catalogue:
    catalogue = Catalogue(command=helper.command)
    made.append(catalogue)
    return catalogue


class TestOneHelperPerProcess:
    def test_the_list_is_the_helpers_first_answer_with_the_servers_own_classes(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs)
        entries = catalogue_over(made, helper).entries()
        by_path = {entry["class_path"]: entry for entry in entries}
        assert by_path[ELSEWHERE] == listed_dict(specs[0])
        # a class only this process registered: described here, without its settings
        assert by_path[SERVER_ONLY]["kind"] == "op" and "params_schema" not in by_path[SERVER_ONLY]
        assert [entry["class_path"] for entry in entries] == sorted(by_path)

    def test_a_second_start_and_a_second_request_wait_for_the_same_helper(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs)
        catalogue = catalogue_over(made, helper)
        catalogue.start()
        catalogue.start()
        catalogue.entries()
        catalogue.entries()
        assert helper.started == 1

    def test_refresh_starts_a_new_helper(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs)
        catalogue = catalogue_over(made, helper)
        catalogue.entries()
        catalogue.entries(refresh=True)
        assert helper.started == 2

    def test_a_helper_that_stops_before_answering_is_refused_and_the_next_request_starts_again(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, fail_first=True)
        catalogue = catalogue_over(made, helper)
        with pytest.raises(CatalogueUnavailable, match=r"stopped before it answered \(exit code 3\)"):
            catalogue.entries()
        assert ELSEWHERE in [entry["class_path"] for entry in catalogue.entries()]
        assert helper.started == 2

    def test_an_answer_that_is_not_json_is_refused(self, tmp_path: Path, made: List[Catalogue]) -> None:
        script = tmp_path / "garbled.py"
        script.write_text("print('not json')\n")
        catalogue = Catalogue(command=[sys.executable, "-P", str(script)])
        made.append(catalogue)
        with pytest.raises(CatalogueUnavailable, match="not JSON"):
            catalogue.entries()

    def test_stop_ends_a_running_helper(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, gated=True)
        catalogue = catalogue_over(made, helper)
        catalogue.entries()  # the list is in; the helper now waits at its gate
        process = catalogue.running()
        assert process is not None and process.poll() is None
        catalogue.stop()
        assert process.poll() is not None
        assert catalogue.running() is None


def test_a_helper_never_outlives_its_server(tmp_path: Path, specs: List[Dict[str, Any]]) -> None:
    # A server process starts a helper that waits at its gate forever, and exits normally (Ctrl+C, a SIGTERM handler,
    # annotaide's ⏻ all end that way): the helper is stopped on the way out.
    helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, gated=True)
    code = (
        "from streamstudio.catalogue import Catalogue\n"
        f"catalogue = Catalogue(command={list(helper.command)!r})\n"
        "catalogue.entries()\n"
        "print(catalogue.running().pid)\n"
    )
    # stderr to /dev/null: the helper inherits it, and a captured one would hold this call open while a helper lives on
    # (measured: with the stop at exit removed, the test hung instead of failing)
    done = subprocess.run(
        [sys.executable, "-P", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True
    )
    pid = int(done.stdout.split()[-1])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return  # gone: stopped on the way out
    os.kill(pid, 9)  # still there: end it, then fail
    pytest.fail(f"the helper (pid {pid}) outlived the server that started it")


def listed_dict(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in spec.items() if key not in ("params_schema", "params_defs")}


class TestAClassesSettings:
    def test_the_settings_come_from_the_helpers_second_answer(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs)
        assert catalogue_over(made, helper).spec(ELSEWHERE) == specs[0]

    def test_a_class_asked_before_the_settings_arrived_waits_for_them(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, gated=True)
        catalogue = catalogue_over(made, helper)
        catalogue.entries()
        answered: List[Dict[str, Any]] = []
        asking = threading.Thread(target=lambda: answered.append(catalogue.spec(ELSEWHERE)))
        asking.start()
        assert not catalogue.settled()  # the helper is held at its gate: the answer cannot be in yet
        helper.release()
        asking.join()
        assert answered == [specs[0]]

    def test_a_class_whose_module_the_server_imported_is_described_at_once(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        # The helper never gives its second answer (it waits at a gate nobody opens); Threshold's module is imported
        # here, so its settings are described in this process — no wait.
        entries = [*[listed_dict(spec) for spec in specs], listed(describe(Threshold))]
        helper = Helper(tmp_path, entries, specs, gated=True)
        catalogue = catalogue_over(made, helper)
        spec = catalogue.spec(THRESHOLD)
        assert "low_level" in spec["params_schema"]
        assert spec == asdict(describe(Threshold))
        assert not catalogue.settled()

    def test_a_class_only_the_server_registered_is_described_in_the_server(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, gated=True)
        assert "gain" in catalogue_over(made, helper).spec(SERVER_ONLY)["params_schema"]

    def test_an_unknown_class_names_the_close_ones(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs)
        with pytest.raises(
            KeyError, match=r"no node class 'somewhere.not_imported.Elsewher' .*did you mean .*Elsewhere"
        ):
            catalogue_over(made, helper).spec("somewhere.not_imported.Elsewher")

    def test_settings_the_helper_never_gave_are_refused(
        self, tmp_path: Path, made: List[Catalogue], specs: List[Dict[str, Any]]
    ) -> None:
        helper = Helper(tmp_path, [listed_dict(spec) for spec in specs], specs, answers=1, exit_code=4)
        with pytest.raises(CatalogueUnavailable, match=r"exit code 4"):
            catalogue_over(made, helper).spec(ELSEWHERE)


# ------------------------------------------------------------------------------------------------
# the routes and the page
# ------------------------------------------------------------------------------------------------


class TestTheRoutes:
    """``GET api/palette`` answers the list, ``GET api/palette/<class>`` one class with its settings, and serving the
    editor's page starts the helper — over a stand-in helper put where the server reads it."""

    @pytest.fixture
    def helper(self, tmp_path: Path, specs: List[Dict[str, Any]]) -> Helper:
        folder = tmp_path / "helper"
        folder.mkdir()
        return Helper(folder, [listed_dict(spec) for spec in specs], specs)

    @pytest.fixture
    def installed(self, monkeypatch: pytest.MonkeyPatch, made: List[Catalogue], helper: Helper) -> Catalogue:
        catalogue = catalogue_over(made, helper)
        monkeypatch.setattr(catalogue_module, "CATALOGUE", catalogue)
        return catalogue

    @pytest.fixture
    def page(self, tmp_path: Path) -> Path:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>editor</title>")
        return dist

    @pytest.fixture
    def client(self, tmp_path: Path, page: Path) -> Any:  # flask.testing.FlaskClient
        from streamstudio.app import create_app

        graphs = tmp_path / "graphs"
        graphs.mkdir()
        return create_app(graphs, web_dist=page).test_client()

    def test_the_list_has_no_settings_and_a_class_has_them(self, client: Any, installed: Catalogue) -> None:
        entries = client.get("/api/palette").get_json()
        assert ELSEWHERE in [entry["class_path"] for entry in entries]
        assert all("params_schema" not in entry and "params_defs" not in entry for entry in entries)
        answer = client.get(f"/api/palette/{ELSEWHERE}")
        assert answer.status_code == 200
        assert answer.get_json()["params_schema"] == {"level": {"type": "number", "default": 1.0}}

    def test_refresh_runs_a_new_helper(self, client: Any, installed: Catalogue, helper: Helper) -> None:
        assert client.get("/api/palette").status_code == 200
        assert client.get("/api/palette?refresh=1").status_code == 200
        assert helper.started == 2

    def test_an_unknown_class_is_a_404_naming_the_close_ones(self, client: Any, installed: Catalogue) -> None:
        answer = client.get("/api/palette/somewhere.not_imported.Elsewher")
        assert answer.status_code == 404
        assert "did you mean somewhere.not_imported.Elsewhere" in answer.get_json()["error"]

    def test_a_helper_that_stops_is_a_503_in_words(
        self, client: Any, monkeypatch: pytest.MonkeyPatch, made: List[Catalogue], tmp_path: Path
    ) -> None:
        folder = tmp_path / "failing"
        folder.mkdir()
        failing = catalogue_over(made, Helper(folder, [], [], answers=0, exit_code=5))
        monkeypatch.setattr(catalogue_module, "CATALOGUE", failing)
        answer = client.get("/api/palette")
        assert answer.status_code == 503
        assert answer.get_json() == {
            "error": "the catalogue's helper process stopped before it answered (exit code 5) — "
            "what it printed is in this server's log"
        }

    def test_serving_the_page_starts_the_helper_and_the_api_alone_does_not(
        self, client: Any, installed: Catalogue, helper: Helper
    ) -> None:
        assert client.get("/api/graphs").status_code == 200
        assert installed.running() is None and helper.started == 0
        assert client.get("/").status_code == 200
        assert client.get("/").status_code == 200  # a second page (another tab, a reload) starts nothing more
        client.get("/api/palette")  # waits for the helper the page started
        assert helper.started == 1

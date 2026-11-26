"""The canvas page (``frontend/``) against the real routes, driven by headless chromium.

The page is a drawing of the document (``docs/rewrite.md`` §0, §2.1), so every assertion here compares
the DOM with what the routes answer for the SAME temporary graph directory: the nodes and wires of
``GET /api/graphs/<name>`` are what is rendered, a handle's type is the socket's type, a widget edit
lands in the YAML the route serves, a node drag in the positions it serves, and the text after edits is
what :class:`streamstudio.document.Document` produces from the file. Edits are held by the editor until
File ▸ Save (Explicit Save): an edit marks the page ``data-dirty`` and leaves the file byte-identical;
Save writes the document and the sidecar (``save_file``). The synthetic documents are the shape
``test_app.py`` uses — recordstream's own numpy ops plus a few tiny classes defined HERE and spelled by
the module name pytest imported this file under.

What the drags pin, and the rule they measure: a drag between sockets whose types do not meet (an
``Image`` output onto a ``Label`` input) adds no edge — React Flow's ``isValidConnection`` refuses it
while dragging; a drag between sockets whose types meet is sent to ``POST …/wires`` and the canvas draws
the graph the server answers: the wire saved in the document's own spelling (``classes: !ref:hugging_face_source``,
``input: image``), or — for a wire the editor cannot save yet (a Stream's record wires ARE its ``ops:``
order, a flow's are ``from:`` / ``bind:``) — one plain line in a toast and NO edge: the page never draws
an edge of its own. A selected wire and Delete is ``POST …/wires/remove``.

The tests read the SPEC through ``data-*`` attributes, never DOM classes, wherever the page can carry
it: ``data-node`` / ``data-kind`` / ``data-class`` on a node, ``data-node`` / ``data-socket`` /
``data-type`` / ``data-kind`` on a handle, ``data-param`` on a widget, ``data-problem`` in the status
bar, ``data-toast`` on a toast, ``data-saves`` (accepted writes so far) and ``data-ready`` on the app
root. An EDGE is React Flow's element: it is counted by ``react-flow__edge`` and found by its ``data-id``, which the
page makes from the wire's two ends (``wire:source.class_names->contract.classes``).

The host classes at the end (``TestMountedInAHost``, ``TestSetSourceInAHost``, ``TestTheHostHearsAndFollows``,
``TestTheHostsOpenKeepsEachEditInItsGraph``, ``TestTheEmbeddedPanelsFields``, ``TestTheEmbeddedPanelOpensFitted``) serve
the page the way a consuming workspace does: a host Flask app of its own with both blueprints under ``/studio`` and a
HOST object (``streamstudio.app.EditorHost``) that names the folder, supplies the record a pane graph runs on and hears
of every Save; its own page holds the editor in an iframe and keeps the messages the page posts. The HuggingFace
lookups are replaced on the package there — no test touches the network.

Skipped, with the reason, when the package carries no built page (no ``streamstudio/canvas/index.html`` — it is
committed, so only a deleted build skips) or headless chromium is not installed — the build is ``cd
streamstudio/frontend && npm install && npm run build``.
"""

import json
import re
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import pytest
from confluid import configurable
from flask import Flask
from PIL import Image as PILImage
from recordstream import Algorithm, Image, Input, Label, Output, Record
from werkzeug.serving import make_server

import streamstudio
from streamstudio import layout
from streamstudio.app import (
    EXTENSION,
    PLACE_VARIABLES,
    RECOVERY_FILE,
    HostRecord,
    Studio,
    create_app,
    editor_blueprint,
    page_blueprint,
    starter_text,
    studio_of,
)
from streamstudio.document import ALREADY_EXISTS, Document

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (pip install playwright)"
)

DIST = Path(__file__).resolve().parents[1] / "streamstudio" / "canvas"
BUILD = "cd streamstudio/frontend && npm install && npm run build"
if not (DIST / "index.html").is_file():
    pytest.skip(f"the canvas page is not built ({DIST / 'index.html'} is absent): {BUILD}", allow_module_level=True)

MOD = __name__

#: How far a node is dragged, in pixels — at zoom 1 a screen pixel is a flow pixel, and both deltas are
#: multiples of the page's 10-px snap grid, so the saved position is the start plus exactly this.
DRAG = (120, 80)

#: A screen point on the EMPTY pane of every document here (the layered layout keeps the nodes in the top rows,
#: the minimap sits bottom-right and the controls bottom-left), where a double-click opens the palette.
EMPTY = (600, 700)

#: How many rows the palette renders at most — the catalogue is hundreds of classes; the rest is reached by typing.
PALETTE_ROWS = 200


# ------------------------------------------------------------------------------------------------
# synthetic classes — the documents below spell them as !class:<MOD>.<Class>
# ------------------------------------------------------------------------------------------------


@configurable(category="source", group="canvas-test")
class TinySource:
    """Two records: a square float image and a label.

    Args:
        size: The side of the square image.
    """

    def __init__(self, size: int = 4) -> None:
        self.size = size

    def __iter__(self) -> Iterator[Record]:
        for index in range(2):
            image = Image(np.full((self.size, self.size), 0.25 * (index + 1), dtype=np.float32))
            yield {"image": image, "class": Label("cat" if index == 0 else "dog")}


@configurable(category="source", group="canvas-test")
class ListedSource:
    """A source whose model comes from a plugin's list (the field is stamped with the list's tag).

    Args:
        model: A model the plugin lists.
    """

    __streamstudio_widget_params__ = {"model": "CANVAS_MODELS"}

    def __init__(self, model: str = "") -> None:
        self.model = model

    def __iter__(self) -> Iterator[Record]:
        return iter([])


@configurable(category="op", group="canvas-test")
class TinyGain:
    """Scale the image entry — declares its interface by NAME.

    Args:
        gain: The multiplier.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, gain: float = 1.0) -> None:
        self.gain = gain

    def __call__(self, record: Record) -> Record:
        return {**record, "image": record["image"] * self.gain}


@configurable(category="op", group="canvas-test")
class TinyShapes:
    """Keep the record — an op whose settings are CONTAINERS of plain values: what the settings form has a field for.

    Args:
        rates_hz: A list of numbers.
        shape: Three whole numbers.
        label_to_index: A whole number per name.
        windows: A list of lists, which no field spells.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(
        self,
        rates_hz: Sequence[float] = (1200.0, 2400.0),
        shape: Tuple[int, int, int] = (3, 224, 224),
        label_to_index: Optional[Dict[str, int]] = None,
        windows: Optional[List[List[float]]] = None,
    ) -> None:
        self.rates_hz = list(rates_hz)
        self.shape = tuple(shape)
        self.label_to_index = dict(label_to_index or {})
        self.windows = windows

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="canvas-test")
class TinyClip:
    """An op whose constructor REFUSES a value — the widget-refusal path.

    Args:
        limit: The clip level; must be positive.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, limit: float = 1.0) -> None:
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        self.limit = limit

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="canvas-test")
class TinyDecode(Algorithm):
    """Produce an image (a stand-in decoder)."""

    image: Image = Output(doc="The decoded image.")

    def compute(self) -> Dict[str, Any]:
        return {"image": Image([[0]])}


@configurable(category="op", group="canvas-test")
class TinyCount(Algorithm):
    """Consume a label (a stand-in counter)."""

    label: Label = Input(doc="The label to count.")
    count: int = Output(doc="How many.")

    def compute(self) -> Dict[str, Any]:
        return {"count": 1}


#: How many parameters ``TinyWide`` takes — more than a hover card has room for in a 900-px window.
WIDE = 40


def _wide_class() -> type:
    """``TinyWide``: an op whose constructor takes ``WIDE`` documented parameters.

    A class's parameters ARE its constructor's signature (the palette reads them, with their ``Args:`` lines, straight
    from the class), so a class with forty of them is spelled out as source and executed here rather than typed forty
    times. Defined in THIS module, so the palette names it ``<MOD>.TinyWide`` like the other tiny classes.
    """
    params = ", ".join(f"knob_{index:02d}: float = {index}.0" for index in range(WIDE))
    args = "".join(
        f"            knob_{index:02d}: Knob {index}, a gain this op would scale by.\n" for index in range(WIDE)
    )
    body = "".join(f"        self.knob_{index:02d} = knob_{index:02d}\n" for index in range(WIDE))
    source = (
        "class TinyWide:\n"
        '    """An op with many parameters — the hover card cannot list them all.\n\n'
        "        Args:\n"
        f"{args}"
        '    """\n\n'
        "    consumes = {'image': 'Image'}\n"
        "    produces = {'image': 'Image'}\n\n"
        f"    def __init__(self, {params}) -> None:\n"
        f"{body}\n"
        "    def __call__(self, record: Record) -> Record:\n"
        "        return record\n"
    )
    namespace: Dict[str, Any] = {"__name__": __name__, "Record": Record}
    exec(source, namespace)  # noqa: S102 - a fixture class built from the text above, nothing external
    wide = configurable(category="op", group="canvas-test")(namespace["TinyWide"])
    assert isinstance(wide, type)
    return wide


TinyWide = _wide_class()


# ------------------------------------------------------------------------------------------------
# the documents
# ------------------------------------------------------------------------------------------------

PETS_TEXT = f"""\
# a two-record stream
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{size: 4}}
  ops:
    - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}   # the mask
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

# decode -> gain -> boost -> count: an Image output on `decode`, an Image input on `boost` that is
# NOT yet wired from `decode` (its wire comes from `gain`), a Label input on `count` (the host feeds it).
WIRES_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.TinyDecode {{}}
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
    from: decode
  boost:
    op: !class:{MOD}.TinyGain {{gain: 3.0}}
    from: gain
  count:
    op: !class:{MOD}.TinyCount {{}}
    from: boost
outputs: count
"""

CLIP_TEXT = f"""\
flow:
  clip:
    op: !class:{MOD}.TinyClip {{limit: 1.0}}
outputs: clip
"""

MISMATCH_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.TinyDecode {{}}
  count:
    op: !class:{MOD}.TinyCount {{keys: {{label: image}}}}
    from: decode
"""

#: One op whose settings are containers of plain values (:class:`TinyShapes`), one of them set.
SHAPES_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{size: 2}}
  ops:
    - !class:{MOD}.TinyShapes {{label_to_index: {{cat: 0, dog: 1}}}}
"""

DOCUMENTS = {"pets": PETS_TEXT, "wires": WIRES_TEXT, "clip": CLIP_TEXT, "mismatch": MISMATCH_TEXT}


# ------------------------------------------------------------------------------------------------
# the harness: the app on a free port in a thread, headless chromium, one page per graph
# ------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:  # playwright.sync_api.Browser — resolved at runtime through the importorskip'd module
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except playwright_api.Error as exc:  # the browser binary is absent
            pytest.skip(f"headless chromium is not installed (playwright install chromium): {exc}")
        yield browser
        browser.close()


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    for name, text in DOCUMENTS.items():
        (tmp_path / f"{name}.yaml").write_text(text)
    return tmp_path


@pytest.fixture
def templates_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The editor's templates folder — a temporary one, never the person's ``~/.streamstudio/templates``."""
    return tmp_path_factory.mktemp("templates")


@pytest.fixture
def server(graph_dir: Path, templates_dir: Path) -> Iterator[str]:
    """``create_app`` over the graph directory, served on a free port by werkzeug in a daemon thread."""
    httpd = make_server("127.0.0.1", 0, create_app(graph_dir, templates_dir=templates_dir), threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, name="streamstudio-canvas-test", daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    thread.join(timeout=10)


class Opened(NamedTuple):
    """One page on one graph, with every console error and page error it produced."""

    page: Any  # playwright.sync_api.Page
    errors: List[str]


def api_get(server: str, path: str) -> Any:  # the route's JSON: an object or a list
    with urllib.request.urlopen(f"{server}/{path}") as response:
        return json.loads(response.read())


def api_put(server: str, path: str, body: Dict[str, Any]) -> Any:  # the route's JSON answer to a PUT
    request = urllib.request.Request(
        f"{server}/{path}", data=json.dumps(body).encode(), method="PUT", headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def api_post(server: str, path: str, body: Dict[str, Any]) -> Any:  # the route's JSON answer to a POST
    request = urllib.request.Request(
        f"{server}/{path}", data=json.dumps(body).encode(), method="POST", headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def wait_saves(page: Any, count: int) -> None:
    """``data-saves`` counts the writes the server accepted — a test waits on it, never on time."""
    page.wait_for_selector(f'.app[data-saves="{count}"]')


def open_menu(page: Any, menu: str) -> None:
    """Open a menu of the bar by its title (``[data-menu=<id>]``); its items exist in the page only while it is open."""
    page.locator(f'[data-menu="{menu}"] > .menubar-title').click()
    page.wait_for_selector(f'[data-menu="{menu}"] .dropdown')


def menu_command(page: Any, menu: str, command: str) -> None:
    """Pick ``command`` from the bar's ``menu`` — the way a person reaches what the toolbar buttons used to do."""
    open_menu(page, menu)
    page.locator(f'[data-menu="{menu}"] [data-command="{command}"]').click()


def copy_selection(page: Any) -> None:
    """Ctrl+C, then wait for the copy's answer (its toast): Paste acts only once the page holds the copied text."""
    page.keyboard.press("Control+c")
    page.locator('[data-toast="info"]', has_text="copied").last.wait_for()


def save_file(page: Any, count: int) -> None:
    """File ▸ Save, then wait until the server's answer is drawn: ``count`` accepted writes and nothing unsaved."""
    menu_command(page, "file", "save")
    wait_saves(page, count)
    page.wait_for_selector('.app[data-dirty="false"]')


def meets(source: str, target: str, types: Dict[str, List[str]]) -> bool:
    """The page's ``compatible`` (``document._meets`` with the ``GET /api/types`` table), for the expected counts."""
    return source == target or "*" in (source, target) or target in types.get(source, [])


@pytest.fixture
def open_graph(browser: Any, server: str) -> Iterator[Callable[[str], Opened]]:
    """Open ``?graph=<name>`` and wait until the model is loaded and every node measured."""
    pages: List[Any] = []

    def _open(name: str) -> Opened:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
        page.goto(f"{server}/?graph={name}")
        wait_ready(page, name)
        pages.append(page)
        return Opened(page, errors)

    yield _open
    for page in pages:
        page.close()


#: A node's root: the one `data-node` element without a `data-socket` (every handle carries `data-node` too).
NODE = "[data-node]:not([data-socket])"


def node_root(name: str) -> str:
    return f'[data-node="{name}"]:not([data-socket])'


def wait_ready(page: Any, name: str) -> None:
    page.wait_for_selector(f'.app[data-graph="{name}"][data-ready="true"]')
    page.wait_for_selector(f'.canvas[data-graph="{name}"][data-initialized="true"]')


#: How long the page may wait for the catalogue. The FIRST ask of a test process builds it — a helper process importing
#: every installed package, 20-28 s on the development machine when idle, past the 30 s default under load — so a test
#: run alone (or one whose earlier test did not build it) waits that long; every later ask answers at once.
COLD_CATALOGUE_MS = 120_000


def wait_palette_loaded(page: Any) -> None:
    """The page asks ``GET api/palette`` on FIRST NEED — when the Nodes sidebar, Add Node ▸, the palette or Set source…
    is first shown, never as it opens; ``data-palette-ready`` says the catalogue has arrived. Call it right after the
    gesture that shows one of them, so what the test reads next is the whole catalogue."""
    page.wait_for_selector('.app[data-palette-ready="true"]', timeout=COLD_CATALOGUE_MS)


def box_select(page: Any, *names: str) -> None:
    """React Flow's own box selection — Shift held while dragging on the pane over the named nodes' boxes."""
    boxes = [page.locator(node_root(name)).bounding_box() for name in names]
    left = min(box["x"] for box in boxes) - 15
    top = min(box["y"] for box in boxes) - 15
    right = max(box["x"] + box["width"] for box in boxes) + 15
    bottom = max(box["y"] + box["height"] for box in boxes) + 15
    page.keyboard.down("Shift")
    drag(page, (left, top), (right, bottom))
    page.keyboard.up("Shift")


def selected_nodes(page: Any) -> List[str]:
    return sorted(page.locator(".react-flow__node.selected").evaluate_all("ns => ns.map(n => n.dataset.id)"))


def node_boxes(page: Any, *names: str) -> Dict[str, Dict[str, float]]:
    return {name: page.locator(node_root(name)).bounding_box() for name in names}


def handle(page: Any, node: str, socket: str, kind: str) -> Any:  # a playwright Locator
    return page.locator(f'.react-flow__handle[data-node="{node}"][data-socket="{socket}"][data-kind="{kind}"]')


def centre(box: Dict[str, float]) -> Tuple[float, float]:
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def drag(page: Any, start: Tuple[float, float], stop: Tuple[float, float], steps: int = 12) -> None:
    """A REAL mouse drag: down at ``start``, move in ``steps`` to ``stop``, up."""
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*stop, steps=steps)
    page.mouse.up()


def edge_count(page: Any) -> int:
    return int(page.locator(".react-flow__edge").count())


def wait_edges(page: Any, count: int) -> None:
    """Wait until the canvas DRAWS ``count`` edges: React Flow draws a new node's edges only once it has measured
    the node, a frame or two after the answer that added it (measured: 4 of 8 drawn when read at once)."""
    page.wait_for_function("n => document.querySelectorAll('.react-flow__edge').length === n", arg=count)


def node_translate(page: Any, node: str) -> Tuple[float, float]:
    """The flow-coordinate position React Flow renders a node at (its wrapper's ``translate``)."""
    style = page.locator(f'.react-flow__node[data-id="{node}"]').get_attribute("style") or ""
    match = re.search(r"translate\(\s*(-?[\d.]+)px\s*,\s*(-?[\d.]+)px\s*\)", style)
    assert match, f"no translate in {style!r}"
    return float(match.group(1)), float(match.group(2))


# ------------------------------------------------------------------------------------------------
# loading
# ------------------------------------------------------------------------------------------------


class TestLoading:
    def test_the_page_loads_with_zero_console_errors(self, open_graph: Callable[[str], Opened]) -> None:
        opened = open_graph("pets")
        assert opened.page.title() == "streamstudio editor"
        assert opened.errors == []

    def test_the_graph_picker_lists_every_graph_and_switches_between_them(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        listed = [entry["name"] for entry in api_get(server, "api/graphs")]
        options = opened.page.locator("select[data-graph-picker] option").evaluate_all("os => os.map(o => o.value)")
        assert options == listed == ["clip", "mismatch", "pets", "wires"]
        opened.page.locator("select[data-graph-picker]").select_option("wires")
        wait_ready(opened.page, "wires")
        names = opened.page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)")
        assert sorted(names) == sorted(node["name"] for node in api_get(server, "api/graphs/wires")["model"]["nodes"])
        assert opened.errors == []

    def test_every_node_of_the_model_is_rendered_by_name_kind_and_class(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        model = api_get(server, "api/graphs/pets")["model"]
        rendered = opened.page.locator(NODE)
        assert rendered.count() == len(model["nodes"]) == 5
        for node in model["nodes"]:
            element = opened.page.locator(node_root(node["name"]))
            assert element.count() == 1, node["name"]
            assert element.get_attribute("data-kind") == node["kind"]
            assert element.get_attribute("data-class") == node["class_path"]
            assert node["name"] in element.inner_text()
            assert node["class_path"].rsplit(".", 1)[-1] in element.inner_text()  # the class short name
        assert opened.errors == []

    def test_every_wire_of_the_model_is_an_edge(self, open_graph: Callable[[str], Opened], server: str) -> None:
        opened = open_graph("pets")
        model = api_get(server, "api/graphs/pets")["model"]
        assert edge_count(opened.page) == len(model["wires"]) == 6

    def test_a_handles_data_type_is_the_sockets_type_from_the_model(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        model = api_get(server, "api/graphs/wires")["model"]
        checked = 0
        for node in model["nodes"]:
            for kind, sockets in (("in", node["inputs"]), ("out", node["outputs"])):
                for socket in sockets:
                    element = handle(opened.page, node["name"], socket["name"], kind)
                    assert element.count() == 1, (node["name"], socket["name"], kind)
                    assert element.get_attribute("data-type") == socket["type"]
                    checked += 1
        assert checked >= 12  # decode: record, op, image; gain/boost: record, image | record, op, image; count; host
        assert handle(opened.page, "decode", "image", "out").get_attribute("data-type") == "Image"
        assert handle(opened.page, "count", "label", "in").get_attribute("data-type") == "Label"

    def test_the_status_bar_names_the_graph_and_its_located_problems(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("mismatch")
        body = api_get(server, "api/graphs/mismatch")
        assert len(body["model"]["problems"]) == 1
        assert opened.page.locator("[data-status-graph]").inner_text() == "mismatch"
        problems = opened.page.locator("[data-problem]")
        assert problems.count() == 1
        text = problems.first.inner_text()
        assert "the types do not meet" in text
        assert f"{graph_dir / 'mismatch.yaml'}:5:9" in text  # the located message, verbatim from the model
        assert opened.errors == []

    def test_the_sidebar_shows_the_documents_text(self, open_graph: Callable[[str], Opened]) -> None:
        opened = open_graph("pets")
        assert opened.page.locator("[data-yaml]").inner_text().rstrip("\n") == PETS_TEXT.rstrip("\n")


# ------------------------------------------------------------------------------------------------
# wires: the types are tested while dragging; a drawn wire is saved by the server, or refused in one line
# ------------------------------------------------------------------------------------------------


def edge(page: Any, source: str, source_socket: str, target: str, target_socket: str) -> Any:  # a playwright Locator
    """The edge the page draws for one wire — its ``data-id`` spells the two ends."""
    return page.locator(f'.react-flow__edge[data-id="wire:{source}.{source_socket}->{target}.{target_socket}"]')


def click_wire(page: Any, source: str, source_socket: str, target: str, target_socket: str) -> None:
    """Click ON the wire: a point of its path that no card covers (a wire may run under a card), found in the page."""
    wanted = f"wire:{source}.{source_socket}->{target}.{target_socket}"
    point = page.evaluate(
        """(id) => {
            const g = document.querySelector(`.react-flow__edge[data-id="${id}"]`);
            const path = g.querySelector('.react-flow__edge-interaction') || g.querySelector('path');
            const total = path.getTotalLength();
            const matrix = path.getScreenCTM();
            for (let step = 1; step < 40; step++) {
                const at = path.getPointAtLength((total * step) / 40);
                const x = at.x * matrix.a + at.y * matrix.c + matrix.e;
                const y = at.x * matrix.b + at.y * matrix.d + matrix.f;
                const hit = document.elementFromPoint(x, y);
                if (hit && hit.closest(`.react-flow__edge[data-id="${id}"]`)) return [x, y];
            }
            return null;
        }""",
        wanted,
    )
    assert point is not None, f"no point of {wanted} is uncovered"
    page.mouse.click(*point)
    page.wait_for_selector(f'.react-flow__edge.selected[data-id="{wanted}"]')


def warning(page: Any) -> str:
    toast = page.locator('[data-toast="warning"]')
    toast.wait_for()
    return str(toast.inner_text())


class TestDrawingWires:
    def test_a_drag_between_types_that_do_not_meet_adds_no_edge(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        before = edge_count(opened.page)
        source = centre(handle(opened.page, "decode", "image", "out").bounding_box())
        target = centre(handle(opened.page, "count", "label", "in").bounding_box())
        drag(opened.page, source, target)
        assert edge_count(opened.page) == before
        assert opened.page.locator("[data-toast]").count() == 0
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_a_wire_in_a_flow_is_refused_in_one_line_and_nothing_is_drawn(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        before = edge_count(opened.page)
        source = centre(handle(opened.page, "decode", "image", "out").bounding_box())
        target = centre(handle(opened.page, "boost", "image", "in").bounding_box())
        drag(opened.page, source, target)
        assert warning(opened.page) == "The editor can't save wires in this kind of graph yet."
        assert edge_count(opened.page) == before  # nothing drawn: the page never draws an edge of its own
        body = api_get(server, "api/graphs/wires")
        assert body["text"] == WIRES_TEXT and body["dirty"] is False
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == [REFUSED_400]

    def test_a_record_wire_on_a_stream_would_change_the_order_of_the_steps(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        before = edge_count(opened.page)
        source = centre(handle(opened.page, "stream.ops[0]", "record", "out").bounding_box())
        target = centre(handle(opened.page, "stream.ops[2]", "record", "in").bounding_box())
        drag(opened.page, source, target)
        assert warning(opened.page) == "This wire would change the order of the steps — the editor can't save that yet."
        assert edge_count(opened.page) == before
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == [REFUSED_400]


#: The source graph a classification workspace seeds: a HuggingFace source in the Stream's slot, the root after it.
SEED_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
  ops: []
# The graph's ROOT — what this graph must deliver, declared by the workspace (its `graphs:`).
# Wire what each output needs; the workspace applies the graph through this root's own check.
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
"""

#: The seed after ``stream.source.class_names → contract.classes``: the source moved out to a key named after its
#: class, its slot a reference.
WIRED_TEXT = """\
hugging_face_source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
stream: !class:recordstream.core.stream.Stream
  source: !ref:hugging_face_source
  ops: []
# The graph's ROOT — what this graph must deliver, declared by the workspace (its `graphs:`).
# Wire what each output needs; the workspace applies the graph through this root's own check.
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
    classes: !ref:hugging_face_source
"""


def drag_wire(page: Any, source: str, source_socket: str, target: str, target_socket: str) -> None:
    start = centre(handle(page, source, source_socket, "out").bounding_box())
    stop = centre(handle(page, target, target_socket, "in").bounding_box())
    drag(page, start, stop)


class TestWiringTheSourceGraph:
    """The user's case on the built page: the seeded source graph, wired by hand, saved, opened again."""

    def test_a_drawn_wire_is_saved_and_drawn_again_after_a_reload(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "source.yaml").write_text(SEED_TEXT)
        opened = open_graph("source")
        page = opened.page
        place = node_translate(page, "stream.source")
        drag_wire(page, "stream.source", "class_names", "contract", "classes")
        wait_saves(page, 1)
        assert page.locator(node_root("hugging_face_source")).count() == 1  # moved out, named after its class
        assert page.locator(node_root("stream.source")).count() == 0
        assert node_translate(page, "hugging_face_source") == place  # and kept its place
        edge(page, "hugging_face_source", "class_names", "contract", "classes").wait_for()
        assert page.locator("[data-yaml]").inner_text().rstrip("\n") == WIRED_TEXT.rstrip("\n")
        assert (graph_dir / "source.yaml").read_text() == SEED_TEXT  # held until Save
        save_file(page, 2)
        assert (graph_dir / "source.yaml").read_text() == WIRED_TEXT

        page.reload()
        wait_ready(page, "source")
        assert edge(page, "hugging_face_source", "class_names", "contract", "classes").count() == 1
        drag_wire(page, "hugging_face_source", "image", "contract", "input")
        wait_saves(page, 1)
        edge(page, "hugging_face_source", "image", "contract", "input").wait_for()
        assert api_get(server, "api/graphs/source")["text"] == WIRED_TEXT + "    input: image\n"
        assert opened.errors == []

    def test_a_selected_wire_and_delete_take_it_out_in_one_edit(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "source.yaml").write_text(WIRED_TEXT + "    input: image\n")
        opened = open_graph("source")
        page = opened.page
        before = edge_count(page)
        click_wire(page, "hugging_face_source", "image", "contract", "input")
        page.keyboard.press("Delete")
        wait_saves(page, 1)
        wait_edges(page, before - 1)
        assert edge(page, "hugging_face_source", "image", "contract", "input").count() == 0
        assert api_get(server, "api/graphs/source")["text"] == WIRED_TEXT
        page.keyboard.press("Control+z")  # one undo brings it back
        wait_saves(page, 2)
        wait_edges(page, before)
        assert api_get(server, "api/graphs/source")["text"] == WIRED_TEXT + "    input: image\n"
        assert opened.errors == []

    def test_the_root_card_shows_its_sockets_and_the_contracts_name_only(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "source.yaml").write_text(SEED_TEXT)
        opened = open_graph("source")
        root = opened.page.locator(node_root("contract"))
        assert root.locator("[data-subtitle]").inner_text() == "classification source"
        assert root.locator("[data-param]").count() == 0  # no rows for name, inputs, outputs, records, delivered
        sockets = root.locator('.react-flow__handle[data-kind="in"]').evaluate_all(
            "hs => hs.map(h => h.dataset.socket)"
        )
        assert sockets == ["stream", "classes", "input", "target"]
        outputs = opened.page.locator('.react-flow__handle[data-node="stream.source"][data-kind="out"]').evaluate_all(
            "hs => hs.map(h => [h.dataset.socket, h.dataset.type])"
        )
        assert outputs == [["stream", "Source"], ["image", "Image"], ["class", "Label"], ["class_names", "List[str]"]]
        assert opened.errors == []

    def test_a_record_entry_read_from_a_key_no_card_writes_shows_the_key_on_its_row(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "source.yaml").write_text(SEED_TEXT + "    input: pixels\n")
        opened = open_graph("source")
        filled = opened.page.locator(f'{node_root("contract")} [data-filled]')
        assert filled.evaluate_all("fs => fs.map(f => f.dataset.filled)") == ["pixels"]
        assert edge(opened.page, "stream.source", "image", "contract", "input").count() == 0  # no card writes it
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# widgets: a change is POST .../param; the served YAML carries it; a refusal is a toast and a revert
# ------------------------------------------------------------------------------------------------


class TestWidgets:
    def test_editing_a_number_widget_patches_the_value_into_the_yaml_and_keeps_the_comment(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        widget = opened.page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        assert widget.input_value() == "0.5"
        widget.fill("0.9")
        widget.press("Enter")
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/pets")["text"]
        assert "low_level: 0.9}   # the mask" in text
        assert text.count("\n") == PETS_TEXT.count("\n")  # one line changed, none added
        assert opened.page.locator("[data-yaml]").inner_text().rstrip("\n") == text.rstrip("\n")
        assert widget.input_value() == "0.9"
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT  # held by the editor until Save
        save_file(opened.page, 2)
        assert (graph_dir / "pets.yaml").read_text() == text
        assert opened.errors == []

    def test_a_value_the_constructor_refuses_shows_the_located_message_and_reverts_the_widget(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("clip")
        widget = opened.page.locator(f'{node_root("clip")} input[data-param="limit"]')
        assert widget.input_value() == "1"
        widget.fill("-1")
        widget.press("Enter")
        toast = opened.page.locator('[data-toast="error"]')
        toast.wait_for()
        message = toast.inner_text()
        assert "limit must be positive, got -1" in message
        assert f"{graph_dir / 'clip.yaml'}:3:9" in message
        assert widget.input_value() == "1"
        assert (graph_dir / "clip.yaml").read_text() == CLIP_TEXT
        assert opened.page.locator(".app").get_attribute("data-saves") == "0"
        # a refusal IS a 400 (the route's contract), and Chrome logs exactly that — nothing else went wrong
        assert opened.errors == [
            "console.error: Failed to load resource: the server responded with a status of 400 (BAD REQUEST)"
        ]

    def test_the_text_after_edits_is_what_document_produces(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        widget = opened.page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        widget.fill("0.75")
        widget.press("Enter")
        opened.page.wait_for_selector('.app[data-saves="1"]')
        save_file(opened.page, 2)
        served = api_get(server, "api/graphs/pets")["text"]
        document = Document.open(graph_dir / "pets.yaml")
        assert served == document.text == (graph_dir / "pets.yaml").read_text()
        assert document.model.node("stream.ops[1]").params["low_level"].value == 0.75
        assert document.check() == []


class TestAPluginsListIsAskedAgainOnTheWord:
    """A plugin's list is asked once per page; the list's last entry, "↻ ask again", asks it again — a model packaged
    after the list was first opened shows only then. Measured before: the page kept the first answer until it was
    reloaded, and an annotation workspace never reloads its settings panel."""

    def test_ask_again_shows_what_the_plugin_lists_now_and_picks_nothing(
        self,
        open_graph: Callable[[str], Opened],
        server: str,
        graph_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from streamstudio import lookups, palette

        models = ["$RUN_DIR/a/package"]
        asked: List[str] = []

        def list_models(values: Mapping[str, str]) -> Tuple[List[str], str]:
            asked.append("asked")
            return list(models), ""

        monkeypatch.setattr(lookups, "_FOUND", {"CANVAS_MODELS": list_models})
        monkeypatch.setattr(palette, "_cache", None)
        monkeypatch.setattr(palette, "_cache_scanned", False)
        monkeypatch.setattr(palette, "_index", {})  # the classes `spec_for` described one by one
        (graph_dir / "listed.yaml").write_text(f"source:\n  _target_: {MOD}.ListedSource\n")
        opened = open_graph("listed")
        page = opened.page
        picker = page.locator(f'{node_root("source")} select[data-picker="plugin"][data-picker-for="model"]')
        offered = "os => os.filter(o => !o.disabled).map(o => o.value)"

        picker.hover()
        page.wait_for_selector(f'{node_root("source")} select[data-picker-for="model"][data-picker-state="ready"]')
        assert picker.locator("option").evaluate_all(offered) == ["", "$RUN_DIR/a/package", "\u0000ask-again"]
        assert picker.locator("option").evaluate_all("os => os.map(o => o.textContent)")[-1] == "↻ ask again"

        models.append("$RUN_DIR/b/package")  # packaged while the page is open
        page.locator(node_root("source")).hover()
        picker.hover()
        assert picker.get_attribute("data-picker-count") == "1", "reaching for it again keeps the page's answer"

        picker.select_option("\u0000ask-again")
        page.wait_for_selector(
            f'{node_root("source")} select[data-picker-for="model"][data-picker-state="ready"][data-picker-count="2"]'
        )
        assert picker.locator("option").evaluate_all(offered) == [
            "",
            "$RUN_DIR/a/package",
            "$RUN_DIR/b/package",
            "\u0000ask-again",
        ]
        assert asked == ["asked", "asked"]
        assert page.locator(".app").get_attribute("data-saves") == "0", "asking again picks nothing"
        assert "$RUN_DIR" not in api_get(server, "api/graphs/listed")["text"]
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# positions: a drag is PUT .../positions; a reload draws the node where the sidecar says
# ------------------------------------------------------------------------------------------------


class TestPositions:
    def test_dragging_a_node_holds_its_position_save_writes_it_and_a_reload_restores_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        before = tuple(api_get(server, "api/graphs/wires")["positions"]["gain"])
        assert node_translate(opened.page, "gain") == before
        title = opened.page.locator(f'{node_root("gain")} .title').bounding_box()
        start = centre(title)
        drag(opened.page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        opened.page.wait_for_selector('.app[data-saves="1"]')
        expected = (before[0] + DRAG[0], before[1] + DRAG[1])
        assert tuple(api_get(server, "api/graphs/wires")["positions"]["gain"]) == expected
        assert opened.page.locator(".app").get_attribute("data-dirty") == "true"
        assert not (graph_dir / "wires.layout.yaml").exists()  # held by the editor until Save
        save_file(opened.page, 2)
        assert layout.read_sidecar(graph_dir / "wires.layout.yaml")["gain"] == expected
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # a position never touches the document
        opened.page.reload()
        wait_ready(opened.page, "wires")
        assert node_translate(opened.page, "gain") == expected
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# the palette: double-click or a dropped wire lists the classes; a pick is POST .../steps
# ------------------------------------------------------------------------------------------------


class TestPalette:
    def test_double_click_on_empty_canvas_opens_the_palette_of_every_class_the_route_lists(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        # the first palette asks for the catalogue; it is closed again, so the open measured below is the palette's
        # own and never the fetch's
        opened.page.mouse.dblclick(*EMPTY)
        wait_palette_loaded(opened.page)
        opened.page.keyboard.press("Escape")
        opened.page.locator("[data-palette]").wait_for(state="detached")
        # every class the route lists but the one no list offers (an empty subgraph is refused; SUBGRAPH below)
        specs = [spec for spec in api_get(server, "api/palette") if spec["class_path"] != SUBGRAPH]
        assert len(specs) > PALETTE_ROWS  # the real catalogue: hundreds of classes, so the cap is exercised
        started = time.perf_counter()
        opened.page.mouse.dblclick(*EMPTY)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        wall_ms = (time.perf_counter() - started) * 1000
        palette = opened.page.locator("[data-palette]")
        assert palette.count() == 1
        assert rows.count() == PALETTE_ROWS
        meta = opened.page.locator("[data-palette-meta]").inner_text()
        assert f"{PALETTE_ROWS} of {len(specs)}" in meta and "type to narrow" in meta
        opened_ms = float(palette.get_attribute("data-opened-ms") or "nan")
        print(
            f"\npalette: {len(specs)} classes, {PALETTE_ROWS} rows rendered; "
            f"wall {wall_ms:.0f} ms, in-page {opened_ms:.1f} ms"
        )
        assert opened_ms < 1000
        # typing narrows: the search box has the focus, the rows follow every keystroke. The class PATH is typed, not
        # "TinyGain": another test module imported in the same run registers a TinyGain of its own.
        needle = f"{MOD}.TinyGain"
        opened.page.keyboard.type(needle)
        matching = [
            spec
            for spec in specs
            if needle.lower() in spec["class_path"].lower() or needle.lower() in spec["name"].lower()
        ]
        assert rows.count() == len(matching) == 1
        assert rows.first.get_attribute("data-class") == f"{MOD}.TinyGain"
        assert f"1 of {len(specs)}" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.keyboard.press("Escape")
        assert palette.count() == 0
        assert opened.errors == []

    def test_picking_a_class_adds_a_step_after_the_last_selected_node_and_draws_it_wired(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        opened.page.locator(f'{node_root("gain")} .title').click()
        assert selected_nodes(opened.page) == ["gain"]
        edges_before = edge_count(opened.page)
        opened.page.mouse.dblclick(*EMPTY)
        wait_palette_loaded(opened.page)
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        rows = opened.page.locator("[data-palette-list] li")
        assert rows.count() == 1
        assert "after gain" in opened.page.locator("[data-palette-meta]").inner_text()
        rows.first.click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        assert opened.page.locator("[data-palette]").count() == 0
        text = api_get(server, "api/graphs/wires")["text"]
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n    from: gain\n" in text
        assert f"  boost:\n    op: !class:{MOD}.TinyGain {{gain: 3.0}}\n    from: threshold\n" in text
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # held by the editor until Save
        opened.page.wait_for_selector(node_root("threshold"))
        wires = api_get(server, "api/graphs/wires")["model"]["wires"]
        assert len(wires) > edges_before
        wait_edges(opened.page, len(wires))
        assert opened.errors == []

    def test_picking_with_nothing_selected_adds_after_the_flows_last_step(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        opened.page.mouse.dblclick(*EMPTY)
        wait_palette_loaded(opened.page)
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        assert "after count" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.locator("[data-palette-list] li").first.click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/wires")["text"]
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n    from: count\n" in text
        assert text.endswith("outputs: threshold\n")
        assert opened.errors == []

    def test_picking_on_a_stream_with_nothing_selected_appends_a_member_to_its_ops_list(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """DESIGN2 §3. Before: the palette said ``added after stream.ops[2]`` and the pick was refused with
        ``'stream.ops[2]' is not a flow step (the steps are: [])``. Now the pick is the list's last member."""
        opened = open_graph("pets")
        opened.page.mouse.dblclick(*EMPTY)
        wait_palette_loaded(opened.page)
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        assert "added at the end of stream.ops" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.locator("[data-palette-list] li").first.click()
        wait_saves(opened.page, 1)
        text = api_get(server, "api/graphs/pets")["text"]
        assert text == PETS_TEXT + "    - !class:recordstream.ops.numpy.Threshold {}\n"
        opened.page.wait_for_selector(f'{node_root("stream.ops[3]")}[data-class="recordstream.ops.numpy.Threshold"]')
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT  # held by the editor until Save
        assert opened.errors == []

    def test_a_wire_dropped_on_empty_canvas_lists_the_classes_with_a_compatible_input_and_the_pick_wires_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        edges_before = edge_count(opened.page)
        source = centre(handle(opened.page, "decode", "image", "out").bounding_box())
        drag(opened.page, source, EMPTY)
        wait_palette_loaded(opened.page)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        expected = [spec for spec in specs if any(meets("Image", socket["type"], types) for socket in spec["inputs"])]
        assert rows.count() == len(expected) >= 2  # TinyGain and TinyClip declare an Image entry
        meta = opened.page.locator("[data-palette-meta]").inner_text()
        assert "Image" in meta and "after decode" in meta
        assert edge_count(opened.page) == edges_before  # a drop on nothing draws no edge
        assert opened.page.locator("[data-toast]").count() == 0
        opened.page.locator(f'[data-palette-list] li[data-class="{MOD}.TinyGain"]').click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/wires")["text"]
        assert f"  tinygain:\n    op: !class:{MOD}.TinyGain {{}}\n    from: decode\n" in text
        assert f"  gain:\n    op: !class:{MOD}.TinyGain {{gain: 2.0}}\n    from: tinygain\n" in text
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # held by the editor until Save
        wires = api_get(server, "api/graphs/wires")["model"]["wires"]
        assert any(
            wire["source"] == "decode" and wire["source_socket"] == "image" and wire["target"] == "tinygain"
            for wire in wires
        )
        opened.page.wait_for_selector(node_root("tinygain"))
        wait_edges(opened.page, len(wires))
        assert opened.errors == []

    def test_a_wire_dropped_from_an_input_inserts_the_pick_before_that_node(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        target = centre(handle(opened.page, "boost", "image", "in").bounding_box())
        drag(opened.page, target, EMPTY)
        wait_palette_loaded(opened.page)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        expected = [spec for spec in specs if any(meets(socket["type"], "Image", types) for socket in spec["outputs"])]
        assert rows.count() == len(expected) >= 3  # TinyDecode, TinyGain, TinyClip write an Image entry
        assert "after gain" in opened.page.locator("[data-palette-meta]").inner_text()  # boost reads from gain
        opened.page.locator(f'[data-palette-list] li[data-class="{MOD}.TinyDecode"]').click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/wires")["text"]
        assert f"  tinydecode:\n    op: !class:{MOD}.TinyDecode {{}}\n    from: gain\n" in text
        assert f"  boost:\n    op: !class:{MOD}.TinyGain {{gain: 3.0}}\n    from: tinydecode\n" in text
        assert opened.errors == []

    def test_a_wire_dropped_on_a_stream_canvas_lists_record_takers_and_the_pick_follows_that_member(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """DESIGN2 §3. Before: the pick after ``stream.ops[2]`` was refused (``is not a flow step``). Now it is the
        member after it — here the list's last."""
        opened = open_graph("pets")
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        source = centre(handle(opened.page, "stream.ops[2]", "record", "out").bounding_box())
        drag(opened.page, source, EMPTY)
        wait_palette_loaded(opened.page)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        expected = [spec for spec in specs if any(meets("Record", socket["type"], types) for socket in spec["inputs"])]
        assert rows.count() == min(len(expected), PALETTE_ROWS) > 50  # every op takes a record
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        assert "after stream.ops[2]" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.locator("[data-palette-list] li").first.click()
        wait_saves(opened.page, 1)
        text = api_get(server, "api/graphs/pets")["text"]
        assert text == PETS_TEXT + "    - !class:recordstream.ops.numpy.Threshold {}\n"
        opened.page.wait_for_selector(f'{node_root("stream.ops[3]")}[data-class="recordstream.ops.numpy.Threshold"]')
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == []


class TestTheCatalogueIsAskedForOnFirstNeed:
    """The page asks ``GET api/palette`` when something that lists the catalogue is first shown — the Nodes sidebar,
    Add Node ▸, the palette, Set source… — and never as it opens (2026-10-02). Measured before: the page asked as it
    opened, beside its first ``GET api/graphs``; on a fresh server, building the catalogue imports every installed
    package that declares classes, and the graph list waited on those imports."""

    @staticmethod
    def show(page: Any, surface: str) -> None:
        """Show ``surface`` the way a person does, and wait until it is drawn."""
        if surface == "sidebar":
            page.locator('button[data-command="toggle-nodes"]').click()
            page.locator("[data-nodes-sidebar]").wait_for()
        elif surface == "add-node":
            page.mouse.click(*EMPTY, button="right")
            page.locator('[data-context-menu="pane"] [data-submenu="add-node"]').click()
            page.locator("[data-add-node-search]").wait_for()
        elif surface == "palette":
            page.mouse.dblclick(*EMPTY)
            page.locator("[data-palette]").wait_for()
        else:
            page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
            page.locator('[data-dialog="set-slot"]').wait_for()

    @staticmethod
    def hide(page: Any, surface: str) -> None:
        if surface == "sidebar":
            page.locator('button[data-command="toggle-nodes"]').click()
            page.locator("[data-nodes-sidebar]").wait_for(state="detached")
        else:
            page.keyboard.press("Escape")
            gone = {
                "add-node": "[data-add-node]",
                "palette": "[data-palette]",
                "set-source": '[data-dialog="set-slot"]',
            }
            page.locator(gone[surface]).wait_for(state="detached")

    @pytest.mark.parametrize("surface", ["sidebar", "add-node", "palette", "set-source"])
    def test_nothing_asks_while_the_page_opens_and_the_first_surface_asks_once(
        self, browser: Any, server: str, surface: str
    ) -> None:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: List[str] = []
        asked: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
        page.on("request", lambda request: asked.append(request.url) if "/api/palette" in request.url else None)
        try:
            page.goto(f"{server}/?graph=pets")
            wait_ready(page, "pets")  # everything the page asks as it opens was asked before it drew the graph
            assert asked == []
            assert page.locator(".app").get_attribute("data-palette-ready") == "false"

            self.show(page, surface)
            wait_palette_loaded(page)
            assert len(asked) == 1

            self.hide(page, surface)  # shown again: the page keeps what arrived
            self.show(page, surface)
            assert len(asked) == 1
            assert errors == []
        finally:
            page.close()


# ------------------------------------------------------------------------------------------------
# groups: a titled, coloured box around a selection, kept in the sidecar's groups: block only
# ------------------------------------------------------------------------------------------------

GROUP = {"id": "g1", "title": "front end", "color": "#3b82f6", "nodes": ["decode", "gain"]}


class TestGroups:
    def test_g_over_a_box_selection_makes_a_group_the_sidecar_keeps_and_a_reload_redraws(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        positions_before = api_get(server, "api/graphs/wires")["positions"]
        boxes_before = node_boxes(opened.page, "decode", "gain")
        box_select(opened.page, "decode", "gain")
        assert selected_nodes(opened.page) == ["decode", "gain"]
        opened.page.keyboard.press("g")
        opened.page.wait_for_selector('.app[data-saves="1"]')
        group = opened.page.locator("[data-group]")
        assert group.count() == 1
        assert group.get_attribute("data-group") == "g1"
        assert group.get_attribute("data-members") == "decode,gain"
        assert group.locator(".group-title").inner_text().startswith("group 1")
        body = api_get(server, "api/graphs/wires")
        assert body["groups"] == [
            {"id": "g1", "title": "group 1", "color": body["groups"][0]["color"], "nodes": ["decode", "gain"]}
        ]
        assert body["groups"][0]["color"].startswith("#")
        assert not (graph_dir / "wires.layout.yaml").exists()  # held by the editor until Save
        save_file(opened.page, 2)  # a clean page reloads without the leave-page prompt
        assert layout.read_groups(graph_dir / "wires.layout.yaml") == body["groups"]
        assert body["positions"] == positions_before  # grouping moves nothing
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # the document has no group spelling
        assert node_boxes(opened.page, "decode", "gain") == boxes_before  # the members are drawn where they were
        opened.page.reload()
        wait_ready(opened.page, "wires")
        assert opened.page.locator("[data-group]").count() == 1
        assert node_boxes(opened.page, "decode", "gain") == boxes_before
        assert opened.errors == []

    def test_dragging_a_group_moves_its_members_by_the_same_delta_and_saves_their_positions(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        api_put(server, "api/graphs/wires/groups", {"groups": [GROUP]})
        opened = open_graph("wires")
        before = api_get(server, "api/graphs/wires")["positions"]
        gap_before = node_translate(opened.page, "gain")[0] - node_translate(opened.page, "decode")[0]
        title = opened.page.locator('[data-group="g1"] .group-title').bounding_box()
        start = centre(title)
        drag(opened.page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        opened.page.wait_for_selector('.app[data-saves="1"]')
        after = api_get(server, "api/graphs/wires")["positions"]
        for name in ("decode", "gain"):
            assert after[name] == [before[name][0] + DRAG[0], before[name][1] + DRAG[1]], name
        for name in ("boost", "count", "<host>"):
            assert after[name] == before[name], name
        save_file(opened.page, 2)
        assert layout.read_sidecar(graph_dir / "wires.layout.yaml")["gain"] == tuple(after["gain"])
        assert layout.read_groups(graph_dir / "wires.layout.yaml") == [GROUP]
        assert node_translate(opened.page, "gain")[0] - node_translate(opened.page, "decode")[0] == gap_before
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_ungrouping_returns_the_members_where_they_are(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        api_put(server, "api/graphs/wires/groups", {"groups": [GROUP]})
        opened = open_graph("wires")
        assert opened.page.locator("[data-group]").count() == 1
        before = api_get(server, "api/graphs/wires")["positions"]
        boxes_before = node_boxes(opened.page, "decode", "gain")
        opened.page.locator('[data-group="g1"] [data-action="ungroup"]').click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        assert opened.page.locator("[data-group]").count() == 0
        assert api_get(server, "api/graphs/wires")["groups"] == []
        save_file(opened.page, 2)
        assert layout.read_groups(graph_dir / "wires.layout.yaml") == []
        assert api_get(server, "api/graphs/wires")["positions"] == before
        assert node_boxes(opened.page, "decode", "gain") == boxes_before
        assert opened.errors == []

    def test_ctrl_z_undoes_a_grouping_on_the_pages_side_and_redo_puts_it_back(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """A group is layout: the sidecar held it, the document never did, so the PAGE writes the old block back."""
        opened = open_graph("wires")
        page = opened.page
        box_select(page, "decode", "gain")
        page.keyboard.press("g")
        wait_saves(page, 1)
        grouped = api_get(server, "api/graphs/wires")["groups"]
        assert [group["nodes"] for group in grouped] == [["decode", "gain"]]
        page.keyboard.press("Control+z")
        wait_saves(page, 2)
        assert api_get(server, "api/graphs/wires")["groups"] == []
        assert page.locator("[data-group]").count() == 0
        page.keyboard.press("Control+Shift+z")
        wait_saves(page, 3)
        assert api_get(server, "api/graphs/wires")["groups"] == grouped
        page.wait_for_selector('[data-group="g1"]')
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_the_graph_menu_groups_the_selection_and_the_title_and_colour_are_edited_on_the_group(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        open_menu(opened.page, "graph")
        assert opened.page.locator('[data-menu="graph"] [data-command="group"]').is_disabled()
        opened.page.keyboard.press("Escape")
        assert opened.page.locator('[data-menu="graph"] .dropdown').count() == 0
        box_select(opened.page, "decode", "gain")
        menu_command(opened.page, "graph", "group")
        opened.page.wait_for_selector('.app[data-saves="1"]')
        group = opened.page.locator('[data-group="g1"]')
        assert group.count() == 1
        colour_before = api_get(server, "api/graphs/wires")["groups"][0]["color"]
        group.locator(".group-title").dblclick()
        title = group.locator("input[data-group-title]")
        title.fill("front end")
        title.press("Enter")
        opened.page.wait_for_selector('.app[data-saves="2"]')
        assert group.locator(".group-title").inner_text().startswith("front end")
        group.locator('[data-action="recolor"]').click()
        opened.page.wait_for_selector('.app[data-saves="3"]')
        groups = api_get(server, "api/graphs/wires")["groups"]
        assert groups[0]["title"] == "front end"
        assert groups[0]["color"] != colour_before
        assert groups[0]["nodes"] == ["decode", "gain"]
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# undo / redo: a document edit through the SERVER's text history, a layout change through the page's own
# ------------------------------------------------------------------------------------------------

#: The one refusal Chrome logs for a 400 — the route's contract for a refused edit.
REFUSED_400 = "console.error: Failed to load resource: the server responded with a status of 400 (BAD REQUEST)"


class TestUndoRedo:
    def test_ctrl_z_reverses_a_move_before_the_edit_made_ahead_of_it_and_shift_redoes_both(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """The split, pinned: the MOVE is the page's (the editor gets its old position back, the document is not
        touched), the EDIT is the server's (the text comes back from the document's own history, the positions are
        not touched) — and one Ctrl+Z reverses whichever came last. Nothing reaches the disk without Save."""
        opened = open_graph("pets")
        page = opened.page

        def served() -> Tuple[str, Tuple[float, float]]:
            body = api_get(server, "api/graphs/pets")
            x, y = body["positions"]["stream.ops[2]"]
            return body["text"], (x, y)

        widget = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        widget.fill("0.9")
        widget.press("Enter")
        wait_saves(page, 1)
        edited, before = served()
        assert "low_level: 0.9}   # the mask" in edited
        start = centre(page.locator(f'{node_root("stream.ops[2]")} .title').bounding_box())
        drag(page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        wait_saves(page, 2)
        moved = (before[0] + DRAG[0], before[1] + DRAG[1])
        assert served() == (edited, moved)

        page.keyboard.press("Control+z")  # the move came last: the page writes the old position back
        wait_saves(page, 3)
        assert served() == (edited, before)
        assert node_translate(page, "stream.ops[2]") == before

        page.keyboard.press("Control+z")  # then the edit: the server's history gives the old text back
        wait_saves(page, 4)
        assert served() == (PETS_TEXT, before)
        assert widget.input_value() == "0.5"

        page.keyboard.press("Control+Shift+z")
        wait_saves(page, 5)
        assert served() == (edited, before)
        assert widget.input_value() == "0.9"
        page.keyboard.press("Control+Shift+z")
        wait_saves(page, 6)
        assert served() == (edited, moved)
        assert node_translate(page, "stream.ops[2]") == moved
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT and not (graph_dir / "pets.layout.yaml").exists()
        assert opened.errors == []

    def test_an_edit_the_page_never_saw_is_undone_through_the_servers_history(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """An agent's ``set_param`` (a route call, no page) is in the document's history, so the page's Ctrl+Z
        reaches it."""
        api_post(server, "api/graphs/pets/param", {"node": "stream.ops[1]", "param": "low_level", "value": 0.7})
        opened = open_graph("pets")
        widget = opened.page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        assert widget.input_value() == "0.7"
        assert opened.page.locator(".app").get_attribute("data-dirty") == "true"  # the agent's edit is unsaved
        opened.page.keyboard.press("Control+z")
        wait_saves(opened.page, 1)
        assert api_get(server, "api/graphs/pets")["text"] == PETS_TEXT
        assert widget.input_value() == "0.5"
        opened.page.wait_for_selector('.app[data-dirty="false"]')  # back to what the file holds
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == []

    def test_the_edit_menu_undoes_and_redoes_a_parameter_edit(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        widget = page.locator(f'{node_root("gain")} input[data-param="gain"]')
        widget.fill("5")
        widget.press("Enter")
        wait_saves(page, 1)
        edited = api_get(server, "api/graphs/wires")["text"]
        assert "{gain: 5}" in edited
        menu_command(page, "edit", "undo")
        wait_saves(page, 2)
        assert api_get(server, "api/graphs/wires")["text"] == WIRES_TEXT
        assert widget.input_value() == "2"
        menu_command(page, "edit", "redo")
        wait_saves(page, 3)
        assert api_get(server, "api/graphs/wires")["text"] == edited
        assert widget.input_value() == "5"
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_with_nothing_to_undo_the_page_says_so_and_writes_nothing(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        menu_command(opened.page, "edit", "undo")
        toast = opened.page.locator('[data-toast="info"]')
        toast.wait_for()
        assert "wires.yaml" in toast.inner_text() and "nothing to undo" in toast.inner_text()
        assert opened.page.locator(".app").get_attribute("data-saves") == "0"
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert not (graph_dir / "wires.layout.yaml").exists()
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# copy / paste: the TEXT of the copied steps (their own lines), pasted after the selection
# ------------------------------------------------------------------------------------------------


class TestCopyPaste:
    def test_paste_on_a_flow_adds_the_copied_steps_own_lines_after_the_selection(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.locator(f'{node_root("gain")} .title').click()
        copy_selection(page)
        page.locator(f'{node_root("boost")} .title').click()
        assert selected_nodes(page) == ["boost"]
        page.keyboard.press("Control+v")
        wait_saves(page, 1)
        text = api_get(server, "api/graphs/wires")["text"]
        # the copied step's own text — `gain: 2.0` as written — under a free name, reading the step it was pasted after
        assert f"  gain_2:\n    op: !class:{MOD}.TinyGain {{gain: 2.0}}\n    from: boost\n" in text
        assert f"  count:\n    op: !class:{MOD}.TinyCount {{}}\n    from: gain_2\n" in text
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # held by the editor until Save
        page.wait_for_selector(node_root("gain_2"))
        assert opened.errors == []

    def test_paste_on_a_stream_inserts_a_copy_of_the_member_after_the_selection(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        copy_selection(page)
        page.keyboard.press("Control+v")
        wait_saves(page, 1)
        text = api_get(server, "api/graphs/pets")["text"]
        member = "    - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}   # the mask\n"
        assert text == PETS_TEXT.replace(member, member * 2)  # the line, comment and all, twice
        page.wait_for_selector(node_root("stream.ops[3]"))
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# arrange: dagre on the page (the default) or the server's layered layout — both saved like a drag
# ------------------------------------------------------------------------------------------------

WIRES_NODES = ("decode", "gain", "boost", "count", "<host>")


def overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


#: Resolves once the viewport's transform has not changed for 5 frames in a row. An arrange — and its undo — ends in a
#: fit ANIMATED over 200 ms, and boxes read one call at a time while it runs are read at different zooms (measured
#: 2026-11-10 on a loaded machine: a socket at 1.15 of its card's width, a column's centres 12-122 px apart).
VIEW_STILL = """() => new Promise((done) => {
  const read = () => document.querySelector('.react-flow__viewport').style.transform;
  let last = read(), still = 0;
  const tick = () => {
    const now = read();
    still = now === last ? still + 1 : 0;
    last = now;
    if (still >= 5) done(true); else requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})"""


def wait_view_still(page: Any) -> None:
    page.wait_for_function(VIEW_STILL)


#: True once gain's record input socket is within 8 px of its card's ``top`` or ``left`` edge (the argument).
GAINS_INPUT_NEAR = """(edge) => {
  const h = document.querySelector('.react-flow__handle[data-node="gain"][data-socket="record"][data-kind="in"]');
  const c = h && h.closest('.react-flow__node');
  return !!c && Math.abs(h.getBoundingClientRect()[edge] - c.getBoundingClientRect()[edge]) < 8;
}"""


class TestArrange:
    def test_arrange_lays_the_chain_out_left_to_right_and_saves_it_and_ctrl_z_puts_it_back(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        before = api_get(server, "api/graphs/wires")["positions"]
        menu_command(page, "view", "arrange")
        wait_saves(page, 1)
        wait_view_still(page)
        placed = api_get(server, "api/graphs/wires")["positions"]
        xs = [placed[name][0] for name in ("decode", "gain", "boost", "count")]
        assert xs == sorted(xs) and len(set(xs)) == 4, placed
        assert all(x % 10 == 0 and y % 10 == 0 for x, y in placed.values())  # on the snap grid
        for name in WIRES_NODES:
            assert node_translate(page, name) == tuple(placed[name]), name
        boxes = list(node_boxes(page, *WIRES_NODES).values())
        assert not any(overlap(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1 :])
        save_file(page, 2)
        assert layout.read_sidecar(graph_dir / "wires.layout.yaml") == {k: tuple(v) for k, v in placed.items()}
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        page.keyboard.press("Control+z")  # an arrange is a layout change: the page writes the old places back
        wait_saves(page, 3)
        assert api_get(server, "api/graphs/wires")["positions"] == before
        assert opened.errors == []

    @pytest.mark.parametrize("command", ["arrange-diagonal", "arrange-down"])
    def test_diagonal_and_top_to_bottom_step_the_chain_down_and_save_it(
        self, open_graph: Callable[[str], Opened], server: str, command: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        menu_command(page, "view", command)
        wait_saves(page, 1)
        wait_view_still(page)
        placed = api_get(server, "api/graphs/wires")["positions"]
        chain = [placed[name] for name in ("decode", "gain", "boost", "count")]
        ys = [y for _x, y in chain]
        assert ys == sorted(ys) and len(set(ys)) == 4, placed  # each step below the one before
        xs = [x for x, _y in chain]
        if command == "arrange-diagonal":
            assert xs == sorted(xs) and len(set(xs)) == 4, placed  # ... and right of it
        for name in WIRES_NODES:
            assert node_translate(page, name) == tuple(placed[name]), name
        drawn = node_boxes(page, *WIRES_NODES)
        if command == "arrange-down":  # the one-input chain is one column (count sits between boost and <host>)
            centres = [drawn[name]["x"] + drawn[name]["width"] / 2 for name in ("decode", "gain", "boost")]
            assert max(centres) - min(centres) <= 10, drawn
        boxes = list(drawn.values())
        assert not any(overlap(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1 :])
        assert opened.errors == []

    def test_top_to_bottom_moves_the_sockets_to_the_top_and_bottom_edges_and_undo_moves_them_back(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        assert page.locator(".canvas").get_attribute("data-direction") == "across"

        def sockets() -> Dict[str, Tuple[float, float]]:
            """Where gain's record sockets are, against its card: (x, y) of each centre, 0..1 across and down."""
            card = node_boxes(page, "gain")["gain"]
            found = {}
            for kind in ("in", "out"):
                x, y = centre(handle(page, "gain", "record", kind).bounding_box())
                found[kind] = ((x - card["x"]) / card["width"], (y - card["y"]) / card["height"])
            return found

        beside = sockets()
        assert beside["in"][0] < 0.05 and beside["out"][0] > 0.95, beside  # on the left and right edges
        menu_command(page, "view", "arrange-down")
        wait_saves(page, 1)
        page.wait_for_selector('.canvas[data-direction="down"]')
        page.wait_for_function(GAINS_INPUT_NEAR, arg="top")
        wait_view_still(page)
        edges = sockets()
        assert edges["in"][1] < 0.05 and edges["out"][1] > 0.95, edges  # on the top and bottom edges
        assert 0.05 < edges["in"][0] < 0.95 and 0.05 < edges["out"][0] < 0.95, edges
        page.keyboard.press("Control+z")  # the places go back, and the sockets with them
        wait_saves(page, 2)
        page.wait_for_selector('.canvas[data-direction="across"]')
        page.wait_for_function(GAINS_INPUT_NEAR, arg="left")
        wait_view_still(page)
        back = sockets()
        assert back["in"][0] < 0.05 and back["out"][0] > 0.95, back
        assert opened.errors == []

    def test_the_server_layout_button_writes_the_documents_layered_layout(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        start = centre(page.locator(f'{node_root("gain")} .title').bounding_box())
        drag(page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        wait_saves(page, 1)
        menu_command(page, "view", "arrange-server")
        wait_saves(page, 2)
        laid = layout.layered_layout(Document.open(graph_dir / "wires.yaml").model)
        assert api_get(server, "api/graphs/wires")["positions"] == {k: [x, y] for k, (x, y) in laid.items()}
        for name in WIRES_NODES:
            assert node_translate(page, name) == laid[name], name
        assert opened.errors == []


class TestFirstLayout:
    """The server's first layout (no sidecar yet) stacks a column's cards by their heights, so a fresh open draws
    no card over another — the page's MEASURED boxes, not the server's estimate."""

    def test_a_pane_shaped_chain_opened_fresh_draws_no_card_over_another(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        """A 29-step chain with a tall first card, a contract root and its host card — a domain pane's shape. Measured
        2026-10-01 on a real pane before the rule: ``contract`` at (40, 240) under the 373 px head (40..413) — the one
        overlapping pair; after: ``contract`` at (40, 430), 17 px below the head's bottom, and the host card under the
        root at (40, 630)."""
        path = graph_dir / "pane_shaped.yaml"
        path.write_text(PANE_SHAPED_TEXT)
        assert not layout.sidecar_path(path).exists()
        assert layout.card_height(Document.open(path).model.node("head")) == 373
        opened = open_graph("pane_shaped")
        page = opened.page
        names = page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)")
        assert len(names) == 31 and "<host>" in names
        boxes = node_boxes(page, *names)
        overlapping = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :] if overlap(boxes[a], boxes[b])]
        assert overlapping == []
        assert node_translate(page, "head") == (40.0, 40.0)
        assert node_translate(page, "contract") == (40.0, 430.0)
        assert node_translate(page, "<host>") == (40.0, 630.0)
        assert opened.errors == []

    def test_a_model_graph_with_configured_members_opened_fresh_draws_no_card_over_another(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        """A model graph shaped like a domain's detection model (measured 2026-10-01 on a real one): its steps, the
        ``ConfigureOp`` step's member cards — its two ops and the target it configures —, the ``contract`` root, which
        wires to nothing, and the host card holding the root's input: none drawn over another."""
        path = graph_dir / "model_shaped.yaml"
        path.write_text(MODEL_SHAPED_TEXT)
        assert Document.open(path).check() == []
        assert not layout.sidecar_path(path).exists()
        opened = open_graph("model_shaped")
        page = opened.page
        names = page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)")
        assert {"threshold.ops[0]", "threshold.ops[1]", "threshold.target", "contract", "<host>"} <= set(names)
        boxes = node_boxes(page, *names)
        overlapping = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :] if overlap(boxes[a], boxes[b])]
        assert overlapping == []
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# rename: double-click a title, Enter is POST .../rename
# ------------------------------------------------------------------------------------------------


class TestRename:
    def test_double_clicking_a_title_renames_the_node_in_one_edit_and_it_stays_where_it_was(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        before = node_translate(page, "gain")
        page.locator(f'{node_root("gain")} .title .name').dblclick()
        field = page.locator("input[data-rename]")
        assert field.input_value() == "gain"
        field.fill("amp")
        field.press("Enter")
        wait_saves(page, 1)
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT  # held by the editor until Save
        text = api_get(server, "api/graphs/wires")["text"]
        assert f"  amp:\n    op: !class:{MOD}.TinyGain {{gain: 2.0}}\n    from: decode\n" in text
        assert f"  boost:\n    op: !class:{MOD}.TinyGain {{gain: 3.0}}\n    from: amp\n" in text
        page.wait_for_selector(node_root("amp"))
        assert page.locator(node_root("gain")).count() == 0
        assert node_translate(page, "amp") == before
        assert opened.errors == []

    def test_a_name_in_use_is_refused_with_the_servers_message_and_the_title_reverts(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.locator(f'{node_root("gain")} .title .name').dblclick()
        field = page.locator("input[data-rename]")
        field.fill("boost")
        field.press("Enter")
        toast = page.locator('[data-toast="error"]')
        toast.wait_for()
        assert "'boost' is already a node" in toast.inner_text()
        assert page.locator("input[data-rename]").count() == 0
        assert page.locator(f'{node_root("gain")} .title .name').inner_text() == "gain"
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert page.locator(".app").get_attribute("data-saves") == "0"
        assert opened.errors == [REFUSED_400]


# ------------------------------------------------------------------------------------------------
# the run panel: the tracer through POST .../trace and /api/runs/<run>/rerun
# ------------------------------------------------------------------------------------------------

# a Stream whose second op's constructor refuses a value — the rerun refusal
CLIPSTREAM_TEXT = f"""\
stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{size: 4}}
  ops:
    - !class:{MOD}.TinyGain {{gain: 2.0}}
    - !class:{MOD}.TinyClip {{limit: 1.0}}
"""

PETS_OPS = ("stream.ops[0]", "stream.ops[1]", "stream.ops[2]")

NO_SOURCE = (
    "wires.yaml: this graph has no source to draw a record from — "
    "a host supplies the record (mount the editor in one) or wire a source"
)


def open_run_tab(page: Any) -> None:
    page.locator('[data-tab="run"]').click()
    page.wait_for_selector("[data-run-panel]")


def wait_answers(page: Any, count: int) -> None:
    """``data-answers`` counts the run answers (and refusals) the panel received."""
    page.wait_for_selector(f'[data-run-panel][data-answers="{count}"]')


def run_statuses(page: Any, *names: str) -> Dict[str, str]:
    return {name: page.locator(node_root(name)).get_attribute("data-run-status") or "" for name in names}


def run_generations(page: Any, *names: str) -> Dict[str, str]:
    return {name: page.locator(node_root(name)).get_attribute("data-generation") or "" for name in names}


class TestRunPanel:
    def test_run_paints_every_traced_node_with_its_status_and_wall_time(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["statuses"] == {name: "ok" for name in PETS_OPS}
        assert run_statuses(page, *PETS_OPS) == run["statuses"]
        for node in run["trace"]["nodes"]:
            shown = page.locator(f'{node_root(node["node"])} [data-ms]').inner_text()
            assert shown == f"{node['ms']:.2f} ms", (node["node"], shown)
            row = page.locator(f'[data-run-row="{node["node"]}"]')
            assert row.locator('[data-field="status"]').inner_text() == "ok"
        # the stream and its source are not traced nodes: no status is painted on them
        assert run_statuses(page, "stream", "stream.source") == {"stream": "", "stream.source": ""}
        assert opened.errors == []

    def test_a_node_clicked_after_a_run_shows_its_input_and_output_summaries(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        page.locator("input[data-run-record]").fill("1")
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["record"] == 1
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        inspected = page.locator('[data-inspect="stream.ops[1]"]')
        inspected.wait_for()
        traced = next(node for node in run["trace"]["nodes"] if node["node"] == "stream.ops[1]")
        image = inspected.locator('tr[data-side="input"][data-entry="image"]')
        assert image.locator('[data-field="type"]').inner_text() == traced["input"]["image"]["type"] == "Image"
        assert image.locator('[data-field="shape"]').inner_text() == "4×4"
        assert image.locator('[data-field="dtype"]').inner_text() == "float32"
        label = inspected.locator('tr[data-side="input"][data-entry="class"]')
        assert label.locator('[data-field="value"]').inner_text() == "dog"  # record #1 is the dog
        mask = inspected.locator('tr[data-side="output"][data-entry="mask"]')
        assert mask.locator('[data-field="type"]').inner_text() == "Mask"
        assert "mask" in inspected.locator("[data-changed]").inner_text()
        assert opened.errors == []

    def test_step_pauses_one_node_further_until_the_run_is_complete(self, open_graph: Callable[[str], Opened]) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        page.locator(f'{node_root("stream.ops[1]")} .title').click()  # the selection is where the run pauses
        page.wait_for_selector('[data-action="run"]:has-text("pausing before stream.ops[1]")')  # the label follows it
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        assert run_statuses(page, *PETS_OPS) == {
            "stream.ops[0]": "ok",
            "stream.ops[1]": "paused",
            "stream.ops[2]": "not reached",
        }
        page.locator('[data-action="step"]').click()
        wait_answers(page, 2)
        assert run_statuses(page, *PETS_OPS) == {
            "stream.ops[0]": "ok",
            "stream.ops[1]": "ok",
            "stream.ops[2]": "paused",
        }
        page.locator('[data-action="step"]').click()
        wait_answers(page, 3)
        assert run_statuses(page, *PETS_OPS) == {name: "ok" for name in PETS_OPS}
        assert page.locator('[data-action="step"]').is_disabled()
        assert opened.errors == []

    def test_rerun_recomputes_the_node_and_what_is_downstream_with_the_canvas_value(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        assert run_generations(page, *PETS_OPS) == {name: "0" for name in PETS_OPS}
        widget = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        widget.fill("0.9")
        widget.press("Enter")
        wait_saves(page, 1)
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        form = page.locator('[data-inspect="stream.ops[1]"] [data-rerun-form]')
        assert form.locator('[data-param="low_level"]').input_value() == "0.9"  # the canvas's current value
        page.locator('[data-action="rerun"]').click()
        wait_answers(page, 2)
        assert run_generations(page, *PETS_OPS) == {"stream.ops[0]": "0", "stream.ops[1]": "1", "stream.ops[2]": "1"}
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["trace"]["nodes"][1]["params"]["low_level"] == 0.9
        assert run["generations"] == {"stream.ops[0]": 0, "stream.ops[1]": 1, "stream.ops[2]": 1}
        assert opened.errors == []

    def test_a_trial_value_the_constructor_refuses_shows_its_message_and_leaves_the_trace(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "clipstream.yaml").write_text(CLIPSTREAM_TEXT)
        opened = open_graph("clipstream")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        run_id = page.locator("[data-run-panel]").get_attribute("data-run")
        before = api_get(server, f"api/runs/{run_id}")
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        trial = page.locator('[data-inspect="stream.ops[1]"] [data-rerun-form] [data-param="limit"]')
        trial.fill("-1")
        trial.press("Enter")  # a trial value: kept by the panel, never written to the document
        assert page.locator(".app").get_attribute("data-saves") == "0"
        page.locator('[data-action="rerun"]').click()
        wait_answers(page, 2)
        message = page.locator("[data-run-error]").inner_text()
        assert "limit must be positive, got -1" in message and "clipstream.yaml" in message
        assert api_get(server, f"api/runs/{run_id}") == before
        assert run_generations(page, "stream.ops[0]", "stream.ops[1]") == {"stream.ops[0]": "0", "stream.ops[1]": "0"}
        assert (graph_dir / "clipstream.yaml").read_text() == CLIPSTREAM_TEXT
        assert opened.errors == [REFUSED_400]

    def test_a_graph_without_a_source_shows_the_servers_refusal_in_the_panel(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        assert page.locator("[data-run-error]").inner_text() == NO_SOURCE
        assert run_statuses(page, *WIRES_NODES) == {name: "" for name in WIRES_NODES}
        assert opened.errors == [REFUSED_400]


class TestContainersOfPlainValues:
    """A list of numbers is ONE text box, a fixed count of numbers that many boxes, names with a value each one row
    per name (the user's picks 2026-11-25: 1 A, 2 B, 3 C) — and each writes the document's own spelling."""

    SHAPES = '[data-form-node="stream.ops[0]"]'

    @pytest.fixture(autouse=True)
    def _the_shapes_graph(self, graph_dir: Path) -> None:
        (graph_dir / "shapes.yaml").write_text(SHAPES_TEXT)

    def _open(self, open_graph: Callable[[str], Opened]) -> Opened:
        opened = open_graph("shapes")
        opened.page.locator('[data-tab="settings"]').click()
        opened.page.locator("[data-settings-all]").check()
        opened.page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        return opened

    def _params(self, server: str) -> Dict[str, Any]:
        nodes = api_get(server, "api/graphs/shapes")["model"]["nodes"]
        shapes = next(node for node in nodes if node["name"] == "stream.ops[0]")
        return {name: param["value"] for name, param in shapes["params"].items()}

    def test_a_list_of_numbers_is_typed_with_commas_and_text_that_is_no_number_goes_back(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = self._open(open_graph)
        page = opened.page
        box = page.locator(f'{self.SHAPES} input[data-param="rates_hz"][data-list="number"]')
        assert box.input_value() == "1200, 2400"  # the document sets none: the constructor's default, shown
        set_widget(page, self.SHAPES, "rates_hz", "1200, 4800.5")
        wait_saves(page, 1)
        assert self._params(server)["rates_hz"] == [1200, 4800.5]
        assert "rates_hz: [1200, 4800.5]" in api_get(server, "api/graphs/shapes")["text"]
        set_widget(page, self.SHAPES, "rates_hz", "1200, fast")
        assert box.input_value() == "1200, 4800.5"  # no list of numbers: nothing sent, the value back
        assert self._params(server)["rates_hz"] == [1200, 4800.5]
        assert opened.errors == []

    def test_three_numbers_are_three_boxes_committed_together(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = self._open(open_graph)
        page = opened.page
        shape = page.locator(f'{self.SHAPES} [data-param="shape"][data-pair-count="3"]')
        ends = [shape.locator(f'[data-pair-end="{place}"]') for place in ("1", "2", "3")]
        assert [end.input_value() for end in ends] == ["3", "224", "224"]  # the default, shown
        ends[1].fill("128")
        ends[1].press("Enter")
        wait_saves(page, 1)
        assert self._params(server)["shape"] == [3, 128, 224]
        assert opened.errors == []

    def test_names_with_a_value_each_are_rows_that_add_change_and_remove(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = self._open(open_graph)
        page = opened.page
        field = page.locator(f'{self.SHAPES} [data-param="label_to_index"][data-map="integer"]')
        rows = field.locator("[data-map-row]")
        read = "rs => rs.map(r => [...r.querySelectorAll('input')].map(i => i.value))"
        assert rows.evaluate_all(read) == [["cat", "0"], ["dog", "1"]]

        field.locator("[data-map-add]").click()
        new = rows.nth(2)
        new.locator("[data-map-name]").fill("bird")
        new.locator("[data-map-name]").press("Enter")  # a name without its value: no map yet, nothing sent
        assert field.get_attribute("data-map-waits") == "true"
        assert self._params(server)["label_to_index"] == {"cat": 0, "dog": 1}
        new.locator("[data-map-value]").fill("2")
        new.locator("[data-map-value]").press("Enter")
        wait_saves(page, 1)
        # the names stay in the order typed: in the answer, in the document and in the rows
        assert list(self._params(server)["label_to_index"].items()) == [("cat", 0), ("dog", 1), ("bird", 2)]
        assert "label_to_index: {cat: 0, dog: 1, bird: 2}" in api_get(server, "api/graphs/shapes")["text"]
        assert rows.evaluate_all(read) == [["cat", "0"], ["dog", "1"], ["bird", "2"]]
        assert field.get_attribute("data-map-waits") is None

        rows.nth(0).locator("[data-map-remove]").click()  # cat
        wait_saves(page, 2)
        assert list(self._params(server)["label_to_index"].items()) == [("dog", 1), ("bird", 2)]
        assert rows.evaluate_all(read) == [["dog", "1"], ["bird", "2"]]
        assert opened.errors == []

    def test_a_list_of_lists_stays_read_only_and_a_card_shows_a_map_read_only(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = self._open(open_graph)
        page = opened.page
        assert page.locator(f'{self.SHAPES} code.ro[data-param="windows"]').count() == 1
        card = page.locator(node_root("stream.ops[0]"))
        assert card.locator('code.ro[data-param="label_to_index"]').count() == 1  # one row per parameter on a card
        assert card.locator("[data-map-row]").count() == 0
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# the settings form: GET .../form as one form per node, through the same setParam as the canvas widgets
# ------------------------------------------------------------------------------------------------


class TestSettingsForm:
    def test_the_settings_tab_renders_one_form_per_node_with_the_widget_each_schema_asks_for(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator('[data-tab="settings"]').click()
        page.locator("[data-settings-all]").check()  # pets has no app: block — it names no settings of its own
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        form = api_get(server, "api/graphs/pets/form")
        names = [node["name"] for node in api_get(server, "api/graphs/pets")["model"]["nodes"]]
        sections = page.locator("[data-form-node]").evaluate_all("ss => ss.map(s => s.dataset.formNode)")
        assert sections == [name for name in names if form[name]]
        threshold = page.locator('[data-form-node="stream.ops[1]"]')
        assert threshold.locator('select[data-param="low_op"] option').evaluate_all("os => os.map(o => o.text)") == [
            ">",
            ">=",
        ]
        assert threshold.locator('input[data-param="low_level"]').input_value() == "0.5"
        assert threshold.locator('input[data-param="output"]').input_value() == "mask"
        assert opened.errors == []

    def test_a_change_in_the_form_is_the_edit_the_canvas_widget_shows_and_back(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator('[data-tab="settings"]').click()
        page.locator("[data-settings-all]").check()
        in_form = page.locator('[data-form-node="stream.ops[1]"] input[data-param="low_level"]')
        on_canvas = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        in_form.fill("0.8")
        in_form.press("Enter")
        wait_saves(page, 1)
        assert "low_level: 0.8}   # the mask" in api_get(server, "api/graphs/pets")["text"]
        assert on_canvas.input_value() == "0.8"
        on_canvas.fill("0.6")
        on_canvas.press("Enter")
        wait_saves(page, 2)
        assert in_form.input_value() == "0.6"
        assert "low_level: 0.6}   # the mask" in api_get(server, "api/graphs/pets")["text"]
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# Explicit Save: an edit is held by the editor (● and data-dirty), File ▸ Save writes it
# ------------------------------------------------------------------------------------------------


def picked_label(page: Any) -> str:
    """The graph dropdown's shown option — ``● pets · graph`` while the graph holds unsaved changes."""
    return str(page.locator("select[data-graph-picker]").evaluate("s => s.options[s.selectedIndex].text"))


def listed_dirty(server: str) -> Dict[str, bool]:
    return {entry["name"]: entry["dirty"] for entry in api_get(server, "api/graphs")}


def edit_low_level(page: Any, value: str) -> None:
    widget = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
    widget.fill(value)
    widget.press("Enter")


class TestExplicitSave:
    def test_an_edit_shows_the_dirty_mark_and_leaves_the_file_untouched(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert page.title() == "streamstudio editor"
        assert page.locator("[data-title]").inner_text() == "pets.yaml"
        assert picked_label(page) == "pets · graph"
        edit_low_level(page, "0.9")
        wait_saves(page, 1)
        page.wait_for_selector('.app[data-dirty="true"]')
        assert page.title() == "● streamstudio editor"
        assert page.locator("[data-title]").inner_text() == "● pets.yaml"
        assert picked_label(page) == "● pets · graph"
        assert listed_dirty(server)["pets"] is True
        assert "low_level: 0.9}   # the mask" in api_get(server, "api/graphs/pets")["text"]
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == []

    def test_file_save_writes_the_file_and_clears_the_mark(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_menu(page, "file")
        assert page.locator('[data-menu="file"] [data-command="save"]').is_disabled()  # nothing to save yet
        page.keyboard.press("Escape")
        edit_low_level(page, "0.9")
        wait_saves(page, 1)
        edited = api_get(server, "api/graphs/pets")["text"]
        save_file(page, 2)
        assert (graph_dir / "pets.yaml").read_text() == edited
        assert page.title() == "streamstudio editor"
        assert page.locator("[data-title]").inner_text() == "pets.yaml"
        assert picked_label(page) == "pets · graph"
        assert listed_dirty(server)["pets"] is False
        assert opened.errors == []

    def test_ctrl_s_saves_too(self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path) -> None:
        opened = open_graph("pets")
        page = opened.page
        edit_low_level(page, "0.7")
        wait_saves(page, 1)
        page.keyboard.press("Control+s")
        wait_saves(page, 2)
        page.wait_for_selector('.app[data-dirty="false"]')
        assert "low_level: 0.7}   # the mask" in (graph_dir / "pets.yaml").read_text()
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# New… and Open… (the file browser is the EDITOR's machine, GET api/files)
# ------------------------------------------------------------------------------------------------


def open_new(page: Any, kind: str) -> None:
    """New ▸ <kind> — the dialog asks the name and the folder; its folder field waits for ``GET api/workspace``."""
    menu_command(page, "new", f"new-{kind}")
    page.locator('[data-dialog="new"]').wait_for()
    page.wait_for_function("() => document.querySelector('[data-new-folder]')?.value !== ''")


def new_path_shown(page: Any) -> str:
    """The file Create would write, as the dialog shows it (``→ /w/fresh.yaml``)."""
    return str(page.locator("[data-new-path]").inner_text())


def picker_values(page: Any) -> List[str]:
    return list(page.locator("select[data-graph-picker] option").evaluate_all("os => os.map(o => o.value)"))


class TestNewAndOpen:
    """DESIGN3 — the user, 2026-09-29: "New ... by mistake should not happen. It should ask to save". New… asks the
    name and the folder and Create WRITES the starter file at once: no graph exists only in the editor."""

    def test_new_flow_graph_writes_the_starter_at_once_and_opens_it_with_nothing_unsaved(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        folder = api_get(server, "api/workspace")["folder"]
        assert Path(folder) == graph_dir.resolve()
        opened = open_graph("wires")
        page = opened.page
        open_new(page, "flow")
        dialog = page.locator('[data-dialog="new"]')
        assert dialog.locator(".modal-title").inner_text() == "New flow graph"
        assert page.locator("[data-new-kind]").get_attribute("data-new-kind") == "flow"
        assert page.locator("[data-new-folder]").input_value() == folder  # the folder the editor was started on
        assert page.locator("[data-new-confirm]").is_disabled()  # no name yet
        page.locator("[data-new-name]").fill("fresh")
        assert new_path_shown(page) == f"{folder}/fresh.yaml"
        assert not (graph_dir / "fresh.yaml").exists()  # typing writes nothing; Create does

        page.locator("[data-new-confirm]").click()
        wait_ready(page, "fresh")
        assert dialog.count() == 0
        # the file is on disk at once, holding the starter, and the editor holds nothing unsaved
        assert (graph_dir / "fresh.yaml").read_text() == starter_text("flow", "fresh")
        body = api_get(server, "api/graphs/fresh")
        assert body["kind"] == "flow" and body["dirty"] is False and body["model"]["problems"] == []
        assert body["text"] == starter_text("flow", "fresh")
        drawn = page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)")
        assert sorted(drawn) == sorted(node["name"] for node in body["model"]["nodes"]) == ["probe"]
        # no ● anywhere: the root, the title, the dropdown
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert page.title() == "streamstudio editor"
        assert page.locator("[data-title]").inner_text() == "fresh.yaml"
        assert "fresh" in picker_values(page)
        assert picker_labels(page)["fresh"] == "fresh · flow"
        assert listed_dirty(server)["fresh"] is False
        open_menu(page, "file")
        assert page.locator('[data-menu="file"] [data-command="save"]').is_disabled()  # nothing to save
        page.keyboard.press("Escape")
        assert opened.errors == []

    def test_an_existing_file_is_refused_in_the_dialog_and_left_byte_for_byte(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        folder = api_get(server, "api/workspace")["folder"]
        before = (graph_dir / "clip.yaml").read_bytes()
        opened = open_graph("wires")
        page = opened.page
        open_new(page, "flow")
        page.locator("[data-new-name]").fill("clip")
        page.locator("[data-new-confirm]").click()
        refused = page.locator("[data-new-refused]")
        refused.wait_for()
        assert refused.inner_text() == ALREADY_EXISTS.format(path=Path(folder) / "clip.yaml")
        assert (graph_dir / "clip.yaml").read_bytes() == before
        # the dialog stays, with what was typed, usable again; the graph shown is still the one it was
        assert page.locator('[data-dialog="new"]').count() == 1
        assert page.locator("[data-new-name]").input_value() == "clip"
        assert page.locator("[data-new-busy]").get_attribute("data-new-busy") == "false"
        assert page.locator("[data-new-confirm]").is_enabled()
        assert page.locator(".app").get_attribute("data-graph") == "wires"
        assert picker_values(page).count("clip") == 1 and not any(v.startswith("clip~") for v in picker_values(page))

        # fixing the name in place clears the refusal, and Create then writes the file
        page.locator("[data-new-name]").fill("clip_two")
        assert refused.count() == 0
        page.locator("[data-new-confirm]").click()
        wait_ready(page, "clip_two")
        assert (graph_dir / "clip_two.yaml").read_text() == starter_text("flow", "clip_two")
        assert (graph_dir / "clip.yaml").read_bytes() == before
        assert opened.errors == [REFUSED_400]

    def test_browse_picks_another_folder_and_the_file_lands_there_and_joins_the_list(
        self,
        open_graph: Callable[[str], Opened],
        server: str,
        graph_dir: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        other = tmp_path_factory.mktemp("elsewhere").resolve()
        folder = api_get(server, "api/workspace")["folder"]
        opened = open_graph("wires")
        page = opened.page
        open_new(page, "pipeline")
        assert page.locator('[data-dialog="new"] .modal-title').inner_text() == "New pipeline"
        page.locator("[data-new-name]").fill("fresh")
        page.locator("[data-new-browse]").click()
        # the server's file dialog, picking a folder, starting in the folder the field holds
        page.wait_for_selector(f'[data-dialog="file"] [data-file-mode="folder"][data-file-dir="{folder}"]')
        path_field = page.locator("[data-file-path]")
        path_field.fill(str(other))
        path_field.press("Enter")
        page.wait_for_selector(f'[data-file-dir="{other}"]')
        page.locator("[data-file-confirm]").click()
        page.locator('[data-dialog="new"]').wait_for()
        assert page.locator("[data-new-folder]").input_value() == str(other)
        assert page.locator("[data-new-name]").input_value() == "fresh"  # what was typed survives the browse
        assert new_path_shown(page) == f"{other}/fresh.yaml"

        page.locator("[data-new-confirm]").click()
        wait_ready(page, "fresh")
        assert (other / "fresh.yaml").read_text() == starter_text("pipeline", "fresh")
        assert not (graph_dir / "fresh.yaml").exists()
        listed = {entry["name"]: entry for entry in api_get(server, "api/graphs")}
        assert Path(listed["fresh"]["path"]).resolve() == other / "fresh.yaml"
        assert listed["fresh"]["dirty"] is False
        assert "fresh" in picker_values(page) and not picker_labels(page)["fresh"].startswith("● ")
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert page.locator(NODE).count() > 0
        assert opened.errors == []

    def test_a_name_the_list_holds_joins_from_another_folder_under_a_free_name(
        self,
        open_graph: Callable[[str], Opened],
        server: str,
        graph_dir: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """A file of another folder joins like one opened with File ▸ Open: ``clip`` is taken, so it is ``clip~2`` —
        the page opens the name the server ANSWERS, not the one typed."""
        other = tmp_path_factory.mktemp("elsewhere").resolve()
        before = (graph_dir / "clip.yaml").read_bytes()
        opened = open_graph("wires")
        page = opened.page
        open_new(page, "flow")
        page.locator("[data-new-name]").fill("clip")
        page.locator("[data-new-folder]").fill(str(other))
        assert new_path_shown(page) == f"{other}/clip.yaml"
        page.locator("[data-new-confirm]").click()
        wait_ready(page, "clip~2")
        assert (other / "clip.yaml").read_text() == starter_text("flow", "clip")
        assert (graph_dir / "clip.yaml").read_bytes() == before
        listed = {entry["name"]: Path(entry["path"]).resolve() for entry in api_get(server, "api/graphs")}
        assert listed["clip"] == (graph_dir / "clip.yaml").resolve() and listed["clip~2"] == other / "clip.yaml"
        assert {"clip", "clip~2"} <= set(picker_values(page))
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert opened.errors == []

    def test_open_through_the_file_dialog_lists_the_file_and_shows_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "elsewhere").mkdir()
        (graph_dir / "elsewhere" / "other.yaml").write_text(CLIP_TEXT)
        opened = open_graph("wires")
        page = opened.page
        menu_command(page, "file", "open")
        page.locator('[data-dialog="file"]').wait_for()
        page.wait_for_selector(f'[data-file-mode="open"][data-file-dir="{graph_dir.resolve()}"]')
        entries = page.locator("[data-file-entry]").evaluate_all(
            "es => es.map(e => [e.dataset.fileEntry, e.dataset.fileKind])"
        )
        assert entries[0] == ["elsewhere", "dir"]  # folders first
        assert ["wires.yaml", "yaml"] in entries
        page.locator('[data-file-entry="elsewhere"]').click()
        page.wait_for_selector(f'[data-file-dir="{(graph_dir / "elsewhere").resolve()}"]')
        page.locator('[data-file-entry="other.yaml"]').click()
        page.locator("[data-file-confirm]").click()
        wait_ready(page, "other")
        listed = {entry["name"]: entry["path"] for entry in api_get(server, "api/graphs")}
        assert Path(listed["other"]).resolve() == (graph_dir / "elsewhere" / "other.yaml").resolve()
        assert "other" in page.locator("select[data-graph-picker] option").evaluate_all("os => os.map(o => o.value)")
        page.wait_for_selector(node_root("clip"))
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# the Nodes sidebar: every class by package and kind; a click adds after the selection
# ------------------------------------------------------------------------------------------------


class TestNodesSidebar:
    def test_the_sidebar_lists_the_packages_and_a_click_adds_a_step_after_the_selection(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        specs = api_get(server, "api/palette")
        page.locator('button[data-command="toggle-nodes"]').click()
        page.locator("[data-nodes-sidebar]").wait_for()
        wait_palette_loaded(page)
        packages = page.locator("[data-nodes-package]").evaluate_all("ps => ps.map(p => p.dataset.nodesPackage)")
        assert sorted(packages) == sorted({spec["class_path"].split(".")[0] for spec in specs})
        page.locator('[data-nodes-package="recordstream"] .nodes-package-head').click()
        kinds = page.locator('[data-nodes-package="recordstream"] [data-nodes-kind]').evaluate_all(
            "ks => ks.map(k => k.dataset.nodesKind)"
        )
        assert set(kinds) == {spec["kind"] for spec in specs if spec["class_path"].startswith("recordstream.")}
        assert kinds.index("source") < kinds.index("op")  # sources first, then ops, …
        page.locator(f'{node_root("gain")} .title').click()
        page.wait_for_selector('.app[data-selection="gain"]')
        assert "Click adds after gain" in page.locator("[data-nodes-sidebar] .nodes-foot").inner_text()
        page.locator("[data-nodes-search]").fill("recordstream.ops.numpy.Threshold")
        entry = page.locator('[data-nodes-entry][data-class="recordstream.ops.numpy.Threshold"]')
        assert entry.count() == 1
        entry.click()
        wait_saves(page, 1)
        text = api_get(server, "api/graphs/wires")["text"]
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n    from: gain\n" in text
        assert f"  boost:\n    op: !class:{MOD}.TinyGain {{gain: 3.0}}\n    from: threshold\n" in text
        page.wait_for_selector(node_root("threshold"))
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_every_level_folds_package_kind_group_remembered_and_a_search_opens_the_path(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        # TinyGain is `@configurable(category="op", group="canvas-test")`: <package> ▸ Ops ▸ canvas-test ▸ TinyGain
        opened = open_graph("wires")
        page = opened.page
        package = MOD.split(".")[0]
        page.locator('button[data-command="toggle-nodes"]').click()
        page.locator("[data-nodes-sidebar]").wait_for()
        wait_palette_loaded(page)
        entry = page.locator(f'[data-nodes-entry][data-class="{MOD}.TinyGain"]')
        head = page.locator(f'[data-nodes-package="{package}"] > .nodes-package-head')
        kind = page.locator(f'[data-nodes-package="{package}"] [data-nodes-kind="op"]')
        kind_head = kind.locator(":scope > .nodes-folder-head")
        group = kind.locator(f'[data-nodes-folder="{package}/op/canvas-test"]')
        group_head = group.locator(":scope > .nodes-folder-head")

        head.click()
        assert kind_head.get_attribute("aria-expanded") == "false"  # the kind is a folder, folded
        assert group.count() == 0 and entry.count() == 0
        kind_head.click()
        assert group_head.get_attribute("aria-expanded") == "false"  # the group is a folder, folded
        assert entry.count() == 0
        group_head.click()
        entry.wait_for()
        assert group_head.inner_text().split() == ["▸", "canvas-test", str(group.locator("[data-nodes-entry]").count())]

        page.reload()  # the folders opened are remembered in this browser
        wait_ready(page, "wires")
        wait_palette_loaded(page)
        entry.wait_for()

        group_head.click()  # fold it again, then search: a search opens every folder on the way to a match
        assert entry.count() == 0
        page.locator("[data-nodes-search]").fill(f"{MOD}.TinyGain")
        entry.wait_for()
        assert group_head.get_attribute("aria-expanded") == "true"
        page.locator("[data-nodes-search]").fill("")
        assert entry.count() == 0  # clearing the search puts the folders back as they were
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# right-click on the canvas: Add Node ▸, Add Group, …; Add Group draws an EMPTY box
# ------------------------------------------------------------------------------------------------


class TestRightClick:
    def test_right_click_on_empty_canvas_offers_add_node_and_add_group_which_draws_an_empty_box(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.mouse.click(*EMPTY, button="right")
        menu = page.locator('[data-context-menu="pane"]')
        menu.wait_for()
        labels = menu.locator(".menu-label").all_inner_texts()
        assert labels[:3] == ["Add Node", "Add Group", "Paste"]
        menu.locator('[data-submenu="add-node"]').click()
        page.locator("[data-add-node] [data-add-node-search]").wait_for()
        wait_palette_loaded(page)  # Add Node ▸ asked for the catalogue
        menu.locator('[data-command="add-group"]').click()
        wait_saves(page, 1)
        box = page.locator("[data-group]")
        assert box.count() == 1
        assert box.get_attribute("data-members") == ""
        assert box.locator(".group-empty").count() == 1
        groups = api_get(server, "api/graphs/wires")["groups"]
        assert len(groups) == 1 and groups[0]["nodes"] == [] and len(groups[0]["bounds"]) == 4
        corner = box.bounding_box()
        assert EMPTY[0] - 10 <= corner["x"] <= EMPTY[0] + 1 and EMPTY[1] - 10 <= corner["y"] <= EMPTY[1] + 1
        assert (corner["width"], corner["height"]) == tuple(groups[0]["bounds"][2:])  # drawn at its stored size
        assert not (graph_dir / "wires.layout.yaml").exists()
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# subgraphs: convert, enter, edit inside, reuse (copy, template, include)
# ------------------------------------------------------------------------------------------------

SCALE_LINE = (
    "          op: !class:recordstream.ops.numpy.Scale "
    "{source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}"
)

SUB_TEXT = f"""\
# a flow holding one subgraph
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}   # to one channel
        scaled:
{SCALE_LINE}
      result: scaled
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
outputs: mask
"""


def drawn_nodes(page: Any) -> List[str]:
    return sorted(page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)"))


def enter_subgraph(page: Any, name: str) -> None:
    """Double-click the subgraph's card (its class line — a double-click on the NAME renames) and wait for the level."""
    page.locator(f"{node_root(name)} .title .cls").dblclick()
    page.wait_for_selector(f'.app[data-level="{name}"]')
    page.wait_for_selector(node_root(f"{name}/<out>"))


def inner_value(body: Dict[str, Any], level: str, step: str, param: str) -> Any:  # a JSON value of any type
    nodes = body["model"]["inside"][level]["nodes"]
    return next(node for node in nodes if node["name"] == f"{level}/{step}")["params"][param]["value"]


class TestSubgraphs:
    def test_two_consecutive_steps_become_one_subgraph_node(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        box_select(page, "gain", "boost")
        assert selected_nodes(page) == ["boost", "gain"]
        menu_command(page, "graph", "to-subgraph")
        wait_saves(page, 1)
        page.wait_for_selector(node_root("subgraph"))
        assert page.locator(node_root("subgraph")).get_attribute("data-kind") == "subgraph"
        assert page.locator(node_root("gain")).count() == 0 and page.locator(node_root("boost")).count() == 0
        inner = page.locator(f'{node_root("subgraph")} [data-inner-steps]').get_attribute("data-inner-steps")
        assert inner == "gain,boost"
        body = api_get(server, "api/graphs/wires")
        steps = [node["name"] for node in body["model"]["nodes"] if node["kind"] != "host"]
        assert steps == ["decode", "subgraph", "count"]
        assert (
            "  subgraph:\n"
            "    op: !class:recordstream.flow.subgraph.Subgraph\n"
            "      steps:\n"
            "        gain:\n"
            f"          op: !class:{MOD}.TinyGain {{gain: 2.0}}\n"
            "        boost:\n"
            f"          op: !class:{MOD}.TinyGain {{gain: 3.0}}\n"
            "          from: gain\n"
            "      result: boost\n"
            "    from: decode\n"
        ) in body["text"]
        assert f"  count:\n    op: !class:{MOD}.TinyCount {{}}\n    from: subgraph\n" in body["text"]
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_a_double_click_shows_the_inside_under_a_breadcrumb_and_escape_goes_back_up(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        assert drawn_nodes(page) == ["mask", "prep"]
        assert page.locator("[data-breadcrumb]").count() == 0
        enter_subgraph(page, "prep")
        crumbs = page.locator("[data-breadcrumb] [data-crumb]").evaluate_all(
            "cs => cs.map(c => [c.dataset.crumb, c.textContent])"
        )
        assert crumbs == [["", "sub"], ["prep", "prep"]]
        assert " › ".join(label for _level, label in crumbs) == "sub › prep"
        inside = api_get(server, "api/graphs/sub")["model"]["inside"]["prep"]
        assert drawn_nodes(page) == sorted(node["name"] for node in inside["nodes"])
        assert drawn_nodes(page) == ["prep/<in>", "prep/<out>", "prep/grey", "prep/scaled"]
        titles = page.locator(f"{NODE} .title .name").all_inner_texts()
        assert sorted(titles) == ["<in>", "<out>", "grey", "scaled"]
        wait_edges(page, len(inside["wires"]))
        page.keyboard.press("Escape")
        page.wait_for_selector('.app[data-level=""]')
        page.wait_for_selector(node_root("mask"))
        assert page.locator("[data-breadcrumb]").count() == 0
        assert drawn_nodes(page) == ["mask", "prep"]
        assert opened.errors == []

    def test_an_inner_parameter_edit_changes_exactly_that_line_once_saved(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        enter_subgraph(page, "prep")
        widget = page.locator(f'{node_root("prep/scaled")} input[data-param="target_max"]')
        assert widget.input_value() == "1"
        widget.fill("2.5")
        widget.press("Enter")
        wait_saves(page, 1)
        assert page.locator(".app").get_attribute("data-level") == "prep"  # an edit keeps the level shown
        assert (graph_dir / "sub.yaml").read_text() == SUB_TEXT
        save_file(page, 2)
        before, after = SUB_TEXT.splitlines(), (graph_dir / "sub.yaml").read_text().splitlines()
        assert len(after) == len(before)
        changed = [(old, new) for old, new in zip(before, after) if old != new]
        assert changed == [(SCALE_LINE, SCALE_LINE.replace("target_max: 1.0}", "target_max: 2.5}"))]
        assert opened.errors == []

    def test_copy_and_paste_of_a_subgraph_node_gives_a_second_independent_block(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        page.locator(f'{node_root("prep")} .title .cls').click()
        copy_selection(page)
        page.keyboard.press("Control+v")
        wait_saves(page, 1)
        body = api_get(server, "api/graphs/sub")
        text = body["text"]
        assert text.count("op: !class:recordstream.flow.subgraph.Subgraph") == 2
        assert text.count("# to one channel") == 2  # the block's own lines, comments kept
        assert sorted(body["model"]["inside"]) == ["prep", "prep_2"]
        block = SUB_TEXT[SUB_TEXT.index("  prep:\n") : SUB_TEXT.index("  mask:\n")]
        assert text == SUB_TEXT.replace(block, block + block.replace("  prep:\n", "  prep_2:\n", 1))
        records = [(w["source"], w["target"]) for w in body["model"]["wires"] if w["kind"] == "record"]
        assert records == [("prep", "prep_2"), ("prep_2", "mask")]  # pasted after prep, read by what read prep
        page.wait_for_selector(node_root("prep_2"))
        enter_subgraph(page, "prep_2")
        widget = page.locator(f'{node_root("prep_2/scaled")} input[data-param="target_max"]')
        widget.fill("2.5")
        widget.press("Enter")
        wait_saves(page, 2)
        body = api_get(server, "api/graphs/sub")
        assert inner_value(body, "prep_2", "scaled", "target_max") == 2.5
        assert inner_value(body, "prep", "scaled", "target_max") == 1.0  # the copy is independent
        assert opened.errors == []

    def test_save_as_template_then_insert_template_adds_a_third_copy(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path, templates_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        page.locator(f'{node_root("prep")} .title .cls').click()
        copy_selection(page)
        page.keyboard.press("Control+v")
        wait_saves(page, 1)
        page.locator(f'{node_root("prep")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="prep"]')
        menu_command(page, "graph", "save-template")
        page.locator('[data-dialog="ask"]').wait_for()
        assert page.locator("[data-ask-input]").input_value() == "prep"
        page.locator("[data-ask-input]").fill("prepper")
        page.locator("[data-ask-confirm]").click()
        page.locator('[data-toast="info"]', has_text="template prepper saved").wait_for()
        saved = (templates_dir / "prepper.steps.yaml").read_text()
        assert saved.startswith("# prepper: grey, scaled — saved from ")
        assert "# to one channel" in saved and "target_max: 1.0}" in saved
        assert page.locator(".app").get_attribute("data-saves") == "1"  # a template is no edit of the graph
        page.locator(f'{node_root("mask")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="mask"]')
        open_menu(page, "graph")
        page.locator('[data-menu="graph"] [data-submenu="insert-template"]').click()
        page.locator('[data-template="prepper"]').click()
        wait_saves(page, 2)
        body = api_get(server, "api/graphs/sub")
        assert body["text"].count("op: !class:recordstream.flow.subgraph.Subgraph") == 3
        assert "include" not in body["text"]  # a COPY: nothing links back to the template
        assert sorted(body["model"]["inside"]) == ["prep", "prep_2", "prepper"]
        inner = [node["name"] for node in body["model"]["inside"]["prepper"]["nodes"]]
        assert inner == ["prepper/<in>", "prepper/grey", "prepper/scaled", "prepper/<out>"]
        page.wait_for_selector(node_root("prepper"))
        assert (graph_dir / "sub.yaml").read_text() == SUB_TEXT
        assert opened.errors == []

    def test_move_to_its_own_file_writes_the_steps_file_on_save_and_the_graph_includes_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        page.locator(f'{node_root("prep")} .title .cls').click(button="right")
        menu = page.locator('[data-context-menu="node"]')
        menu.wait_for()
        menu.locator('[data-command="extract"]').click()
        page.locator('[data-dialog="file"]').wait_for()
        assert page.locator("[data-file-name]").input_value() == "prep.steps.yaml"
        page.locator("[data-file-confirm]").click()
        wait_saves(page, 1)
        text = api_get(server, "api/graphs/sub")["text"]
        assert "      steps: {include: prep.steps.yaml}\n" in text and "grey:" not in text
        assert not (graph_dir / "prep.steps.yaml").exists()  # held by the editor until Save, like any change
        assert (graph_dir / "sub.yaml").read_text() == SUB_TEXT
        save_file(page, 2)
        steps = (graph_dir / "prep.steps.yaml").read_text()
        assert "grey:" in steps and "# to one channel" in steps and "target_max: 1.0}" in steps
        assert (graph_dir / "sub.yaml").read_text() == text
        body = api_get(server, "api/graphs/sub")
        assert body["model"]["problems"] == []
        grey = next(node for node in body["model"]["inside"]["prep"]["nodes"] if node["name"] == "prep/grey")
        assert grey["loc"].startswith(f"{graph_dir / 'prep.steps.yaml'}:")  # located in the steps file
        assert "prep.steps.yaml" not in [entry["name"] for entry in api_get(server, "api/graphs")]
        enter_subgraph(page, "prep")
        assert drawn_nodes(page) == ["prep/<in>", "prep/<out>", "prep/grey", "prep/scaled"]
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# the run panel over a subgraph: its inner nodes are rows under it
# ------------------------------------------------------------------------------------------------

RUNSUB_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource
    files: [{png}]
  ops:
    - !class:recordstream.ops.image.ReadImage {{}}
    - !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {{mode: L}}
        scaled:
          op: !class:recordstream.ops.numpy.Scale
            {{source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}}
      result: scaled
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
"""

RUNSUB_ROWS = ("stream.ops[0]", "stream.ops[1]", "stream.ops[1]/grey", "stream.ops[1]/scaled", "stream.ops[2]")


def write_demo_png(path: Path) -> None:
    """Two bright rectangles on a dark ground — the shape of the editor's demo image, generated here."""
    pixels = np.zeros((120, 160, 3), dtype=np.uint8)
    pixels[:] = (20, 20, 30)
    pixels[20:71, 15:61] = 230
    pixels[50:106, 90:146] = 200
    PILImage.fromarray(pixels).save(path)


class TestRunPanelInsideSubgraphs:
    def test_the_run_panel_lists_the_inner_rows_under_their_subgraph_and_a_run_pauses_inside(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "runsub.yaml").write_text(RUNSUB_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("runsub")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        rows = page.locator("[data-run-row]").evaluate_all(
            "rs => rs.map(r => [r.dataset.runRow, r.dataset.runParent ?? null, r.dataset.depth])"
        )
        assert rows == [
            ["stream.ops[0]", None, "0"],
            ["stream.ops[1]", None, "0"],
            ["stream.ops[1]/grey", "stream.ops[1]", "1"],
            ["stream.ops[1]/scaled", "stream.ops[1]", "1"],
            ["stream.ops[2]", None, "0"],
        ]
        assert page.locator('[data-run-row="stream.ops[1]/grey"] td').first.inner_text().startswith("└ grey")
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["statuses"] == {name: "ok" for name in RUNSUB_ROWS}
        for name in RUNSUB_ROWS:
            assert page.locator(f'[data-run-row="{name}"] [data-field="status"]').inner_text() == "ok", name

        # inside the subgraph, the selected inner node is where the next run pauses
        enter_subgraph(page, "stream.ops[1]")
        page.locator(f'{node_root("stream.ops[1]/scaled")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="stream.ops[1]/scaled"]')
        page.locator('[data-action="run"]').click()
        wait_answers(page, 2)
        assert run_statuses(page, "stream.ops[1]/grey", "stream.ops[1]/scaled") == {
            "stream.ops[1]/grey": "ok",
            "stream.ops[1]/scaled": "paused",
        }
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["paused_at"] == "stream.ops[1]/scaled"
        assert run["statuses"]["stream.ops[2]"] == "not reached"
        assert opened.errors == []


# ================================================================================================
# the fixes after the build (FIXES.md K1–K8 and the review's confirmed items)
# ================================================================================================

#: The class a document spells a subgraph step with — never offered as a class to add (an empty one is refused).
SUBGRAPH = "recordstream.flow.subgraph.Subgraph"

CHAIN_TEXT = """\
# chain — a plain flow graph with four steps and comments
flow:
  grey:
    op: !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel
  scaled:
    op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
  mask:
    # keep the bright part
    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
  parts:
    op: !class:recordstream.ops.numpy.ConnectedComponents {}
outputs: parts
"""

BLOBS_TEXT = """\
# blobs — read, prepare inside a subgraph, threshold, count
flow:
  read:
    op: !class:recordstream.ops.image.ReadImage {}
  prep:
    from: read
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {mode: L}
        scaled:
          op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
      result: scaled
  mask:
    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
  parts:
    op: !class:recordstream.ops.numpy.ConnectedComponents {}
outputs: parts
"""

DEMO_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource
    files: [{png}]
  ops:
    - !class:recordstream.ops.image.ReadImage {{}}
    - !class:recordstream.ops.image.ConvertMode {{mode: L}}
    - !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}}
    - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    - !class:recordstream.ops.numpy.ConnectedComponents {{}}
"""

STEPS_FILE_TEXT = """\
# prep — make the image grey, then scale it to 0..1
grey:
  op: !class:recordstream.ops.image.ConvertMode {mode: L}
scaled:
  op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
"""


def steps_of(body: Dict[str, Any]) -> List[str]:
    """The top level's steps, in the model's order (the host's side left out)."""
    return [node["name"] for node in body["model"]["nodes"] if node["kind"] != "host"]


def rects_overlap(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return (
        a["x"] < b["x"] + b["width"]
        and b["x"] < a["x"] + a["width"]
        and a["y"] < b["y"] + b["height"]
        and b["y"] < a["y"] + a["height"]
    )


def flow_point(page: Any, screen: Tuple[float, float]) -> Tuple[float, float]:
    """The flow coordinates under a screen point at the top level's START viewport (zoom 1, origin 56 px down),
    snapped to the canvas's 10-px grid as React Flow's ``screenToFlowPosition`` snaps them."""
    pane = page.locator(".canvas .react-flow").bounding_box()
    return round((screen[0] - pane["x"]) / 10) * 10, round((screen[1] - pane["y"] - 56) / 10) * 10


class TestPasteRightAfterCopy:
    """K1: Ctrl/Cmd+V pressed while the copy is still on its way is never dropped — it waits for the copy, then
    pastes."""

    def test_ctrl_v_before_the_copy_answered_waits_for_it_and_pastes(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        held: List[Any] = []  # the copy request, held until the paste key has been pressed
        page.route("**/api/graphs/chain/copy", lambda route: held.append(route))
        page.locator(f'{node_root("grey")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="grey"]')
        page.keyboard.press("Control+c")
        while not held:  # each round trip lets playwright hand the intercepted copy request to the route above
            page.wait_for_function("() => true")
        page.keyboard.press("Control+v")  # the copy has NOT answered: the paste must wait for it
        held[0].continue_()
        wait_saves(page, 1)
        text = api_get(server, "api/graphs/chain")["text"]
        assert text.count("op: !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel") == 2
        assert "  grey_2:\n" in text
        assert opened.errors == []


class TestSubgraphIsMadeNotAdded:
    """K2: the Subgraph class is left out of the Nodes sidebar, the palette and Add Node ▸ (an empty one is refused);
    the sidebar says how a subgraph is made instead."""

    def test_no_list_offers_the_subgraph_class_and_the_sidebar_names_the_three_ways(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        assert SUBGRAPH in [spec["class_path"] for spec in api_get(server, "api/palette")]  # the catalogue holds it
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill("recordstream.flow.subgraph")
        assert page.locator(f'[data-nodes-entry][data-class="{SUBGRAPH}"]').count() == 0
        hint = page.locator("[data-nodes-subgraph-hint]").inner_text()
        assert "Convert selection to subgraph" in hint and "Insert template" in hint and "Insert steps file" in hint
        page.mouse.dblclick(*EMPTY)
        page.locator("[data-palette-search]").fill(SUBGRAPH)
        assert page.locator("[data-palette-list] li").count() == 0
        page.keyboard.press("Escape")
        page.mouse.click(*EMPTY, button="right")
        page.locator('[data-context-menu="pane"] [data-submenu="add-node"]').click()
        page.locator("[data-add-node-search]").fill(SUBGRAPH)
        assert page.locator(f'[data-add-node] [data-class="{SUBGRAPH}"]').count() == 0
        assert opened.errors == []


class TestSettingsFollowTheLevel:
    """K3: the settings tab lists the nodes of the level the canvas shows."""

    def test_inside_a_subgraph_the_settings_tab_lists_and_edits_its_inner_steps(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        page.locator('[data-tab="settings"]').click()
        page.locator("[data-settings-all]").check()
        sections = "ss => ss.map(s => s.dataset.formNode)"
        page.wait_for_selector('[data-form-node="mask"]')
        assert page.locator("[data-form-node]").evaluate_all(sections) == ["prep", "mask"]
        enter_subgraph(page, "prep")
        page.wait_for_selector('[data-form-node="prep/grey"]')
        assert page.locator("[data-form-node]").evaluate_all(sections) == ["prep/grey", "prep/scaled"]
        widget = page.locator('[data-form-node="prep/scaled"] input[data-param="target_max"]')
        widget.fill("2.5")
        widget.press("Enter")
        wait_saves(page, 1)
        assert inner_value(api_get(server, "api/graphs/sub"), "prep", "scaled", "target_max") == 2.5
        page.locator('[data-crumb=""]').click()
        page.wait_for_selector('[data-form-node="mask"]')
        assert page.locator("[data-form-node]").evaluate_all(sections) == ["prep", "mask"]
        assert opened.errors == []


class TestLevelInTheUrl:
    """K5: ``?level=`` follows the canvas like ``?graph=``; a reload opens that level."""

    def test_the_level_shown_is_in_the_url_and_a_reload_opens_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        assert "level=" not in page.url
        enter_subgraph(page, "prep")
        page.wait_for_function("() => new URL(location.href).searchParams.get('level') === 'prep'")
        assert "graph=sub" in page.url
        page.reload()
        wait_ready(page, "sub")
        page.wait_for_selector('.app[data-level="prep"]')
        page.wait_for_selector(node_root("prep/<out>"))
        assert drawn_nodes(page) == ["prep/<in>", "prep/<out>", "prep/grey", "prep/scaled"]
        page.keyboard.press("Escape")
        page.wait_for_selector('.app[data-level=""]')
        page.wait_for_function("() => !new URL(location.href).searchParams.has('level')")
        assert opened.errors == []

    def test_a_level_the_graph_does_not_hold_opens_the_top(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "sub.yaml").write_text(SUB_TEXT)
        opened = open_graph("sub")
        page = opened.page
        page.goto(f"{server}/?graph=sub&level=nope")
        wait_ready(page, "sub")
        page.wait_for_selector(node_root("mask"))
        assert page.locator(".app").get_attribute("data-level") == ""
        page.wait_for_function("() => !new URL(location.href).searchParams.has('level')")
        assert opened.errors == []


class TestCutAndDeleteAreOneEdit:
    """K4 (the page's half): Cut and Delete remove every selected step in ONE request — one edit, one undo."""

    def test_deleting_two_steps_is_one_request_and_one_undo_brings_both_back(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        sent: List[str] = []
        # every request touching steps, as `METHOD path` (`POST api/graphs/chain/steps/remove`)
        page.on(
            "request",
            lambda request: (
                sent.append(f"{request.method} {request.url.split('/', 3)[3]}") if "/steps" in request.url else None
            ),
        )
        box_select(page, "grey", "scaled")
        assert selected_nodes(page) == ["grey", "scaled"]
        page.keyboard.press("Delete")
        wait_saves(page, 1)
        page.wait_for_selector(node_root("grey"), state="detached")
        assert steps_of(api_get(server, "api/graphs/chain")) == ["mask", "parts"]
        assert sent == ["POST api/graphs/chain/steps/remove"]  # ONE request, not a DELETE per step
        page.keyboard.press("Control+z")
        wait_saves(page, 2)
        assert api_get(server, "api/graphs/chain")["text"] == CHAIN_TEXT
        assert opened.errors == []

    def test_cut_removes_the_steps_in_one_edit_and_paste_puts_them_back_elsewhere(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        box_select(page, "grey", "scaled")
        page.keyboard.press("Control+x")
        wait_saves(page, 1)
        assert steps_of(api_get(server, "api/graphs/chain")) == ["mask", "parts"]
        page.locator(f'{node_root("parts")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="parts"]')
        page.keyboard.press("Control+v")
        wait_saves(page, 2)
        assert steps_of(api_get(server, "api/graphs/chain")) == ["mask", "parts", "grey", "scaled"]
        assert opened.errors == []


class TestConvertingAGroup:
    """The review's HIGH item: Convert group to subgraph draws the server's answer — ONE subgraph node at the box's
    corner — and one undo brings back the steps AND the box."""

    def test_the_group_becomes_one_subgraph_node_where_the_box_was(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        box_select(page, "grey", "scaled")
        page.keyboard.press("g")
        wait_saves(page, 1)
        corner = node_translate(page, "group:g1")
        page.locator(".group-title").first.click(button="right")
        page.locator('[data-context-menu="group"] [data-command="group-to-subgraph"]').click()
        wait_saves(page, 2)
        page.wait_for_selector(node_root("group_1"))
        body = api_get(server, "api/graphs/chain")
        assert steps_of(body) == ["group_1", "mask", "parts"]
        assert drawn_nodes(page) == ["group_1", "mask", "parts"]
        assert page.locator(node_root("group_1")).get_attribute("data-kind") == "subgraph"
        assert body["groups"] == []
        assert page.locator("[data-group]").count() == 0
        assert (
            "  group_1:\n    op: !class:recordstream.flow.subgraph.Subgraph\n"
            in page.locator("[data-yaml]").inner_text()
        )
        assert node_translate(page, "group_1") == corner
        assert body["positions"]["group_1"] == list(corner)
        page.keyboard.press("Control+z")
        wait_saves(page, 3)
        page.wait_for_selector(node_root("grey"))
        body = api_get(server, "api/graphs/chain")
        assert body["text"] == CHAIN_TEXT
        assert [group["nodes"] for group in body["groups"]] == [["grey", "scaled"]]
        page.wait_for_selector('[data-group="g1"][data-members="grey,scaled"]')
        assert opened.errors == []

    def test_converting_boxed_steps_from_the_menu_keeps_the_box_around_the_new_node(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """The review's LOW item: the box's members that became the subgraph are replaced by it — never a box naming
        nodes that are gone."""
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        box_select(page, "grey", "scaled")
        page.keyboard.press("g")
        wait_saves(page, 1)
        page.mouse.click(*EMPTY)
        box_select(page, "grey", "scaled")
        menu_command(page, "graph", "to-subgraph")
        wait_saves(page, 2)
        page.wait_for_selector(node_root("subgraph"))
        assert [group["nodes"] for group in api_get(server, "api/graphs/chain")["groups"]] == [["subgraph"]]
        page.wait_for_selector('[data-group="g1"][data-members="subgraph"]')
        assert opened.errors == []


class TestSelectionAfterConvertingListMembers:
    """The review's MEDIUM item: list members are named by position, so after a conversion the old names belong to
    OTHER nodes — nothing stays selected, and ▶ Run runs the whole stream."""

    def test_nothing_stays_selected_and_run_runs_every_member(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("demo")
        page = opened.page
        box_select(page, "stream.ops[1]", "stream.ops[2]")
        assert page.locator(".app").get_attribute("data-selection") == "stream.ops[1],stream.ops[2]"
        menu_command(page, "graph", "to-subgraph")
        wait_saves(page, 1)
        page.wait_for_selector(f'{node_root("stream.ops[1]")}[data-kind="subgraph"]')
        page.wait_for_selector('.app[data-selection=""]')
        assert selected_nodes(page) == []
        open_run_tab(page)
        assert "pausing" not in (page.locator('button[data-command="run"]').get_attribute("title") or "")
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        run = api_get(server, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["paused_at"] is None
        assert run["statuses"]["stream.ops[3]"] == "ok"
        assert opened.errors == []


class TestHoverCard:
    """The review's MEDIUM item: the hover card stays inside the window and says how many parameters it could not
    show."""

    def test_a_long_parameter_list_stays_in_the_window_and_counts_the_hidden_ones(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(f"{MOD}.TinyWide")
        page.locator(f'[data-nodes-entry][data-class="{MOD}.TinyWide"]').hover()
        card = page.locator(f'[data-nodes-card="{MOD}.TinyWide"]')
        card.wait_for()
        page.wait_for_selector("[data-card-more]")
        box = card.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 900  # inside the 1400×900 window
        shown = card.locator(".node-card-param").count()
        assert 0 < shown < WIDE
        assert card.locator("[data-card-more]").inner_text() == f"+{WIDE - shown} more parameters"
        for row in card.locator(".node-card-param").all():  # every row drawn is drawn whole
            row_box = row.bounding_box()
            assert row_box["y"] + row_box["height"] <= box["y"] + box["height"]
        page.locator("[data-nodes-search]").fill(f"{MOD}.TinyGain")
        page.mouse.move(*EMPTY)  # the new list is drawn under a pointer that did not move: move it away, then back
        page.locator(f'[data-nodes-entry][data-class="{MOD}.TinyGain"]').hover()
        small = page.locator(f'[data-nodes-card="{MOD}.TinyGain"]')
        small.wait_for()
        assert small.locator(".node-card-param").count() == 1
        assert small.locator("[data-card-more]").count() == 0
        assert opened.errors == []


class TestDocsMarkup:
    """K7: a docstring's ``literal``, `interpreted` and :role:`target` show as inline code — never the markup."""

    def test_the_sidebar_shows_a_role_and_a_literal_as_inline_code(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        specs = api_get(server, "api/palette")
        marked = next(
            spec
            for spec in specs
            if re.search(r":[a-z]+:`[^`]+`", spec["doc"]) and "``" in spec["doc"] and "~" not in spec["doc"]
        )
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(marked["class_path"])
        entry = page.locator(f'[data-nodes-entry][data-class="{marked["class_path"]}"]')
        doc = entry.locator(".nodes-entry-doc")
        shown = doc.inner_text()
        assert "``" not in shown and not re.search(r":[a-z]+:", shown), shown
        role_match = re.search(r":[a-z]+:`([^`]+)`", marked["doc"])
        literal_match = re.search(r"``(.+?)``", marked["doc"])
        assert role_match and literal_match  # the spec was picked for carrying both
        role, literal = role_match.group(1), literal_match.group(1)
        codes = doc.locator("code").all_inner_texts()
        assert role in codes and literal in codes
        assert opened.errors == []


class TestAddGroupFromTheBar:
    """The review's LOW item: Graph ▸ Add group drops its empty box on empty canvas — never over cards it does not
    hold."""

    def test_the_box_lands_where_no_card_is(self, open_graph: Callable[[str], Opened], server: str) -> None:
        positions = api_get(server, "api/graphs/wires")["positions"]
        # cards across the middle of the view, where the bar's Add group used to centre the box
        crowded = {**positions, "decode": [100, 330], "gain": [390, 330], "boost": [680, 330], "count": [390, 520]}
        api_put(server, "api/graphs/wires/positions", {"positions": crowded})
        opened = open_graph("wires")
        page = opened.page
        menu_command(page, "graph", "add-group")
        wait_saves(page, 1)
        box = page.locator("[data-group]")
        assert box.get_attribute("data-members") == ""
        drawn = box.bounding_box()
        for name in ("decode", "gain", "boost", "count"):
            assert not rects_overlap(drawn, page.locator(node_root(name)).bounding_box()), name
        pane = page.locator(".canvas .react-flow").bounding_box()
        assert drawn["x"] >= pane["x"] and drawn["x"] + drawn["width"] <= pane["x"] + pane["width"]
        assert opened.errors == []


#: Every card of the level lies inside the canvas's visible pane — what a person sees without scrolling.
CARDS_IN_VIEW = """() => {
  const pane = document.querySelector('.canvas .react-flow').getBoundingClientRect();
  const cards = [...document.querySelectorAll('.react-flow__node')].map((n) => n.getBoundingClientRect());
  return cards.length > 0 && cards.every((c) =>
    c.left >= pane.left && c.right <= pane.right && c.top >= pane.top && c.bottom <= pane.bottom);
}"""


def viewport_zoom(page: Any) -> float:
    style = page.locator(".react-flow__viewport").get_attribute("style") or ""
    found = re.search(r"scale\(([\d.]+)\)", style)
    assert found, style
    return float(found.group(1))


class TestEnteringASubgraphIsReadable:
    """Entering a subgraph shows its WHOLE inside when it fits at a readable zoom (0.8 and up, never above 1),
    with its ``<in>`` in view either way. Measured before (2026-09-29, a 1440x900 window): the two-step inside of
    ``prep`` opened at zoom 1 with ``<out>`` off the right edge; at 0.66 (the first version's plain fit) the cards'
    text was 6-8 px."""

    def test_a_small_inside_opens_whole_at_a_readable_zoom(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "blobs.yaml").write_text(BLOBS_TEXT)
        opened = open_graph("blobs")
        page = opened.page
        enter_subgraph(page, "prep")
        page.wait_for_selector('.canvas[data-view-level="prep"]')
        page.wait_for_function(CARDS_IN_VIEW)
        assert 0.8 <= viewport_zoom(page) <= 1.0
        assert drawn_nodes(page) == ["prep/<in>", "prep/<out>", "prep/grey", "prep/scaled"]
        assert opened.errors == []


class TestArrangeShowsTheGraph:
    """View ▸ Arrange lays the level out AND shows it: measured before (2026-09-29), the arranged demo stream
    stepped down to the bottom-right corner, half behind the side panel and the minimap."""

    def test_after_arrange_every_card_is_in_view(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("demo")
        page = opened.page
        menu_command(page, "view", "arrange")
        wait_saves(page, 1)
        page.wait_for_function(CARDS_IN_VIEW)
        assert viewport_zoom(page) <= 1.0
        assert opened.errors == []


class TestFileDialogPath:
    """The review's LOW item: a deep folder's path field shows the folder's own name, not only the start of the path."""

    def test_the_path_field_is_scrolled_to_the_current_folder(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        deep = graph_dir / ("a_rather_long_folder_name_" * 3) / "and_another_long_folder_for_the_dialog" / "graphs"
        deep.mkdir(parents=True)
        (deep / "deep.yaml").write_text(CLIP_TEXT)
        api_post(server, "api/graphs/open", {"path": str(deep / "deep.yaml")})
        opened = open_graph("deep")
        page = opened.page
        menu_command(page, "file", "open")
        page.wait_for_selector(f'[data-file-dir="{deep.resolve()}"]')
        field = page.locator("[data-file-path]")
        left, width, shown = field.evaluate("e => [e.scrollLeft, e.scrollWidth, e.clientWidth]")
        assert width > shown  # the path is longer than the field
        assert left + shown >= width - 1  # scrolled to its end: "…/graphs" is what shows
        assert opened.errors == []


class TestFileDialogPlacesAndNewFolder:
    """The dialog's shortcut buttons — the graph folder and each place variable set to a folder — and its New folder
    (user request 2026-10-28)."""

    @pytest.fixture
    def places(self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
        for variable in PLACE_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        data = tmp_path_factory.mktemp("data_root").resolve()
        monkeypatch.setenv("DATA_ROOT", str(data))
        return data

    def test_a_place_button_lists_its_folder_and_is_marked_while_it_is_shown(
        self, open_graph: Callable[[str], Opened], graph_dir: Path, places: Path
    ) -> None:
        (places / "captures").mkdir()
        opened = open_graph("wires")
        page = opened.page
        menu_command(page, "file", "open")
        page.wait_for_selector(f'[data-file-mode="open"][data-file-dir="{graph_dir.resolve()}"]')
        buttons = page.locator("[data-file-place]").evaluate_all("bs => bs.map(b => [b.dataset.filePlace, b.title])")
        assert buttons == [["Graphs", str(graph_dir.resolve())], ["DATA_ROOT", str(places)]]
        assert page.locator('[data-file-place="Graphs"]').get_attribute("aria-pressed") == "true"
        page.locator('[data-file-place="DATA_ROOT"]').click()
        page.wait_for_selector(f'[data-file-dir="{places}"]')
        assert page.locator("[data-file-path]").input_value() == str(places)
        assert page.locator("[data-file-entry]").evaluate_all("es => es.map(e => e.dataset.fileEntry)") == ["captures"]
        assert page.locator('[data-file-place="DATA_ROOT"]').get_attribute("aria-pressed") == "true"
        assert page.locator('[data-file-place="Graphs"]').get_attribute("aria-pressed") == "false"
        page.locator('[data-file-place="Graphs"]').click()
        page.wait_for_selector(f'[data-file-dir="{graph_dir.resolve()}"]')
        assert opened.errors == []

    def test_open_offers_no_new_folder(self, open_graph: Callable[[str], Opened], graph_dir: Path) -> None:
        opened = open_graph("wires")
        page = opened.page
        menu_command(page, "file", "open")
        page.wait_for_selector(f'[data-file-mode="open"][data-file-dir="{graph_dir.resolve()}"]')
        assert page.locator("[data-file-new-folder]").count() == 0
        assert opened.errors == []

    def test_save_as_makes_a_new_folder_enters_it_and_saves_there(
        self, open_graph: Callable[[str], Opened], graph_dir: Path, places: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        menu_command(page, "file", "save-as")
        page.wait_for_selector(f'[data-file-mode="save"][data-file-dir="{graph_dir.resolve()}"]')
        page.locator('[data-file-place="DATA_ROOT"]').click()
        page.wait_for_selector(f'[data-file-dir="{places}"]')
        page.locator("[data-file-new-folder]").click()
        page.locator("[data-file-new-folder-name]").fill("outputs")
        page.locator("[data-file-new-folder-name]").press("Enter")
        page.wait_for_selector(f'[data-file-dir="{places / "outputs"}"]')
        assert (places / "outputs").is_dir()
        assert page.locator("[data-file-new-folder-name]").count() == 0  # the name field closes once it is made
        assert page.locator("[data-file-name]").input_value() == "pets.yaml"  # the typed file name survives
        page.locator("[data-file-confirm]").click()
        wait_ready(page, "pets")
        assert (places / "outputs" / "pets.yaml").read_text() == PETS_TEXT
        assert opened.errors == []

    def test_a_refused_name_says_why_and_makes_nothing(
        self, open_graph: Callable[[str], Opened], graph_dir: Path, places: Path
    ) -> None:
        (places / "outputs").mkdir()
        opened = open_graph("wires")
        page = opened.page
        open_new(page, "pipeline")
        page.locator("[data-new-browse]").click()
        page.wait_for_selector('[data-dialog="file"] [data-file-mode="folder"]')
        page.locator('[data-file-place="DATA_ROOT"]').click()
        page.wait_for_selector(f'[data-file-dir="{places}"]')
        page.locator("[data-file-new-folder]").click()
        page.locator("[data-file-new-folder-name]").fill("outputs")
        page.locator("[data-file-new-folder-name]").press("Enter")
        page.wait_for_selector(".file-error")
        assert page.locator(".file-error").inner_text() == f"{places / 'outputs'} already exists — pick another name"
        assert page.locator("[data-file-dir]").get_attribute("data-file-dir") == str(places)
        assert page.locator("[data-file-new-folder-name]").input_value() == "outputs"  # kept to correct
        page.locator("[data-file-new-folder-name]").press("Escape")
        assert page.locator("[data-file-new-folder-name]").count() == 0
        page.locator('[data-dialog="file"]').wait_for()  # Escape closed the name field, not the dialog
        assert sorted(child.name for child in places.iterdir()) == ["outputs"]
        assert opened.errors == [REFUSED_400]

    @pytest.mark.parametrize("mode", ["open", "save"])
    def test_a_long_folder_in_a_short_window_scrolls_the_list_alone(
        self, open_graph: Callable[[str], Opened], graph_dir: Path, mode: str
    ) -> None:
        """The list gives up height when the window is short; the box around it never scrolls beside it (two
        scrollbars)."""
        for index in range(80):
            (graph_dir / f"folder_{index:03d}").mkdir()
        opened = open_graph("pets")
        page = opened.page
        menu_command(page, "file", "open" if mode == "open" else "save-as")
        page.wait_for_selector(f'[data-file-mode="{mode}"][data-file-dir="{graph_dir.resolve()}"]')
        page.wait_for_selector("[data-file-entry]")
        if mode == "save":
            page.locator("[data-file-new-folder]").click()  # the dialog at its tallest: a name field beside the list
            page.locator("[data-file-new-folder-name]").wait_for()
        scrolls = "el => el.scrollHeight > el.clientHeight + 1"
        roomy = page.locator(".file-list").bounding_box()["height"]
        assert page.locator(".file-list").evaluate(scrolls)
        assert not page.locator('[data-dialog="file"] .modal-body').evaluate(scrolls)
        page.set_viewport_size({"width": 1400, "height": 520})
        assert page.locator(".file-list").evaluate(scrolls)
        assert not page.locator('[data-dialog="file"] .modal-body').evaluate(scrolls)
        assert page.locator(".file-list").bounding_box()["height"] < roomy  # it is the list that gave way
        confirm = page.locator("[data-file-confirm]").bounding_box()
        assert confirm["y"] + confirm["height"] <= 520  # the dialog still fits the window
        assert opened.errors == []


class TestToastsKeepClearOfTheTabs:
    """The review's LOW item: a toast never covers the side panel's tabs (or the bar), and a click on a tab switches."""

    def test_a_toast_leaves_the_tabs_clickable(self, open_graph: Callable[[str], Opened], graph_dir: Path) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        page.locator(f'{node_root("grey")} .title .cls').click()
        copy_selection(page)
        toast = page.locator("[data-toast]").first.bounding_box()
        bar = page.locator("header.bar").bounding_box()
        assert toast["y"] >= bar["y"] + bar["height"]
        for tab in ("text", "settings", "run"):
            tab_box = page.locator(f'[data-tab="{tab}"]').bounding_box()
            assert not rects_overlap(toast, tab_box), tab
            hit = page.evaluate(
                "([x, y]) => document.elementFromPoint(x, y)?.dataset.tab ?? null", list(centre(tab_box))
            )
            assert hit == tab
        page.locator('[data-tab="run"]').click()
        page.locator("[data-run-panel]").wait_for()
        assert page.locator("[data-toast]").count() >= 1  # the toast was still up when the tab switched
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# K8: the gestures the build shipped without a browser test
# ------------------------------------------------------------------------------------------------


class TestFileMenuGestures:
    def test_save_as_through_the_file_dialog_writes_the_new_file_and_shows_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        edit_low_level(page, "0.9")
        wait_saves(page, 1)
        menu_command(page, "file", "save-as")
        page.locator('[data-dialog="file"]').wait_for()
        page.wait_for_selector(f'[data-file-mode="save"][data-file-dir="{graph_dir.resolve()}"]')
        assert page.locator("[data-file-name]").input_value() == "pets.yaml"
        page.locator("[data-file-name]").fill("pets_copy")
        page.locator("[data-file-confirm]").click()
        wait_ready(page, "pets_copy")
        page.wait_for_selector('.app[data-dirty="false"]')
        assert "low_level: 0.9}   # the mask" in (graph_dir / "pets_copy.yaml").read_text()
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT  # the old file is left as it was
        assert "graph=pets_copy" in page.url
        assert "pets_copy" in page.locator("select[data-graph-picker] option").evaluate_all(
            "os => os.map(o => o.value)"
        )
        assert opened.errors == []

    def test_revert_from_the_file_menu_drops_the_unsaved_edit(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        edit_low_level(page, "0.9")
        wait_saves(page, 1)
        menu_command(page, "file", "revert")
        page.locator('[data-dialog="ask"]').wait_for()
        page.locator("[data-ask-confirm]").click()
        wait_saves(page, 2)
        page.wait_for_selector('.app[data-dirty="false"]')
        assert api_get(server, "api/graphs/pets")["text"] == PETS_TEXT
        assert page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]').input_value() == "0.5"
        assert page.locator("[data-yaml]").inner_text().rstrip("\n") == PETS_TEXT.rstrip("\n")
        assert opened.errors == []

    def test_insert_steps_file_adds_a_subgraph_step_that_includes_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "prep.steps.yaml").write_text(STEPS_FILE_TEXT)
        opened = open_graph("wires")
        page = opened.page
        page.mouse.click(*EMPTY, button="right")
        page.locator('[data-context-menu="pane"] [data-command="insert-steps"]').click()
        page.locator('[data-dialog="file"]').wait_for()
        page.wait_for_selector(f'[data-file-mode="open"][data-file-dir="{graph_dir.resolve()}"]')
        page.locator('[data-file-entry="prep.steps.yaml"]').click()
        page.locator("[data-file-confirm]").click()
        wait_saves(page, 1)
        body = api_get(server, "api/graphs/wires")
        assert "steps: {include: prep.steps.yaml}" in body["text"]
        assert [node["name"] for node in body["model"]["inside"]["prep"]["nodes"]] == [
            "prep/<in>",
            "prep/grey",
            "prep/scaled",
            "prep/<out>",
        ]
        page.wait_for_selector(f'{node_root("prep")}[data-kind="subgraph"]')
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert opened.errors == []

    def test_leaving_the_page_while_unsaved_asks_first(self, open_graph: Callable[[str], Opened]) -> None:
        opened = open_graph("pets")
        page = opened.page
        asked: List[str] = []

        def answer(dialog: Any) -> None:  # playwright.sync_api.Dialog: record what was asked, then leave
            asked.append(dialog.type)
            dialog.accept()

        page.on("dialog", answer)
        page.locator(f'{node_root("stream.ops[1]")} .title .cls').click()  # a gesture, as a person makes one
        page.reload()
        wait_ready(page, "pets")
        assert asked == []  # nothing unsaved: no question
        edit_low_level(page, "0.9")
        wait_saves(page, 1)
        page.reload()
        wait_ready(page, "pets")
        assert asked == ["beforeunload"]
        assert opened.errors == []


class TestGroupGestures:
    def test_resizing_a_box_over_another_node_takes_it_in(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        box_select(page, "decode", "gain")
        page.keyboard.press("g")
        wait_saves(page, 1)
        box = page.locator('[data-group="g1"]')
        assert box.get_attribute("data-members") == "decode,gain"
        grip = page.locator('[data-group="g1"] [data-group-grip]').bounding_box()
        boost = page.locator(node_root("boost")).bounding_box()
        drag(
            page,
            centre(grip),
            (boost["x"] + boost["width"] + 30, max(centre(grip)[1], boost["y"] + boost["height"] + 30)),
        )
        wait_saves(page, 2)
        page.wait_for_selector('[data-group="g1"][data-members="decode,gain,boost"]')
        group = api_get(server, "api/graphs/wires")["groups"][0]
        assert group["nodes"] == ["decode", "gain", "boost"]
        assert len(group["bounds"]) == 4
        assert opened.errors == []

    def test_dragging_a_node_into_a_box_adds_it_and_out_again_lets_it_go(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.mouse.click(400, 450, button="right")
        page.locator('[data-context-menu="pane"] [data-command="add-group"]').click()
        wait_saves(page, 1)
        box = page.locator("[data-group]")
        assert box.get_attribute("data-members") == ""
        drawn = box.bounding_box()
        title = centre(page.locator(f'{node_root("boost")} .title').bounding_box())
        drag(page, title, (drawn["x"] + drawn["width"] / 2, drawn["y"] + 90))
        wait_saves(page, 2)
        page.wait_for_selector('[data-group][data-members="boost"]')
        assert api_get(server, "api/graphs/wires")["groups"][0]["nodes"] == ["boost"]
        inside = centre(page.locator(f'{node_root("boost")} .title').bounding_box())
        drag(page, inside, title)
        wait_saves(page, 3)
        page.wait_for_selector('[data-group][data-members=""]')
        group = api_get(server, "api/graphs/wires")["groups"][0]
        assert group["nodes"] == [] and len(group["bounds"]) == 4  # the box stays where it was, empty
        assert opened.errors == []


class TestAddingFromMenusAndTheSidebar:
    def test_add_node_from_the_right_click_menu_adds_the_class_where_the_canvas_was_clicked(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.mouse.click(500, 500, button="right")
        page.locator('[data-context-menu="pane"] [data-submenu="add-node"]').click()
        wait_palette_loaded(page)
        page.locator("[data-add-node-search]").fill("recordstream.ops.numpy.Threshold")
        page.locator('[data-add-node] [data-class="recordstream.ops.numpy.Threshold"]').click()
        wait_saves(page, 1)
        body = api_get(server, "api/graphs/wires")
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n    from: count\n" in body["text"]
        at = flow_point(page, (500, 500))
        assert body["positions"]["threshold"] == [pytest.approx(at[0], abs=1), pytest.approx(at[1], abs=1)]
        page.wait_for_selector(node_root("threshold"))
        assert opened.errors == []

    def test_dragging_a_sidebar_entry_onto_the_canvas_adds_it_at_the_drop_point(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill("recordstream.ops.numpy.Threshold")
        entry = page.locator('[data-nodes-entry][data-class="recordstream.ops.numpy.Threshold"]')
        pane = page.locator(".canvas .react-flow__pane")
        pane_box = pane.bounding_box()
        entry.drag_to(pane, target_position={"x": 300, "y": 450})
        wait_saves(page, 1)
        body = api_get(server, "api/graphs/wires")
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n" in body["text"]
        at = flow_point(page, (pane_box["x"] + 300, pane_box["y"] + 450))
        assert body["positions"]["threshold"] == [pytest.approx(at[0], abs=1), pytest.approx(at[1], abs=1)]
        page.wait_for_selector(node_root("threshold"))
        assert opened.errors == []


# ================================================================================================
# round 2 (DESIGN2): unsaved work survives a stop, op lists are editable, Shift+click adds
# ================================================================================================

#: What the last session's edit made of the pets document's ``Threshold`` line.
PETS_KEPT_LINE = "low_level: 0.9}   # the mask"


#: What the last session's edit made of ``fresh``'s one step (New… wrote ``fresh.yaml`` with ``{label: probe}``).
FRESH_KEPT_LINE = "{label: edited}"


def keep_unsaved_work(graph_dir: Path, templates_dir: Path) -> Path:
    """What a stop leaves behind, written by the editor's own code: a Studio over the folder edits ``pets`` (unsaved),
    makes ``fresh`` with New… (which WRITES ``fresh.yaml``) and edits its step, and edits ``clip``, then
    :meth:`Studio.stash` — what ``streamstudio stop`` runs — keeps them. ``clip.yaml`` then changes on disk, as if
    edited elsewhere after the stop."""
    studio = Studio(graph_dir, templates_dir=templates_dir)
    studio.set_param("pets", "stream.ops[1]", "low_level", 0.9)
    studio.new_graph("fresh", "flow")
    assert (graph_dir / "fresh.yaml").read_text() == starter_text("flow", "fresh")
    studio.set_param("fresh", "probe", "label", "edited")
    studio.set_param("clip", "clip", "limit", 2.0)
    assert sorted(Path(kept).name for kept in studio.stash()) == ["clip.yaml", "fresh.yaml", "pets.yaml"]
    (graph_dir / "clip.yaml").write_text(CLIP_TEXT + "# changed on disk after the stop\n")
    return graph_dir / RECOVERY_FILE


def open_asking(browser: Any, server: str, name: str) -> Opened:
    """Open ``?graph=<name>`` on a folder holding a recovery file: the page asks before it loads any graph, so this
    waits for the question, never for the graph."""
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: List[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    page.goto(f"{server}/?graph={name}")
    page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
    return Opened(page, errors)


def picker_labels(page: Any) -> Dict[str, str]:
    """Every option of the graph dropdown, by graph name: ``● pets · graph`` while it holds unsaved changes."""
    options = page.locator("select[data-graph-picker] option").evaluate_all("os => os.map(o => [o.value, o.text])")
    return {value: text for value, text in options}


class TestUnsavedWorkOfTheLastSession:
    """DESIGN2 §1 — the user's item 1: "streamstudio stop or restart should save the file to a tmp workspace file
    (hidden), when opening that same workspace streamstudio should ask to start from the tmp file or the original"."""

    def test_the_page_asks_first_and_restore_brings_the_unsaved_work_back(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path
    ) -> None:
        recovery = keep_unsaved_work(graph_dir, templates_dir)
        opened = open_asking(browser, server, "pets")
        page = opened.page
        try:
            dialog = page.locator('[data-dialog="recovery"]')
            assert dialog.locator(".modal-title").inner_text().startswith("Unsaved work from the last session (")
            rows = page.locator("[data-recovery-graph]").evaluate_all(
                "rs => rs.map(r => [r.dataset.recoveryGraph, r.dataset.recoveryDisk, r.dataset.changedOnDisk,"
                " r.innerText.replace(/\\s+/g, ' ').trim()])"
            )
            # every kept graph is a FILE with changes: New… wrote fresh.yaml, so it is "changed" like the others
            assert sorted(rows) == [
                ["clip", "changed", "true", "clip.yaml changed — the file on disk changed after the editor stopped"],
                ["fresh", "unchanged", "false", "fresh.yaml changed"],
                ["pets", "unchanged", "false", "pets.yaml changed"],
            ]
            assert page.locator(NODE).count() == 0  # nothing is loaded while the question is open
            page.keyboard.press("Escape")  # a choice is required: Escape does not answer it
            assert page.locator('.app[data-recovery="asking"] [data-dialog="recovery"]').count() == 1

            page.locator('[data-recovery-action="restore"]').click()
            page.wait_for_selector('.app[data-recovery="restored"]')
            wait_ready(page, "pets")
            assert PETS_KEPT_LINE in page.locator("[data-yaml]").inner_text()
            assert page.locator(".app").get_attribute("data-dirty") == "true"
            labels = picker_labels(page)
            assert {name for name, label in labels.items() if label.startswith("● ")} == {"clip", "fresh", "pets"}
            assert labels["wires"] == "wires · flow"
            # nothing reached the disk: the files are as the stop left them, and the recovery file is gone
            assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
            assert (graph_dir / "fresh.yaml").read_text() == starter_text(
                "flow", "fresh"
            )  # New wrote it; the edit is held
            assert not recovery.exists()
            assert PETS_KEPT_LINE in api_get(server, "api/graphs/pets")["text"]
            assert FRESH_KEPT_LINE in api_get(server, "api/graphs/fresh")["text"]
            assert listed_dirty(server) == {
                "clip": True,
                "fresh": True,
                "mismatch": False,
                "pets": True,
                "wires": False,
            }
            assert api_get(server, "api/workspace")["recovery"] is None
            assert opened.errors == []
        finally:
            page.close()

    def test_open_the_saved_files_asks_once_more_then_drops_the_kept_work(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path
    ) -> None:
        recovery = keep_unsaved_work(graph_dir, templates_dir)
        opened = open_asking(browser, server, "pets")
        page = opened.page
        try:
            page.locator('[data-recovery-action="discard"]').click()
            ask = page.locator('[data-dialog="ask"]')
            ask.wait_for()
            assert "Discard the unsaved work of the last session?" in ask.inner_text()
            assert "This cannot be undone." in ask.inner_text()
            page.locator("[data-ask-cancel]").click()  # Cancel goes back to the question, the file untouched
            page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
            assert recovery.is_file()

            page.locator('[data-recovery-action="discard"]').click()
            page.locator("[data-ask-confirm]").click()
            page.wait_for_selector('.app[data-recovery="discarded"]')
            wait_ready(page, "pets")
            assert page.locator("[data-yaml]").inner_text().rstrip("\n") == PETS_TEXT.rstrip("\n")
            assert page.locator(".app").get_attribute("data-dirty") == "false"
            labels = picker_labels(page)
            assert not any(label.startswith("● ") for label in labels.values())
            # New… wrote fresh.yaml: dropping the kept work drops its edit, never the file
            assert labels["fresh"] == "fresh · flow"
            assert (graph_dir / "fresh.yaml").read_text() == starter_text("flow", "fresh")
            assert FRESH_KEPT_LINE not in api_get(server, "api/graphs/fresh")["text"]
            assert not recovery.exists()
            assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
            assert opened.errors == []
        finally:
            page.close()


#: The one class these tests add to an op list: a pass-through op, so every chain around it still checks.
PROBE = "recordstream.ops.debug.PrintRecordOp"


class TestOpListsAreEditable:
    """DESIGN2 §3 — the user's item 3 ("yes"): on a Stream the Nodes sidebar adds MEMBERS to its ``ops`` list and
    Delete removes them. Before: both were refused with ``'stream.ops[1]' is not a flow step (the steps are: [])``."""

    def test_add_after_a_member_delete_two_in_one_undo_and_append_at_the_end(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        demo = DEMO_TEXT.format(png=graph_dir / "demo.png")
        (graph_dir / "demo.yaml").write_text(demo)
        # the positions the editor laid out, HELD (as after a drag): the later members must keep theirs when renumbered
        before = api_get(server, "api/graphs/demo")["positions"]
        api_put(server, "api/graphs/demo/positions", {"positions": before})
        opened = open_graph("demo")
        page = opened.page
        sent: List[str] = []
        page.on(
            "request",
            lambda request: (
                sent.append(f"{request.method} {request.url.split('/', 3)[3]}") if "/steps" in request.url else None
            ),
        )
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(PROBE)
        entry = page.locator(f'[data-nodes-entry][data-class="{PROBE}"]')

        # 1. stream.ops[1] selected: a click inserts the member right after it
        page.locator(f'{node_root("stream.ops[1]")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="stream.ops[1]"]')
        assert page.locator("[data-nodes-target]").get_attribute("data-nodes-target") == "after"
        assert "Click adds after stream.ops[1]" in page.locator("[data-nodes-sidebar] .nodes-foot").inner_text()
        entry.click()
        wait_saves(page, 1)
        added = api_get(server, "api/graphs/demo")
        convert = "    - !class:recordstream.ops.image.ConvertMode {mode: L}\n"
        assert convert + f"    - !class:{PROBE} {{}}\n" in added["text"]
        assert added["text"] == demo.replace(convert, convert + f"    - !class:{PROBE} {{}}\n")
        assert sent == ["POST api/graphs/demo/steps"]
        page.wait_for_selector(f'{node_root("stream.ops[2]")}[data-class="{PROBE}"]')
        # the members after it moved down one name and kept their places; the new one is not drawn on top of them
        positions = added["positions"]
        for index in (0, 1):
            assert positions[f"stream.ops[{index}]"] == before[f"stream.ops[{index}]"]
        for index in (2, 3, 4):
            assert positions[f"stream.ops[{index + 1}]"] == before[f"stream.ops[{index}]"], index
        assert positions["stream.ops[2]"] not in [before[f"stream.ops[{index}]"] for index in range(5)]
        assert node_translate(page, "stream.ops[3]") == tuple(before["stream.ops[2]"])
        assert (graph_dir / "demo.yaml").read_text() == demo  # held by the editor until Save

        # 2. two members selected, Delete: ONE request, and one undo brings both back
        page.locator(f'{node_root("stream.ops[2]")} .title .cls').click()
        page.locator(f'{node_root("stream.ops[3]")} .title .cls').click(modifiers=["Shift"])
        page.wait_for_selector('.app[data-selection="stream.ops[2],stream.ops[3]"]')
        sent.clear()
        page.keyboard.press("Delete")
        wait_saves(page, 2)
        removed = api_get(server, "api/graphs/demo")
        scale = "    - !class:recordstream.ops.numpy.Scale"
        assert PROBE not in removed["text"] and scale not in removed["text"]
        assert [node["name"] for node in removed["model"]["nodes"] if node["name"].startswith("stream.ops[")] == [
            f"stream.ops[{index}]" for index in range(4)
        ]
        assert sent == ["POST api/graphs/demo/steps/remove"]
        page.wait_for_selector('.app[data-selection=""]')  # members are named by position: nothing stays selected
        page.keyboard.press("Control+z")
        wait_saves(page, 3)
        assert api_get(server, "api/graphs/demo")["text"] == added["text"]
        page.wait_for_selector(f'{node_root("stream.ops[2]")}[data-class="{PROBE}"]')

        # 3. nothing selected: a click appends the member at the end of the list
        page.wait_for_selector('.app[data-selection=""]')
        assert page.locator("[data-nodes-target]").get_attribute("data-nodes-target") == "end"
        assert "Click adds at the end of stream.ops" in page.locator("[data-nodes-sidebar] .nodes-foot").inner_text()
        entry.click()
        wait_saves(page, 4)
        appended = api_get(server, "api/graphs/demo")["text"]
        assert appended == added["text"] + f"    - !class:{PROBE} {{}}\n"
        page.wait_for_selector(f'{node_root("stream.ops[6]")}[data-class="{PROBE}"]')
        assert (graph_dir / "demo.yaml").read_text() == demo
        assert opened.errors == []


class TestShiftClickAdds:
    """DESIGN2 §4 — the user's item 4 ("match", ComfyUI's "Ctrl/Shift + Click — Add clicked node to selection").
    Before (a Mac): click ``mask``, Shift+click ``grey`` → the selection stayed ``mask``."""

    def test_shift_click_adds_a_card_and_shift_drag_on_the_pane_still_draws_a_box(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        page.locator(f'{node_root("mask")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="mask"]')
        page.locator(f'{node_root("grey")} .title .cls').click(modifiers=["Shift"])
        page.wait_for_selector('.app[data-selection="grey,mask"]')
        assert selected_nodes(page) == ["grey", "mask"]
        # Shift+click never takes a card OUT: on a selected card it leaves the selection as it is
        page.locator(f'{node_root("mask")} .title .cls').click(modifiers=["Shift"])
        assert page.locator(".app").get_attribute("data-selection") == "grey,mask"
        assert selected_nodes(page) == ["grey", "mask"]
        # Shift + drag on EMPTY canvas is still React Flow's box
        page.mouse.click(*EMPTY)
        page.wait_for_selector('.app[data-selection=""]')
        box_select(page, "scaled", "mask")
        assert selected_nodes(page) == ["mask", "scaled"]
        assert sorted((page.locator(".app").get_attribute("data-selection") or "").split(",")) == ["mask", "scaled"]
        assert page.locator(".app").get_attribute("data-saves") == "0"  # a selection writes nothing
        assert (graph_dir / "chain.yaml").read_text() == CHAIN_TEXT
        assert opened.errors == []


# ================================================================================================
# round 2, the review's findings (R1 … R12, P1 … P10): each test names the finding it pins
# ================================================================================================

#: Chrome's console line for a request refused on purpose by the session guard (the editor restarted).
REFUSED_409 = "console.error: Failed to load resource: the server responded with a status of 409 (CONFLICT)"
#: … and for a recovery answer another tab already gave.
REFUSED_404 = "console.error: Failed to load resource: the server responded with a status of 404 (NOT FOUND)"


@pytest.fixture
def served(graph_dir: Path, templates_dir: Path) -> Iterator[Tuple[str, Any]]:
    """The ``server`` fixture, with the app itself — a test restarts the editor IN PLACE: what ``streamstudio restart``
    does as seen from a page (the same address, a new editor with a new session, the unsaved work kept on the way
    out)."""
    app = create_app(graph_dir, templates_dir=templates_dir)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, name="streamstudio-canvas-restart", daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", app
    httpd.shutdown()
    thread.join(timeout=10)


def restart_in_place(app: Any, graph_dir: Path, templates_dir: Path) -> None:
    """Stop the editor the way a stop does (:meth:`Studio.stash`), then serve a NEW one on the same address."""
    app.extensions[EXTENSION].stash()
    app.extensions[EXTENSION] = Studio(graph_dir, templates_dir=templates_dir)


def open_page(browser: Any, url: str, name: str) -> Opened:
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: List[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    page.goto(f"{url}/?graph={name}")
    return Opened(page, errors)


class TestATabLeftOpenAcrossARestart:
    """R1 / P1 (high): the user's own case — ``streamstudio restart`` while the editor tab stays open. Measured before:
    the tab edited the NEW editor's document (read from disk), so the kept ``0.9`` vanished on the next change, Save
    wrote the file without it and said clean, and the next stop replaced the kept version."""

    def test_the_next_edit_changes_nothing_and_the_tab_asks_the_recovery_question(
        self, browser: Any, served: Tuple[str, Any], graph_dir: Path, templates_dir: Path
    ) -> None:
        url, app = served
        opened = open_page(browser, url, "pets")
        page = opened.page
        try:
            wait_ready(page, "pets")
            edit_low_level(page, "0.9")
            wait_saves(page, 1)
            restart_in_place(app, graph_dir, templates_dir)
            widget = page.locator(f'{node_root("stream.ops[0]")} input[data-param="target_max"]')
            widget.fill("3.0")
            widget.press("Enter")
            page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
            rows = page.locator("[data-recovery-graph]").evaluate_all("rs => rs.map(r => r.dataset.recoveryGraph)")
            assert rows == ["pets"]
            # nothing reached the new editor, nor the disk
            assert listed_dirty(url)["pets"] is False
            assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
            page.locator('[data-recovery-action="restore"]').click()
            page.wait_for_selector('.app[data-recovery="restored"]')
            wait_ready(page, "pets")
            shown = page.locator("[data-yaml]").inner_text()
            assert PETS_KEPT_LINE in shown and "target_max: 3.0" not in shown
            assert page.locator(".app").get_attribute("data-dirty") == "true"
            assert opened.errors == [REFUSED_409]
        finally:
            page.close()

    def test_a_save_changes_nothing_and_the_kept_work_survives_the_next_stop(
        self, browser: Any, served: Tuple[str, Any], graph_dir: Path, templates_dir: Path
    ) -> None:
        url, app = served
        opened = open_page(browser, url, "pets")
        page = opened.page
        try:
            wait_ready(page, "pets")
            edit_low_level(page, "0.9")
            wait_saves(page, 1)
            restart_in_place(app, graph_dir, templates_dir)
            page.keyboard.press("Control+s")
            page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
            assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT  # Save did not write the disk version
            app.extensions[EXTENSION].stash()  # the next stop, nobody having answered
            kept = json.loads((graph_dir / RECOVERY_FILE).read_text())
            assert [entry["name"] for entry in kept["graphs"]] == ["pets"]
            assert PETS_KEPT_LINE in next(iter(kept["graphs"][0]["texts"].values()))
            assert opened.errors == [REFUSED_409]
        finally:
            page.close()


class TestTheQuestionAnsweredInAnotherTab:
    """R12 / P4 (b): measured before, a second tab still showing the question was stuck once the first had answered —
    Restore and Open the saved files both refused (``no unsaved work``), and Escape did nothing."""

    @pytest.mark.parametrize("second", ["restore", "discard"])
    def test_the_second_tab_drops_the_question_and_loads_the_graphs(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path, second: str
    ) -> None:
        keep_unsaved_work(graph_dir, templates_dir)
        first, other = open_asking(browser, server, "pets"), open_asking(browser, server, "pets")
        try:
            first.page.locator('[data-recovery-action="restore"]').click()
            wait_ready(first.page, "pets")
            other.page.locator(f'[data-recovery-action="{second}"]').click()
            if second == "discard":
                other.page.locator("[data-ask-confirm]").click()
            other.page.wait_for_selector('.app[data-recovery="none"]')
            wait_ready(other.page, "pets")
            assert PETS_KEPT_LINE in other.page.locator("[data-yaml]").inner_text()  # what the editor holds
            assert "already answered" in other.page.locator('[data-toast="info"]').last.inner_text()
            assert other.errors == [REFUSED_404]
        finally:
            first.page.close()
            other.page.close()


class TestTheQuestionKeepsTheKeyboard:
    """P5: measured before, Tab reached the menu bar and the graph picker behind the question (a graph was made, or
    loaded from disk, before it was answered); P10: the irreversible discard had the focus on Discard."""

    def test_tab_stays_in_the_dialog_and_the_page_behind_it_is_inert(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path
    ) -> None:
        keep_unsaved_work(graph_dir, templates_dir)
        opened = open_asking(browser, server, "pets")
        page = opened.page
        try:
            in_dialog = "() => document.activeElement?.closest('[data-dialog]')?.dataset.dialog ?? null"
            for _ in range(6):
                page.keyboard.press("Tab")
                assert page.evaluate(in_dialog) == "recovery"
            page.keyboard.press("Shift+Tab")
            assert page.evaluate(in_dialog) == "recovery"
            assert page.locator("header.bar").get_attribute("inert") is not None
            assert page.locator("main.body").get_attribute("inert") is not None
            assert page.locator(".app").get_attribute("data-graph") == ""
            page.locator('[data-recovery-action="restore"]').click()
            wait_ready(page, "pets")
            assert page.locator("header.bar").get_attribute("inert") is None
            assert opened.errors == []
        finally:
            page.close()

    def test_the_discard_question_names_the_files_and_keeps_the_focus_on_cancel(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path
    ) -> None:
        recovery = keep_unsaved_work(graph_dir, templates_dir)
        opened = open_asking(browser, server, "pets")
        page = opened.page
        try:
            page.locator('[data-recovery-action="discard"]').click()
            ask = page.locator('[data-dialog="ask"]')
            ask.wait_for()
            assert "The unsaved changes of pets.yaml, fresh.yaml and clip.yaml are lost." in ask.inner_text()
            assert page.evaluate("() => document.activeElement?.hasAttribute('data-ask-cancel')") is True
            page.keyboard.press("Enter")  # a stray Enter cancels: the kept work is still there
            page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
            assert recovery.is_file()
            assert opened.errors == []
        finally:
            page.close()

    def test_an_unreadable_recovery_file_is_said_to_be_moved_aside_never_lost(
        self, browser: Any, server: str, graph_dir: Path
    ) -> None:
        """R9: measured before, the question said "This cannot be undone." of a file that is only moved aside."""
        (graph_dir / RECOVERY_FILE).write_text("{not json")
        opened = open_asking(browser, server, "pets")
        page = opened.page
        try:
            aside = graph_dir / ".streamstudio-workspace.broken.json"
            assert f"moves that file aside to {aside}" in page.locator("[data-recovery-error]").inner_text()
            page.locator('[data-recovery-action="discard"]').click()
            ask = page.locator('[data-dialog="ask"]')
            assert "Set the unreadable unsaved work aside?" in ask.inner_text()
            assert "cannot be undone" not in ask.inner_text()
            page.locator("[data-ask-confirm]").click()
            wait_ready(page, "pets")
            assert aside.read_text() == "{not json"
            assert opened.errors == []
        finally:
            page.close()


class TestTheRowsSayWhatTheDiskHolds:
    """R10, R11: a kept graph whose file was deleted says so; a file of another folder is named by its way there."""

    def test_a_deleted_file_and_a_file_of_another_folder(
        self, browser: Any, server: str, graph_dir: Path, templates_dir: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        other = tmp_path_factory.mktemp("other") / "pets.yaml"
        other.write_text(PETS_TEXT)
        studio = Studio(graph_dir, templates_dir=templates_dir)
        studio.set_param("pets", "stream.ops[1]", "low_level", 0.9)
        studio.open_graph(str(other))
        studio.set_param("pets~2", "stream.ops[1]", "low_level", 0.8)
        studio.stash()
        (graph_dir / "pets.yaml").unlink()
        opened = open_asking(browser, server, "wires")
        page = opened.page
        try:
            rows = page.locator("[data-recovery-graph]").evaluate_all(
                "rs => rs.map(r => [r.dataset.recoveryDisk, r.querySelector('.recovery-file').innerText])"
            )
            assert rows[0] == ["gone", "pets.yaml"]
            assert rows[1] == ["unchanged", f"../{other.parent.name}/pets.yaml"]
            note = page.locator('[data-recovery-graph="pets"] .recovery-note').inner_text()
            assert note == "changed — the file was deleted or moved away after the editor stopped; Save writes it again"
            assert opened.errors == []
        finally:
            page.close()


PIPED_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource
    files: [{png}]
  ops:
    - !class:recordstream.ops.image.ReadImage {{}}
    - !class:recordstream.transform.Pipeline
      transforms:
        - !class:recordstream.ops.image.ConvertMode {{mode: L}}
        - !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    - !class:recordstream.ops.debug.PrintRecordOp {{}}
"""


class TestAddingIntoAPipelinesTransforms:
    """P3: measured before, no page gesture added into a Pipeline's ``transforms`` — the Pipeline selected, a click
    added after it in ``stream.ops`` although the sidebar footer said to select the node holding the list. P8: a member
    added after the first transform was drawn on top of the one that moved down."""

    def test_the_pipeline_selected_a_click_appends_to_its_transforms(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "piped.yaml").write_text(PIPED_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("piped")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(PROBE)
        page.locator(f'{node_root("stream.ops[1]")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="stream.ops[1]"]')
        foot = page.locator("[data-nodes-sidebar] .nodes-foot").inner_text()
        assert "Click adds at the end of stream.ops[1].transforms" in foot
        page.locator(f'[data-nodes-entry][data-class="{PROBE}"]').click()
        wait_saves(page, 1)
        assert (
            "        - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n"
            f"        - !class:{PROBE} {{}}\n"
            f"    - !class:{PROBE} {{}}\n"
        ) in api_get(server, "api/graphs/piped")["text"]
        assert opened.errors == []

    def test_a_member_added_after_the_first_transform_covers_no_card(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        """P8, on the graph fresh from disk (nothing dragged, so the server lays the later members out again)."""
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "piped.yaml").write_text(PIPED_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("piped")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(PROBE)
        page.locator(f'{node_root("stream.ops[1].transforms[0]")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="stream.ops[1].transforms[0]"]')
        page.locator(f'[data-nodes-entry][data-class="{PROBE}"]').click()
        wait_saves(page, 1)
        page.wait_for_selector(f'{node_root("stream.ops[1].transforms[1]")}[data-class="{PROBE}"]')
        names = [f"stream.ops[1].transforms[{index}]" for index in range(3)] + [
            f"stream.ops[{index}]" for index in range(3)
        ]
        page.wait_for_function(
            'names => names.every(n => document.querySelector(`.react-flow__node[data-id="${n}"]`))', arg=names
        )
        boxes = node_boxes(page, *names)  # zoom 1: a screen box is the card's flow box
        overlapping = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :] if rects_overlap(boxes[a], boxes[b])]
        assert overlapping == []
        assert opened.errors == []

    def test_a_wire_dragged_out_of_the_empty_transforms_input_appends_to_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        empty = PIPED_TEXT.format(png=graph_dir / "demo.png").replace(
            "      transforms:\n"
            "        - !class:recordstream.ops.image.ConvertMode {mode: L}\n"
            "        - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}\n",
            "      transforms: []\n",
        )
        (graph_dir / "piped.yaml").write_text(empty)
        opened = open_graph("piped")
        page = opened.page
        start = handle(page, "stream.ops[1]", "transforms[0]", "in").bounding_box()
        drag(page, centre(start), EMPTY)
        page.wait_for_selector("[data-palette]")
        wait_palette_loaded(page)
        assert "added at the end of stream.ops[1].transforms" in page.locator("[data-palette-meta]").inner_text()
        page.locator("[data-palette-search]").fill(PROBE)
        page.locator(f'[data-palette-list] [data-class="{PROBE}"]').first.click()
        wait_saves(page, 1)
        assert f"      transforms:\n        - !class:{PROBE} {{}}\n" in api_get(server, "api/graphs/piped")["text"]
        assert opened.errors == []


TWOSUB_TEXT = """\
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:recordstream.flow.subgraph.Subgraph
      steps:
        read:
          op: !class:recordstream.ops.image.ReadImage {}
    - !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {mode: L}
        mask:
          op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
"""


class TestAnUndoInsideASubgraphMember:
    """P7: measured before, Ctrl+Z inside ``stream.ops[0]`` put the removed member back before it and the canvas kept
    the NAME — it showed the OTHER subgraph under the same breadcrumb."""

    def test_the_canvas_follows_the_member_to_its_new_name(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "twosub.yaml").write_text(TWOSUB_TEXT)
        opened = open_graph("twosub")
        page = opened.page
        page.locator(f'{node_root("stream.ops[0]")} .title .cls').click()
        page.keyboard.press("Delete")
        wait_saves(page, 1)
        page.wait_for_selector(f'{node_root("stream.ops[0]")}[data-kind="subgraph"]')
        page.locator(f'{node_root("stream.ops[0]")} .title .cls').dblclick()
        page.wait_for_selector('.app[data-level="stream.ops[0]"]')
        page.wait_for_selector(node_root("stream.ops[0]/grey"))
        page.keyboard.press("Control+z")
        wait_saves(page, 2)
        page.wait_for_selector('.app[data-level="stream.ops[1]"]')
        page.wait_for_selector(node_root("stream.ops[1]/grey"))
        assert page.locator(node_root("stream.ops[0]/read")).count() == 0
        assert opened.errors == []


class TestShiftHeldWhileDragging:
    """P9: measured before, with Shift still held after Shift+clicking cards, dragging a selected card did nothing."""

    def test_a_drag_with_shift_held_moves_the_whole_selection(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "chain.yaml").write_text(CHAIN_TEXT)
        opened = open_graph("chain")
        page = opened.page
        before = {name: node_translate(page, name) for name in ("grey", "scaled", "mask", "parts")}
        page.locator(f'{node_root("mask")} .title .cls').click()
        page.keyboard.down("Shift")
        page.locator(f'{node_root("grey")} .title .cls').click()
        page.locator(f'{node_root("scaled")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="grey,scaled,mask"]')
        start = centre(page.locator(f'{node_root("mask")} .title .cls').bounding_box())
        drag(page, start, (start[0] - DRAG[0], start[1] + DRAG[1]))
        page.keyboard.up("Shift")
        wait_saves(page, 1)
        after = {name: node_translate(page, name) for name in before}
        moved = {name: (after[name][0] - before[name][0], after[name][1] - before[name][1]) for name in before}
        assert moved == {
            "grey": (-DRAG[0], DRAG[1]),
            "scaled": (-DRAG[0], DRAG[1]),
            "mask": (-DRAG[0], DRAG[1]),
            "parts": (0, 0),
        }
        assert selected_nodes(page) == ["grey", "mask", "scaled"]
        assert opened.errors == []


class TestAnUndoDrawsTheMembersWhereTheEditorHoldsThem:
    """Found while re-running the review's page scripts: an undo that renamed members (an add undone) left a card drawn
    at the place of the member that had held its name, and the next plain click wrote that stale place back — a
    Threshold card moved onto the undone member's spot. Every member is drawn where the answer puts it."""

    def test_after_an_add_is_undone_every_card_is_at_its_answered_place(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        write_demo_png(graph_dir / "demo.png")
        (graph_dir / "demo.yaml").write_text(DEMO_TEXT.format(png=graph_dir / "demo.png"))
        opened = open_graph("demo")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        wait_palette_loaded(page)
        page.locator("[data-nodes-search]").fill(PROBE)
        page.locator(f'{node_root("stream.ops[1]")} .title .cls').click()
        page.wait_for_selector('.app[data-selection="stream.ops[1]"]')
        page.locator(f'[data-nodes-entry][data-class="{PROBE}"]').click()
        wait_saves(page, 1)
        page.mouse.click(*EMPTY)
        page.keyboard.press("Control+z")
        wait_saves(page, 2)
        answered = api_get(server, "api/graphs/demo")["positions"]
        members = [f"stream.ops[{index}]" for index in range(5)]
        page.wait_for_function(
            """places => Object.entries(places).every(([n, [x, y]]) => {
                const style = document.querySelector(`.react-flow__node[data-id="${n}"]`)?.getAttribute("style") ?? "";
                return style.includes(`translate(${x}px, ${y}px)`);
            })""",
            arg={name: answered[name] for name in members},
        )
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# the editor MOUNTED in a host app (DESIGN5 step 1): both blueprints under /studio, a host that names the folder,
# supplies the record a pane graph runs on and hears of every Save — what a consuming workspace builds
# ------------------------------------------------------------------------------------------------

#: Where the host mounts the editor — the API and the page side by side.
STUDIO = "/studio"


@configurable(category="op", group="canvas-test")
class PaneWindow:
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


@configurable(category="op", group="canvas-test")
class PaneBand:
    """Keep the record and write the band the host filled in — a stand-in for a tuner with a ``(min, max)`` gate.

    Args:
        f0_hz: The band's lower edge.
        f1_hz: The band's upper edge.
        deviation_hz: The ``(min, max)`` deviation a burst may have.
    """

    consumes = {"image": "Image"}
    produces = {"band": "*"}

    def __init__(
        self, f0_hz: float = 0.0, f1_hz: float = 0.0, deviation_hz: Tuple[float, float] = (1.0e5, 7.5e5)
    ) -> None:
        self.f0_hz = float(f0_hz)
        self.f1_hz = float(f1_hz)
        self.deviation_hz = (float(deviation_hz[0]), float(deviation_hz[1]))

    def __call__(self, record: Record) -> Record:
        return {**record, "band": [self.f0_hz, self.f1_hz]}


@configurable(category="op", group="canvas-test")
class TinyFileName:
    """Name the file a record carries — what a files source's record holds (``{"file": "<path>"}``).

    Args:
        suffix: Appended to the name.
        names_file: A file of names — a ``…file`` string, so its field gets the file chooser.
    """

    def __init__(self, suffix: str = "", names_file: str = "") -> None:
        self.suffix = suffix
        self.names_file = names_file

    def __call__(self, record: Record) -> Record:
        return {**record, "name": Path(str(record["file"])).name + self.suffix}


#: A pane graph as a host serves it: a flow with no source — the host supplies the record, the window and the band —
#: and an ``app:`` block naming the three settings a person tunes from the host (a ``__lo``/``__hi`` pair is ONE
#: field; ``TinyGain`` is the class of two steps, so its entry is a field on each).
@configurable(category="op", group="canvas-test")
class TallHead:
    """A first step with ten parameters: as tall a card as a domain pane's head (one input, ten parameters, three
    outputs — 373 px, measured 2026-10-01 on a real one).

    Args:
        p0: A parameter.
        p1: A parameter.
        p2: A parameter.
        p3: A parameter.
        p4: A parameter.
        p5: A parameter.
        p6: A parameter.
        p7: A parameter.
        p8: A parameter.
        p9: A parameter.
    """

    produces = {"image": "Image"}

    def __init__(
        self,
        p0: float = 0.0,
        p1: float = 0.0,
        p2: float = 0.0,
        p3: float = 0.0,
        p4: float = 0.0,
        p5: float = 0.0,
        p6: float = 0.0,
        p7: float = 0.0,
        p8: float = 0.0,
        p9: float = 0.0,
    ) -> None:
        self.p0, self.p1, self.p2, self.p3, self.p4 = p0, p1, p2, p3, p4
        self.p5, self.p6, self.p7, self.p8, self.p9 = p5, p6, p7, p8, p9

    def __call__(self, record: Record) -> Record:
        return {**record, "image": Image(np.zeros((2, 2), dtype=np.float32))}


@configurable(category="op", group="canvas-test")
class PaneDecide:
    """Raise ``wide`` or ``narrow`` — a stand-in for a classifier whose outcomes gate the steps after it.

    Args:
        level: The width that splits the two.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}
    flags = ("wide", "narrow")

    def __init__(self, level: float = 0.5) -> None:
        self.level = float(level)

    @property
    def wide(self) -> bool:
        return True

    @property
    def narrow(self) -> bool:
        return False

    def __call__(self, record: Record) -> Record:
        return record


@configurable(category="op", group="canvas-test")
class PaneGated:
    """Keep the record — a step that runs only on the flag a decision before it raised.

    Args:
        level: A threshold.
        requires: The flag this step runs on (the gate a branch sets).
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, level: float = 0.5, requires: str = "") -> None:
        self.level = float(level)
        self.requires = requires

    def __call__(self, record: Record) -> Record:
        return record


HOST_PANE_TEXT = f"""\
# a pane: the host supplies the record, the window and the band
flow:
  window:
    op: !class:{MOD}.PaneWindow {{}}
  band:
    op: !class:{MOD}.PaneBand {{}}
    from: window
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}   # the knob
    from: band
  boost:
    op: !class:{MOD}.TinyGain {{gain: 3.0}}
    from: gain
outputs: boost

app:
  inputs:
    - [PaneBand, deviation_hz__lo]
    - [PaneBand, deviation_hz__hi]
    - [TinyGain, gain]
"""

HOST_PANE_STEPS = ["window", "band", "gain", "boost"]


def _pane_shaped_text() -> str:
    """A 29-step chain shaped like a domain's pane: the tall ``head``, 28 ``TinyGain`` steps in a line, and a contract
    root whose two inputs no step reads by name (so neither the root nor its host card carries a wire)."""
    steps = [f"  head:\n    op: !class:{MOD}.TallHead {{}}\n"]
    previous = "head"
    for index in range(1, 29):
        steps.append(f"  step{index}:\n    op: !class:{MOD}.TinyGain {{}}\n    from: {previous}\n")
        previous = f"step{index}"
    return (
        "flow:\n"
        + "".join(steps)
        + f"outputs: {previous}\n"
        + "contract: !class:recordstream.ops.contract.GraphContract\n"
        + "  name: pane-shaped view\n  inputs: {capture: Image, boxes: Boxes}\n  outputs: {}\n"
    )


PANE_SHAPED_TEXT = _pane_shaped_text()

#: A model graph shaped like a domain's detection model: a ``ConfigureOp`` step whose two ops and target are cards of
#: their own, and a contract root.
MODEL_SHAPED_TEXT = f"""\
flow:
  decode:
    op: !class:{MOD}.TinyDecode {{}}
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
    from: decode
  threshold:
    op: !class:recordstream.ops.configure.ConfigureOp
      ops:
        - !class:recordstream.ops.formula.FormulaOp {{field: image, formula: "amax(a)"}}
        - !class:recordstream.ops.formula.FormulaOp {{field: image, formula: "a * 0.5"}}
      source: image
      param: low_level
      target: !class:recordstream.ops.numpy.Threshold
        field: image
    from: gain
  components:
    op: !class:recordstream.ops.numpy.ConnectedComponents
      min_area_bins: 4
      output: bin_boxes
    from: threshold
  narrowed:
    op: !class:recordstream.ops.structure.SelectFields
      keys: [image, bin_boxes]
    from: components
outputs: narrowed
contract: !class:recordstream.ops.contract.GraphContract
  name: model-shaped graph
  inputs: {{image: Image}}
  outputs: {{}}
"""

#: A pane whose ``app:`` block names a ``(min, max)`` range as a ``__lo``/``__hi`` pair and one plain parameter, and
#: whose last step runs on a decision's flag (``requires``).
GATED_PANE_TEXT = f"""\
# a pane: settings and a gate
flow:
  window:
    op: !class:{MOD}.PaneWindow {{}}
  band:
    op: !class:{MOD}.PaneBand {{}}
    from: window
  decide:
    op: !class:{MOD}.PaneDecide {{}}
    from: band
  wide:
    op: !class:{MOD}.PaneGated
      level: 0.25
      requires: wide
    from: decide
outputs: wide

app:
  inputs:
    - [PaneBand, deviation_hz__lo]
    - [PaneBand, deviation_hz__hi]
    - [PaneGated, level]
"""

#: A chain whose records come from a source still to be chosen (Set source…).
HOST_SOURCE_TEXT = f"""\
# the files chain: its source is chosen in the editor
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{MOD}.TinyFileName {{}}
"""

HOST_WINDOW = {"start_s": 0.25, "stop_s": 0.75}
HOST_BAND = {"f0_hz": 1.0e6, "f1_hz": 2.0e6}
HOST_LABEL = "rec_7 · 0.25–0.75 s · 1–2 MHz"

#: The host's own page: the editor in a PANEL (an iframe), and every message the editor posts kept in order.
HOST_PAGE = f"""<!doctype html>
<title>the host</title>
<h1>the host's own page</h1>
<script>
  window.received = [];
  window.addEventListener("message", (event) => {{
    const said = event.data;
    if (event.origin === location.origin && said && said.source === "streamstudio") window.received.push(said);
  }});
</script>
<iframe id="studio" src="{STUDIO}/?graph=pane&embed=1" style="width: 1300px; height: 800px"></iframe>
"""


class BrowserHost:
    """The smallest host: a name, a folder it may move, one selected record, and what the editor told it."""

    name = "the test host"

    def __init__(self, folder: Path) -> None:
        self.where = folder
        self.asked: List[Tuple[str, Path]] = []
        self.saves: List[List[Path]] = []

    def folder(self) -> Path:
        return self.where

    def record(self, graph: str, path: Path) -> HostRecord:
        self.asked.append((graph, path))
        image = Image(np.full((2, 2), 0.5, dtype=np.float32))
        return HostRecord(record={"image": image}, fills=(HOST_WINDOW, HOST_BAND), label=HOST_LABEL)

    def saved(self, paths: Sequence[Path]) -> None:
        self.saves.append([Path(path) for path in paths])

    def shipped(self) -> Mapping[str, Path]:
        return {}


class Hosted(NamedTuple):
    """A host app serving the editor under :data:`STUDIO`, on a free port in a thread."""

    url: str
    host: BrowserHost
    app: Any  # flask.Flask
    folder: Path


def host_app(host: BrowserHost, templates_dir: Path) -> Any:  # flask.Flask
    """A host app as a consuming workspace builds it: its own page and its OWN ``/api/workspace``, and the editor's API
    and page (the checkout's build: ``page_blueprint()``) side by side under ``/studio``."""
    app = Flask("host")

    @app.get("/")
    def home() -> str:
        return HOST_PAGE

    @app.get("/api/workspace")
    def workspace() -> Dict[str, str]:
        return {"owner": "the host"}

    app.register_blueprint(editor_blueprint(host=host, templates_dir=templates_dir), url_prefix=STUDIO)
    app.register_blueprint(page_blueprint(), url_prefix=STUDIO)
    return app


@pytest.fixture
def hosted(tmp_path: Path, templates_dir: Path) -> Iterator[Hosted]:
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "pane.yaml").write_text(HOST_PANE_TEXT)
    (folder / "source.yaml").write_text(HOST_SOURCE_TEXT)
    captures = folder / "captures"
    captures.mkdir()
    for name in ("a.txt", "b.txt"):
        (captures / name).write_text(name)
    host = BrowserHost(folder)
    app = host_app(host, templates_dir)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, name="streamstudio-canvas-host", daemon=True)
    thread.start()
    yield Hosted(f"http://127.0.0.1:{httpd.server_port}", host, app, folder)
    httpd.shutdown()
    thread.join(timeout=10)


def open_hosted(browser: Any, hosted: Hosted, query: str) -> Opened:
    """Open ``/studio/?<query>`` in the host app — the page under the host's prefix."""
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: List[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    page.goto(f"{hosted.url}{STUDIO}/?{query}")
    return Opened(page, errors)


def hosted_api(hosted: Hosted, path: str) -> Any:  # the route's JSON under the host's prefix
    return api_get(f"{hosted.url}{STUDIO}", path)


def set_widget(page: Any, scope: str, param: str, value: str) -> None:
    """Type a value into the text/number widget ``param`` inside ``scope`` and commit it (Enter blurs)."""
    widget = page.locator(f'{scope} input[data-param="{param}"]')
    widget.fill(value)
    widget.press("Enter")


class TestMountedInAHost:
    """The page under a host's prefix (``/studio/``), the host's record, the settings a link opens, an unknown graph."""

    def test_the_page_under_the_prefix_asks_the_editor_there_and_fits_a_panel(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        page = opened.page
        requests: List[str] = []
        page.on("request", lambda request: requests.append(request.url))
        wait_ready(page, "pane")
        root = page.locator(".app")
        assert (root.get_attribute("data-embed"), root.get_attribute("data-host")) == ("true", "the test host")
        assert page.locator(".bar .brand").count() == 0  # the host's page says what app this is
        assert page.locator(".react-flow__minimap").count() == 0  # a panel's small canvas: off until asked
        assert page.locator('[data-menu="file"]').count() == 1  # the menus and every function stay
        drawn = page.locator(NODE).evaluate_all("ns => ns.map(n => n.dataset.node)")
        assert sorted(drawn) == sorted(node["name"] for node in hosted_api(hosted, "api/graphs/pane")["model"]["nodes"])
        assert set(HOST_PANE_STEPS) < set(drawn)  # the four steps, and the record entering the flow
        page.locator('[data-tab="text"]').click()
        assert "# a pane: the host supplies the record" in page.locator("[data-yaml]").inner_text()
        page.reload()
        wait_ready(page, "pane")
        asked = [url for url in requests if "/api/" in url]
        assert asked and all(url.startswith(f"{hosted.url}{STUDIO}/api/") for url in asked), asked
        assert opened.errors == []

    def test_run_on_a_pane_runs_the_hosts_record_with_its_window_and_band_and_names_it(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        set_widget(page, node_root("gain"), "gain", "5")  # held, NOT saved: the value a Run uses
        wait_saves(page, 1)
        open_run_tab(page)
        assert page.locator(".run-controls").get_attribute("data-run-from") == "host"
        assert page.locator("input[data-run-record]").count() == 0
        assert page.locator(".run-from-host").inner_text() == "record: the test host's selection"
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        assert page.locator("[data-run-error]").count() == 0
        assert run_statuses(page, *HOST_PANE_STEPS) == {name: "ok" for name in HOST_PANE_STEPS}
        rows = page.locator("[data-run-row]").evaluate_all("rs => rs.map(r => r.dataset.runRow)")
        assert rows == HOST_PANE_STEPS
        assert page.locator("[data-run-ran-on]").inner_text() == HOST_LABEL
        filled = page.locator("[data-run-filled] li").evaluate_all("ls => ls.map(l => l.innerText)")
        assert sorted(filled) == ["band: f0_hz = 1000000, f1_hz = 2000000", "window: start_s = 0.25, stop_s = 0.75"]
        run = hosted_api(hosted, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["record"] is None and run["host"]["label"] == HOST_LABEL
        gains = {node["node"]: node["params"].get("gain") for node in run["trace"]["nodes"]}
        assert (gains["gain"], gains["boost"]) == (5.0, 3.0)  # the unsaved value ran
        assert hosted.host.asked == [("pane", hosted.folder / "pane.yaml")]
        assert (hosted.folder / "pane.yaml").read_text() == HOST_PANE_TEXT
        assert opened.errors == []

    def test_a_link_to_the_settings_tab_lists_only_the_app_fields_and_the_tick_box_lists_all(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&tab=settings&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        settings = page.wait_for_selector('[data-settings][data-settings-mode="app"]')
        assert page.locator(".app").get_attribute("data-side-tab") == "settings"
        app = hosted_api(hosted, "api/graphs/pane/app")
        assert [(field["node"], field["param"]) for field in app["fields"]] == [
            ("band", "deviation_hz"),
            ("gain", "gain"),
            ("boost", "gain"),
        ]
        assert settings.get_attribute("data-settings-fields") == "3"
        shown = page.locator("[data-form-node]").evaluate_all(
            "ss => ss.map(s => [s.dataset.formNode, [...s.querySelectorAll('[data-param]')].map(p => p.dataset.param)])"
        )
        assert shown == [["band", ["deviation_hz"]], ["gain", ["gain"]], ["boost", ["gain"]]]
        # the (min, max) pair is ONE field of two numbers — what the ComfyUI editor drew as __lo / __hi
        pair = page.locator('[data-form-node="band"] [data-param="deviation_hz"]')
        assert [pair.locator(f'[data-pair-end="{end}"]').input_value() for end in ("min", "max")] == [
            "100000",
            "750000",
        ]
        pair.locator('[data-pair-end="max"]').fill("800000")
        pair.locator('[data-pair-end="max"]').press("Enter")
        wait_saves(page, 1)
        # a SETTING (the app: block names it): held for the graph's settings file, the graph's own text untouched
        held = hosted_api(hosted, "api/graphs/pane")
        band = next(node for node in held["model"]["nodes"] if node["name"] == "band")
        assert band["params"]["deviation_hz"]["value"] == [100000, 800000]
        assert held["text"] == HOST_PANE_TEXT and held["dirty"] is True

        page.locator("[data-settings-all]").check()
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        form = hosted_api(hosted, "api/graphs/pane/form")
        every = sum(len(params) for params in form.values())
        assert every == 7  # window: start_s, stop_s · band: f0_hz, f1_hz, deviation_hz · gain · boost
        assert page.locator("[data-settings]").get_attribute("data-settings-fields") == str(every)
        page.locator("[data-settings-all]").uncheck()
        page.wait_for_selector('[data-settings][data-settings-mode="app"][data-settings-fields="3"]')
        assert opened.errors == []

    def test_a_graph_without_an_app_block_says_it_names_no_settings_until_all_parameters_is_ticked(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Review 2026-10-02: ``settings`` on a pane whose graph has no ``app:`` block listed every parameter — its
        Stream's plumbing, the window the host fills at run time, the root contract — with no word why."""
        opened = open_hosted(browser, hosted, "graph=source&tab=settings&embed=1")
        page = opened.page
        wait_ready(page, "source")
        settings = page.wait_for_selector('[data-settings][data-settings-mode="app"]')
        assert settings.get_attribute("data-settings-fields") == "0"
        assert page.locator("[data-form-node]").count() == 0
        assert page.locator("[data-settings-none]").inner_text() == (
            "This graph names no settings — it has no app: block. "
            "Its parameters are on the canvas; tick all parameters to list every one here."
        )
        assert not page.locator("[data-settings-all]").is_checked()

        page.locator("[data-settings-all]").check()

        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        every = sum(len(params) for params in hosted_api(hosted, "api/graphs/source/form").values())
        assert every > 0 and page.locator("[data-settings]").get_attribute("data-settings-fields") == str(every)
        assert page.locator("[data-settings-none]").count() == 0
        assert opened.errors == []

    def test_a_panes_app_fields_are_its_settings_and_a_gate_is_never_one(self, browser: Any, hosted: Hosted) -> None:
        """A pane whose ``app:`` block lists three entries — a ``__lo``/``__hi`` pair and one plain parameter — shows
        two fields, where the whole graph shows every parameter of its nodes; a step's ``requires`` gate is a field in
        neither list, though the gate is in the graph (its wire). Measured first on a domain's pane: 32 entries, 30
        fields."""
        (hosted.folder / "gated.yaml").write_text(GATED_PANE_TEXT)
        model = hosted_api(hosted, "api/graphs/gated")["model"]
        assert [wire["kind"] for wire in model["wires"]].count("gate") == 1
        opened = open_hosted(browser, hosted, "graph=gated&tab=settings&embed=1")
        page = opened.page
        wait_ready(page, "gated")
        settings = page.wait_for_selector('[data-settings][data-settings-mode="app"]')
        app = hosted_api(hosted, "api/graphs/gated/app")
        assert (len(app["fields"]), app["problems"]) == (2, [])
        assert settings.get_attribute("data-settings-fields") == "2"
        page.locator("[data-settings-all]").check()
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        form = hosted_api(hosted, "api/graphs/gated/form")
        every = sum(len(params) for params in form.values())
        assert every > 2
        assert page.locator("[data-settings]").get_attribute("data-settings-fields") == str(every)
        assert page.locator('[data-settings] [data-param="requires"]').count() == 0
        assert opened.errors == []

    def test_a_link_to_a_graph_that_is_not_there_says_so_and_offers_the_graphs_there(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=nosuch&embed=1")
        page = opened.page
        missing = page.wait_for_selector('[data-graph-missing="nosuch"]')
        assert missing.query_selector("[data-graph-missing-message]").inner_text() == (
            f"There is no graph 'nosuch' in {hosted.folder} — the graphs there are: pane and source."
        )
        assert page.locator(".app").get_attribute("data-graph") == ""  # nothing else was opened in its place
        assert page.locator(NODE).count() == 0
        choices = page.locator("[data-graph-choice]").evaluate_all("bs => bs.map(b => b.dataset.graphChoice)")
        assert choices == ["pane", "source"]
        page.locator('[data-graph-choice="pane"]').click()
        wait_ready(page, "pane")
        assert "graph=pane" in page.url and "embed=1" in page.url
        assert opened.errors == []


def choose_slot_class(page: Any, class_path: str, search: str) -> None:
    """Set source…'s first step: search the classes the slot takes and pick ``class_path``; its form comes up."""
    page.locator("[data-slot-search]").fill(search)
    page.locator(f'[data-slot-class="{class_path}"]').click()
    page.wait_for_selector(f'[data-slot-chosen="{class_path}"]')


FILES_SOURCE = "recordstream.sources.files.FilesSource"
HF_SOURCE = "recordstream.sources.huggingface.HuggingFaceSource"


class TestSetSourceInAHost:
    """Set source…: a class and its values into a Stream's empty ``source`` slot — with the folder chooser for a path
    field and the HuggingFace lists — after which the chain runs its OWN records, not the host's."""

    def test_a_files_source_with_a_folder_chosen_in_the_browser_goes_into_the_slot_and_the_chain_runs_it(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        open_run_tab(page)
        assert page.locator(".run-controls").get_attribute("data-run-from") == "host"  # no source of its own yet
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        dialog = page.wait_for_selector('[data-dialog="set-slot"] [data-slot-step="pick"]')
        assert dialog.get_attribute("data-slot-node") == "stream" and dialog.get_attribute("data-slot-name") == "source"
        offered = page.locator("[data-slot-class]").evaluate_all("ls => ls.map(l => l.dataset.slotClass)")
        assert FILES_SOURCE in offered and HF_SOURCE in offered
        assert not any(path.startswith("recordstream.ops.") for path in offered)  # an op never fills a source slot
        choose_slot_class(page, FILES_SOURCE, "FilesSource")
        assert page.locator("[data-slot-effect]").inner_text() == "FilesSource goes into the empty source slot."

        # the folder chooser: root is a folder field — the editor's file browser, folder mode, on the editor's machine
        page.locator('[data-dialog="set-slot"] button[data-browse="root"]').click()
        browser_dialog = page.wait_for_selector(
            f'[data-dialog="file"] [data-file-mode="folder"][data-file-dir="{hosted.folder}"]'
        )
        assert browser_dialog is not None
        page.locator('[data-dialog="file"] [data-file-entry="captures"]').click()
        page.wait_for_selector(f'[data-dialog="file"] [data-file-dir="{hosted.folder / "captures"}"]')
        page.locator('[data-dialog="file"] [data-file-confirm]').click()
        page.wait_for_selector('[data-dialog="file"]', state="detached")
        root = page.locator('[data-dialog="set-slot"] input[data-param="root"]')
        assert root.input_value() == str(hosted.folder / "captures")

        page.locator("[data-slot-confirm]").click()
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")
        wait_saves(page, 1)
        text = hosted_api(hosted, "api/graphs/source")["text"]
        assert f"  source: !class:{FILES_SOURCE} {{root: {hosted.folder / 'captures'}}}" in text
        assert (hosted.folder / "source.yaml").read_text() == HOST_SOURCE_TEXT  # held until Save
        page.wait_for_selector(node_root("stream.source"))
        assert page.locator(".app").get_attribute("data-dirty") == "true"

        # the chain now draws its own records: Run asks for record #N of the files source, never the host
        assert page.locator(".run-controls").get_attribute("data-run-from") == "source"
        page.locator("input[data-run-record]").fill("1")
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        assert page.locator("[data-run-error]").count() == 0
        assert run_statuses(page, "stream.ops[0]") == {"stream.ops[0]": "ok"}
        run = hosted_api(hosted, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        assert run["record"] == 1 and run["host"] is None
        assert page.locator("[data-run-ran-on]").inner_text() == "record #1"
        traced = run["trace"]["nodes"][0]
        assert "b.txt" in json.dumps(traced["output"]["name"])  # record #1 of the folder is b.txt
        assert hosted.host.asked == []

        # one undo takes the source out again
        page.mouse.click(*EMPTY)
        page.keyboard.press("Control+z")
        wait_saves(page, 2)
        assert hosted_api(hosted, "api/graphs/source")["text"] == HOST_SOURCE_TEXT
        assert opened.errors == []

    def test_the_node_menu_and_a_wire_dragged_out_of_the_slot_open_the_same_dialog(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} .title .cls').click(button="right")
        item = page.locator('[data-context-menu="node"] [data-command="set-slot"][data-slot="source"]')
        assert item.inner_text().startswith("Set source…")
        item.click()
        page.wait_for_selector('[data-dialog="set-slot"][aria-label="Set the source of stream"]')
        wait_palette_loaded(page)
        page.locator("[data-slot-cancel]").click()
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")

        start = handle(page, "stream", "source", "in").bounding_box()
        drag(page, centre(start), EMPTY)  # a wire out of the slot onto nothing: the slot's dialog, never the palette
        dialog = page.wait_for_selector('[data-dialog="set-slot"] [data-slot-step="pick"]')
        assert dialog.get_attribute("data-slot-name") == "source"
        assert page.locator("[data-palette]").count() == 0
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")
        assert page.locator(".app").get_attribute("data-saves") == "0"
        assert opened.errors == []

    def test_a_file_field_on_a_card_takes_a_file_chosen_in_the_editors_file_browser(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        card = node_root("stream.ops[0]")
        assert page.locator(f'{card} [data-picked="file"] input[data-param="names_file"]').count() == 1
        page.locator(f'{card} button[data-browse="names_file"]').click()
        page.wait_for_selector(f'[data-dialog="file"] [data-file-mode="file"][data-file-dir="{hosted.folder}"]')
        page.locator('[data-dialog="file"] [data-file-entry="captures"]').click()
        page.wait_for_selector(f'[data-dialog="file"] [data-file-dir="{hosted.folder / "captures"}"]')
        kinds = page.locator("[data-dialog=file] [data-file-entry]").evaluate_all(
            "es => es.map(e => [e.dataset.fileEntry, e.dataset.fileKind])"
        )
        assert kinds == [["a.txt", "file"], ["b.txt", "file"]]  # every file, not only graphs
        page.locator('[data-dialog="file"] [data-file-entry="a.txt"]').click()
        page.locator('[data-dialog="file"] [data-file-confirm]').click()
        wait_saves(page, 1)
        chosen = hosted.folder / "captures" / "a.txt"
        assert page.locator(f'{card} input[data-param="names_file"]').input_value() == str(chosen)
        assert (
            f"    - !class:{MOD}.TinyFileName {{names_file: {chosen}}}"
            in hosted_api(hosted, "api/graphs/source")["text"]
        )
        assert opened.errors == []

    def test_the_huggingface_lists_offer_what_the_editors_machine_knows(
        self, browser: Any, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lookups are the editor's (its machine's cache), replaced here — no test touches the network. A list is
        asked only when the person reaches for it, and the dataset picked is what the splits and columns are asked of.
        """
        asked: List[Tuple[str, Any]] = []

        def datasets() -> Tuple[List[str], str]:
            asked.append(("datasets", None))
            return ["uoft-cs/cifar10", "ylecun/mnist"], ""

        def splits(path: str, name: Any = None) -> List[str]:
            asked.append(("splits", path))
            return {"ylecun/mnist": ["train", "test"]}.get(path, [])

        def features(path: str, name: Any = None, split: Any = None) -> List[str]:
            asked.append(("features", (path, split)))
            return {"ylecun/mnist": ["image", "label", "digit_name"]}.get(path, [])

        monkeypatch.setattr(streamstudio, "cached_hf_datasets", datasets)
        monkeypatch.setattr(streamstudio, "list_hf_dataset_splits", splits)
        monkeypatch.setattr(streamstudio, "list_hf_dataset_features", features)
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        choose_slot_class(page, HF_SOURCE, "HuggingFaceSource")
        form = '[data-dialog="set-slot"]'
        assert asked == []  # nothing is asked as the form draws

        dataset = page.locator(f'{form} select[data-picker="hf_dataset"][data-picker-for="path"]')
        dataset.hover()
        page.wait_for_selector(f'{form} select[data-picker="hf_dataset"][data-picker-state="ready"]')
        assert dataset.locator("option").evaluate_all("os => os.map(o => o.value)") == [
            "",
            "uoft-cs/cifar10",
            "ylecun/mnist",
        ]
        dataset.select_option("ylecun/mnist")
        assert page.locator(f'{form} input[data-param="path"]').input_value() == "ylecun/mnist"

        split = page.locator(f'{form} select[data-picker="hf_split"][data-picker-for="split"]')
        split.hover()
        page.wait_for_selector(f'{form} select[data-picker="hf_split"][data-picker-state="ready"]')
        assert split.locator("option").evaluate_all("os => os.map(o => o.value)") == ["", "train", "test"]
        split.select_option("test")
        assert page.locator(f'{form} input[data-param="split"]').input_value() == "test"

        target = page.locator(f'{form} select[data-picker="hf_feature"][data-picker-for="target_feature"]')
        target.hover()
        page.wait_for_selector(f'{form} select[data-picker-for="target_feature"][data-picker-state="ready"]')
        assert target.locator("option").evaluate_all("os => os.map(o => o.value)") == [
            "",
            "image",
            "label",
            "digit_name",
        ]
        target.select_option("digit_name")
        assert asked == [("datasets", None), ("splits", "ylecun/mnist"), ("features", ("ylecun/mnist", "test"))]

        page.locator("[data-slot-confirm]").click()
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")
        wait_saves(page, 1)
        text = hosted_api(hosted, "api/graphs/source")["text"]
        assert f"source: !class:{HF_SOURCE} {{path: ylecun/mnist, split: test, target_feature: digit_name}}" in text
        assert opened.errors == []

    def test_an_empty_dataset_list_says_why_in_the_servers_words(
        self, browser: Any, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(streamstudio, "cached_hf_datasets", lambda: ([], "no datasets cached at /nowhere/hub"))
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        choose_slot_class(page, HF_SOURCE, "HuggingFaceSource")
        dataset = page.locator('[data-dialog="set-slot"] select[data-picker="hf_dataset"]')
        dataset.hover()
        page.wait_for_selector('[data-dialog="set-slot"] select[data-picker="hf_dataset"][data-picker-state="ready"]')
        assert dataset.get_attribute("data-picker-count") == "0"
        assert dataset.get_attribute("title") == "no datasets cached at /nowhere/hub"
        assert opened.errors == []


class TestTheHostHearsAndFollows:
    """Save tells the host which files it wrote; the host's folder is followed when it moves; the host's panel hears
    the page's messages and opens a graph by command; unsaved work survives the host's stop."""

    def test_save_tells_the_host_which_files_it_wrote(self, browser: Any, hosted: Hosted) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        set_widget(page, node_root("gain"), "gain", "4")
        wait_saves(page, 1)
        assert hosted.host.saves == []  # an edit is held: nothing is written, nothing told
        save_file(page, 2)
        # `gain` is one of the pane's SETTINGS (its app: block names it): the Save writes the settings file only
        assert hosted.host.saves == [[hosted.folder / "pane.settings.yaml"]]
        assert (hosted.folder / "pane.settings.yaml").read_text() == "flow.gain.op.gain: 4\n"
        assert (hosted.folder / "pane.yaml").read_text() == HOST_PANE_TEXT
        assert opened.errors == []

    def test_a_folder_the_host_moves_is_followed_at_the_next_request(self, browser: Any, hosted: Hosted) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        set_widget(page, node_root("gain"), "gain", "6")  # held in the editor, unsaved, when the folder moves
        wait_saves(page, 1)
        moved = hosted.folder.parent / "saved.d"
        shutil.move(str(hosted.folder), str(moved))  # what a host does on a workspace's first save
        hosted.host.where = moved
        save_file(page, 2)
        assert not hosted.folder.exists()
        assert hosted.host.saves == [[moved / "pane.settings.yaml"]]  # `gain` is a setting: its file, moved too
        assert (moved / "pane.settings.yaml").read_text() == "flow.gain.op.gain: 6\n"
        workspace = hosted_api(hosted, "api/workspace")
        assert workspace["folder"] == str(moved) and workspace["host"] == {"name": "the test host"}
        assert [entry["path"] for entry in hosted_api(hosted, "api/graphs")] == [
            str(moved / "pane.yaml"),
            str(moved / "source.yaml"),
        ]
        open_run_tab(page)
        page.locator('[data-action="run"]').click()  # the run, too, is the moved file's
        wait_answers(page, 1)
        assert hosted.host.asked == [("pane", moved / "pane.yaml")]
        assert opened.errors == []

    def test_commit_writes_the_field_being_typed_in_and_answers_committed(self, browser: Any, hosted: Hosted) -> None:
        """A host about to read what the page writes (a settings panel's run) posts ``commit``: the field still being
        typed in is written, and ``committed`` comes only after that write — never before it."""
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
        page.goto(hosted.url)
        page.wait_for_function("() => window.received.some(m => m.type === 'state' && m.graph === 'pane')")
        frame = page.frame_locator("#studio")
        frame.locator('.canvas[data-graph="pane"][data-initialized="true"]').wait_for()
        frame.locator(f'{node_root("gain")} input[data-param="gain"]').fill("7")  # typed, NOT left
        page.evaluate(
            "document.querySelector('#studio').contentWindow.postMessage("
            "{target: 'streamstudio', type: 'commit'}, location.origin)"
        )
        page.wait_for_function("() => window.received.some(m => m.type === 'committed')")
        committed = page.evaluate("window.received.find(m => m.type === 'committed')")
        assert committed == {"source": "streamstudio", "type": "committed", "graph": "pane"}
        # the editor held the typed value when it answered: a Save asked of it NOW writes it
        saved = hosted.app.test_client().post(f"{STUDIO}/api/graphs/pane/save", json={})
        assert saved.status_code == 200, saved.get_json()
        assert (hosted.folder / "pane.settings.yaml").read_text() == "flow.gain.op.gain: 7\n"
        assert errors == []

    def test_the_hosts_panel_hears_state_and_saved_and_opens_a_graph_by_command(
        self, browser: Any, hosted: Hosted
    ) -> None:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
        page.goto(hosted.url)
        page.wait_for_function("() => window.received.some(m => m.type === 'state' && m.graph === 'pane')")
        frame = page.frame_locator("#studio")
        frame.locator('.app[data-graph="pane"][data-ready="true"]').wait_for()
        frame.locator('.canvas[data-graph="pane"][data-initialized="true"]').wait_for()
        received = page.evaluate("window.received")
        assert received[-1] == {
            "source": "streamstudio",
            "type": "state",
            "graph": "pane",
            "dirty": False,
            "unsaved": [],
            "tab": "text",
            "asking": False,
        }

        widget = frame.locator(f'{node_root("gain")} input[data-param="gain"]')
        widget.fill("7")
        widget.press("Enter")
        page.wait_for_function("() => window.received.some(m => m.type === 'state' && m.dirty)")
        assert page.evaluate("window.received.filter(m => m.dirty).pop()")["unsaved"] == ["pane"]
        frame.locator('[data-menu="file"] > .menubar-title').click()
        frame.locator('[data-menu="file"] [data-command="save"]').click()
        page.wait_for_function("() => window.received.some(m => m.type === 'saved')")
        saved = page.evaluate("window.received.find(m => m.type === 'saved')")
        assert saved == {
            "source": "streamstudio",
            "type": "saved",
            "graph": "pane",
            "path": str(hosted.folder / "pane.yaml"),
        }
        assert hosted.host.saves == [[hosted.folder / "pane.settings.yaml"]]  # `gain` is a setting

        # the host's own button: open another graph on its settings tab, without reloading the panel
        page.evaluate(
            "document.querySelector('#studio').contentWindow.postMessage("
            "{target: 'streamstudio', type: 'open', graph: 'source', tab: 'run'}, location.origin)"
        )
        frame.locator('.app[data-graph="source"][data-ready="true"][data-side-tab="run"]').wait_for()
        page.wait_for_function(
            "() => window.received.some(m => m.type === 'state' && m.graph === 'source' && m.tab === 'run')"
        )
        page.evaluate(
            "document.querySelector('#studio').contentWindow.postMessage("
            "{target: 'streamstudio', type: 'open', graph: 'nosuch'}, location.origin)"
        )
        frame.locator('[data-graph-missing="nosuch"]').wait_for()
        page.close()
        assert errors == []

    def test_unsaved_work_kept_on_the_hosts_stop_is_offered_back_by_the_mounted_page(
        self, browser: Any, hosted: Hosted, templates_dir: Path
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        set_widget(page, node_root("gain"), "gain", "8")
        wait_saves(page, 1)
        page.close()
        kept = studio_of(hosted.app).stash()  # the host's stop
        assert kept == [str(hosted.folder / "pane.yaml")] and (hosted.folder / RECOVERY_FILE).is_file()
        hosted.app.extensions[EXTENSION] = Studio(host=hosted.host, templates_dir=templates_dir)  # the next start

        again = open_hosted(browser, hosted, "graph=pane&embed=1")
        again.page.wait_for_selector('.app[data-recovery="asking"] [data-dialog="recovery"]')
        again.page.locator('[data-recovery-action="restore"]').click()
        again.page.wait_for_selector('.app[data-recovery="restored"]')
        wait_ready(again.page, "pane")
        assert again.page.locator(f'{node_root("gain")} input[data-param="gain"]').input_value() == "8"
        assert again.page.locator(".app").get_attribute("data-dirty") == "true"
        assert (hosted.folder / "pane.yaml").read_text() == HOST_PANE_TEXT
        assert again.errors == []

    def test_the_host_hears_while_the_page_asks_about_kept_work_and_the_graph_once_answered(
        self, browser: Any, hosted: Hosted, templates_dir: Path
    ) -> None:
        """No graph opens before the recovery question is answered, so a host covering its panel until the graph it
        asked for shows must learn the page is ASKING — measured before: a host's "loading pane…" veil covered the
        question for good, and the person saw nothing else."""
        opened = open_hosted(browser, hosted, "graph=pane&embed=1")
        wait_ready(opened.page, "pane")
        set_widget(opened.page, node_root("gain"), "gain", "8")
        wait_saves(opened.page, 1)
        opened.page.close()
        studio_of(hosted.app).stash()  # the host's stop keeps the unsaved work
        hosted.app.extensions[EXTENSION] = Studio(host=hosted.host, templates_dir=templates_dir)  # the next start

        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
        page.goto(hosted.url)
        page.wait_for_function("() => window.received.some(m => m.type === 'state' && m.asking)")
        frame = page.frame_locator("#studio")
        frame.locator('.app[data-recovery="asking"] [data-dialog="recovery"]').wait_for()
        asking = page.evaluate("window.received.filter(m => m.type === 'state').pop()")
        assert asking["asking"] is True and asking["graph"] == ""

        frame.locator('[data-recovery-action="restore"]').click()
        page.wait_for_function(
            "() => { const s = window.received.filter(m => m.type === 'state').pop();"
            " return s.graph === 'pane' && s.asking === false; }"
        )
        page.close()
        assert errors == []


# ------------------------------------------------------------------------------------------------
# the host's panel keeps each edit in its own graph, follows the host, and opens fitted (review 5 of DESIGN5 step 1)
# ------------------------------------------------------------------------------------------------

#: Two graphs that share a step name — a value typed into one must never land in the other.
VIEW_TEXT = f"""\
# a view: one gain step
flow:
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
outputs: gain
"""

#: A pane chain as a host serves it: a Stream with no source of its own — its slot ``stream.source`` exists exactly as
#: in ``source.yaml``, so a source meant for one would fit the other.
PANE_STREAM_TEXT = f"""\
# a pane chain (no source: the host supplies the record)
stream: !class:recordstream.core.stream.Stream
  ops:
    - !class:{MOD}.TinyFileName {{suffix: pane}}
"""

#: A pane that SPELLS its window as 0 and 0, as the shipped panes do — the host fills the real window before a run.
SPELLED_WINDOW_TEXT = f"""\
# a pane spelling its window: the host fills it
flow:
  window:
    op: !class:{MOD}.PaneWindow {{start_s: 0, stop_s: 0}}
  gain:
    op: !class:{MOD}.TinyGain {{gain: 2.0}}
    from: window
outputs: gain
"""


class HostPage(NamedTuple):
    """The host's own page with the editor in its panel (an iframe), the panel's frame, and every error of both."""

    page: Any  # playwright.sync_api.Page
    frame: Any  # playwright.sync_api.Frame — the panel's document
    errors: List[str]


def open_host_page(browser: Any, hosted: Hosted, graph: str) -> HostPage:
    """The host's page; its panel opens ``pane`` and is then told to open ``graph`` (the host's own ``open``)."""
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: List[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None)
    page.goto(hosted.url)
    frame = page.wait_for_selector("#studio").content_frame()
    wait_ready(frame, "pane")
    if graph != "pane":
        host_opens(page, graph)
        wait_ready(frame, graph)
    return HostPage(page, frame, errors)


def host_opens(page: Any, graph: str) -> None:
    """The host posts ``open`` to its panel — what a pane's ``graph`` button does — with no click inside the panel."""
    page.evaluate(
        "graph => document.querySelector('#studio').contentWindow.postMessage("
        "{target: 'streamstudio', type: 'open', graph}, location.origin)",
        graph,
    )


def held_text(hosted: Hosted, graph: str) -> str:
    return str(hosted_api(hosted, f"api/graphs/{graph}")["text"])


class TestTheHostsOpenKeepsEachEditInItsGraph:
    """The host switches the panel's graph by itself (``open``) — after its own save, a selection change, a pane's
    ``graph`` button. Whatever the person was doing in the graph shown stays with THAT graph."""

    def test_set_source_is_closed_when_the_host_opens_another_graph(self, browser: Any, hosted: Hosted) -> None:
        """Measured before: the dialog stayed up over the new graph, and Set wrote the source into it — a pane that then
        draws its own records instead of the host's."""
        (hosted.folder / "view_psd.yaml").write_text(PANE_STREAM_TEXT)
        shown = open_host_page(browser, hosted, "source")
        frame = shown.frame
        frame.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(frame)
        choose_slot_class(frame, FILES_SOURCE, "FilesSource")
        frame.locator('[data-dialog="set-slot"] input[data-param="pattern"]').fill("*.iq")
        host_opens(shown.page, "view_psd")
        wait_ready(frame, "view_psd")
        frame.wait_for_selector('[data-dialog="set-slot"]', state="detached")
        assert held_text(hosted, "source") == HOST_SOURCE_TEXT
        assert held_text(hosted, "view_psd") == PANE_STREAM_TEXT
        assert frame.locator(".app").get_attribute("data-saves") == "0"
        assert shown.errors == []

    def test_a_value_typed_but_not_left_goes_into_the_graph_it_was_typed_in(self, browser: Any, hosted: Hosted) -> None:
        """Measured before: the card of ``gain`` stayed the same component across the switch, kept the typed draft, and
        committed it into the NEW graph's ``gain`` when the field was left."""
        (hosted.folder / "view_a.yaml").write_text(VIEW_TEXT)
        (hosted.folder / "view_b.yaml").write_text(VIEW_TEXT)
        shown = open_host_page(browser, hosted, "view_a")
        frame = shown.frame
        frame.locator(f'{node_root("gain")} input[data-param="gain"]').fill("7")  # typed, still in the field
        host_opens(shown.page, "view_b")
        wait_ready(frame, "view_b")
        frame.wait_for_selector('.app[data-saves="1"]')  # the value typed in view_a was written there
        assert frame.locator(f'{node_root("gain")} input[data-param="gain"]').input_value() == "2"
        frame.locator("[data-status]").click()  # leaving whatever has the focus commits nothing more
        assert f"op: !class:{MOD}.TinyGain {{gain: 7}}" in held_text(hosted, "view_a")
        assert held_text(hosted, "view_b") == VIEW_TEXT
        assert frame.locator(".app").get_attribute("data-dirty") == "false"
        dirty = {entry["name"]: entry["dirty"] for entry in hosted_api(hosted, "api/graphs")}
        assert (dirty["view_a"], dirty["view_b"]) == (True, False)
        assert shown.errors == []

    def test_a_file_the_host_rewrote_is_shown_as_the_file_says_when_the_host_opens_it(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Measured before: the panel, a reload and Run kept the first text read, ``data-dirty`` false."""
        shown = open_host_page(browser, hosted, "pane")
        frame = shown.frame
        (hosted.folder / "pane.yaml").write_text(
            HOST_PANE_TEXT.replace("gain: 2.0}   # the knob", "gain: 9.0}   # the knob")
        )
        host_opens(shown.page, "pane")
        frame.wait_for_function(
            "() => document.querySelector("
            "'[data-node=\"gain\"]:not([data-socket]) input[data-param=\"gain\"]')?.value === '9'"
        )
        assert frame.locator(".app").get_attribute("data-dirty") == "false"
        assert shown.errors == []

    def test_a_setting_the_host_saved_through_the_editor_is_shown_when_the_host_opens_it(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """A host saving a SETTING itself (a pane's knob) leaves the graph's text as it was — measured before: the
        panel compared the text alone, so it kept showing the old value after the host's ``open``."""
        shown = open_host_page(browser, hosted, "pane")
        frame = shown.frame
        studio = studio_of(hosted.app)
        studio.document("pane").set_param("gain", "gain", 5)
        studio.save("pane", only="settings")
        assert (hosted.folder / "pane.settings.yaml").read_text() == "flow.gain.op.gain: 5\n"
        host_opens(shown.page, "pane")
        frame.wait_for_function(
            "() => document.querySelector("
            "'[data-node=\"gain\"]:not([data-socket]) input[data-param=\"gain\"]')?.value === '5'"
        )
        assert frame.locator(".app").get_attribute("data-dirty") == "false"
        assert shown.errors == []

    def test_new_tells_the_host_which_file_it_wrote(self, browser: Any, hosted: Hosted) -> None:
        """New… writes a graph file in the host's folder: the host hears it, as it hears a Save. Measured before: no
        ``saved`` message and nothing on ``saved()``, so a host finding its panes through them never saw a new one."""
        shown = open_host_page(browser, hosted, "pane")
        frame = shown.frame
        open_new(frame, "flow")
        frame.locator("[data-new-name]").fill("view_new")
        frame.locator("[data-new-confirm]").click()
        wait_ready(frame, "view_new")
        shown.page.wait_for_function("() => window.received.some(m => m.type === 'saved' && m.graph === 'view_new')")
        said = shown.page.evaluate("window.received.find(m => m.type === 'saved')")
        assert said == {
            "source": "streamstudio",
            "type": "saved",
            "graph": "view_new",
            "path": str(hosted.folder / "view_new.yaml"),
        }
        assert hosted.host.saves == [[hosted.folder / "view_new.yaml"]]
        assert shown.errors == []

    def test_after_the_host_moves_its_folder_new_and_a_missing_graph_name_the_new_folder(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Measured before: the page read the folder once, so New… said it writes ``state/fresh.yaml`` and wrote
        ``saved.d/fresh.yaml``, and a missing graph was looked for "in" the folder the host had left."""
        shown = open_host_page(browser, hosted, "pane")
        frame = shown.frame
        moved = hosted.folder.parent / "saved.d"
        shutil.move(str(hosted.folder), str(moved))
        hosted.host.where = moved
        open_new(frame, "flow")
        assert frame.locator("[data-new-folder]").input_value() == str(moved)
        frame.locator("[data-new-name]").fill("fresh")
        assert new_path_shown(frame).endswith(str(moved / "fresh.yaml"))
        frame.locator("[data-new-confirm]").click()
        wait_ready(frame, "fresh")
        assert (moved / "fresh.yaml").is_file()
        host_opens(shown.page, "nosuch")
        frame.wait_for_selector('[data-graph-missing="nosuch"]')
        assert frame.locator("[data-graph-missing-message]").inner_text() == (
            f"There is no graph 'nosuch' in {moved} — the graphs there are: fresh, pane and source."
        )
        assert shown.errors == []


class TestTheEmbeddedPanelsFields:
    """The fields of the panel: a ``(min, max)`` pair typed end to end, a rerun of a step the host filled, the
    settings tab's tick, the folder browser over Set source…, the keyboard in Set source…, a HuggingFace list that
    failed once."""

    def test_a_pair_typed_min_tab_max_keeps_what_was_typed_while_the_min_answer_arrives(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Leaving the min box writes ``[min, old max]``; its answer arrives while the max box is being typed into. The
        server is held until the first key is in the max box — that ordering, not a typing speed. Measured before: the
        answer reset BOTH boxes, so the max box went back to 750000 and the next keys were appended to it
        (``75000061000``, refused, or written)."""
        gate, arrived = threading.Event(), threading.Event()
        inner = hosted.app.wsgi_app

        def held(environ: Dict[str, Any], start_response: Any) -> Any:  # a WSGI app: the environ and its callback
            if environ["PATH_INFO"].endswith("/api/graphs/pane/param") and not gate.is_set():
                arrived.set()
                gate.wait(timeout=30)
            return inner(environ, start_response)

        hosted.app.wsgi_app = held
        opened = open_hosted(browser, hosted, "graph=pane&tab=settings&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        page.wait_for_selector('[data-settings][data-settings-mode="app"]')
        pair = page.locator('[data-form-node="band"] [data-param="deviation_hz"]')
        low, high = pair.locator('[data-pair-end="min"]'), pair.locator('[data-pair-end="max"]')
        low.fill("111000")
        low.press("Tab")  # into the max box, its text selected
        assert arrived.wait(timeout=30), "leaving the min box wrote nothing"
        page.keyboard.type("76")
        gate.set()  # the min box's answer arrives now, with "76" typed into the max box
        wait_saves(page, 1)
        assert high.input_value() == "76"
        page.keyboard.type("1000")
        page.keyboard.press("Enter")
        wait_saves(page, 2)
        band = next(node for node in hosted_api(hosted, "api/graphs/pane")["model"]["nodes"] if node["name"] == "band")
        assert band["params"]["deviation_hz"]["value"] == [111000, 761000]  # a setting: held for the settings file
        assert held_text(hosted, "pane") == HOST_PANE_TEXT
        assert [low.input_value(), high.input_value()] == ["111000", "761000"]
        assert opened.errors == []

    def test_rerun_of_a_step_the_host_filled_keeps_the_hosts_window(self, browser: Any, hosted: Hosted) -> None:
        """Measured before (``power_time_view``): the rerun form showed the document's ``0`` / ``0``, "rerun this node"
        sent them, and the window became the whole capture while the panel still said the host's window ran."""
        (hosted.folder / "spelled.yaml").write_text(SPELLED_WINDOW_TEXT)
        opened = open_hosted(browser, hosted, "graph=spelled&embed=1")
        page = opened.page
        wait_ready(page, "spelled")
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        page.locator(f'{node_root("window")} .title').click()
        form = page.locator('[data-inspect="window"] [data-rerun-form]')
        assert [form.locator(f'[data-param="{name}"]').input_value() for name in ("start_s", "stop_s")] == [
            "0.25",
            "0.75",
        ]
        with page.expect_request("**/api/runs/*/rerun") as asked:
            page.locator('[data-action="rerun"]').click()
        assert json.loads(asked.value.post_data or "{}") == {"node": "window", "params": {}}
        wait_answers(page, 2)
        run = hosted_api(hosted, f"api/runs/{page.locator('[data-run-panel]').get_attribute('data-run')}")
        window = next(node for node in run["trace"]["nodes"] if node["node"] == "window")
        assert window["params"] == HOST_WINDOW
        assert opened.errors == []

    def test_the_all_parameters_tick_stays_when_the_person_leaves_the_tab(self, browser: Any, hosted: Hosted) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&tab=settings&embed=1")
        page = opened.page
        wait_ready(page, "pane")
        page.wait_for_selector('[data-settings][data-settings-mode="app"]')
        page.locator("[data-settings-all]").check()
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        open_run_tab(page)
        page.locator('[data-tab="settings"]').click()
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        assert page.locator("[data-settings-all]").is_checked()
        assert opened.errors == []

    def test_escape_in_the_folder_browser_over_set_source_closes_the_browser_alone(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Measured before: the browser did not take the focus, so Escape closed Set source… under it (and what was
        typed there), and the folder then chosen went nowhere; on a card, Escape did nothing."""
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        choose_slot_class(page, FILES_SOURCE, "FilesSource")
        page.locator('[data-dialog="set-slot"] input[data-param="pattern"]').fill("*.txt")
        page.locator('[data-dialog="set-slot"] input[data-param="pattern"]').press("Enter")
        page.locator('[data-dialog="set-slot"] button[data-browse="root"]').click()
        page.wait_for_selector(f'[data-dialog="file"] [data-file-dir="{hosted.folder}"]')
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-dialog="file"]', state="detached")
        assert page.locator('[data-dialog="set-slot"]').count() == 1
        assert page.locator('[data-dialog="set-slot"] input[data-param="pattern"]').input_value() == "*.txt"
        page.locator('[data-dialog="set-slot"] button[data-browse="root"]').click()
        page.locator('[data-dialog="file"] [data-file-entry="captures"]').click()
        page.wait_for_selector(f'[data-dialog="file"] [data-file-dir="{hosted.folder / "captures"}"]')
        page.locator('[data-dialog="file"] [data-file-confirm]').click()
        page.wait_for_selector('[data-dialog="file"]', state="detached")
        assert page.locator('[data-dialog="set-slot"] input[data-param="root"]').input_value() == str(
            hosted.folder / "captures"
        )
        page.locator("[data-slot-cancel]").click()
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")

        page.locator(f'{node_root("stream.ops[0]")} button[data-browse="names_file"]').click()  # on a card
        page.wait_for_selector('[data-dialog="file"] [data-file-mode="file"]')
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-dialog="file"]', state="detached")
        assert opened.errors == []

    def test_while_a_picked_classs_settings_load_the_dialog_keeps_the_keyboard(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """The search box goes with the list when a class is picked, and the first field takes the keyboard only once
        the class's settings arrive. Measured 2026-10-05: in between, the focus was on the page, where Escape did
        nothing. The settings are held back here, so that moment lasts as long as the test needs it."""
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        held: List[Any] = []
        page.route(f"**/api/palette/{FILES_SOURCE}", lambda route: held.append(route))
        try:
            wait_ready(page, "source")
            page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
            wait_palette_loaded(page)
            page.locator("[data-slot-search]").fill("FilesSource")
            page.locator("[data-slot-search]").press("Enter")
            page.wait_for_selector(f'[data-slot-chosen="{FILES_SOURCE}"][data-slot-settings="loading"]')
            assert held, "the settings were never asked — the hold proves nothing"
            assert page.evaluate("() => document.activeElement?.closest('[data-dialog]')?.dataset.dialog") == "set-slot"
            page.keyboard.press("Escape")
            page.wait_for_selector('[data-dialog="set-slot"]', state="detached")
        finally:
            for route in held:
                route.abort()

    def test_set_source_keeps_the_keyboard_after_a_pick_and_scrolls_to_the_highlighted_class(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """Measured before: after Enter picked a class the focus fell to the page (Escape did nothing), and ArrowDown
        moved the highlight below the list's bottom edge."""
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        page.locator("[data-slot-search]").fill("FilesSource")
        page.locator("[data-slot-search]").press("Enter")
        page.wait_for_selector(f'[data-slot-chosen="{FILES_SOURCE}"]')
        # the first field takes the keyboard once the class's settings have ARRIVED — they are asked when it is
        # picked; read before that, the check raced them (measured 2026-10-05: about half the runs on a busy machine)
        page.wait_for_selector('[data-slot-settings="ready"]')
        focused = page.evaluate(
            "() => [document.activeElement?.closest('[data-dialog]')?.dataset.dialog,"
            " document.activeElement?.dataset.param]"
        )
        assert focused[0] == "set-slot" and focused[1] is not None
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-dialog="set-slot"]', state="detached")

        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        page.wait_for_selector('[data-dialog="set-slot"] [data-slot-step="pick"]')
        rows = page.locator("[data-slot-class]").count()
        assert rows > 14, "too few classes to scroll — the check would prove nothing"
        for _ in range(14):
            page.keyboard.press("ArrowDown")
        inside = page.evaluate(
            """() => { const list = document.querySelector('[data-slot-list]').getBoundingClientRect();
                       const row = document.querySelector('[data-slot-class][aria-selected="true"]')
                         .getBoundingClientRect();
                       return row.top >= list.top - 1 && row.bottom <= list.bottom + 1; }"""
        )
        assert inside
        assert opened.errors == []

    def test_a_huggingface_list_that_failed_asks_again_when_reached_for_again(
        self, browser: Any, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measured before: once failed, hovering and focusing the list again asked nothing — only reopening the dialog
        (or reloading the page) did."""
        answers: List[str] = []

        def datasets() -> Tuple[List[str], str]:
            answers.append("asked")
            if len(answers) == 1:
                raise RuntimeError("the HuggingFace cache could not be read: permission denied")
            return ["ylecun/mnist"], ""

        monkeypatch.setattr(streamstudio, "cached_hf_datasets", datasets)
        opened = open_hosted(browser, hosted, "graph=source&embed=1")
        page = opened.page
        wait_ready(page, "source")
        page.locator(f'{node_root("stream")} button[data-set-slot="source"]').click()
        wait_palette_loaded(page)
        choose_slot_class(page, HF_SOURCE, "HuggingFaceSource")
        dataset = page.locator('[data-dialog="set-slot"] select[data-picker="hf_dataset"]')
        dataset.hover()
        page.wait_for_selector('[data-dialog="set-slot"] select[data-picker="hf_dataset"][data-picker-state="failed"]')
        page.locator("[data-slot-effect]").hover()
        dataset.hover()
        page.wait_for_selector('[data-dialog="set-slot"] select[data-picker="hf_dataset"][data-picker-state="ready"]')
        assert dataset.locator("option").evaluate_all("os => os.map(o => o.value)") == ["", "ylecun/mnist"]
        assert answers == ["asked", "asked"]
        assert opened.errors == [REFUSED_400]


class TestTheEmbeddedPanelOpensFitted:
    @pytest.mark.parametrize("graph", ["pane", "pane_shaped"])
    def test_a_graph_opened_in_a_small_panel_shows_every_card(self, browser: Any, hosted: Hosted, graph: str) -> None:
        """Measured before: zoom 1 from the top-left — at 900x700, 1 of a domain pane's 30 cards and 1 of this pane's 5
        (and, with the fit clamped at zoom 0.1, 17 of the 30); ``pane_shaped`` is that pane's shape, 31 cards. The
        standalone page keeps its zoom-1 first view (``TestEnteringASubgraphIsReadable`` and the drag tests measure at
        zoom 1)."""
        if graph == "pane_shaped":
            (hosted.folder / "pane_shaped.yaml").write_text(PANE_SHAPED_TEXT)
        page = browser.new_page(viewport={"width": 900, "height": 700})
        errors: List[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.goto(f"{hosted.url}{STUDIO}/?graph={graph}&embed=1")
        wait_ready(page, graph)
        page.wait_for_selector('.canvas[data-view-level=""]')
        outside = page.evaluate(
            """() => { const pane = document.querySelector('.react-flow').getBoundingClientRect();
                       return [...document.querySelectorAll('.react-flow__node')]
                         .map(n => [n.dataset.id, n.getBoundingClientRect()])
                         .filter(([, r]) => r.left < pane.left - 1 || r.right > pane.right + 1
                                            || r.top < pane.top - 1 || r.bottom > pane.bottom + 1)
                         .map(([id]) => id); }"""
        )
        assert outside == []
        assert viewport_zoom(page) <= 1
        assert errors == []


class TestTheSettingsOnlyPage:
    """``?only=settings`` — a host's settings SHEET: the graph's ``app:`` fields and nothing else, each change SAVED at
    once (the user's pick 2026-10-05), and saved where every setting goes — the graph's settings file, never the graph.
    """

    def test_it_shows_the_settings_and_nothing_else_and_saves_each_change_at_once(
        self, browser: Any, hosted: Hosted
    ) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1&only=settings")
        page = opened.page
        page.wait_for_selector('.app[data-graph="pane"][data-ready="true"] [data-settings]')
        root = page.locator(".app")
        assert root.get_attribute("data-only") == "settings"
        assert page.locator(".bar").count() == 0  # no menus, no graph picker, no Run
        assert page.locator(".canvas").count() == 0 and page.locator(".react-flow").count() == 0
        assert page.locator(".side .tabs").count() == 0
        assert page.locator("[data-settings-all]").count() == 0  # nothing that would change the graph itself
        sections = page.locator("[data-form-node]").evaluate_all("ss => ss.map(s => s.dataset.formNode)")
        assert sections == ["band", "gain", "boost"]  # TinyGain's two nodes, each by its name
        set_widget(page, '[data-form-node="gain"]', "gain", "5.5")
        wait_saves(page, 1)  # the setting and its Save, one request (`save: "settings"`)
        assert root.get_attribute("data-dirty") == "false"
        assert (hosted.folder / "pane.settings.yaml").read_text() == "flow.gain.op.gain: 5.5\n"
        assert (hosted.folder / "pane.yaml").read_text() == HOST_PANE_TEXT
        assert hosted.host.saves == [[(hosted.folder / "pane.settings.yaml").resolve()]]
        shown = page.locator('[data-form-node="gain"] input[data-param="gain"]').input_value()
        assert float(shown) == 5.5
        assert opened.errors == []

    def test_a_change_whose_save_is_refused_is_not_kept_and_the_form_shows_what_is(
        self, browser: Any, hosted: Hosted
    ) -> None:
        """A save refused as changed on disk keeps nothing of the sheet's change: the server undoes it in the same
        request, so a stop never keeps it as unsaved work. The refusal needs an unsaved settings change of another view
        (the editor's own tab) while another program writes the file. Measured before 2026-11-21: the change stayed
        held after the refusal, and the next stop kept it."""
        settings = hosted.folder / "pane.settings.yaml"
        settings.write_text("flow.gain.op.gain: 4.0\n")
        opened = open_hosted(browser, hosted, "graph=pane&embed=1&only=settings")
        page = opened.page
        page.wait_for_selector('.app[data-graph="pane"][data-ready="true"] [data-settings]')
        studio = studio_of(hosted.app)
        api_post(f"{hosted.url}{STUDIO}", "api/graphs/pane/param", {"node": "boost", "param": "gain", "value": 9.0})
        with studio.lock:
            held = studio.document("pane").held()  # the other view's change, unsaved
        settings.write_text("flow.gain.op.gain: 6.0\n")  # another program

        set_widget(page, '[data-form-node="gain"]', "gain", "5.5")

        assert "changed on disk" in page.locator('[data-toast="error"]').inner_text()
        with studio.lock:
            assert studio.document("pane").held() == held, "only the other view's change is held"
        assert settings.read_text() == "flow.gain.op.gain: 6.0\n" and hosted.host.saves == []
        page.wait_for_function(
            """() => Number(document.querySelector('[data-form-node="gain"] input[data-param="gain"]').value) === 4"""
        )
        assert opened.errors == [REFUSED_400]

    def test_the_host_sets_the_font_and_a_line_under_a_setting(self, browser: Any, hosted: Hosted) -> None:
        """``style`` hands the form the host page's font; ``hints`` a line under a setting (what the host measured for
        it) — shown, never written."""
        opened = open_hosted(browser, hosted, "graph=pane&embed=1&only=settings")
        page = opened.page
        page.wait_for_selector('.app[data-graph="pane"][data-ready="true"] [data-settings]')
        before = page.locator('[data-form-node="gain"] .field').first.evaluate("e => getComputedStyle(e).fontSize")
        assert before == "12px"
        page.evaluate(
            "() => { window.postMessage({target: 'streamstudio', type: 'style', font_size_px: 15}, location.origin);"
            " window.postMessage({target: 'streamstudio', type: 'hints', graph: 'pane',"
            " hints: {gain: 'measured 3.2 dB'}}, location.origin); }"
        )
        page.wait_for_selector('[data-form-node="gain"] [data-field-hint="gain"]')
        assert page.locator('[data-form-node="gain"] [data-field-hint="gain"]').inner_text() == "measured 3.2 dB"
        size = page.locator('[data-form-node="gain"] .field').first.evaluate("e => getComputedStyle(e).fontSize")
        widget = page.locator('[data-form-node="gain"] input[data-param="gain"]').evaluate(
            "e => getComputedStyle(e).fontSize"
        )
        assert (size, widget) == ("15px", "15px")
        assert not (hosted.folder / "pane.settings.yaml").exists(), "a hint is never written"
        assert opened.errors == []

    def test_a_number_written_with_its_unit_is_typed_and_saved_as_written(self, browser: Any, hosted: Hosted) -> None:
        """A setting that takes a number OR text (``Union[float, str]``: recordstream's ``Quantity.value``) is a text
        box: ``2.5 MSa/s`` is saved as typed, unit and all (a number box saved ``200 kSa/s`` as ``200``), and a plain
        number as a number."""
        (hosted.folder / "rate.yaml").write_text(
            f"rate_value: !class:recordstream.quantity.Quantity\n  value: '0'\n  kind: rate\n"
            f"flow:\n  gain:\n    op: !class:{MOD}.TinyGain {{gain: 2.0}}\noutputs: gain\n"
            "app:\n  inputs:\n    - [Quantity, value]\n"
        )
        opened = open_hosted(browser, hosted, "graph=rate&embed=1&only=settings")
        page = opened.page
        page.wait_for_selector('.app[data-graph="rate"][data-ready="true"] [data-settings]')
        widget = page.locator('[data-form-node="rate_value"] input[data-param="value"]')
        assert widget.get_attribute("type") == "text"
        set_widget(page, '[data-form-node="rate_value"]', "value", "2.5 MSa/s")
        wait_saves(page, 1)
        assert (hosted.folder / "rate.settings.yaml").read_text() == "rate_value.value: 2.5 MSa/s\n"
        assert widget.input_value() == "2.5 MSa/s"
        set_widget(page, '[data-form-node="rate_value"]', "value", "250000")
        wait_saves(page, 2)
        assert (hosted.folder / "rate.settings.yaml").read_text() == "rate_value.value: 250000\n"
        assert opened.errors == []

    def test_a_graph_with_no_settings_says_so_and_offers_no_tick_box(self, browser: Any, hosted: Hosted) -> None:
        opened = open_hosted(browser, hosted, "graph=source&embed=1&only=settings")
        page = opened.page
        page.wait_for_selector('.app[data-graph="source"][data-ready="true"] [data-settings-none]')
        assert (
            page.locator("[data-settings-none]").inner_text() == "This graph names no settings — it has no app: block."
        )
        assert page.locator("[data-settings-all]").count() == 0
        assert opened.errors == []


#: The files graph a host workspace seeds (2026-10-05): the host's ``files`` go into ``file_source``; the Stream reads
#: each file as an image; the root delivers the stream, reads ``input`` from ``image`` and MAY deliver a class list.
FILES_GRAPH_TEXT = """\
file_source: !class:recordstream.sources.files.FilesSource {}
stream: !class:recordstream.core.stream.Stream
  source: !ref:file_source
  ops:
    - !class:recordstream.ops.image.ReadImage {field: file, output: image}
contract: !class:recordstream.ops.contract.GraphContract
  name: classification files
  inputs: {files: "List[str]"}
  outputs: {stream: Stream}
  optional: {classes: "List[str]"}
  records: {input: Image}
  delivered:
    stream: !ref:stream
    input: image
"""

#: The host wire of the files graph: the host card's ``files`` into the FilesSource's ``files``.
FILES_HOST_WIRE = ("<host>", "files", "file_source", "files")


class TestTheFilesGraphsHostCard:
    """The files graph on the built page: the host card titled after the host, its dashed wire to where the host puts
    the files, the delivered wires, the optional slot without a ``*`` — and a Delete on the dashed wire refused in the
    server's one line, nothing changed."""

    def test_it_draws_the_host_card_its_dashed_wire_and_the_delivered_wires(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "files.yaml").write_text(FILES_GRAPH_TEXT)
        opened = open_graph("files")
        page = opened.page
        wait_edges(page, 5)
        assert sorted(page.locator(".react-flow__edge").evaluate_all("es => es.map(e => e.dataset.id)")) == sorted(
            [
                "wire:<host>.files->file_source.files",
                "wire:file_source.stream->stream.source",
                "wire:stream.ops[0].op->stream.ops[0]",
                "wire:stream.stream->contract.stream",
                "wire:stream.ops[0].image->contract.input",
            ]
        )
        host = page.locator(node_root("<host>"))
        assert host.get_attribute("data-kind") == "host"
        assert host.locator("[data-host-title]").inner_text() == "from the host"  # standalone
        assert host.locator("[data-rename]").count() == 0
        dashed = page.evaluate(
            """(id) => getComputedStyle(
                document.querySelector(`.react-flow__edge[data-id="${id}"] .react-flow__edge-path`)).strokeDasharray""",
            "wire:<host>.files->file_source.files",
        )
        assert dashed not in ("", "none")
        assert "wire-host" in (edge(page, *FILES_HOST_WIRE).get_attribute("class") or "")
        rows = page.locator(f'{node_root("contract")} .row.in .label').evaluate_all(
            "ls => ls.map(l => [l.firstChild.textContent, l.querySelector('b')?.textContent ?? ''])"
        )
        assert rows == [["stream", "*"], ["input", ""], ["classes", ""]]  # the optional slot has no required mark
        assert page.locator("[data-problem]").count() == 0
        assert opened.errors == []

    def test_deleting_the_dashed_wire_is_refused_in_one_line_and_nothing_changes(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "files.yaml").write_text(FILES_GRAPH_TEXT)
        opened = open_graph("files")
        page = opened.page
        wait_edges(page, 5)
        click_wire(page, *FILES_HOST_WIRE)
        page.keyboard.press("Delete")
        assert warning(page) == "The host fills this when it runs the graph — it isn't a wire you can remove."
        assert edge(page, *FILES_HOST_WIRE).count() == 1
        assert edge_count(page) == 5
        body = api_get(server, "api/graphs/files")
        assert body["text"] == FILES_GRAPH_TEXT and body["dirty"] is False
        assert page.locator(".app").get_attribute("data-dirty") == "false"
        assert opened.errors == [REFUSED_400]

    def test_in_a_hosts_panel_the_card_is_titled_after_the_host(self, browser: Any, hosted: Hosted) -> None:
        (hosted.folder / "files.yaml").write_text(FILES_GRAPH_TEXT)
        opened = open_hosted(browser, hosted, "graph=files&embed=1")
        page = opened.page
        wait_ready(page, "files")
        title = page.locator(f'{node_root("<host>")} [data-host-title]')
        assert title.inner_text() == f"from {BrowserHost.name}"
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# a class that is not an op is a card of its own (the user's case, 2026-10-05)
# ------------------------------------------------------------------------------------------------

#: The files graph as a host workspace's template spells it — a header, then the root's own comment line.
FILES_TEMPLATE_TEXT = "# a template: the files graph a host workspace seeds\n" + FILES_GRAPH_TEXT.replace(
    "contract: ", "# The graph's ROOT — what this graph must deliver.\ncontract: ", 1
)

#: The class the person picked, and the one a wire out of the root's ``classes`` takes.
SCAN_CLASS = "recordstream.ops.contract.ClassNamesScan"
NAMES_CLASS = "recordstream.ops.contract.ClassNamesOutput"

#: Where the files graph's cards stand: every one in view, left of the side panel and clear of the minimap.
FILES_PLACES = {
    "<host>": (40.0, 40.0),
    "file_source": (40.0, 200.0),
    "stream": (380.0, 40.0),
    "stream.ops[0]": (380.0, 300.0),
    "contract": (720.0, 40.0),
}


def with_card_above_the_root(text: str, line: str) -> str:
    """``text`` with ``line`` written directly above the root's own comment line."""
    return text.replace("# The graph's ROOT", f"{line}\n# The graph's ROOT", 1)


def empty_point(page: Any) -> Tuple[float, float]:
    """A screen point on EMPTY canvas, below every card of the files graph — where a right-click opens the pane's menu
    and a wire's release opens the palette."""
    boxes = [page.locator(node_root(name)).bounding_box() for name in FILES_PLACES]
    point = (boxes[-1]["x"] + 40, max(box["y"] + box["height"] for box in boxes) + 60)
    hit = page.evaluate(
        "([x, y]) => document.elementFromPoint(x, y)?.classList.contains('react-flow__pane')", list(point)
    )
    assert hit is True, point
    return point


class TestACardOfItsOwn:
    """The user's case on the built page: right-click empty canvas ▸ Add Node ▸ ClassNamesScan on the files graph.
    Measured before: a toast ``add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at …:12:7:
    cannot wire stream.ops[1].object (object) into stream.ops[1] (Op) …`` with nothing selected, ``a new flow step needs
    a name — after '<host>', it has none`` with the host card selected. Now a ``class_names_scan`` card where the
    canvas was clicked, its line above the root's comment, no toast, nothing for Check graph. And a wire dragged out of
    the root's ``classes`` and dropped on empty canvas, ClassNamesOutput picked: the card and its wire, ONE edit, one
    undo."""

    def _open(self, open_graph: Callable[[str], Opened], graph_dir: Path) -> Opened:
        (graph_dir / "files.yaml").write_text(FILES_TEMPLATE_TEXT)
        layout.write_sidecar(graph_dir / "files.layout.yaml", FILES_PLACES)
        opened = open_graph("files")
        wait_edges(opened.page, 5)
        return opened

    def _add_node(self, page: Any, point: Tuple[float, float], class_path: str, search: str) -> None:
        page.mouse.click(*point, button="right")
        page.locator('[data-context-menu="pane"] [data-submenu="add-node"]').click()
        wait_palette_loaded(page)
        page.locator("[data-add-node-search]").fill(search)
        page.locator(f'[data-add-node] [data-class="{class_path}"]').click()

    def test_add_node_writes_the_card_above_the_roots_comment_and_draws_it_where_the_canvas_was_clicked(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = self._open(open_graph, graph_dir)
        page = opened.page
        point = empty_point(page)
        self._add_node(page, point, SCAN_CLASS, "ClassNamesScan")
        wait_saves(page, 1)
        page.wait_for_selector(node_root("class_names_scan"))
        body = api_get(server, "api/graphs/files")
        assert body["text"] == with_card_above_the_root(
            FILES_TEMPLATE_TEXT, f"class_names_scan: !class:{SCAN_CLASS} {{}}"
        )
        at = flow_point(page, point)
        assert body["positions"]["class_names_scan"] == [pytest.approx(at[0], abs=1), pytest.approx(at[1], abs=1)]
        assert page.locator(node_root("class_names_scan")).get_attribute("data-kind") == "value"
        assert page.locator("[data-toast]").count() == 0
        assert api_get(server, "api/graphs/files/check") == {"problems": []}
        assert opened.errors == []

    def test_with_the_host_card_selected_the_same_card(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = self._open(open_graph, graph_dir)
        page = opened.page
        point = empty_point(page)
        page.locator(f'{node_root("<host>")} [data-host-title]').click()
        page.wait_for_selector('.app[data-selection="<host>"]')
        self._add_node(page, point, SCAN_CLASS, "ClassNamesScan")
        wait_saves(page, 1)
        page.wait_for_selector(node_root("class_names_scan"))
        assert api_get(server, "api/graphs/files")["text"] == with_card_above_the_root(
            FILES_TEMPLATE_TEXT, f"class_names_scan: !class:{SCAN_CLASS} {{}}"
        )
        assert page.locator("[data-toast]").count() == 0
        assert opened.errors == []

    def test_a_wire_from_the_roots_classes_dropped_on_empty_canvas_picks_a_wired_card_in_one_edit(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = self._open(open_graph, graph_dir)
        page = opened.page
        point = empty_point(page)
        drag(page, centre(handle(page, "contract", "classes", "in").bounding_box()), point)
        page.locator("[data-palette]").wait_for()
        wait_palette_loaded(page)
        page.locator("[data-palette-search]").fill("ClassNamesOutput")
        page.locator(f'[data-palette-list] [data-class="{NAMES_CLASS}"]').click()
        wait_saves(page, 1)
        wait_edges(page, 6)
        assert edge(page, "class_names_output", "class_names", "contract", "classes").count() == 1
        body = api_get(server, "api/graphs/files")
        wired = with_card_above_the_root(FILES_TEMPLATE_TEXT, f"class_names_output: !class:{NAMES_CLASS} {{}}")
        assert body["text"] == wired + "    classes: !ref:class_names_output\n"
        at = flow_point(page, point)
        assert body["positions"]["class_names_output"] == [pytest.approx(at[0], abs=1), pytest.approx(at[1], abs=1)]
        assert page.locator("[data-toast]").count() == 0
        page.keyboard.press("Control+z")  # ONE undo takes the card and its wire
        wait_saves(page, 2)
        wait_edges(page, 5)
        assert api_get(server, "api/graphs/files")["text"] == FILES_TEMPLATE_TEXT
        assert opened.errors == []


#: A knob at the top of the graph, read by a card's parameter.
KNOB_TEXT = """\
level: 0.25
mask: !class:recordstream.ops.numpy.Threshold
  low_level: !ref:level
"""


class TestAValueReadFromATopLevelKey:
    """``low_level: !ref:level`` under ``level: 0.25`` is a field naming the key and its value, not typed in — on the
    card and in the settings tab. Measured before: an open socket, and ``names no node`` in the problems."""

    def test_the_field_names_the_key_and_its_value_and_is_not_a_box(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        (graph_dir / "knob.yaml").write_text(KNOB_TEXT)
        opened = open_graph("knob")
        page = opened.page
        on_card = page.locator('.react-flow__node[data-id="mask"] code[data-param="low_level"]')
        assert on_card.inner_text() == "level = 0.25"
        assert on_card.get_attribute("data-ref") == "level"
        assert on_card.get_attribute("title") == "set by the top-level key level — change it in the text tab"
        assert page.locator('.react-flow__node[data-id="mask"] input[data-param="low_level"]').count() == 0
        page.locator('[data-tab="settings"]').click()
        page.locator("[data-settings-all]").check()
        page.wait_for_selector('[data-settings][data-settings-mode="all"]')
        assert page.locator('[data-form-node="mask"] code[data-param="low_level"]').inner_text() == "level = 0.25"
        assert api_get(server, "api/graphs/knob")["model"]["problems"] == []
        assert opened.errors == []


class TestThePicturesOfATracedNode:
    """An entry that is a raster is shown as a picture under its summary (user pick A, 2026-11-25): what the node made
    is open, the rest waits behind "show", and a click opens every pixel of it in a dialog."""

    LOADED = "img => img.complete && img.naturalWidth"

    def _inspect(self, open_graph: Callable[[str], Opened]) -> Opened:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="run"]').click()
        wait_answers(page, 1)
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        page.locator('[data-inspect="stream.ops[1]"]').wait_for()
        return opened

    def _wait_loaded(self, page: Any, selector: str) -> int:
        page.wait_for_function(
            "s => { const i = document.querySelector(s); return i !== null && i.complete && i.naturalWidth > 0; }",
            arg=selector,
        )
        return int(page.locator(selector).evaluate("img => img.naturalWidth"))

    def test_what_the_node_made_is_shown_and_what_it_was_handed_waits_behind_show(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = self._inspect(open_graph)
        page = opened.page
        made = '[data-inspect="stream.ops[1]"] [data-summary="output"] img[data-picture="mask"]'
        assert self._wait_loaded(page, made) == 4  # the 4 x 4 mask the threshold wrote
        handed = page.locator('[data-inspect="stream.ops[1]"] [data-summary="input"]')
        assert handed.locator("img").count() == 0
        toggle = handed.locator('[data-picture-toggle="image"]')
        assert toggle.inner_text() == "show"
        toggle.click()
        assert (
            self._wait_loaded(page, '[data-inspect="stream.ops[1]"] [data-summary="input"] img[data-picture="image"]')
            == 4
        )
        assert toggle.inner_text() == "hide"
        toggle.click()
        assert handed.locator("img").count() == 0
        label = page.locator('[data-inspect="stream.ops[1]"] tr[data-side="input"][data-entry="class"]')
        assert label.locator("[data-picture-toggle]").count() == 0  # a label has no picture
        assert opened.errors == []

    def test_a_click_on_a_picture_opens_every_pixel_of_it_in_a_dialog(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = self._inspect(open_graph)
        page = opened.page
        made = '[data-inspect="stream.ops[1]"] [data-summary="output"] img[data-picture="mask"]'
        self._wait_loaded(page, made)
        page.locator(made).click()
        assert self._wait_loaded(page, '[data-dialog="picture"] [data-picture-full] img') == 4
        assert "stream.ops[1] · output · mask" in page.locator('[data-dialog="picture"] .modal-title').inner_text()
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-dialog="picture"]', state="detached")
        assert opened.errors == []


#: A subgraph whose steps are a top-level key (``steps: !ref:prep``): the run traces INTO it, the canvas draws no card
#: for ``grey`` and ``mask``.
REFSTEPS_TEXT = f"""\
prep:
  grey:
    op: !class:recordstream.ops.numpy.Scale {{source_min: 0.0, source_max: 1.0, target_min: 0.0, target_max: 2.0}}
  mask:
    op: !class:recordstream.ops.numpy.Threshold {{low_level: 0.5}}
    from: grey

stream: !class:recordstream.core.stream.Stream
  source: !class:{MOD}.TinySource {{size: 4}}
  ops:
    - !class:recordstream.flow.subgraph.Subgraph
      steps: !ref:prep
"""


class TestARowOfTheRunTableInspectsItsStep:
    """A click on a row of the run table opens that step's entry (user pick A, 2026-11-25) — with or without a card
    on the canvas. Measured before: a step traced inside a subgraph whose steps are a ``!ref:`` had no card, so
    nothing could open its entry."""

    def _run(self, open_graph: Callable[[str], Opened], graph: str) -> Opened:
        opened = open_graph(graph)
        open_run_tab(opened.page)
        opened.page.locator('[data-action="run"]').click()
        wait_answers(opened.page, 1)
        return opened

    def test_a_row_opens_the_entry_of_a_step_that_has_a_card_without_selecting_the_card(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = self._run(open_graph, "pets")
        page = opened.page
        page.locator('[data-run-row="stream.ops[1]"]').click()
        inspected = page.locator('[data-inspect="stream.ops[1]"]')
        inspected.wait_for()
        assert inspected.locator('tr[data-side="output"][data-entry="mask"] [data-field="type"]').inner_text() == "Mask"
        assert inspected.locator("[data-rerun-form]").count() == 1  # it has a card: its values can be tried
        assert page.locator('[data-run-row="stream.ops[1]"]').get_attribute("data-inspected") == "true"
        assert selected_nodes(page) == []  # the selection is where the next run pauses: a row leaves it alone
        assert page.locator('[data-action="run"]').inner_text() == "run"
        assert opened.errors == []

    def test_a_row_opens_the_entry_of_a_step_the_canvas_draws_no_card_for(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "refsteps.yaml").write_text(REFSTEPS_TEXT)
        opened = self._run(open_graph, "refsteps")
        page = opened.page
        rows = page.locator("[data-run-row]").evaluate_all("rs => rs.map(r => r.dataset.runRow)")
        assert rows == ["stream.ops[0]", "stream.ops[0]/grey", "stream.ops[0]/mask"]
        page.locator('[data-run-row="stream.ops[0]/mask"]').click()
        inspected = page.locator('[data-inspect="stream.ops[0]/mask"]')
        inspected.wait_for()
        assert inspected.locator('tr[data-side="output"][data-entry="mask"] [data-field="type"]').inner_text() == "Mask"
        page.wait_for_function(
            "() => { const i = document.querySelector('[data-inspect] [data-summary=output] img[data-picture=mask]');"
            " return i !== null && i.complete && i.naturalWidth === 4; }"
        )
        assert inspected.locator("[data-rerun-form]").count() == 0  # no card: nothing to try a value on
        assert inspected.locator('[data-action="rerun"]').count() == 0
        page.locator('[data-run-row="stream.ops[0]/grey"]').press("Enter")  # the keyboard opens a row too
        page.locator('[data-inspect="stream.ops[0]/grey"]').wait_for()
        assert opened.errors == []


class TestTheSidebarsAreSizable:
    """Each sidebar's edge that faces the canvas is dragged to change its width (user request 2026-11-26); the widths
    are remembered in the browser and a double click puts one back. Measured before: 300 px and 380 px, fixed."""

    @staticmethod
    def _width(page: Any, selector: str) -> int:
        return round(page.locator(selector).bounding_box()["width"])

    @staticmethod
    def _drag(page: Any, sidebar: str, by: int) -> None:
        box = page.locator(f'[data-size-edge="{sidebar}"]').bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + 300
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + by / 2, y)
        page.mouse.move(x + by, y)
        page.mouse.up()

    def test_a_dragged_edge_changes_the_width_which_a_reload_keeps_and_a_double_click_puts_back(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator('button[data-command="toggle-nodes"]').click()
        page.locator("[data-nodes-sidebar]").wait_for()
        assert (self._width(page, "[data-nodes-sidebar]"), self._width(page, "aside.side")) == (300, 380)

        self._drag(page, "nodes", 120)
        self._drag(page, "side", -200)
        assert (self._width(page, "[data-nodes-sidebar]"), self._width(page, "aside.side")) == (420, 580)
        assert self._width(page, ".canvas") == 1400 - 420 - 580  # the canvas takes the rest

        page.reload()
        page.locator('.app[data-ready="true"]').wait_for()
        page.locator("[data-nodes-sidebar]").wait_for()
        assert (self._width(page, "[data-nodes-sidebar]"), self._width(page, "aside.side")) == (420, 580)

        page.locator('[data-size-edge="side"]').dblclick()
        page.locator('[data-size-edge="nodes"]').dblclick()
        assert (self._width(page, "[data-nodes-sidebar]"), self._width(page, "aside.side")) == (300, 380)
        assert page.evaluate("() => Object.keys(localStorage).filter(k => k.endsWith('.width'))") == []
        assert opened.errors == []

    def test_a_drag_stops_at_the_limits(self, open_graph: Callable[[str], Opened]) -> None:
        opened = open_graph("pets")
        page = opened.page
        self._drag(page, "side", 350)  # narrower than a sidebar may be
        assert self._width(page, "aside.side") == 200
        self._drag(page, "side", -1300)  # wider than the canvas allows
        assert self._width(page, "aside.side") == 1400 - 240
        assert self._width(page, ".canvas") == 240
        assert opened.errors == []

    def test_the_settings_only_sheet_has_no_edge(self, browser: Any, hosted: Any) -> None:
        opened = open_hosted(browser, hosted, "graph=pane&embed=1&only=settings")
        opened.page.wait_for_selector('.app[data-graph="pane"][data-ready="true"] [data-settings]')
        assert opened.page.locator("[data-size-edge]").count() == 0
        assert opened.errors == []


#: Lets a `TinyGatedSink`'s writes through — a test holds a full run at its first record with it.
RUN_GATE = threading.Event()


@configurable(category="source", group="canvas-test")
class TinyRows:
    """Five records that can be counted and indexed: what a full run walks."""

    def __len__(self) -> int:
        return 5

    def __getitem__(self, index: int) -> Record:
        return {"image": Image(np.full((2, 2), float(index), dtype=np.float32))}

    def __iter__(self) -> Iterator[Record]:
        return (self[index] for index in range(len(self)))


@configurable(category="sink", group="canvas-test")
class TinyGatedSink:
    """Takes each record once ``RUN_GATE`` is set.

    Args:
        gated: Whether a write waits for the gate.
    """

    def __init__(self, gated: bool = False) -> None:
        self.gated = gated

    def write(self, record: Record) -> None:
        if self.gated:
            assert RUN_GATE.wait(timeout=20)

    def flush(self) -> None:
        pass


def _convert_text(gated: bool) -> str:
    return f"""\
stop: 5

small: !scope:small
  stop: 2

runnable: !class:recordstream.processing.DatasetProcessor
  stop: !ref:stop
  stream: !class:recordstream.core.stream.Stream
    source: !class:{MOD}.TinyRows {{}}
  sink: !class:{MOD}.TinyGatedSink {{gated: {str(gated).lower()}}}
"""


class TestTheFullRun:
    """A graph that holds a ``runnable:`` is run WHOLE from the editor (user request 2026-11-25): the plan first,
    then one job with its count and Stop — in the run tab, and in a strip under the bar in every tab."""

    def test_the_plan_is_shown_a_scope_changes_it_and_the_run_ends_with_its_count(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "convert.yaml").write_text(_convert_text(gated=False))
        opened = open_graph("convert")
        page = opened.page
        open_run_tab(page)
        page.locator('[data-action="plan"]').click()
        page.locator('[data-run-all][data-plan="shown"]').wait_for()
        assert (
            "records: 5, start: 0, to_build: 5, sink: TinyGatedSink" in page.locator("[data-plan-lines]").inner_text()
        )
        page.locator('[data-scope="small"]').check()
        page.wait_for_function("() => document.querySelector('[data-plan-lines]')?.innerText.includes('to_build: 2')")
        page.locator('[data-scope="small"]').uncheck()
        page.wait_for_function("() => document.querySelector('[data-plan-lines]')?.innerText.includes('to_build: 5')")
        page.locator('[data-action="start-run"]').click()
        ending = page.locator("[data-run-all] [data-job-ending]")
        ending.wait_for()
        assert ending.get_attribute("data-job-status") == "done"
        assert ending.inner_text().startswith("convert: 5 records run (")
        assert page.locator("[data-run-all]").get_attribute("data-plan") == "closed"
        assert page.locator("[data-job-strip]").count() == 0  # the strip is there only while a run is going
        assert opened.errors == []

    def test_the_bars_button_opens_the_plan_and_the_strip_stops_the_run_from_another_tab(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "convert.yaml").write_text(_convert_text(gated=True))
        RUN_GATE.clear()
        opened = open_graph("convert")
        page = opened.page
        try:
            page.locator('button[data-command="run-all"]').click()
            page.locator('[data-run-all][data-plan="shown"]').wait_for()  # the run tab opened on the plan
            assert page.locator(".app").get_attribute("data-side-tab") == "run"
            page.locator('[data-action="start-run"]').click()
            page.locator('[data-run-all] [data-job-status="running"]').wait_for()
            assert page.locator('button[data-command="run-all"]').is_disabled()  # one run at a time
            page.locator('[data-tab="text"]').click()
            strip = page.locator("[data-job-strip]")
            strip.wait_for()
            assert strip.locator(".job-title").inner_text() == "convert"
            strip.locator('[data-action="stop-run"]').click()
            page.wait_for_function(
                "() => document.querySelector('[data-job-strip] [data-action=stop-run]')?.innerText === 'stopping…'"
            )
        finally:
            RUN_GATE.set()
        strip.wait_for(state="detached")
        open_run_tab(page)
        ending = page.locator("[data-run-all] [data-job-ending]")
        ending.wait_for()
        assert ending.get_attribute("data-job-status") == "stopped"
        assert ending.inner_text().startswith("convert: stopped after 1 of 5 records — what was written is kept (")
        assert opened.errors == []

    def test_unsaved_changes_are_refused_in_the_plan(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        (graph_dir / "convert.yaml").write_text(_convert_text(gated=False))
        opened = open_graph("convert")
        page = opened.page
        api_put(
            page.url.split("/?")[0],
            "api/graphs/convert",
            {"text": _convert_text(gated=False).replace("stop: 5", "stop: 4")},
        )
        open_run_tab(page)
        page.locator('[data-action="plan"]').click()
        refused = page.locator("[data-plan-error]")
        refused.wait_for()
        assert refused.inner_text() == (
            "convert.yaml holds unsaved changes — save the graph first: a full run reads the saved file"
        )
        assert page.locator('[data-action="start-run"]').is_disabled()
        assert opened.errors == [REFUSED_400]

    def test_a_graph_without_a_runnable_greys_both_buttons_and_says_why(
        self, open_graph: Callable[[str], Opened]
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        open_run_tab(page)
        why = "this graph has no runnable: — a full run runs the object under that key"
        assert page.locator('[data-action="plan"]').is_disabled()
        assert page.locator('button[data-command="run-all"]').is_disabled()
        assert page.locator('button[data-command="run-all"]').get_attribute("title") == why
        assert page.locator(".run-all-why").inner_text() == why
        assert opened.errors == []

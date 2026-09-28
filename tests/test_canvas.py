"""The canvas page (``frontend/``) against the real routes, driven by headless chromium.

The page is a drawing of the document (``docs/rewrite.md`` §0, §2.1), so every assertion here compares
the DOM with what the routes answer for the SAME temporary graph directory: the nodes and wires of
``GET /api/graphs/<name>`` are what is rendered, a handle's type is the socket's type, a widget edit
lands in the YAML the route serves, a node drag lands in the sidecar, and the text after edits is what
:class:`streamstudio.document.Document` produces from the file. The synthetic documents are the shape
``test_app.py`` uses — recordstream's own numpy ops plus a few tiny classes defined HERE and spelled by
the module name pytest imported this file under.

What the drags pin, and the rule they measure: a drag between sockets whose types do not meet (an
``Image`` output onto a ``Label`` input) adds no edge — React Flow's ``isValidConnection`` refuses it
while dragging; a drag between two ``Image`` sockets adds ONE edge, and that edge is NOT written to
the document. Phase 3 part 1 draws the wires the document has and writes PARAMETERS, POSITIONS, NAMES
and STEPS; no route yet spells a new wire (a Stream's record wires ARE its ``ops:`` order, a flow's are
``from:`` / ``bind:``, a contract's are ``!ref:``), so the drawn wire stays on the canvas as a dashed
PROPOSAL, a toast names the spelling the document would need, and the file is byte-identical.

The tests read the SPEC through ``data-*`` attributes, never DOM classes, wherever the page can carry
it: ``data-node`` / ``data-kind`` / ``data-class`` on a node, ``data-node`` / ``data-socket`` /
``data-type`` / ``data-kind`` on a handle, ``data-param`` on a widget, ``data-problem`` in the status
bar, ``data-toast`` on a toast, ``data-saves`` (accepted writes so far) and ``data-ready`` on the app
root. Only an EDGE has no attribute of its own (React Flow renders it), so edges are counted by the
``react-flow__edge`` element and a proposal by the ``wire-proposal`` class the page puts on it.

Skipped, with the reason, when the page is not built (no ``frontend/dist/index.html``) or headless
chromium is not installed — the build is ``cd streamstudio/frontend && npm install && npm run build``.
"""

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Tuple

import numpy as np
import pytest
from confluid import configurable
from recordstream import Algorithm, Image, Input, Label, Output, Record
from werkzeug.serving import make_server

from streamstudio import layout
from streamstudio.app import create_app
from streamstudio.document import Document

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (pip install playwright)"
)

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
DIST = FRONTEND / "dist"
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
def server(graph_dir: Path) -> Iterator[str]:
    """``create_app`` over the graph directory, served on a free port by werkzeug in a daemon thread."""
    httpd = make_server("127.0.0.1", 0, create_app(graph_dir), threaded=True)
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


def wait_palette_loaded(page: Any) -> None:
    """The page fetches ``GET api/palette`` once in the background; ``data-palette-ready`` says it has arrived, so
    a measured open time is the palette's own and never the fetch's."""
    page.wait_for_selector('.app[data-palette-ready="true"]')


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
        assert opened.page.locator(".react-flow__edge.wire-proposal").count() == 0

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
# wires: the types are tested while dragging; a drawn wire is a proposal, never a write
# ------------------------------------------------------------------------------------------------


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

    def test_a_drag_between_images_adds_a_dashed_proposal_that_is_not_written(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        before = edge_count(opened.page)
        source = centre(handle(opened.page, "decode", "image", "out").bounding_box())
        target = centre(handle(opened.page, "boost", "image", "in").bounding_box())
        drag(opened.page, source, target)
        assert edge_count(opened.page) == before + 1
        assert opened.page.locator(".react-flow__edge.wire-proposal").count() == 1
        toast = opened.page.locator("[data-toast]")
        toast.wait_for()
        message = toast.inner_text()
        assert "wires.yaml" in message
        assert "decode.image" in message and "boost.image" in message
        assert "from: decode" in message  # the spelling a flow document would need
        assert "not written" in message
        # nothing reached the document: the file, the served text and the served wires are as before
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        body = api_get(server, "api/graphs/wires")
        assert body["text"] == WIRES_TEXT
        assert len(body["model"]["wires"]) == before
        assert opened.errors == []

    def test_a_proposal_on_a_stream_names_the_ops_order_as_the_reason(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        before = edge_count(opened.page)
        source = centre(handle(opened.page, "stream.ops[0]", "record", "out").bounding_box())
        target = centre(handle(opened.page, "stream.ops[2]", "record", "in").bounding_box())
        drag(opened.page, source, target)
        assert edge_count(opened.page) == before + 1
        message = opened.page.locator("[data-toast]").inner_text()
        assert "pets.yaml" in message and "ops" in message and "order" in message
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT


# ------------------------------------------------------------------------------------------------
# widgets: a change is POST .../param; the served YAML carries it; a refusal is a toast and a revert
# ------------------------------------------------------------------------------------------------


class TestWidgets:
    def test_editing_a_number_widget_writes_the_value_into_the_yaml_and_keeps_the_comment(
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
        assert (graph_dir / "pets.yaml").read_text() == text
        assert text.count("\n") == PETS_TEXT.count("\n")  # one line changed, none added
        assert opened.page.locator("[data-yaml]").inner_text().rstrip("\n") == text.rstrip("\n")
        assert widget.input_value() == "0.9"
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
        served = api_get(server, "api/graphs/pets")["text"]
        document = Document.open(graph_dir / "pets.yaml")
        assert served == document.text == (graph_dir / "pets.yaml").read_text()
        assert document.model.node("stream.ops[1]").params["low_level"].value == 0.75
        assert document.check() == []


# ------------------------------------------------------------------------------------------------
# positions: a drag is PUT .../positions; a reload draws the node where the sidecar says
# ------------------------------------------------------------------------------------------------


class TestPositions:
    def test_dragging_a_node_saves_its_position_and_a_reload_restores_it(
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
        wait_palette_loaded(opened.page)
        specs = api_get(server, "api/palette")
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
        wait_palette_loaded(opened.page)
        opened.page.locator(f'{node_root("gain")} .title').click()
        assert selected_nodes(opened.page) == ["gain"]
        edges_before = edge_count(opened.page)
        opened.page.mouse.dblclick(*EMPTY)
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
        assert (graph_dir / "wires.yaml").read_text() == text
        opened.page.wait_for_selector(node_root("threshold"))
        wires = api_get(server, "api/graphs/wires")["model"]["wires"]
        assert len(wires) > edges_before
        wait_edges(opened.page, len(wires))
        assert opened.errors == []

    def test_picking_with_nothing_selected_adds_after_the_flows_last_step(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        wait_palette_loaded(opened.page)
        opened.page.mouse.dblclick(*EMPTY)
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        assert "after count" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.locator("[data-palette-list] li").first.click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/wires")["text"]
        assert "  threshold:\n    op: !class:recordstream.ops.numpy.Threshold {}\n    from: count\n" in text
        assert text.endswith("outputs: threshold\n")
        assert opened.errors == []

    def test_picking_on_a_stream_is_refused_with_the_servers_message(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        wait_palette_loaded(opened.page)
        opened.page.mouse.dblclick(*EMPTY)
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        assert "after stream.ops[2]" in opened.page.locator("[data-palette-meta]").inner_text()
        opened.page.locator("[data-palette-list] li").first.click()
        toast = opened.page.locator('[data-toast="error"]')
        toast.wait_for()
        assert "'stream.ops[2]' is not a flow step (the steps are: [])" in toast.inner_text()
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.page.locator(".app").get_attribute("data-saves") == "0"
        assert opened.errors == [
            "console.error: Failed to load resource: the server responded with a status of 400 (BAD REQUEST)"
        ]

    def test_a_wire_dropped_on_empty_canvas_lists_the_classes_with_a_compatible_input_and_the_pick_wires_it(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        wait_palette_loaded(opened.page)
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        edges_before = edge_count(opened.page)
        source = centre(handle(opened.page, "decode", "image", "out").bounding_box())
        drag(opened.page, source, EMPTY)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        expected = [spec for spec in specs if any(meets("Image", socket["type"], types) for socket in spec["inputs"])]
        assert rows.count() == len(expected) >= 2  # TinyGain and TinyClip declare an Image entry
        meta = opened.page.locator("[data-palette-meta]").inner_text()
        assert "Image" in meta and "after decode" in meta
        assert edge_count(opened.page) == edges_before  # a drop on nothing is neither an edge nor a proposal
        assert opened.page.locator("[data-toast]").count() == 0
        opened.page.locator(f'[data-palette-list] li[data-class="{MOD}.TinyGain"]').click()
        opened.page.wait_for_selector('.app[data-saves="1"]')
        text = api_get(server, "api/graphs/wires")["text"]
        assert f"  tinygain:\n    op: !class:{MOD}.TinyGain {{}}\n    from: decode\n" in text
        assert f"  gain:\n    op: !class:{MOD}.TinyGain {{gain: 2.0}}\n    from: tinygain\n" in text
        assert (graph_dir / "wires.yaml").read_text() == text
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
        wait_palette_loaded(opened.page)
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        target = centre(handle(opened.page, "boost", "image", "in").bounding_box())
        drag(opened.page, target, EMPTY)
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

    def test_a_wire_dropped_on_a_stream_canvas_lists_record_takers_and_the_pick_is_refused(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        wait_palette_loaded(opened.page)
        specs = api_get(server, "api/palette")
        types = api_get(server, "api/types")
        source = centre(handle(opened.page, "stream.ops[2]", "record", "out").bounding_box())
        drag(opened.page, source, EMPTY)
        rows = opened.page.locator("[data-palette-list] li")
        rows.first.wait_for()
        expected = [spec for spec in specs if any(meets("Record", socket["type"], types) for socket in spec["inputs"])]
        assert rows.count() == min(len(expected), PALETTE_ROWS) > 50  # every op takes a record
        opened.page.locator("[data-palette-search]").fill("recordstream.ops.numpy.Threshold")
        opened.page.locator("[data-palette-list] li").first.click()
        toast = opened.page.locator('[data-toast="error"]')
        toast.wait_for()
        assert "'stream.ops[2]' is not a flow step (the steps are: [])" in toast.inner_text()
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert opened.page.locator(".app").get_attribute("data-saves") == "0"


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

    def test_the_toolbar_groups_the_selection_and_the_title_and_colour_are_edited_on_the_group(
        self, open_graph: Callable[[str], Opened], server: str
    ) -> None:
        opened = open_graph("wires")
        assert opened.page.locator('[data-action="group"]').is_disabled()
        box_select(opened.page, "decode", "gain")
        opened.page.locator('[data-action="group"]').click()
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
        """The split, pinned: the MOVE is the page's (the sidecar gets its old position back, the document is not
        touched), the EDIT is the server's (the text comes back from the document's own history, the sidecar is
        not touched) — and one Ctrl+Z reverses whichever came last."""
        opened = open_graph("pets")
        page = opened.page
        sidecar = graph_dir / "pets.layout.yaml"
        widget = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        widget.fill("0.9")
        widget.press("Enter")
        wait_saves(page, 1)
        edited = (graph_dir / "pets.yaml").read_text()
        assert "low_level: 0.9}   # the mask" in edited
        before = tuple(api_get(server, "api/graphs/pets")["positions"]["stream.ops[2]"])
        start = centre(page.locator(f'{node_root("stream.ops[2]")} .title').bounding_box())
        drag(page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        wait_saves(page, 2)
        moved = (before[0] + DRAG[0], before[1] + DRAG[1])
        assert layout.read_sidecar(sidecar)["stream.ops[2]"] == moved

        page.keyboard.press("Control+z")  # the move came last: the page writes the old position back
        wait_saves(page, 3)
        assert layout.read_sidecar(sidecar)["stream.ops[2]"] == before
        assert node_translate(page, "stream.ops[2]") == before
        assert (graph_dir / "pets.yaml").read_text() == edited

        page.keyboard.press("Control+z")  # then the edit: the server's history gives the old text back
        wait_saves(page, 4)
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert widget.input_value() == "0.5"
        assert layout.read_sidecar(sidecar)["stream.ops[2]"] == before

        page.keyboard.press("Control+Shift+z")
        wait_saves(page, 5)
        assert (graph_dir / "pets.yaml").read_text() == edited
        assert widget.input_value() == "0.9"
        page.keyboard.press("Control+Shift+z")
        wait_saves(page, 6)
        assert layout.read_sidecar(sidecar)["stream.ops[2]"] == moved
        assert node_translate(page, "stream.ops[2]") == moved
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
        opened.page.keyboard.press("Control+z")
        wait_saves(opened.page, 1)
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert widget.input_value() == "0.5"
        assert opened.errors == []

    def test_the_toolbar_pair_undoes_and_redoes_a_parameter_edit(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        widget = page.locator(f'{node_root("gain")} input[data-param="gain"]')
        widget.fill("5")
        widget.press("Enter")
        wait_saves(page, 1)
        edited = (graph_dir / "wires.yaml").read_text()
        assert "{gain: 5}" in edited
        page.locator('[data-action="undo"]').click()
        wait_saves(page, 2)
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert widget.input_value() == "2"
        page.locator('[data-action="redo"]').click()
        wait_saves(page, 3)
        assert (graph_dir / "wires.yaml").read_text() == edited
        assert widget.input_value() == "5"
        assert opened.errors == []

    def test_with_nothing_to_undo_the_page_says_so_and_writes_nothing(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        opened.page.locator('[data-action="undo"]').click()
        toast = opened.page.locator('[data-toast="info"]')
        toast.wait_for()
        assert "wires.yaml" in toast.inner_text() and "nothing to undo" in toast.inner_text()
        assert opened.page.locator(".app").get_attribute("data-saves") == "0"
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        assert not (graph_dir / "wires.layout.yaml").exists()
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# copy / paste: a paste on a flow is add_step of the same class with the same values
# ------------------------------------------------------------------------------------------------


class TestCopyPaste:
    def test_paste_on_a_flow_adds_the_copied_class_with_its_values_after_the_selection(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        page.locator(f'{node_root("gain")} .title').click()
        page.keyboard.press("Control+c")
        page.locator(f'{node_root("boost")} .title').click()
        assert selected_nodes(page) == ["boost"]
        page.keyboard.press("Control+v")
        wait_saves(page, 1)
        text = (graph_dir / "wires.yaml").read_text()
        # `2.0` arrives as `2`: JSON has ONE number type, so a whole float cannot cross the browser as a float — the
        # widget path writes `5` for a typed 5 the same way. The constructor takes either (TinyGain(gain=2).gain == 2).
        assert f"  tinygain:\n    op: !class:{MOD}.TinyGain {{gain: 2}}\n    from: boost\n" in text
        assert f"  count:\n    op: !class:{MOD}.TinyCount {{}}\n    from: tinygain\n" in text
        assert api_get(server, "api/graphs/wires")["text"] == text
        page.wait_for_selector(node_root("tinygain"))
        assert opened.errors == []

    def test_paste_on_a_stream_is_refused_with_the_servers_message(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator(f'{node_root("stream.ops[1]")} .title').click()
        page.keyboard.press("Control+c")
        page.keyboard.press("Control+v")
        toast = page.locator('[data-toast="error"]')
        toast.wait_for()
        assert "'stream.ops[1]' is not a flow step (the steps are: [])" in toast.inner_text()
        assert (graph_dir / "pets.yaml").read_text() == PETS_TEXT
        assert page.locator(".app").get_attribute("data-saves") == "0"
        assert opened.errors == [REFUSED_400]


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


class TestArrange:
    def test_arrange_lays_the_chain_out_left_to_right_and_saves_it_and_ctrl_z_puts_it_back(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        before = api_get(server, "api/graphs/wires")["positions"]
        page.locator('[data-action="arrange"]').click()
        wait_saves(page, 1)
        placed = api_get(server, "api/graphs/wires")["positions"]
        xs = [placed[name][0] for name in ("decode", "gain", "boost", "count")]
        assert xs == sorted(xs) and len(set(xs)) == 4, placed
        assert all(x % 10 == 0 and y % 10 == 0 for x, y in placed.values())  # on the snap grid
        for name in WIRES_NODES:
            assert node_translate(page, name) == tuple(placed[name]), name
        boxes = list(node_boxes(page, *WIRES_NODES).values())
        assert not any(overlap(a, b) for i, a in enumerate(boxes) for b in boxes[i + 1 :])
        assert layout.read_sidecar(graph_dir / "wires.layout.yaml") == {k: tuple(v) for k, v in placed.items()}
        assert (graph_dir / "wires.yaml").read_text() == WIRES_TEXT
        page.keyboard.press("Control+z")  # an arrange is a layout change: the page writes the old places back
        wait_saves(page, 2)
        assert api_get(server, "api/graphs/wires")["positions"] == before
        assert opened.errors == []

    def test_the_server_layout_button_writes_the_documents_layered_layout(
        self, open_graph: Callable[[str], Opened], server: str, graph_dir: Path
    ) -> None:
        opened = open_graph("wires")
        page = opened.page
        start = centre(page.locator(f'{node_root("gain")} .title').bounding_box())
        drag(page, start, (start[0] + DRAG[0], start[1] + DRAG[1]))
        wait_saves(page, 1)
        page.locator('[data-action="arrange-server"]').click()
        wait_saves(page, 2)
        laid = layout.layered_layout(Document.open(graph_dir / "wires.yaml").model)
        assert api_get(server, "api/graphs/wires")["positions"] == {k: [x, y] for k, (x, y) in laid.items()}
        for name in WIRES_NODES:
            assert node_translate(page, name) == laid[name], name
        assert opened.errors == []


# ------------------------------------------------------------------------------------------------
# rename: double-click a title, Enter is POST .../rename
# ------------------------------------------------------------------------------------------------


class TestRename:
    def test_double_clicking_a_title_renames_the_node_in_one_edit_and_it_stays_where_it_was(
        self, open_graph: Callable[[str], Opened], graph_dir: Path
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
        text = (graph_dir / "wires.yaml").read_text()
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
        page.wait_for_selector("[data-settings]")
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
        self, open_graph: Callable[[str], Opened], graph_dir: Path
    ) -> None:
        opened = open_graph("pets")
        page = opened.page
        page.locator('[data-tab="settings"]').click()
        in_form = page.locator('[data-form-node="stream.ops[1]"] input[data-param="low_level"]')
        on_canvas = page.locator(f'{node_root("stream.ops[1]")} input[data-param="low_level"]')
        in_form.fill("0.8")
        in_form.press("Enter")
        wait_saves(page, 1)
        assert "low_level: 0.8}   # the mask" in (graph_dir / "pets.yaml").read_text()
        assert on_canvas.input_value() == "0.8"
        on_canvas.fill("0.6")
        on_canvas.press("Enter")
        wait_saves(page, 2)
        assert in_form.input_value() == "0.6"
        assert "low_level: 0.6}   # the mask" in (graph_dir / "pets.yaml").read_text()
        assert opened.errors == []

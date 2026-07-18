"""Tests for ``fluxstudio.__init__`` — entry-point discovery and the ComfyUI
directory-picker route.

Two surfaces live here, both awkward to reach because they run at *import*
time:

* ``_discover_and_register_nodes`` walks the ``confluid.configurables`` entry
  points and forwards each module to ``register_module_nodes`` (which derives the
  ``Taidal/<Package>/<Category>[/<group>]`` palette path — see bridge._node_category).
  We call it directly with ``entry_points`` / ``register_module_nodes`` mocked so
  the forwarding and the deliberately-quiet (``debug``, not ``warning``)
  skip-on-failure path are pinned down without depending on the installed
  entry-point set.
* The ``/taidal/choose_directory`` POST handler is only defined when ``import
  server`` succeeds — i.e. inside ComfyUI. Outside ComfyUI (the dev/CI venv)
  that import fails and the whole block is skipped, so the only way to exercise
  it is to inject a fake ``server`` + ``aiohttp`` into ``sys.modules`` and reload
  the package. ``_fake_comfy`` does that and captures the registered handler so
  its success / Cancel / generic-error branches can be driven directly.
"""

import asyncio
import importlib
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

import fluxstudio


@pytest.fixture(autouse=True)
def _reset_discovery_cache() -> Iterator[None]:
    """Clear the process-shared one-shot discovery cache before/after each test.

    ``_discover_and_register_nodes`` caches its result on the ``sys`` singleton so a
    second module identity (editable install + ComfyUI custom_nodes symlink) reuses
    it instead of re-scanning. That cache is populated during the real
    ``import fluxstudio`` at collection time, so the direct-call tests below must
    reset it to exercise a fresh scan.
    """
    sys.__dict__.pop("_fluxstudio_discovered_nodes", None)
    yield
    sys.__dict__.pop("_fluxstudio_discovered_nodes", None)


# --------------------------------------------------------------------------- #
# _discover_and_register_nodes
# --------------------------------------------------------------------------- #


def test_discover_returns_quietly_when_entry_points_fail() -> None:
    fake_logger = MagicMock()
    with patch.object(fluxstudio, "logger", fake_logger):
        with patch("importlib.metadata.entry_points", side_effect=RuntimeError("boom")):
            # Must not raise — a broken metadata read should not crash ComfyUI.
            fluxstudio._discover_and_register_nodes()
    fake_logger.error.assert_called_once()


def test_discover_forwards_module_to_register() -> None:
    """__init__ forwards the entry-point module, shared mappings, and dist_name so the [PKG]
    label tag is derived from the distribution name (not just the top-level Python package)."""
    fake_dist = SimpleNamespace(name="waivefront-helios")
    fake_ep = SimpleNamespace(value="waivefront.helios", dist=fake_dist)
    fake_register = MagicMock()
    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        with patch.object(fluxstudio, "register_module_nodes", fake_register):
            fluxstudio._discover_and_register_nodes()

    fake_register.assert_called_once()
    args, kwargs = fake_register.call_args
    assert args[0] == "waivefront.helios"
    assert args[1] is fluxstudio.NODE_CLASS_MAPPINGS
    assert args[2] is fluxstudio.NODE_DISPLAY_NAME_MAPPINGS
    # dist_name carries the distribution package name for the [PKG] label.
    assert kwargs.get("dist_name") == "waivefront-helios"
    # No category/root override — the bridge builds the path from the module + class tags.
    assert "category" not in kwargs
    assert "root" not in kwargs


def test_discover_forwards_none_dist_name_when_dist_absent() -> None:
    """When ep.dist is absent (e.g. in tests using SimpleNamespace without dist), dist_name=None."""
    fake_ep = SimpleNamespace(value="waivefront.sinks")  # no .dist attribute
    fake_register = MagicMock()
    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        with patch.object(fluxstudio, "register_module_nodes", fake_register):
            fluxstudio._discover_and_register_nodes()

    fake_register.assert_called_once()
    _, kwargs = fake_register.call_args
    assert kwargs.get("dist_name") is None


def test_discover_is_one_shot_per_process() -> None:
    """The second call reuses the ``sys``-cached mappings — it does NOT re-scan.

    FluxStudio's ``__init__`` runs twice (editable install + ComfyUI custom_nodes symlink); the
    scan/log must happen once while a second module identity still ends up with the full node set.
    """
    fake_ep = SimpleNamespace(value="waivefront.sinks")
    fake_register = MagicMock(side_effect=lambda mod, cmap, dmap, **kw: cmap.update({"Taidal_X": object()}))
    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        with patch.object(fluxstudio, "register_module_nodes", fake_register):
            fluxstudio._discover_and_register_nodes()  # first call: scans
            # Simulate the second module identity: its own (empty) mappings.
            fluxstudio.NODE_CLASS_MAPPINGS.clear()
            fluxstudio._discover_and_register_nodes()  # second call: must reuse cache, no re-scan

    fake_register.assert_called_once()  # scanned exactly once
    # ...yet the second identity's mappings were repopulated from the cache.
    assert "Taidal_X" in fluxstudio.NODE_CLASS_MAPPINGS


def test_discover_dedups_duplicate_entry_points() -> None:
    """A duplicate entry point (stale metadata) is scanned only once."""
    eps = [SimpleNamespace(value="waivefront.sinks"), SimpleNamespace(value="waivefront.sinks")]
    fake_register = MagicMock()
    with patch("importlib.metadata.entry_points", return_value=eps):
        with patch.object(fluxstudio, "register_module_nodes", fake_register):
            fluxstudio._discover_and_register_nodes()
    fake_register.assert_called_once()


def test_discover_skips_failing_module_at_debug_level() -> None:
    fake_ep = SimpleNamespace(value="broken.module")
    fake_logger = MagicMock()
    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        with patch.object(fluxstudio, "register_module_nodes", side_effect=ImportError("nope")):
            with patch.object(fluxstudio, "logger", fake_logger):
                fluxstudio._discover_and_register_nodes()
    # Per the mandate, a module that fails to scan is skipped quietly (debug),
    # never warning/info, so one bad package can't empty the palette.
    fake_logger.debug.assert_called_once()
    fake_logger.warning.assert_not_called()


# --------------------------------------------------------------------------- #
# _prune_default_comfy_nodes (the --no-default-nodes hide path)
# --------------------------------------------------------------------------- #


def _fake_comfy_nodes(mappings: dict, displays: dict) -> ModuleType:
    """A stand-in for ComfyUI's global ``nodes`` module carrying the two registry dicts."""
    mod = ModuleType("nodes")
    mod.NODE_CLASS_MAPPINGS = mappings  # type: ignore[attr-defined]
    mod.NODE_DISPLAY_NAME_MAPPINGS = displays  # type: ignore[attr-defined]
    return mod


def test_prune_default_comfy_nodes_keeps_only_custom_nodes() -> None:
    # A default node is told apart from a custom one by RELATIVE_PYTHON_MODULE (server.py defaults a
    # missing attr to "nodes"): built-in/extra/api nodes are pruned, custom_nodes (other extensions
    # + our own, merged in afterwards) survive.
    base = type("KSampler", (), {})  # no RELATIVE_PYTHON_MODULE -> treated as "nodes" (a built-in)
    extra = type("SaveImage", (), {"RELATIVE_PYTHON_MODULE": "comfy_extras.nodes_images"})
    api = type("ApiNode", (), {"RELATIVE_PYTHON_MODULE": "comfy_api_nodes.nodes_x"})
    other = type("ThirdParty", (), {"RELATIVE_PYTHON_MODULE": "custom_nodes.someext"})
    mappings = {"KSampler": base, "SaveImage": extra, "ApiNode": api, "ThirdParty": other}
    displays = {"KSampler": "KSampler", "SaveImage": "Save Image", "ApiNode": "Api", "ThirdParty": "Third"}

    fake_nodes = _fake_comfy_nodes(mappings, displays)
    with patch.dict(sys.modules, {"nodes": fake_nodes}):
        removed = fluxstudio._prune_default_comfy_nodes()
        # Idempotent: a second call (the other module identity) removes nothing.
        again = fluxstudio._prune_default_comfy_nodes()

    assert removed == 3
    assert again == 0
    assert set(mappings) == {"ThirdParty"}  # only the custom-node survives
    assert set(displays) == {"ThirdParty"}


def test_prune_default_comfy_nodes_spares_keep_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The keep-list (PreviewImage/PreviewAny always, + the config file's keep_nodes) survives the prune
    # even though those are built-ins — so the native viewers the user relies on stay available.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("keep_nodes: [MaskToImage]\n")
    monkeypatch.setenv("FLUXSTUDIO_CONFIG", str(cfg))
    preview_img = type("PreviewImage", (), {})  # built-in (default "nodes" module)
    preview_any = type("PreviewAny", (), {})  # built-in
    mask = type("MaskToImage", (), {"RELATIVE_PYTHON_MODULE": "comfy_extras.nodes_mask"})  # extra, but kept
    ksampler = type("KSampler", (), {})  # built-in, NOT kept
    mappings = {"PreviewImage": preview_img, "PreviewAny": preview_any, "MaskToImage": mask, "KSampler": ksampler}
    displays = {k: k for k in mappings}

    fake_nodes = _fake_comfy_nodes(mappings, displays)
    with patch.dict(sys.modules, {"nodes": fake_nodes}):
        removed = fluxstudio._prune_default_comfy_nodes()

    assert removed == 1  # only KSampler pruned
    assert set(mappings) == {"PreviewImage", "PreviewAny", "MaskToImage"}


def test_import_block_prunes_when_hide_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """With FLUXSTUDIO_HIDE_DEFAULT_NODES set, the in-ComfyUI import block prunes the default nodes."""
    monkeypatch.setenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", "1")
    base = type("KSampler", (), {})
    custom = type("Taidal_X", (), {"RELATIVE_PYTHON_MODULE": "custom_nodes.fluxstudio"})
    mappings = {"KSampler": base, "Taidal_X": custom}
    displays = {"KSampler": "KSampler", "Taidal_X": "X"}

    saved = {k: sys.modules.get(k) for k in ("server", "aiohttp", "nodes")}
    routes = _FakeRoutes(existing=False)
    fake_server = ModuleType("server")
    fake_server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))  # type: ignore[attr-defined]
    fake_aiohttp = ModuleType("aiohttp")
    fake_aiohttp.web = _FakeWeb()  # type: ignore[attr-defined]
    sys.modules["server"] = fake_server
    sys.modules["aiohttp"] = fake_aiohttp
    sys.modules["nodes"] = _fake_comfy_nodes(mappings, displays)
    try:
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)

    # The built-in KSampler was pruned from ComfyUI's global registry; the custom node stayed.
    assert set(mappings) == {"Taidal_X"}


def test_import_block_skips_prune_when_hide_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag not set): the import block leaves ComfyUI's global node registry untouched."""
    monkeypatch.delenv("FLUXSTUDIO_HIDE_DEFAULT_NODES", raising=False)
    base = type("KSampler", (), {})
    mappings = {"KSampler": base}
    displays = {"KSampler": "KSampler"}

    saved = {k: sys.modules.get(k) for k in ("server", "aiohttp", "nodes")}
    routes = _FakeRoutes(existing=False)
    fake_server = ModuleType("server")
    fake_server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))  # type: ignore[attr-defined]
    fake_aiohttp = ModuleType("aiohttp")
    fake_aiohttp.web = _FakeWeb()  # type: ignore[attr-defined]
    sys.modules["server"] = fake_server
    sys.modules["aiohttp"] = fake_aiohttp
    sys.modules["nodes"] = _fake_comfy_nodes(mappings, displays)
    try:
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)

    assert set(mappings) == {"KSampler"}  # nothing pruned — defaults stay visible


# --------------------------------------------------------------------------- #
# /taidal/choose_directory route handler
# --------------------------------------------------------------------------- #


class _FakeRoutes:
    """Stand-in for ``server.PromptServer.instance.routes``.

    It is both iterated (the ``route_exists`` pre-check) and used as a decorator
    factory (``.post(path)``), so it must support both. When ``existing`` is
    True it reports the route as already registered, exercising the skip branch.
    """

    def __init__(self, existing: bool) -> None:
        self._existing = existing
        self.handler: Any = None
        self.registered_path: Optional[str] = None
        self.post_handlers: dict = {}
        self.get_handler: Any = None
        self.get_path: Optional[str] = None
        self.get_handlers: dict = {}

    def __iter__(self) -> Iterator[SimpleNamespace]:
        # When ``existing`` is True, report ALL taidal routes as already registered so the
        # skip branch of every registration block is exercised (there are several POST/GET
        # routes now — choose_directory, export_ops, hf_datasets, node_specs).
        if self._existing:
            yield SimpleNamespace(path="/taidal/choose_directory", method="POST")
            yield SimpleNamespace(path="/taidal/export_ops", method="POST")
            yield SimpleNamespace(path="/taidal/export_ops_graph", method="POST")
            yield SimpleNamespace(path="/taidal/import_yaml", method="POST")
            yield SimpleNamespace(path="/taidal/export_workflow", method="POST")
            yield SimpleNamespace(path="/taidal/hf_datasets", method="GET")
            yield SimpleNamespace(path="/taidal/hf_dataset_features", method="GET")
            yield SimpleNamespace(path="/taidal/hf_dataset_splits", method="GET")
            yield SimpleNamespace(path="/taidal/node_specs", method="GET")

    def post(self, path: str):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            self.registered_path = path
            self.handler = fn
            self.post_handlers[path] = fn
            return fn

        return decorator

    def get(self, path: str):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            self.get_path = path
            self.get_handler = fn
            self.get_handlers[path] = fn
            return fn

        return decorator


class _FakeWeb:
    """Records ``json_response`` calls so handler outputs are assertable."""

    def __init__(self) -> None:
        self.calls: list = []

    def json_response(self, data, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((data, kwargs))
        return SimpleNamespace(data=data, kwargs=kwargs)


@contextmanager
def _fake_comfy(existing_route: bool = False) -> Iterator[tuple]:
    """Reload ``fluxstudio`` with a fake ComfyUI ``server`` + ``aiohttp`` present.

    ``entry_points`` is stubbed to ``[]`` during the reload so node discovery is
    a no-op (fast, side-effect-free); only the route-registration block is the
    subject under test. sys.modules and the package are restored on exit.
    """
    saved = {k: sys.modules.get(k) for k in ("server", "aiohttp")}

    routes = _FakeRoutes(existing_route)
    fake_server = ModuleType("server")
    fake_server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))  # type: ignore[attr-defined]
    fake_web = _FakeWeb()
    fake_aiohttp = ModuleType("aiohttp")
    fake_aiohttp.web = fake_web  # type: ignore[attr-defined]

    sys.modules["server"] = fake_server
    sys.modules["aiohttp"] = fake_aiohttp
    try:
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)
        yield routes, fake_web
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod
        with patch("importlib.metadata.entry_points", return_value=[]):
            importlib.reload(fluxstudio)


def test_route_registered_and_returns_selected_path() -> None:
    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers.get("/taidal/choose_directory")
        assert handler is not None

        with patch("subprocess.run", return_value=SimpleNamespace(stdout="/Users/me/data\n")) as run_mock:
            asyncio.run(handler(SimpleNamespace()))

        # The native macOS picker is invoked via osascript...
        assert run_mock.call_args.args[0][0] == "osascript"
        # ...and the trimmed POSIX path is returned to the frontend.
        assert web.calls[-1] == ({"path": "/Users/me/data"}, {})


def test_route_returns_empty_path_on_cancel() -> None:
    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers["/taidal/choose_directory"]
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "osascript")):
            asyncio.run(handler(SimpleNamespace()))
    # A CalledProcessError means the user clicked Cancel -> empty path, no error.
    assert web.calls[-1] == ({"path": ""}, {})


def test_route_returns_500_on_generic_error() -> None:
    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers["/taidal/choose_directory"]
        with patch("subprocess.run", side_effect=RuntimeError("kaboom")):
            asyncio.run(handler(SimpleNamespace()))
    data, kwargs = web.calls[-1]
    assert data == {"error": "kaboom"}
    assert kwargs == {"status": 500}


def test_route_not_re_registered_when_already_present() -> None:
    # If ComfyUI (or a prior import) already registered the routes, the blocks
    # must not define second handlers.
    with _fake_comfy(existing_route=True) as (routes, _web):
        assert routes.post_handlers == {}


def _json_request(body: Any) -> SimpleNamespace:
    """A fake aiohttp request whose ``await request.json()`` yields ``body``."""

    async def _json() -> Any:
        return body

    return SimpleNamespace(json=_json)


def test_export_ops_route_returns_yaml() -> None:
    # The /taidal/export_ops POST handler compiles the posted graph's ops into a Confluid
    # YAML op-list using the live NODE_CLASS_MAPPINGS.
    from sampleflux.ops.torch import RescaleOp

    from fluxstudio.bridge import create_comfy_node

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers.get("/taidal/export_ops")
        assert handler is not None
        fluxstudio.NODE_CLASS_MAPPINGS["Taidal_RescaleOp"] = create_comfy_node(RescaleOp, "Taidal_RescaleOp")
        prompt = {"1": {"class_type": "Taidal_RescaleOp", "inputs": {"in_min": 0.0, "in_max": 255.0}}}
        asyncio.run(handler(_json_request({"prompt": prompt})))
    data, _kwargs = web.calls[-1]
    assert "ops:" in data["yaml"]
    assert "RescaleOp" in data["yaml"]
    # A single chain → exactly one doc; `yaml` mirrors it for older clients of the route.
    assert data["yamls"] == [data["yaml"]]


def test_export_ops_graph_route_returns_op_workflow() -> None:
    # The /taidal/export_ops_graph POST handler extracts JUST the op-DAG nodes from the posted UI
    # workflow into a standalone workflow.json (the raw graph as drawn, no compilation).
    from sampleflux.ops.torch import RescaleOp

    from fluxstudio.bridge import create_comfy_node

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers.get("/taidal/export_ops_graph")
        assert handler is not None
        fluxstudio.NODE_CLASS_MAPPINGS["rescale"] = create_comfy_node(RescaleOp, "rescale")
        workflow = {
            "nodes": [
                {"id": 1, "type": "src", "inputs": []},  # a non-op node (unmapped) → dropped
                {"id": 2, "type": "rescale", "inputs": [{"name": "sample", "link": 1}]},
            ],
            "links": [[1, 1, 0, 2, 0, "SAMPLEFLUX_SOURCE"]],
        }
        asyncio.run(handler(_json_request({"workflow": workflow})))
    data, _kwargs = web.calls[-1]
    assert "workflow" in data
    assert [n["id"] for n in data["workflow"]["nodes"]] == [2]  # only the op node survives
    assert data["workflow"]["links"] == []  # the src→op link drops with the source
    assert data["workflow"]["nodes"][0]["inputs"][0]["link"] is None  # detached input


def test_import_yaml_route_returns_workflow() -> None:
    # The /taidal/import_yaml POST handler converts a posted YAML config into a workflow.json.
    from sampleflux.ops.torch import RescaleOp
    from sampleflux.sources import HuggingFaceSource

    from fluxstudio.bridge import create_comfy_node

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers.get("/taidal/import_yaml")
        assert handler is not None
        fluxstudio.NODE_CLASS_MAPPINGS["src"] = create_comfy_node(HuggingFaceSource, "src")
        fluxstudio.NODE_CLASS_MAPPINGS["rescale"] = create_comfy_node(RescaleOp, "rescale")
        yaml = "raw: !class:sampleflux.sources.HuggingFaceSource()\n  path: mnist\n"
        asyncio.run(handler(_json_request({"yaml": yaml, "as": "graph"})))
    data, _kwargs = web.calls[-1]
    assert "workflow" in data
    assert any(n["type"] == "src" for n in data["workflow"]["nodes"])


def test_export_workflow_route_returns_yaml() -> None:
    # The /taidal/export_workflow POST handler converts a whole workflow.json into hoisted YAML.
    from sampleflux.sources import HuggingFaceSource

    from fluxstudio.bridge import create_comfy_node
    from fluxstudio.graphio import yaml_to_workflow

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers.get("/taidal/export_workflow")
        assert handler is not None
        nm = {"src": create_comfy_node(HuggingFaceSource, "src")}
        fluxstudio.NODE_CLASS_MAPPINGS["src"] = nm["src"]
        workflow = yaml_to_workflow({"raw": __import__("confluid").Instance(HuggingFaceSource, path="mnist")}, nm)
        asyncio.run(handler(_json_request({"workflow": workflow})))
    data, _kwargs = web.calls[-1]
    assert "HuggingFaceSource" in data["yaml"]


def test_export_ops_route_multi_chain_returns_one_doc_per_chain(tmp_path: Path) -> None:
    # Two disconnected chains no longer 400: the route compiles EACH chain into its own YAML
    # doc and (with `path`) writes one stem-suffixed file per chain.
    from sampleflux.ops.torch import RescaleOp

    from fluxstudio.bridge import create_comfy_node

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers["/taidal/export_ops"]
        fluxstudio.NODE_CLASS_MAPPINGS["Taidal_RescaleOp"] = create_comfy_node(RescaleOp, "Taidal_RescaleOp")
        prompt = {
            "2": {"class_type": "Taidal_RescaleOp", "inputs": {"sample": ["x", 0], "in_min": 0.0, "in_max": 255.0}},
            "3": {"class_type": "Taidal_RescaleOp", "inputs": {"sample": ["y", 0], "in_min": 0.0, "in_max": 1.0}},
        }
        asyncio.run(handler(_json_request({"prompt": prompt, "path": str(tmp_path / "ops.yaml")})))
    data, _kwargs = web.calls[-1]
    assert len(data["yamls"]) == 2
    assert all("RescaleOp" in doc for doc in data["yamls"])
    assert data["yaml"] == data["yamls"][0]
    assert [Path(p).name for p in data["paths"]] == ["ops_1.yaml", "ops_2.yaml"]
    for written, doc in zip(data["paths"], data["yamls"]):
        assert Path(written).read_text() == doc


def test_export_ops_route_400_on_bad_topology() -> None:
    # A structurally-unexportable graph (a sample-wire CYCLE) -> UnsupportedTopologyError -> 400.
    # (A widget driven by a link is no longer an error — it's omitted with a note.)
    from sampleflux.ops.torch import RescaleOp

    from fluxstudio.bridge import create_comfy_node

    with _fake_comfy() as (routes, web):
        handler = routes.post_handlers["/taidal/export_ops"]
        fluxstudio.NODE_CLASS_MAPPINGS["Taidal_RescaleOp"] = create_comfy_node(RescaleOp, "Taidal_RescaleOp")
        prompt = {
            "2": {"class_type": "Taidal_RescaleOp", "inputs": {"sample": ["3", 0], "in_min": 0.0, "in_max": 255.0}},
            "3": {"class_type": "Taidal_RescaleOp", "inputs": {"sample": ["2", 0], "in_min": 0.0, "in_max": 1.0}},
        }
        asyncio.run(handler(_json_request({"prompt": prompt})))
    data, kwargs = web.calls[-1]
    assert "error" in data
    assert kwargs == {"status": 400}


def test_hf_datasets_route_lists_cached_datasets() -> None:
    # The /taidal/hf_datasets GET handler returns whatever list_cached_hf_datasets() yields,
    # which the HF dataset-id picker (web/hf_dataset_chooser.js) offers as options.
    with _fake_comfy() as (routes, web):
        handler = routes.get_handlers.get("/taidal/hf_datasets")
        assert handler is not None
        with patch.object(fluxstudio, "list_cached_hf_datasets", return_value=["cifar10", "mnist"]):
            asyncio.run(handler(SimpleNamespace()))
    data, _kwargs = web.calls[-1]
    assert data == {"datasets": ["cifar10", "mnist"]}


def test_list_cached_hf_datasets_filters_to_dataset_repos() -> None:
    ds = SimpleNamespace(repo_id="mnist", repo_type="dataset")
    model = SimpleNamespace(repo_id="bert-base", repo_type="model")
    fake_hub = ModuleType("huggingface_hub")
    fake_hub.scan_cache_dir = lambda: SimpleNamespace(repos=[model, ds])  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
        assert fluxstudio.list_cached_hf_datasets() == ["mnist"]


def test_list_cached_hf_datasets_degrades_to_empty_on_error() -> None:
    def _boom() -> Any:
        raise RuntimeError("no cache dir")

    fake_hub = ModuleType("huggingface_hub")
    fake_hub.scan_cache_dir = _boom  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
        # Must never raise — the picker is a convenience over a free-text field.
        assert fluxstudio.list_cached_hf_datasets() == []


def test_hf_dataset_features_route_lists_features() -> None:
    # The /taidal/hf_dataset_features GET handler reads the path/name/split query params and
    # returns whatever list_hf_dataset_features() yields (offered by web/hf_feature_chooser.js).
    with _fake_comfy() as (routes, web):
        handler = routes.get_handlers.get("/taidal/hf_dataset_features")
        assert handler is not None
        request = SimpleNamespace(query={"path": "mnist", "name": "", "split": "train"})
        with patch.object(fluxstudio, "list_hf_dataset_features", return_value=["image", "label"]) as feat_mock:
            asyncio.run(handler(request))
        # path is forwarded; blank name -> None; split forwarded.
        feat_mock.assert_called_once_with("mnist", name=None, split="train")
    data, _kwargs = web.calls[-1]
    assert data == {"features": ["image", "label"]}


def _fake_datasets(builder_features: Any = None, stream_rows: Any = None) -> ModuleType:
    """A fake ``datasets`` module for list_hf_dataset_features tests.

    ``builder_features`` backs ``load_dataset_builder().info.features`` (a dict, or None to
    force the streaming fallback); ``stream_rows`` backs the streaming ``load_dataset`` fallback.
    """
    mod = ModuleType("datasets")

    def _load_builder(path: Any, name: Any = None) -> Any:
        return SimpleNamespace(info=SimpleNamespace(features=builder_features))

    def _load_dataset(path: Any, name: Any = None, split: Any = None, streaming: bool = False) -> Any:
        if stream_rows is None:
            raise RuntimeError("no streaming dataset")
        return iter(stream_rows)

    mod.load_dataset_builder = _load_builder  # type: ignore[attr-defined]
    mod.load_dataset = _load_dataset  # type: ignore[attr-defined]
    return mod


def test_list_hf_dataset_features_empty_path_returns_empty() -> None:
    # No dataset id -> nothing to introspect; never touches `datasets`.
    assert fluxstudio.list_hf_dataset_features("") == []


def test_list_hf_dataset_features_from_builder_metadata() -> None:
    # The cheap path: builder.info.features carries the column schema (insertion order preserved).
    fake = _fake_datasets(builder_features={"image": object(), "label": object()})
    with patch.dict(sys.modules, {"datasets": fake}):
        assert fluxstudio.list_hf_dataset_features("mnist") == ["image", "label"]


def test_list_hf_dataset_features_streaming_fallback() -> None:
    # builder.info.features is None (e.g. an imagefolder) -> stream one example and read its keys.
    fake = _fake_datasets(builder_features=None, stream_rows=[{"img": 1, "lbl": 2}])
    with patch.dict(sys.modules, {"datasets": fake}):
        assert fluxstudio.list_hf_dataset_features("local/imgs", split="train") == ["img", "lbl"]


def test_list_hf_dataset_features_degrades_to_empty_on_error() -> None:
    # Both paths fail (no builder features, no streaming rows) -> [] and never raises.
    fake = _fake_datasets(builder_features=None, stream_rows=None)
    with patch.dict(sys.modules, {"datasets": fake}):
        assert fluxstudio.list_hf_dataset_features("nonexistent/dataset") == []


def test_hf_dataset_splits_route_lists_splits() -> None:
    # The /taidal/hf_dataset_splits GET handler reads the path/name query params and returns
    # whatever list_hf_dataset_splits() yields (offered by web/hf_split_chooser.js).
    with _fake_comfy() as (routes, web):
        handler = routes.get_handlers.get("/taidal/hf_dataset_splits")
        assert handler is not None
        request = SimpleNamespace(query={"path": "mnist", "name": ""})
        with patch.object(fluxstudio, "list_hf_dataset_splits", return_value=["train", "test"]) as split_mock:
            asyncio.run(handler(request))
        split_mock.assert_called_once_with("mnist", name=None)  # blank name -> None
    data, _kwargs = web.calls[-1]
    assert data == {"splits": ["train", "test"]}


def test_list_hf_dataset_splits_empty_path_returns_empty() -> None:
    # No dataset id -> nothing to introspect; never touches `datasets`.
    assert fluxstudio.list_hf_dataset_splits("") == []


def test_list_hf_dataset_splits_from_datasets_api() -> None:
    fake = ModuleType("datasets")
    fake.get_dataset_split_names = lambda path, config_name=None: ["train", "test"]  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"datasets": fake}):
        assert fluxstudio.list_hf_dataset_splits("mnist") == ["train", "test"]


def test_list_hf_dataset_splits_degrades_to_empty_on_error() -> None:
    def _boom(path: Any, config_name: Any = None) -> Any:
        raise RuntimeError("unknown dataset")

    fake = ModuleType("datasets")
    fake.get_dataset_split_names = _boom  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"datasets": fake}):
        # Must never raise — the picker is a convenience over a free-text field.
        assert fluxstudio.list_hf_dataset_splits("nonexistent/dataset") == []


def test_node_specs_route_returns_declared_specs() -> None:
    # The /taidal/node_specs GET handler surfaces each node's TAIDAL_ACCEPTS/PRODUCES so the
    # canvas connection-validator can fetch them.
    with _fake_comfy() as (routes, web):
        assert routes.get_path == "/taidal/node_specs"
        handler = routes.get_handler
        assert handler is not None
        fluxstudio.NODE_CLASS_MAPPINGS["Taidal_X"] = type(
            "Taidal_X", (), {"TAIDAL_ACCEPTS": {"kind": "any"}, "TAIDAL_PRODUCES": None}
        )
        asyncio.run(handler(SimpleNamespace()))
    data, _kwargs = web.calls[-1]
    assert data["Taidal_X"] == {"accepts": {"kind": "any"}, "produces": None}

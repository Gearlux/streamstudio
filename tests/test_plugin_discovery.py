"""Tests for the ``streamstudio.plugins`` entry-point seam (``_discover_and_register_plugins``).

A plugin is an installed package whose entry point names a MODULE contributing editor surface:
``ROUTES`` (an aiohttp ``RouteTableDef`` merged onto ComfyUI's route table) and/or
``WEB_DIRECTORY`` (a frontend JS directory registered into ComfyUI's ``EXTENSION_WEB_DIRS``).
Everything runs at streamstudio import time inside ComfyUI, so — like ``test_init_discovery`` —
the tests call the discovery function directly with ``entry_points`` patched and fake ``server`` /
``nodes`` modules injected into ``sys.modules``.
"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

import streamstudio


@pytest.fixture(autouse=True)
def _reset_plugin_sentinel() -> Iterator[None]:
    """Clear the process-shared one-shot plugin sentinel before/after each test.

    Mirrors ``test_init_discovery._reset_discovery_cache``: the sentinel is set during the real
    ``import streamstudio`` at collection time, so direct-call tests must reset it to exercise a
    fresh scan.
    """
    sys.__dict__.pop("_streamstudio_discovered_plugins", None)
    yield
    sys.__dict__.pop("_streamstudio_discovered_plugins", None)


class _FakeRouteTable(list):
    """A stand-in for aiohttp's ``RouteTableDef``: iterable of route entries + the ``route`` decorator."""

    def route(self, method: str, path: str, **kwargs: Any) -> Any:
        def deco(handler: Any) -> Any:
            self.append(SimpleNamespace(method=method, path=path, handler=handler, kwargs=kwargs))
            return handler

        return deco


def _fake_server_module(table: Any) -> ModuleType:
    mod = ModuleType("server")
    mod.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=table))  # type: ignore[attr-defined]
    return mod


def _fake_nodes_module(ext_dirs: dict) -> ModuleType:
    mod = ModuleType("nodes")
    mod.EXTENSION_WEB_DIRS = ext_dirs  # type: ignore[attr-defined]
    return mod


def _fake_plugin_module(
    name: str,
    routes: Any = None,
    web_directory: Optional[str] = None,
    file: Optional[str] = None,
) -> ModuleType:
    mod = ModuleType(name)
    if routes is not None:
        mod.ROUTES = routes  # type: ignore[attr-defined]
    if web_directory is not None:
        mod.WEB_DIRECTORY = web_directory  # type: ignore[attr-defined]
    if file is not None:
        mod.__file__ = file
    return mod


def _route_entry(path: str = "/taidal/fake_plugin", method: str = "GET") -> SimpleNamespace:
    async def handler(request: Any) -> Any:  # pragma: no cover - never awaited in these tests
        return None

    return SimpleNamespace(method=method, path=path, handler=handler, kwargs={})


# --------------------------------------------------------------------------- #
# _discover_and_register_plugins
# --------------------------------------------------------------------------- #


def test_discover_plugins_returns_quietly_when_entry_points_fail() -> None:
    fake_logger = MagicMock()
    with patch.object(streamstudio, "logger", fake_logger):
        with patch("importlib.metadata.entry_points", side_effect=RuntimeError("boom")):
            streamstudio._discover_and_register_plugins()  # must not raise
    fake_logger.error.assert_called_once()


def test_discover_plugins_imports_module_and_registers_both_surfaces() -> None:
    plugin = _fake_plugin_module("acme.studio_plugin")
    fake_ep = SimpleNamespace(name="acme", value="acme.studio_plugin", dist=SimpleNamespace(name="acme-widgets"))
    routes_reg = MagicMock(return_value=1)
    web_reg = MagicMock(return_value=True)
    with patch.dict(sys.modules, {"acme.studio_plugin": plugin}):
        with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
            with patch.object(streamstudio, "_register_plugin_routes", routes_reg):
                with patch.object(streamstudio, "_register_plugin_web_directory", web_reg):
                    streamstudio._discover_and_register_plugins()
    # The DISTRIBUTION name (not the entry-point name) keys both registrations.
    routes_reg.assert_called_once_with(plugin, "acme-widgets")
    web_reg.assert_called_once_with(plugin, "acme-widgets")


def test_discover_plugins_dist_name_falls_back_to_entry_point_name() -> None:
    plugin = _fake_plugin_module("acme.studio_plugin")
    fake_ep = SimpleNamespace(name="acme", value="acme.studio_plugin")  # no .dist attribute
    routes_reg = MagicMock(return_value=0)
    with patch.dict(sys.modules, {"acme.studio_plugin": plugin}):
        with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
            with patch.object(streamstudio, "_register_plugin_routes", routes_reg):
                with patch.object(streamstudio, "_register_plugin_web_directory", MagicMock(return_value=False)):
                    streamstudio._discover_and_register_plugins()
    routes_reg.assert_called_once_with(plugin, "acme")


def test_discover_plugins_isolates_a_failing_plugin_at_warning_level() -> None:
    """A broken plugin is skipped at WARNING (an explicit opt-in is actionable — unlike the
    debug-level ``confluid.configurables`` scan) and the OTHER plugins still load."""
    good = _fake_plugin_module("good.plugin")
    eps = [
        SimpleNamespace(name="bad", value="broken.plugin.module"),  # not importable
        SimpleNamespace(name="good", value="good.plugin", dist=SimpleNamespace(name="good-dist")),
    ]
    fake_logger = MagicMock()
    web_reg = MagicMock(return_value=False)
    with patch.dict(sys.modules, {"good.plugin": good}):
        with patch("importlib.metadata.entry_points", return_value=eps):
            with patch.object(streamstudio, "_register_plugin_routes", MagicMock(return_value=0)):
                with patch.object(streamstudio, "_register_plugin_web_directory", web_reg):
                    with patch.object(streamstudio, "logger", fake_logger):
                        streamstudio._discover_and_register_plugins()
    fake_logger.warning.assert_called_once()
    web_reg.assert_called_once_with(good, "good-dist")  # the good plugin still registered
    assert sys._streamstudio_discovered_plugins == ("good-dist",)  # type: ignore[attr-defined]


def test_discover_plugins_is_one_shot_per_process() -> None:
    plugin = _fake_plugin_module("acme.studio_plugin")
    fake_ep = SimpleNamespace(name="acme", value="acme.studio_plugin", dist=SimpleNamespace(name="acme-widgets"))
    routes_reg = MagicMock(return_value=0)
    with patch.dict(sys.modules, {"acme.studio_plugin": plugin}):
        with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
            with patch.object(streamstudio, "_register_plugin_routes", routes_reg):
                with patch.object(streamstudio, "_register_plugin_web_directory", MagicMock(return_value=False)):
                    streamstudio._discover_and_register_plugins()  # first call: scans
                    streamstudio._discover_and_register_plugins()  # second identity: no re-scan
    routes_reg.assert_called_once()


def test_discover_plugins_dedups_duplicate_entry_points() -> None:
    plugin = _fake_plugin_module("acme.studio_plugin")
    eps = [
        SimpleNamespace(name="acme", value="acme.studio_plugin"),
        SimpleNamespace(name="acme", value="acme.studio_plugin"),
    ]
    routes_reg = MagicMock(return_value=0)
    with patch.dict(sys.modules, {"acme.studio_plugin": plugin}):
        with patch("importlib.metadata.entry_points", return_value=eps):
            with patch.object(streamstudio, "_register_plugin_routes", routes_reg):
                with patch.object(streamstudio, "_register_plugin_web_directory", MagicMock(return_value=False)):
                    streamstudio._discover_and_register_plugins()
    routes_reg.assert_called_once()


# --------------------------------------------------------------------------- #
# _register_plugin_routes
# --------------------------------------------------------------------------- #


def test_register_plugin_routes_is_a_noop_outside_comfyui() -> None:
    # No `server` module importable (the dev/CI venv): silent no-op, no exception.
    plugin = _fake_plugin_module("p", routes=[_route_entry()])
    assert "server" not in sys.modules
    assert streamstudio._register_plugin_routes(plugin, "p-dist") == 0


def test_register_plugin_routes_merges_onto_the_prompt_server_table() -> None:
    table = _FakeRouteTable()
    plugin = _fake_plugin_module("p", routes=[_route_entry("/taidal/fake_plugin")])
    with patch.dict(sys.modules, {"server": _fake_server_module(table)}):
        added = streamstudio._register_plugin_routes(plugin, "p-dist")
    assert added == 1
    assert len(table) == 1
    assert (table[0].method, table[0].path) == ("GET", "/taidal/fake_plugin")


def test_register_plugin_routes_dedups_per_method_and_path() -> None:
    table = _FakeRouteTable([SimpleNamespace(method="GET", path="/taidal/fake_plugin", handler=object())])
    plugin = _fake_plugin_module(
        "p",
        routes=[_route_entry("/taidal/fake_plugin"), _route_entry("/taidal/fake_plugin", method="POST")],
    )
    with patch.dict(sys.modules, {"server": _fake_server_module(table)}):
        added = streamstudio._register_plugin_routes(plugin, "p-dist")
    # The GET duplicate keeps the FIRST registration; the POST on the same path is new.
    assert added == 1
    assert len(table) == 2


def test_register_plugin_routes_skips_a_static_shaped_entry_with_warning() -> None:
    table = _FakeRouteTable()
    static_like = SimpleNamespace(prefix="/assets", path="/tmp/x")  # no method/handler, like a StaticDef
    plugin = _fake_plugin_module("p", routes=[static_like, _route_entry()])
    fake_logger = MagicMock()
    with patch.dict(sys.modules, {"server": _fake_server_module(table)}):
        with patch.object(streamstudio, "logger", fake_logger):
            added = streamstudio._register_plugin_routes(plugin, "p-dist")
    assert added == 1  # the real route still lands
    fake_logger.warning.assert_called_once()


def test_register_plugin_routes_against_a_real_aiohttp_route_table() -> None:
    """Pins the ``routes.route(method, path, **kwargs)(handler)`` contract against real aiohttp."""
    web = pytest.importorskip("aiohttp.web")
    target = web.RouteTableDef()
    plugin_routes = web.RouteTableDef()

    @plugin_routes.get("/taidal/fake_plugin")
    async def handler(request: Any) -> Any:  # pragma: no cover - registration shape only
        return web.json_response({})

    plugin = _fake_plugin_module("p", routes=plugin_routes)
    with patch.dict(sys.modules, {"server": _fake_server_module(target)}):
        added = streamstudio._register_plugin_routes(plugin, "p-dist")
    assert added == 1
    assert any(
        getattr(r, "method", None) == "GET" and getattr(r, "path", None) == "/taidal/fake_plugin" for r in target
    )


# --------------------------------------------------------------------------- #
# _register_plugin_web_directory
# --------------------------------------------------------------------------- #


def test_register_plugin_web_directory_is_a_noop_outside_comfyui(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    plugin = _fake_plugin_module("p", web_directory="web", file=str(tmp_path / "__init__.py"))
    assert "nodes" not in sys.modules
    assert streamstudio._register_plugin_web_directory(plugin, "p-dist") is False


def test_register_plugin_web_directory_registers_abspath_under_dist_name(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "picker.js").write_text("// js")
    plugin = _fake_plugin_module("p", web_directory="web", file=str(tmp_path / "__init__.py"))
    ext_dirs: dict = {}
    with patch.dict(sys.modules, {"nodes": _fake_nodes_module(ext_dirs)}):
        assert streamstudio._register_plugin_web_directory(plugin, "p-dist") is True
    assert ext_dirs == {"p-dist": str((tmp_path / "web").resolve())}


def test_register_plugin_web_directory_warns_on_missing_directory(tmp_path: Path) -> None:
    plugin = _fake_plugin_module("p", web_directory="web", file=str(tmp_path / "__init__.py"))  # no web/ dir
    ext_dirs: dict = {}
    fake_logger = MagicMock()
    with patch.dict(sys.modules, {"nodes": _fake_nodes_module(ext_dirs)}):
        with patch.object(streamstudio, "logger", fake_logger):
            assert streamstudio._register_plugin_web_directory(plugin, "p-dist") is False
    assert ext_dirs == {}
    fake_logger.warning.assert_called_once()


def test_register_plugin_web_directory_keeps_the_first_registration(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    plugin = _fake_plugin_module("p", web_directory="web", file=str(tmp_path / "__init__.py"))
    ext_dirs = {"p-dist": "/already/registered"}
    with patch.dict(sys.modules, {"nodes": _fake_nodes_module(ext_dirs)}):
        assert streamstudio._register_plugin_web_directory(plugin, "p-dist") is False
    assert ext_dirs == {"p-dist": "/already/registered"}


def test_plugin_without_routes_or_web_directory_registers_nothing() -> None:
    plugin = _fake_plugin_module("bare.plugin")
    assert streamstudio._register_plugin_routes(plugin, "bare") == 0
    assert streamstudio._register_plugin_web_directory(plugin, "bare") is False

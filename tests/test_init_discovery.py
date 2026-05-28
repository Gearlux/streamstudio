"""Tests for ``fluxstudio.__init__`` — entry-point discovery and the ComfyUI
directory-picker route.

Two surfaces live here, both awkward to reach because they run at *import*
time:

* ``_discover_and_register_nodes`` walks the ``confluid.configurables`` entry
  points and is the sole place the ``Taidal/<Module/Path>`` category string is
  derived. We call it directly with ``entry_points`` / ``register_module_nodes``
  mocked so the category math and the deliberately-quiet (``debug``, not
  ``warning``) skip-on-failure path are pinned down without depending on the
  installed entry-point set.
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
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import fluxstudio

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


def test_discover_registers_with_derived_category() -> None:
    fake_ep = SimpleNamespace(value="waivefront.sinks")
    fake_register = MagicMock()
    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        with patch.object(fluxstudio, "register_module_nodes", fake_register):
            fluxstudio._discover_and_register_nodes()

    fake_register.assert_called_once()
    args, kwargs = fake_register.call_args
    assert args[0] == "waivefront.sinks"
    assert args[1] is fluxstudio.NODE_CLASS_MAPPINGS
    assert args[2] is fluxstudio.NODE_DISPLAY_NAME_MAPPINGS
    assert kwargs["category"] == "Taidal/Waivefront/Sinks"


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

    def __iter__(self) -> Iterator[SimpleNamespace]:
        if self._existing:
            yield SimpleNamespace(path="/taidal/choose_directory", method="POST")

    def post(self, path: str):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            self.registered_path = path
            self.handler = fn
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
        assert routes.registered_path == "/taidal/choose_directory"
        handler = routes.handler
        assert handler is not None

        with patch("subprocess.run", return_value=SimpleNamespace(stdout="/Users/me/data\n")) as run_mock:
            asyncio.run(handler(SimpleNamespace()))

        # The native macOS picker is invoked via osascript...
        assert run_mock.call_args.args[0][0] == "osascript"
        # ...and the trimmed POSIX path is returned to the frontend.
        assert web.calls[-1] == ({"path": "/Users/me/data"}, {})


def test_route_returns_empty_path_on_cancel() -> None:
    with _fake_comfy() as (routes, web):
        handler = routes.handler
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "osascript")):
            asyncio.run(handler(SimpleNamespace()))
    # A CalledProcessError means the user clicked Cancel -> empty path, no error.
    assert web.calls[-1] == ({"path": ""}, {})


def test_route_returns_500_on_generic_error() -> None:
    with _fake_comfy() as (routes, web):
        handler = routes.handler
        with patch("subprocess.run", side_effect=RuntimeError("kaboom")):
            asyncio.run(handler(SimpleNamespace()))
    data, kwargs = web.calls[-1]
    assert data == {"error": "kaboom"}
    assert kwargs == {"status": 500}


def test_route_not_re_registered_when_already_present() -> None:
    # If ComfyUI (or a prior import) already registered the route, the block
    # must not define a second handler.
    with _fake_comfy(existing_route=True) as (routes, _web):
        assert routes.handler is None

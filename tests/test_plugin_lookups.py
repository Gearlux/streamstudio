"""A plugin's LISTS beside a field — the lookup pickers an installed package supplies.

A producer stamps a parameter with a tag of its own (``__streamstudio_widget_params__ = {"dataset":
"SHOP_DATASET"}``); a ``streamstudio.plugins`` module that declares ``LOOKUPS = {"SHOP_DATASET": <function>}``
supplies the list the field gets. The function receives the node's current values (strings) and answers
``(names, reason)`` — so a list can depend on another field (the versions of the dataset named beside it). The form
marks such a field ``lookup: true``; the page
asks ``GET api/lookups/<tag>?<the node's values>`` for it. A tag no plugin lists stays a plain field, as before.

No test touches a network or the installed entry points: the plugin table is replaced.
"""

import sys
import threading
import types
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest
from confluid import configurable

from streamstudio import lookups, palette
from streamstudio.app import create_app

MOD = __name__


@configurable(category="source", group="lookup-test")
class ShopSource:
    """A source whose dataset and version come from a shop.

    Args:
        dataset: The shop's dataset name.
        version: Its version.
        other: A tag nobody lists.
    """

    __streamstudio_widget_params__ = {"dataset": "SHOP_DATASET", "version": "SHOP_VERSION", "other": "NOBODY_LISTS"}

    def __init__(self, dataset: str = "", version: str = "latest", other: str = "") -> None:
        self.dataset = dataset
        self.version = version
        self.other = other

    def __iter__(self) -> Any:
        return iter([])


def _datasets(values: Mapping[str, str]) -> Tuple[List[str], str]:
    return ["fruit", "veg"], ""


def _versions(values: Mapping[str, str]) -> Tuple[List[str], str]:
    dataset = values.get("dataset", "")
    if not dataset:
        return [], "name a dataset first"
    return [f"{dataset}-1.0", f"{dataset}-2.0"], ""


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch) -> Dict[str, lookups.LookupFn]:
    table: Dict[str, lookups.LookupFn] = {"SHOP_DATASET": _datasets, "SHOP_VERSION": _versions}
    monkeypatch.setattr(lookups, "_FOUND", table)
    # the palette is described once per process — a test that built it earlier described these classes without the
    # table above, so it is built again here (and again after, from the real table); `spec_for` reads the classes it
    # described one by one from `_index`, so that goes too
    monkeypatch.setattr(palette, "_cache", None)
    monkeypatch.setattr(palette, "_cache_scanned", False)
    monkeypatch.setattr(palette, "_index", {})
    return table


def _graph(folder: Path) -> Path:
    path = folder / "shop.yaml"
    path.write_text(f"source:\n  _target_: {MOD}.ShopSource\n")
    return path


class TestTheFormMarksAListedField:
    def test_a_stamped_field_a_plugin_lists_is_a_lookup(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        _graph(tmp_path)
        form = create_app(tmp_path).test_client().get("/api/graphs/shop/form").get_json()
        assert form["source"]["dataset"]["picker"] == "SHOP_DATASET"
        assert form["source"]["dataset"]["lookup"] is True
        assert form["source"]["version"]["lookup"] is True

    def test_CON_a_stamp_no_plugin_lists_stays_a_plain_field(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        _graph(tmp_path)
        form = create_app(tmp_path).test_client().get("/api/graphs/shop/form").get_json()
        assert form["source"]["other"]["picker"] == "NOBODY_LISTS"
        assert "lookup" not in form["source"]["other"]


class TestTheLookupRoute:
    def test_the_list_and_its_reason(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        client = create_app(tmp_path).test_client()
        assert client.get("/api/lookups/SHOP_DATASET").get_json() == {"names": ["fruit", "veg"], "reason": ""}

    def test_the_nodes_values_reach_the_list(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        client = create_app(tmp_path).test_client()
        body = client.get("/api/lookups/SHOP_VERSION?dataset=fruit&version=latest").get_json()
        assert body == {"names": ["fruit-1.0", "fruit-2.0"], "reason": ""}
        assert client.get("/api/lookups/SHOP_VERSION").get_json() == {"names": [], "reason": "name a dataset first"}

    def test_a_tag_no_plugin_lists_answers_nothing_and_says_so(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        body = create_app(tmp_path).test_client().get("/api/lookups/NOBODY_LISTS").get_json()
        assert body == {"names": [], "reason": "no installed plugin lists NOBODY_LISTS"}

    def test_a_list_that_fails_answers_nothing_and_says_why(self, tmp_path: Path, shop: Dict[str, Any]) -> None:
        def broken(values: Mapping[str, str]) -> Tuple[List[str], str]:
            raise ConnectionError("the shop is closed")

        shop["SHOP_DATASET"] = broken
        body = create_app(tmp_path).test_client().get("/api/lookups/SHOP_DATASET").get_json()
        assert body == {"names": [], "reason": "ConnectionError: the shop is closed"}

    def test_a_list_that_does_not_answer_in_time_answers_nothing(
        self, tmp_path: Path, shop: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()

        def stuck(values: Mapping[str, str]) -> Tuple[List[str], str]:
            release.wait(5.0)
            return ["late"], ""

        shop["SHOP_DATASET"] = stuck
        monkeypatch.setattr(lookups, "TIMEOUT_S", 0.05)
        try:
            body = create_app(tmp_path).test_client().get("/api/lookups/SHOP_DATASET").get_json()
        finally:
            release.set()
        assert body == {"names": [], "reason": "the list did not answer within 0.05 s"}

    def test_a_bare_list_and_names_that_are_not_text(self, shop: Dict[str, Any]) -> None:
        shop["SHOP_DATASET"] = lambda values: ["a", 3, None, "b"]
        assert lookups.answer("SHOP_DATASET", {}) == {"names": ["a", "3", "b"], "reason": ""}


class TestTheLookupsAreDiscovered:
    """The table is read off the ``streamstudio.plugins`` modules' ``LOOKUPS``, once per process."""

    def _entry(self, name: str, module: str) -> Any:
        return types.SimpleNamespace(name=name, value=module, dist=None)

    def test_a_plugins_lookups_are_found_and_a_module_without_any_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_lists = types.ModuleType("shop_plugin")
        with_lists.LOOKUPS = {"SHOP_DATASET": _datasets}  # type: ignore[attr-defined]
        without = types.ModuleType("plain_plugin")
        monkeypatch.setitem(sys.modules, "shop_plugin", with_lists)
        monkeypatch.setitem(sys.modules, "plain_plugin", without)
        monkeypatch.setattr(
            lookups,
            "_entry_points",
            lambda: [self._entry("shop", "shop_plugin"), self._entry("plain", "plain_plugin")],
        )
        monkeypatch.setattr(lookups, "_FOUND", None)
        assert lookups.plugin_lookups() == {"SHOP_DATASET": _datasets}

    def test_a_plugin_that_fails_to_load_is_skipped_with_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warned: List[str] = []
        monkeypatch.setattr(lookups.logger, "warning", lambda message: warned.append(str(message)))
        monkeypatch.setattr(lookups, "_entry_points", lambda: [self._entry("gone", "no_such_plugin_module_xyz")])
        monkeypatch.setattr(lookups, "_FOUND", None)
        assert lookups.plugin_lookups() == {}
        assert warned and "no_such_plugin_module_xyz" in warned[0]

    def test_a_lookups_table_that_is_not_a_mapping_of_functions_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        odd = types.ModuleType("odd_plugin")
        odd.LOOKUPS = {"GOOD": _datasets, "BAD": "not a function"}  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "odd_plugin", odd)
        monkeypatch.setattr(lookups, "_entry_points", lambda: [self._entry("odd", "odd_plugin")])
        monkeypatch.setattr(lookups, "_FOUND", None)
        assert lookups.plugin_lookups() == {"GOOD": _datasets}

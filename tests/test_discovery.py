"""Op discovery tests."""

from __future__ import annotations

from typing import Iterable

from fluxstudio.discovery import DEFAULT_MODULES, scan_packages


def test_default_scan_finds_dataflux_ops() -> None:
    schemas = scan_packages()
    assert schemas, "expected at least the dataflux op modules to produce schemas"
    paths = {s["path"] for s in schemas}
    # Spot check: one rep from numpy ops and one from torch ops should be present.
    assert any(p.startswith("dataflux.ops.numpy:") for p in paths)
    assert any(p.startswith("dataflux.ops.torch:") for p in paths)


def test_default_scan_categorizes_known_modules() -> None:
    schemas = scan_packages()
    categories = {s["category"] for s in schemas}
    # DataOps must always be present; sinks/sources/visualizers depend on
    # what's installed (waivefront is optional).
    assert "DataOp" in categories


def test_each_schema_has_required_fields() -> None:
    schemas = scan_packages(["dataflux.ops.numpy"])
    assert schemas
    for s in schemas:
        for key in ("path", "name", "doc", "parameters", "module", "category"):
            assert key in s, f"schema missing {key}: {s}"


def test_unknown_module_is_skipped_not_raised() -> None:
    schemas = scan_packages(["this.module.definitely.does.not.exist"])
    assert schemas == []


def test_default_modules_is_a_tuple_of_strs() -> None:
    assert isinstance(DEFAULT_MODULES, tuple)
    assert all(isinstance(m, str) for m in DEFAULT_MODULES)


def test_categorization_marks_storage_as_data_source_or_sink() -> None:
    schemas = scan_packages(["dataflux.storage.hdf5"])
    by_category: Iterable[str] = (s["category"] for s in schemas)
    assert "DataSource" in set(by_category) or "DataSink" in {s["category"] for s in schemas}

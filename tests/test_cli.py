"""End-to-end tests for the FluxStudio CLI.

The `run` command is exercised through Liquify's `script_command` machinery
so the YAML positional argument is auto-promoted to `context.config_path`.
The previous regular-`@command` registration silently dropped the
positional arg and crashed with `TypeError: run() missing 1 required
positional argument: 'yaml_path'`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from fluxstudio.cli import app, main
from liquifai.context import set_context


@pytest.fixture(autouse=True)
def _reset_liquify_context() -> Any:
    set_context(None)  # type: ignore[arg-type]
    app.context = None
    yield
    set_context(None)  # type: ignore[arg-type]
    app.context = None


def test_run_emits_one_event_per_op(
    monkeypatch: Any,
    capsys: Any,
    tiny_pipeline_yaml: Path,
) -> None:
    """`fluxstudio run examples/tiny_pipeline.yaml` must emit 4 samples × 2 ops = 8 NDJSON events."""
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "run", str(tiny_pipeline_yaml)])

    main()

    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 8, f"expected 8 NDJSON events, got {len(out)}"
    events = [json.loads(line) for line in out]
    assert {e["op_name"] for e in events} == {"RescaleOp", "StandardizeOp"}
    # Last event for the last sample is final.
    assert events[-1]["final"] is True
    assert events[-1]["sample_index"] == 3


def test_run_without_yaml_path_errors(monkeypatch: Any, capsys: Any) -> None:
    """Bare `fluxstudio run` (no YAML) must exit non-zero with a helpful message."""
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "run"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2


def test_discover_emits_palette(monkeypatch: Any, capsys: Any) -> None:
    """`fluxstudio discover` must print a JSON op palette to stdout."""
    monkeypatch.setattr(sys, "argv", ["fluxstudio", "discover"])

    main()

    palette = json.loads(capsys.readouterr().out)
    assert isinstance(palette, list)

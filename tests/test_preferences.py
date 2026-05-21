"""Unit tests for the persisted user preferences module + API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fluxstudio.preferences import (
    Preferences,
    load_preferences,
    preferences_path,
    save_preferences,
    update_preferences,
)
from fluxstudio.server import create_app


@pytest.fixture(autouse=True)
def _redirect_xdg_config_home(monkeypatch: Any, tmp_path: Path) -> Path:
    """Every test in this module gets its own XDG config dir under tmp_path."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_preferences_path_uses_xdg_config_home(tmp_path: Path) -> None:
    assert preferences_path() == tmp_path / "fluxstudio" / "preferences.json"


def test_load_missing_returns_defaults() -> None:
    prefs = load_preferences()
    assert prefs == Preferences()
    assert prefs.confirm_clear_pipeline is True
    assert prefs.show_internal_ops is False
    assert prefs.default_save_filename == "pipeline.yaml"


def test_save_then_load_round_trip(tmp_path: Path) -> None:
    prefs = Preferences(confirm_clear_pipeline=False, show_internal_ops=True)
    save_preferences(prefs)
    path = tmp_path / "fluxstudio" / "preferences.json"
    assert path.exists()
    on_disk = json.loads(path.read_text())
    # keys are sorted in the persisted file for stable diffs
    assert list(on_disk.keys()) == [
        "confirm_clear_pipeline",
        "default_save_filename",
        "show_internal_ops",
        "theme",
    ]
    assert load_preferences() == prefs


def test_update_preferences_merges_partial() -> None:
    update_preferences({"show_internal_ops": True})
    update_preferences({"confirm_clear_pipeline": False})
    final = load_preferences()
    assert final.show_internal_ops is True
    assert final.confirm_clear_pipeline is False
    assert final.default_save_filename == "pipeline.yaml"
    assert final.theme == "auto"


def test_update_theme_accepts_known_values() -> None:
    for value in ("auto", "light", "dark"):
        update_preferences({"theme": value})
        assert load_preferences().theme == value


def test_update_theme_rejects_unknown_value() -> None:
    with pytest.raises(Exception):  # Pydantic Literal validation
        update_preferences({"theme": "purple"})


def test_update_rejects_unknown_field() -> None:
    with pytest.raises(
        Exception
    ):  # Pydantic ValidationError; message validated at API level
        update_preferences({"not_a_real_pref": True})


def test_get_preferences_endpoint_defaults() -> None:
    client = TestClient(create_app())
    resp = client.get("/api/preferences")
    assert resp.status_code == 200
    assert resp.json() == Preferences().model_dump()


def test_put_preferences_endpoint_persists_and_merges(tmp_path: Path) -> None:
    client = TestClient(create_app())
    resp = client.put("/api/preferences", json={"confirm_clear_pipeline": False})
    assert resp.status_code == 200
    assert resp.json()["confirm_clear_pipeline"] is False
    # second PUT preserves the first's value
    resp = client.put("/api/preferences", json={"show_internal_ops": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "confirm_clear_pipeline": False,
        "show_internal_ops": True,
        "default_save_filename": "pipeline.yaml",
        "theme": "auto",
    }
    # GET sees the same persisted state
    assert client.get("/api/preferences").json() == body


def test_put_preferences_rejects_unknown_field() -> None:
    client = TestClient(create_app())
    resp = client.put("/api/preferences", json={"bogus_field": 42})
    assert resp.status_code == 400

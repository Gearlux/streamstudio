"""Persisted user preferences for the FluxStudio editor.

Lives in `$XDG_CONFIG_HOME/fluxstudio/preferences.json` (or
`~/.config/fluxstudio/preferences.json` if `XDG_CONFIG_HOME` is unset).
The file is created on first write — `load_preferences()` returns
defaults when the file is missing.

The schema is intentionally tiny: each editor feature that wants a
persisted toggle adds a field here, the matching widget in
`SettingsDialog.tsx`, and (if needed) a default in the corresponding
component. Pydantic's strict validation rejects unknown fields, so a
stale on-disk file from a future build is detected loudly rather than
silently ignored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict

from logflow import get_logger

logger = get_logger(__name__)


class Preferences(BaseModel):
    """User-level editor preferences. Add new fields here as features need them."""

    model_config = ConfigDict(extra="forbid")

    confirm_clear_pipeline: bool = True
    show_internal_ops: bool = False
    default_save_filename: str = "pipeline.yaml"
    theme: Literal["auto", "light", "dark"] = "auto"


def preferences_path() -> Path:
    """Resolve the on-disk preferences path. Honors `XDG_CONFIG_HOME`."""
    base_env = os.environ.get("XDG_CONFIG_HOME")
    base = Path(base_env) if base_env else Path.home() / ".config"
    return base / "fluxstudio" / "preferences.json"


def load_preferences() -> Preferences:
    """Load preferences from disk, returning defaults if the file is missing."""
    path = preferences_path()
    if not path.exists():
        return Preferences()
    raw = json.loads(path.read_text())
    return Preferences(**raw)


def save_preferences(prefs: Preferences) -> None:
    """Write preferences to disk (creating the parent directory if needed)."""
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs.model_dump(), indent=2, sort_keys=True) + "\n")
    logger.debug(f"Wrote preferences to {path}")


def update_preferences(partial: Dict[str, Any]) -> Preferences:
    """Merge ``partial`` into the persisted preferences, validate, save, return."""
    current = load_preferences().model_dump()
    current.update(partial)
    merged = Preferences(**current)
    save_preferences(merged)
    return merged

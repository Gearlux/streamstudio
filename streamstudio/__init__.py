"""The streamstudio package: a graph editor for YAML pipelines (``streamstudio.app`` and the modules it imports).

The package root is cheap to import — a host app that mounts the editor imports it at its own start. It answers
``__version__`` and the HuggingFace lookups the editor's pickers call (``GET /api/hf/datasets``, ``…/features``,
``…/splits``); the routes call them through the package at call time, so a test patches them here.
"""

import importlib.metadata
import os
from typing import List, Optional

from loggair import get_logger

try:
    # Single source of truth: the installed distribution's metadata, i.e. pyproject.toml's
    # `version` (the distribution is `streamstudio`). Never type the number in here as well.
    __version__ = importlib.metadata.version("streamstudio")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - uninstalled source checkout
    __version__ = "0.0.0.dev0"

logger = get_logger(__name__)


def cached_hf_datasets() -> "tuple[List[str], str]":
    """``(dataset repo ids, reason)`` — the local HF cache, and why it is empty when it is.

    Backs ``GET /api/hf/datasets`` and the dataset-id picker. Still degrades to an empty list on ANY failure — the
    picker is a convenience over a free-text field and must never break the editor — but the REASON travels with it,
    because an empty picker that explains nothing is indistinguishable from a broken one. The canonical case:
    ``HF_HOME`` changed, the process still holds the OLD value (``huggingface_hub`` reads it at import), and the picker
    silently shows nothing.
    """
    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        ids = {repo.repo_id for repo in info.repos if getattr(repo, "repo_type", None) == "dataset"}
        if not ids:
            return [], f"no datasets cached at {_hf_cache_dir()}"
        return sorted(ids), ""
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # A MISSING cache directory is actionable and is what a changed HF_HOME looks like from
        # here, so it is a warning naming the path this process is actually reading — the value
        # is read at import, so a running editor keeps the one it was started with.
        if type(exc).__name__ == "CacheNotFound":
            logger.warning(
                f"the HuggingFace cache directory does not exist: {_hf_cache_dir()} "
                f"(this process was started with HF_HOME={os.environ.get('HF_HOME', '<unset>')}; "
                "restart the editor after changing it)"
            )
        else:
            logger.debug(f"cached_hf_datasets: returning [] ({reason})")
        return [], reason


def _hf_cache_dir() -> str:
    """The hub cache path THIS process resolved — read at import, so it can be stale."""
    try:
        from huggingface_hub import constants

        return str(constants.HF_HUB_CACHE)
    except Exception:  # pragma: no cover - huggingface_hub absent
        return "<huggingface_hub not installed>"


def list_hf_dataset_features(path: str, name: Optional[str] = None, split: Optional[str] = None) -> List[str]:
    """Return the feature/column names of a HuggingFace dataset (e.g. ``["image", "label"]``).

    Backs ``GET /api/hf/features`` and the feature picker, which lets a ``HuggingFace*`` source's
    ``input_feature`` / ``target_feature`` fields be filled from the *selected* dataset's actual columns instead of
    typed blind.

    Tries the cheap metadata-only path first (``load_dataset_builder().info.features``, no row download), then falls
    back to streaming a single example (handles datasets whose ``info.features`` is unset until a split is prepared,
    e.g. local imagefolders). Column order is preserved (more useful than alphabetical for picking input vs target).
    Degrades to an empty list on ANY failure (``datasets`` missing, unknown id, offline) — the picker is a convenience
    over a free-text field and must never break the editor.
    """
    if not path:
        return []
    # 1. Cheap path: the dataset builder's info carries the feature schema without
    #    downloading any rows.
    try:
        from datasets import load_dataset_builder

        builder = load_dataset_builder(path, name=name or None)
        features = getattr(builder.info, "features", None)
        if features:
            return list(features.keys())
    except Exception as e:
        logger.debug(f"list_hf_dataset_features: builder path failed ({type(e).__name__}: {e})")
    # 2. Fallback: stream one example and read its keys.
    try:
        from datasets import load_dataset

        stream = load_dataset(path, name=name or None, split=split or "train", streaming=True)
        first = next(iter(stream))
        return list(first.keys())
    except Exception as e:
        logger.debug(f"list_hf_dataset_features: streaming fallback failed ({type(e).__name__}: {e})")
        return []


def list_hf_dataset_splits(path: str, name: Optional[str] = None) -> List[str]:
    """Return the available split names of a HuggingFace dataset (e.g. ``["train", "test"]``).

    Backs ``GET /api/hf/splits`` and the split picker, which fills a ``HuggingFace*`` source's ``split`` field from
    the *selected* dataset's actual splits. The field stays free text (HF supports slice syntax like ``train[:90%]`` /
    ``train+test``); this only offers the enumerated splits. Metadata-only (``datasets.get_dataset_split_names``, no
    row download). Degrades to an empty list on ANY failure (``datasets`` missing, unknown id, offline) — the picker is
    a convenience over a free-text field and must never break the editor.
    """
    if not path:
        return []
    try:
        from datasets import get_dataset_split_names

        return list(get_dataset_split_names(path, config_name=name or None))
    except Exception as e:
        logger.debug(f"list_hf_dataset_splits: returning [] ({type(e).__name__}: {e})")
        return []

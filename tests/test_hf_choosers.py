"""The HuggingFace pickers must survive a graph LOAD, not only a hand-picked dataset.

There is no JS test harness in this project (the frontend is verified live), so what is
pinned here is the cross-language contract the JS cannot import: each dependent picker
builds its option list from ANOTHER widget's value, and a loaded graph fills widget values
in ``configure()`` -- AFTER ``onNodeCreated``, by assigning ``widget.value`` directly, which
fires no callback. A picker that only refreshes in ``onNodeCreated`` therefore keeps the
list it built while the dataset id was still ``""``.

Measured before this hook existed: opening a saved workspace graph whose HuggingFaceSource
carried ``path: ylecun/mnist`` left the split picker showing only its placeholder, while
choosing the same dataset by hand populated it -- because that path fires the id widget's
callback. The user reported it as "the node was not aware that the mnist dataset was used
(although it was in the path)".

``hf_dataset_chooser.js`` is deliberately NOT in this list: it lists the cached datasets of
the whole machine and depends on no other widget, so it has nothing to re-read.
"""

from pathlib import Path

import pytest

import streamstudio

WEB = Path(streamstudio.__file__).parent / "web"

# (file, the stash the onConfigure hook calls, the widget tag that makes it dependent)
DEPENDENT_PICKERS = [
    ("hf_split_chooser.js", "_taidalRefreshSplits", "HF_SPLIT"),
    ("hf_feature_chooser.js", "_taidalRefreshFeatures", "HF_FEATURE"),
    ("hf_metadata_chooser.js", "_taidalRefreshColumns", "HF_METADATA"),
]


@pytest.mark.parametrize("name,stash,tag", DEPENDENT_PICKERS)
def test_a_dependent_picker_refreshes_after_a_graph_load(name: str, stash: str, tag: str) -> None:
    source = (WEB / name).read_text()
    assert "onConfigure" in source, f"{name}: no onConfigure hook -- a loaded graph never refreshes it"
    assert f"node.{stash} =" in source, f"{name}: the refresh is not stashed for onConfigure to call"
    assert f"this.{stash}()" in source, f"{name}: onConfigure does not call the stashed refresh"


@pytest.mark.parametrize("name,stash,tag", DEPENDENT_PICKERS)
def test_the_previous_onConfigure_handler_is_chained(name: str, stash: str, tag: str) -> None:
    """Another extension may already own onConfigure -- replacing it would break that one."""
    source = (WEB / name).read_text()
    assert "onConfigure.apply(this, arguments)" in source, f"{name}: clobbers a previous onConfigure"


@pytest.mark.parametrize("name,stash,tag", DEPENDENT_PICKERS)
def test_the_picker_reads_the_dataset_id_widget(name: str, stash: str, tag: str) -> None:
    """It is dependent BECAUSE it keys off HF_DATASET_ID -- that is what makes the load order matter."""
    source = (WEB / name).read_text()
    assert "HF_DATASET_ID" in source and tag in source


def test_the_cached_dataset_picker_needs_no_refresh_hook() -> None:
    """The CON case: it depends on no other widget, so onConfigure would be noise."""
    source = (WEB / "hf_dataset_chooser.js").read_text()
    assert "HF_DATASET_ID" in source
    assert "onConfigure" not in source

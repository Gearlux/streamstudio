"""The pickers: which parameter gets which, the folder listing a path picker browses, the HuggingFace lookups.

Ported from the ComfyUI nodes' widgets, by the same rules, read off the class the way the palette reads everything
else — never a per-class table. The palette marks a parameter's JSON-schema fragment with ``picker`` (so
``GET …/form`` carries it to the canvas card and the settings form alike):

* ``directory`` — the parameter's NAME holds ``path`` / ``dir`` / ``root`` / ``folder``, or it is annotated
  ``pathlib.Path``; ``file`` — the name holds ``file`` / ``checkpoint`` / ``ckpt`` and no directory word
  (``checkpoint_dir`` is a folder of checkpoints). A bool / int / float parameter never gets one
  (``checkpoint_strict``), nor does a ``Literal`` (its choices are already a list);
* on a ``HuggingFace*`` class: ``path`` / ``repo_id`` → ``hf_dataset`` (a hub id, NOT a folder),
  ``input_feature`` / ``target_feature`` → ``hf_feature``, ``split`` → ``hf_split``, ``metadata_features`` →
  ``hf_metadata``;
* a producer's ``__streamstudio_widget_params__`` stamp names the picker itself and wins over every rule above.

The routes behind them: ``GET /api/files?dir=…&show=all`` lists every file (a file picker) besides the graph
browser's folders-and-graphs listing, ``$VAR`` in ``dir`` expanded; ``GET /api/hf/datasets`` / ``…/features`` /
``…/splits`` answer the HuggingFace lookups — replaced here, so no test touches the network.
"""

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import pytest
from confluid import configurable
from recordstream import Record

import streamstudio
from streamstudio import palette
from streamstudio.app import create_app

MOD = __name__


@configurable(category="sink", group="picker-test")
class PickerWriter:
    """Every name rule, both ways.

    Args:
        output_dir: A folder.
        checkpoint_path: A file.
        checkpoint_dir: A folder of checkpoints.
        checkpoint_strict: A switch named like a file.
        root: A folder by type.
        log_file: A file.
        name: Neither.
        root_count: A number named like a folder.
        path_mode: A choice named like a path.
        maybe_folder: An optional folder.
    """

    def __init__(
        self,
        output_dir: str = "",
        checkpoint_path: str = "",
        checkpoint_dir: str = "",
        checkpoint_strict: bool = False,
        root: Path = Path("."),
        log_file: str = "",
        name: str = "",
        root_count: int = 0,
        path_mode: Literal["copy", "link"] = "copy",
        maybe_folder: Optional[str] = None,
    ) -> None:
        self.output_dir = output_dir


@configurable(category="source", group="picker-test")
class HuggingFacePicker:
    """A hub dataset.

    Args:
        path: The hub id.
        input_feature: The input column.
        target_feature: The target column.
        split: The split.
        metadata_features: More columns.
        cache_dir: A folder.
    """

    def __init__(
        self,
        path: str = "",
        input_feature: str = "image",
        target_feature: str = "label",
        split: str = "train",
        metadata_features: Optional[List[str]] = None,
        cache_dir: str = "",
    ) -> None:
        self.path = path

    def __iter__(self) -> Any:
        return iter([])


@configurable(category="source", group="picker-test")
class PlainSplit:
    """``split`` on a class that is not a HuggingFace one is not the split picker.

    Args:
        split: A fraction view.
        part: Another.
    """

    def __init__(self, split: Literal["train", "val", "test"] = "train", part: str = "a") -> None:
        self.split = split

    def __iter__(self) -> Any:
        return iter([])


@configurable(category="sink", group="picker-test")
class StampedWriter:
    """A producer names the picker of ``output_dir`` itself.

    Args:
        output_dir: A platform dataset id, though named like a folder.
        other_dir: A folder.
    """

    __streamstudio_widget_params__ = {"output_dir": "PLATFORM_DATASET_ID"}

    def __init__(self, output_dir: str = "", other_dir: str = "") -> None:
        self.output_dir = output_dir


@configurable(category="op", group="picker-test")
class PickerOp:
    """An op with a folder parameter, for the form route.

    Args:
        save_dir: Where to write.
    """

    consumes = {"image": "Image"}
    produces = {"image": "Image"}

    def __init__(self, save_dir: str = "") -> None:
        self.save_dir = save_dir

    def __call__(self, record: Record) -> Record:
        return record


def _pickers(cls: type) -> Dict[str, Optional[str]]:
    schema = palette.describe(cls).params_schema
    return {name: fragment.get("picker") for name, fragment in schema.items()}


class TestWhichParameterGetsWhichPicker:
    def test_the_name_rules_and_their_exceptions(self) -> None:
        assert _pickers(PickerWriter) == {
            "output_dir": "directory",
            "checkpoint_path": "file",
            "checkpoint_dir": "directory",
            "checkpoint_strict": None,
            "root": "directory",
            "log_file": "file",
            "name": None,
            "root_count": None,
            "path_mode": None,
            "maybe_folder": "directory",
        }

    def test_a_huggingface_source_gets_the_hub_pickers_and_its_path_is_no_folder(self) -> None:
        assert _pickers(HuggingFacePicker) == {
            "path": "hf_dataset",
            "input_feature": "hf_feature",
            "target_feature": "hf_feature",
            "split": "hf_split",
            "metadata_features": "hf_metadata",
            "cache_dir": "directory",
        }

    def test_split_on_another_class_is_no_hub_picker(self) -> None:
        assert _pickers(PlainSplit) == {"split": None, "part": None}

    def test_a_producers_stamp_wins(self) -> None:
        assert _pickers(StampedWriter) == {"output_dir": "PLATFORM_DATASET_ID", "other_dir": "directory"}

    def test_the_form_carries_the_picker_to_the_page(self, tmp_path: Path) -> None:
        (tmp_path / "write.yaml").write_text(f"flow:\n  save:\n    op: !class:{MOD}.PickerOp {{}}\noutputs: save\n")
        form = create_app(tmp_path).test_client().get("/api/graphs/write/form").get_json()
        assert form["save"]["save_dir"]["picker"] == "directory"


class TestTheFolderListing:
    @pytest.fixture
    def folder(self, tmp_path: Path) -> Path:
        (tmp_path / "graph.yaml").write_text("flow:\n  a:\n    op: !class:recordstream.ops.debug.PrintRecordOp {}\n")
        (tmp_path / "graph.layout.yaml").write_text("positions: {}\n")
        (tmp_path / "model.ckpt").write_text("weights")
        (tmp_path / ".hidden").write_text("")
        (tmp_path / "sub").mkdir()
        return tmp_path

    def _entries(self, client: Any, query: str) -> List[Tuple[str, str]]:
        body = client.get(f"/api/files?{query}").get_json()
        return [(entry["name"], entry["kind"]) for entry in body["entries"]]

    def test_the_graph_browser_lists_folders_and_graphs(self, folder: Path) -> None:
        client = create_app(folder).test_client()
        assert self._entries(client, f"dir={folder}") == [("sub", "dir"), ("graph.yaml", "yaml")]

    def test_a_file_picker_lists_every_file(self, folder: Path) -> None:
        client = create_app(folder).test_client()
        assert self._entries(client, f"dir={folder}&show=all") == [
            ("sub", "dir"),
            ("graph.layout.yaml", "file"),
            ("graph.yaml", "file"),
            ("model.ckpt", "file"),
        ]

    def test_an_unknown_listing_is_refused_naming_the_two(self, folder: Path) -> None:
        response = create_app(folder).test_client().get("/api/files?show=everything")
        assert response.status_code == 400
        assert response.get_json() == {"error": "show must be one of graphs, all — got 'everything'"}

    def test_a_variable_in_the_folder_is_expanded(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path parameter spelled ``$DATA_ROOT/…`` opens its picker where the variable points."""
        monkeypatch.setenv("PICKER_ROOT", str(folder))
        body = create_app(folder).test_client().get("/api/files?dir=$PICKER_ROOT/sub").get_json()
        assert body["dir"] == str((folder / "sub").resolve())

    @pytest.mark.parametrize("spelled", ["$PICKER_UNSET/captures", "${PICKER_UNSET}/captures"])
    def test_a_variable_that_is_not_set_is_named(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
    ) -> None:
        """Measured before: with the variable unset the path was read against the graph folder, and the browser said
        ``no directory <graph folder>/$DATA_ROOT/captures`` — naming a folder nobody typed, not the missing variable."""
        monkeypatch.delenv("PICKER_UNSET", raising=False)
        response = create_app(folder).test_client().get(f"/api/files?dir={spelled}")
        assert response.status_code == 404
        assert response.get_json() == {"error": f"PICKER_UNSET is not set on the editor's machine ({spelled})"}

    def test_a_folder_really_named_with_a_dollar_is_listed(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CON: a folder whose name holds ``$`` and a word is a folder, not a variable that is missing."""
        monkeypatch.delenv("PICKER_LITERAL", raising=False)
        (folder / "$PICKER_LITERAL").mkdir()
        body = create_app(folder).test_client().get(f"/api/files?dir={folder}/$PICKER_LITERAL").get_json()
        assert body["dir"] == str((folder / "$PICKER_LITERAL").resolve()) and body["entries"] == []


class TestTheHubLookups:
    def test_the_cached_datasets_and_why_there_are_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = create_app(tmp_path).test_client()
        monkeypatch.setattr(streamstudio, "cached_hf_datasets", lambda: (["cifar10", "mnist"], ""))
        assert client.get("/api/hf/datasets").get_json() == {"datasets": ["cifar10", "mnist"], "reason": ""}
        monkeypatch.setattr(streamstudio, "cached_hf_datasets", lambda: ([], "no datasets cached at /x/hub"))
        assert client.get("/api/hf/datasets").get_json() == {"datasets": [], "reason": "no datasets cached at /x/hub"}

    def test_the_columns_of_the_named_dataset_and_the_every_other_column_word(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: List[Tuple[str, Optional[str], Optional[str]]] = []

        def features(path: str, name: Optional[str] = None, split: Optional[str] = None) -> List[str]:
            asked.append((path, name, split))
            return ["image", "label", "source"]

        monkeypatch.setattr(streamstudio, "list_hf_dataset_features", features)
        client = create_app(tmp_path).test_client()
        body = client.get("/api/hf/features?path=ylecun/mnist&name=&split=test").get_json()
        assert body == {"features": ["image", "label", "source"], "all_columns": "*"}
        assert asked == [("ylecun/mnist", None, "test")]

    def test_the_splits_of_the_named_dataset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: List[Tuple[str, Optional[str]]] = []

        def splits(path: str, name: Optional[str] = None) -> List[str]:
            asked.append((path, name))
            return ["train", "test"]

        monkeypatch.setattr(streamstudio, "list_hf_dataset_splits", splits)
        client = create_app(tmp_path).test_client()
        assert client.get("/api/hf/splits?path=ylecun/mnist&name=mnist").get_json() == {"splits": ["train", "test"]}
        assert asked == [("ylecun/mnist", "mnist")]

    def test_no_dataset_named_asks_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def never(*args: Any, **kwargs: Any) -> List[str]:
            raise AssertionError("the lookup ran without a dataset")

        monkeypatch.setattr(streamstudio, "list_hf_dataset_features", never)
        monkeypatch.setattr(streamstudio, "list_hf_dataset_splits", never)
        client = create_app(tmp_path).test_client()
        assert client.get("/api/hf/features").get_json() == {"features": [], "all_columns": "*"}
        assert client.get("/api/hf/splits?path=").get_json() == {"splits": []}

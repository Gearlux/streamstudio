"""A *Build Record* node in an *Extract as Op* cone compiles to record ops.

The canvas states the contract by WIRING — ``Extract → Build Record → Extract as Op → op``, every
arrow present. A ``Stream`` cannot run those nodes: *Extract from Record* and *Build Record* are
canvas glue with no recordstream counterpart. So the COMPILER translates the shape, and that is the
whole point of putting it here rather than dropping loose ``RenameField`` nodes on the canvas —
those were equivalent and unreadable, because nothing connected them to the outputs they described
(user instruction 2026-08-26: *"this makes the whole process visible, now it seems a lot of hocus
pocus without connections"*).

The translation, per builder:

* each wired entry is ``{slot name: [extract id, slot]}``; the extract's ``field`` widget says where
  the value came from, the slot NAME says which record key it is promoted to — together, a rename;
* a closing ``SelectFields`` over the entry names, so the record then holds exactly what the
  boundary promised and nothing else;
* the chain is rooted at whatever fed the extracts, so it runs over the incoming record.
"""

from typing import Any, Dict, List

import streamstudio  # noqa: F401  (populates NODE_CLASS_MAPPINGS)
from streamstudio import NODE_CLASS_MAPPINGS
from streamstudio.export import expand_record_builders

BUILDER = "Taidal_RecordBuilder"
EXTRACT = "Taidal_ExtractRecord"
TERMINATOR = "Taidal_SubgraphOp"
RENAME = "Taidal_recordstream_op_structure_RenameField"
SELECT = "Taidal_recordstream_op_structure_SelectFields"


def _extract(record_from: Any, field: str) -> Dict[str, Any]:
    return {
        "class_type": EXTRACT,
        "inputs": {"record": record_from, "field": field, "colormap": "viridis", "max_size": 512},
    }


def _chain(prompt: Dict[str, Any], start: str) -> List[str]:
    """The recordstream OPS reachable backwards from ``start`` through ``record`` links.

    Stops at the first non-op (the source that feeds the chain) — that node is the chain's root,
    not a member of it.
    """
    out: List[str] = []
    cur = prompt[start]["inputs"].get("record")
    while isinstance(cur, list) and cur[0] in prompt:
        node = prompt[cur[0]]
        if not node["class_type"].startswith("Taidal_recordstream_op_"):
            break
        args = {k: v for k, v in node["inputs"].items() if k != "record"}
        out.insert(0, f"{node['class_type'].split('_')[-1]}({args})")
        cur = node["inputs"].get("record")
    return out


def test_the_node_is_registered_and_builds_a_record_from_its_slot_names() -> None:
    node = NODE_CLASS_MAPPINGS[BUILDER]()
    assert node.build(text=3, image="img") == ({"text": 3, "image": "img"},)


def test_an_unwired_slot_does_not_put_a_none_entry_in_the_record() -> None:
    """A socket added then cleared must not satisfy a contract falsely."""
    assert NODE_CLASS_MAPPINGS[BUILDER]().build(text=3, image=None) == ({"text": 3},)


def test_a_builder_becomes_renames_plus_a_closing_select() -> None:
    """The user's own subgraph: field 'label' promoted as 'text', field 'input' as 'image'."""
    prompt = {
        "src": {"class_type": "Taidal_WalkDataset", "inputs": {}},
        "e1": _extract(["src", 0], "label"),
        "e2": _extract(["src", 0], "input"),
        "b": {"class_type": BUILDER, "inputs": {"text": ["e1", 2], "image": ["e2", 0]}},
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["b", 0]}},
    }
    expanded = expand_record_builders(prompt, NODE_CLASS_MAPPINGS)
    assert _chain(expanded, "t") == [
        "RenameField({'src': 'label', 'dst': 'text'})",
        "RenameField({'src': 'input', 'dst': 'image'})",
        "SelectFields({'keys': 'text,image'})",
    ]


def test_the_chain_is_rooted_at_whatever_fed_the_extracts() -> None:
    """It must run over the INCOMING record, not float free."""
    prompt = {
        "src": {"class_type": "Taidal_WalkDataset", "inputs": {}},
        "e1": _extract(["src", 0], "label"),
        "b": {"class_type": BUILDER, "inputs": {"text": ["e1", 2]}},
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["b", 0]}},
    }
    expanded = expand_record_builders(prompt, NODE_CLASS_MAPPINGS)
    head = next(n for n in expanded.values() if n["class_type"] == RENAME)
    assert head["inputs"]["record"] == ["src", 0]


def test_an_entry_already_named_correctly_needs_no_rename() -> None:
    """`field == label` is already the right key — only the close is needed."""
    prompt = {
        "src": {"class_type": "Taidal_WalkDataset", "inputs": {}},
        "e1": _extract(["src", 0], "image"),
        "b": {"class_type": BUILDER, "inputs": {"image": ["e1", 0]}},
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["b", 0]}},
    }
    expanded = expand_record_builders(prompt, NODE_CLASS_MAPPINGS)
    assert _chain(expanded, "t") == ["SelectFields({'keys': 'image'})"]


def test_the_builder_itself_is_gone_from_the_expanded_prompt() -> None:
    """It is canvas glue — a Stream could not run it, so it must not survive into the ops."""
    prompt = {
        "src": {"class_type": "Taidal_WalkDataset", "inputs": {}},
        "e1": _extract(["src", 0], "label"),
        "b": {"class_type": BUILDER, "inputs": {"text": ["e1", 2]}},
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["b", 0]}},
    }
    expanded = expand_record_builders(prompt, NODE_CLASS_MAPPINGS)
    assert not any(n["class_type"] in (BUILDER, EXTRACT) for n in expanded.values())


def test_a_prompt_without_a_builder_is_returned_untouched() -> None:
    prompt = {
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["x", 0]}},
        "x": {"class_type": "Taidal_recordstream_op_torch_ToTensor", "inputs": {}},
    }
    assert expand_record_builders(prompt, NODE_CLASS_MAPPINGS) == prompt


def test_a_builder_fed_by_something_that_is_not_an_extract_is_left_alone() -> None:
    """Only the extract shape carries a `field`; anything else has no rename to derive."""
    prompt = {
        "m": {"class_type": "Taidal_MixRecords", "inputs": {}},
        "b": {"class_type": BUILDER, "inputs": {"thing": ["m", 0]}},
        "t": {"class_type": TERMINATOR, "inputs": {"record": ["b", 0]}},
    }
    expanded = expand_record_builders(prompt, NODE_CLASS_MAPPINGS)
    assert expanded["b"]["class_type"] == BUILDER  # untouched rather than mis-compiled

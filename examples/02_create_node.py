"""Build a ComfyUI node from a plain Python callable and run it.

Demonstrates the core StreamStudio bridge: ``create_comfy_node`` turns any callable
into a ComfyUI node class whose ``INPUT_TYPES`` / ``RETURN_TYPES`` are derived
from the signature and whose ``execute`` method invokes the wrapped function.

Runs headlessly — no ComfyUI process required. CI verifies this exits 0.
"""

import json
import sys

import datasets

from streamstudio.bridge import create_comfy_node


def add_offset(dataset: datasets.Dataset, amount: int = 1) -> datasets.Dataset:
    """Record op: add ``amount`` to every row's ``val`` column."""
    return dataset.map(lambda row: {"val": row["val"] + amount})


def main() -> int:
    node_cls = create_comfy_node(add_offset, "Taidal_add_offset")

    contract = {
        "INPUT_TYPES": node_cls.INPUT_TYPES(),
        "RETURN_TYPES": node_cls.RETURN_TYPES,
        "FUNCTION": node_cls.FUNCTION,
        "CATEGORY": node_cls.CATEGORY,
    }
    json.dump(contract, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")

    # Instantiate and execute exactly as ComfyUI would.
    node = node_cls()
    dataset = datasets.Dataset.from_dict({"val": [1, 2, 3]})
    # node.execute() returns {"ui": {...timing...}, "result": (dataset,)}
    response = node.execute(dataset=dataset, amount=10)
    (result,) = response["result"]

    print(f"# executed node -> val={result['val']}", file=sys.stderr)
    if result["val"] != [11, 12, 13]:
        print("ERROR: unexpected node output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

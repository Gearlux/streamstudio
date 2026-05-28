"""Enumerate the ComfyUI nodes FluxStudio auto-generates from installed packages.

Importing ``fluxstudio`` triggers ``_discover_and_register_nodes`` (see
``fluxstudio/__init__.py``), which walks the ``confluid.configurables``
entry-point group and wraps every compatible callable into a ComfyUI node via
``fluxstudio.bridge``. This dumps the resulting node palette as JSON.

Runs headlessly — no ComfyUI process required. CI verifies this exits 0 and finds
at least one node (``dataflux`` is a hard dependency, so the palette is never
empty).
"""

import json
import sys

import fluxstudio


def main() -> int:
    mappings = fluxstudio.NODE_CLASS_MAPPINGS
    palette = [
        {
            "key": key,
            "display_name": fluxstudio.NODE_DISPLAY_NAME_MAPPINGS.get(key, key),
            "category": node_cls.CATEGORY,
            "inputs": list(node_cls.INPUT_TYPES().get("required", {})),
            "return_types": list(node_cls.RETURN_TYPES),
        }
        for key, node_cls in sorted(mappings.items())
    ]
    json.dump(palette, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")

    categories = sorted({entry["category"] for entry in palette})
    print(
        f"# discovered {len(palette)} node(s) across {len(categories)} category(ies)",
        file=sys.stderr,
    )
    if not palette:
        print("ERROR: entry-point discovery produced no nodes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

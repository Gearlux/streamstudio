"""Print the FluxStudio op palette as JSON.

CI verifies that this script runs to completion and produces a non-empty list.
"""

from __future__ import annotations

import json
import sys

from fluxstudio.discovery import scan_packages


def main() -> int:
    schemas = scan_packages()
    if not schemas:
        print("ERROR: discovery returned no schemas", file=sys.stderr)
        return 1
    json.dump(schemas, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    print(
        f"# discovered {len(schemas)} schema(s) across " f"{len({s['module'] for s in schemas})} module(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

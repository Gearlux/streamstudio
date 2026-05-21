"""Step through `tiny_pipeline.yaml` with the SteppingExecutor and print one
NDJSON event per op application to stdout. CI verifies this exits cleanly and
emits the expected number of events (4 samples × 2 ops = 8 events).
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from confluid.loader import load as confluid_load
from fluxstudio.executor import SteppingExecutor


def main() -> int:
    yaml_path = Path(__file__).parent / "tiny_pipeline.yaml"
    if not yaml_path.exists():
        print(f"ERROR: tiny_pipeline.yaml not found at {yaml_path}", file=sys.stderr)
        return 1

    flux = confluid_load(yaml_path.read_text())
    executor = SteppingExecutor(flux)

    count = 0
    for event in executor.iter_events():
        json.dump(dataclasses.asdict(event), sys.stdout, default=str)
        sys.stdout.write("\n")
        count += 1
    print(f"# emitted {count} step event(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Edit a YAML graph through the editor's routes, without a browser or a port.

The editor is a Flask app over a folder of graphs (``streamstudio.app.create_app``). This example writes a one-step
flow into a temporary folder, changes one setting through ``POST /api/graphs/<name>/param`` — the same call the page
makes when a field is typed in — and saves it with ``POST /api/graphs/<name>/save``. The file on disk then differs
from the original in that one line: an edit is a patch of the line it is about, never a re-dump of the document.

Runs headless: CI executes it and checks that it exits 0.
"""

import difflib
import tempfile
from pathlib import Path

from streamstudio.app import create_app

GRAPH = """\
# One step that prints each record — a comment the edit leaves alone.
flow:
  probe:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: before}
outputs: probe
"""


def main() -> int:
    folder = Path(tempfile.mkdtemp())
    path = folder / "demo.yaml"
    path.write_text(GRAPH)
    client = create_app(folder).test_client()

    nodes = [node["name"] for node in client.get("/api/graphs/demo").get_json()["model"]["nodes"]]
    print(f"nodes: {nodes}")

    answer = client.post("/api/graphs/demo/param", json={"node": "probe", "param": "label", "value": "after"})
    assert answer.status_code == 200, answer.get_json()
    print(f"unsaved after the edit: {client.get('/api/graphs').get_json()[0]['dirty']}")

    saved = client.post("/api/graphs/demo/save", json={})
    assert saved.status_code == 200, saved.get_json()
    diff = difflib.unified_diff(GRAPH.splitlines(), path.read_text().splitlines(), "before", "after", lineterm="")
    print("\n".join(diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

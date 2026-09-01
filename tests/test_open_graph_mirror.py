"""The editor-side graph opener: per-GRAPH workflow documents, a path-keyed save mirror.

String pins over web/open_graph.js — the project verifies frontend behaviour live, but the
two invariants that corrupted or misrouted graphs (2026-08-31) must not regress silently:
every graph gets its OWN ComfyUI workflow document, and a save mirrors ONLY to the graph
file its own document was opened from.
"""

from pathlib import Path

_JS = Path(__file__).resolve().parents[1] / "streamstudio" / "web" / "open_graph.js"


class TestPerGraphDocuments:
    def test_the_workflow_path_keeps_the_graph_stem(self) -> None:
        js = _JS.read_text()
        assert 'workflows/taidal/${owner || "graphs"}/${stem || "graph"}.json' in js, (
            "collapsing to the workspace name alone made every graph share ONE document — "
            "opening echo landed on the efficientvit canvas"
        )


class TestThePathKeyedMirror:
    def test_the_single_target_key_is_gone(self) -> None:
        assert "taidal.sourceFile" not in _JS.read_text()

    def test_saves_mirror_only_their_own_document(self) -> None:
        js = _JS.read_text()
        assert "taidal.graphFiles" in js, "a MAP: workflow document -> graph file"
        assert (
            'decoded.includes("/userdata/" + workflowPath)' in js
        ), "a save of any OTHER workflow in the tab must never touch this workspace's graphs"

    def test_the_app_mode_derivative_mirrors_to_the_same_file(self) -> None:
        """ComfyUI's App/Linear view saves the workflow as `<stem>.app.json` — the same
        document under its app identity. Unmapped, a parameter typed into the app form
        (an update sink's output directory) saved to the derivative and the workspace's
        graph file kept the stale value (measured 2026-09-01)."""
        js = _JS.read_text()
        assert 'workflowPath.replace(/\\.json$/, ".app.json")' in js

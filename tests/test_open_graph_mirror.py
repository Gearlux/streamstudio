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


class TestTheRequestedView:
    """A launcher may say which FACE to open a graph on: ``taidal_view=app`` is the settings
    form (the App view), ``taidal_view=graph`` the node canvas (user request 2026-09-05).

    The frontend derives the opening mode from the DOCUMENT's ``extra.linearMode`` at load, so
    the choice is applied to the in-memory workflow BEFORE it is handed to ``loadGraphData`` —
    both load paths receive that same object, so one mutation covers both.
    """

    def test_the_view_param_is_read_and_stripped_like_the_graph_param(self) -> None:
        js = _JS.read_text()
        assert 'url.searchParams.get("taidal_view")' in js
        assert 'url.searchParams.delete("taidal_view")' in js, "never left in the URL for a reload to replay"

    def test_the_view_is_applied_BEFORE_either_load_path(self) -> None:
        js = _JS.read_text()
        applied = js.index("const view = applyRequestedView(workflow, requestedView);")
        assert applied < js.index("opened = await openAsNamedWorkflow(workflow, graphPath);")
        assert applied < js.index("if (!opened) await loadUntilItSticks(workflow);")

    def test_app_sets_the_flag_and_graph_clears_it(self) -> None:
        body = _JS.read_text().split("function applyRequestedView")[1].split("async function applyOpenParams")[0]
        assert 'workflow.extra.linearMode = view === "app";' in body

    def test_a_graph_without_a_form_falls_back_to_the_canvas_and_says_so(self) -> None:
        """An App view over a graph that declares no ``linearData`` would be an empty form —
        the failure mode the importer refuses at build time, refused again here at open time."""
        body = _JS.read_text().split("function applyRequestedView")[1].split("async function applyOpenParams")[0]
        assert "linearData" in body and 'return "graph";' in body
        assert "declares no App view" in body

    def test_asking_for_a_view_re_navigates_a_graph_already_on_screen(self) -> None:
        """The duplicate-tab guard must not swallow a request for the OTHER face of the same
        graph — that is exactly the case where the canvas already shows it."""
        js = _JS.read_text()
        assert "if (fingerprint() === want && !requestedView)" in js

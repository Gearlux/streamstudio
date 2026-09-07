"""The editor-side graph opener: per-GRAPH workflow documents, a path-keyed save mirror.

String pins over web/open_graph.js — the project verifies frontend behaviour live, but the
two invariants that corrupted or misrouted graphs (2026-08-31) must not regress silently:
every graph gets its OWN ComfyUI workflow document, and a save mirrors ONLY to the graph
file its own document was opened from.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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

    The face is driven the way the frontend drives it (read off its 1.44 bundle): the
    workflow OBJECT's ``initialMode`` — which the frontend derives from the file's flag only
    while the object has none — then ``activeMode`` once toggled. The first cut set the flag
    on the in-memory document instead, and a document already opened once in the session
    kept its first face: ``settings`` after ``graph`` stayed on the canvas (user report
    2026-09-06). The document is never mutated now, so the save mirror cannot flip the
    file's own flag on every open either.
    """

    def test_the_view_param_is_read_and_stripped_like_the_graph_param(self) -> None:
        js = _JS.read_text()
        assert 'url.searchParams.get("taidal_view")' in js
        assert 'url.searchParams.delete("taidal_view")' in js, "never left in the URL for a reload to replay"

    def test_the_document_is_never_mutated_for_the_view(self) -> None:
        js = _JS.read_text()
        assert "workflow.extra.linearMode" not in js
        assert "applyRequestedView" not in js

    def test_the_face_is_set_on_the_stores_workflow_object_before_the_load(self) -> None:
        body = _JS.read_text().split("async function openAsNamedWorkflow")[1].split("\n}\n")[0]
        assert "if (view) persisted.initialMode = view;" in body
        assert body.index("persisted.initialMode = view") < body.index(
            "await app.loadGraphData(workflow, true, true, persisted)"
        )
        assert "return persisted;" in body, "the caller needs the object to settle against"

    def test_after_the_load_the_face_is_toggled_only_on_a_measured_mismatch(self) -> None:
        body = _JS.read_text().split("async function settleOpen")[1].split("\n}\n")[0]
        assert "if (view && currentMode() !== view)" in body
        assert 'command.execute("Comfy.ToggleLinear")' in body
        mode = _JS.read_text().split("function currentMode()")[1].split("\n}\n")[0]
        assert "active.activeMode || active.initialMode" in mode, "the frontend's own expression"

    def test_the_named_path_verifies_the_canvas_holds_the_graph(self) -> None:
        """A late session restore replaced our load and the console still said 'loaded'."""
        js = _JS.read_text()
        assert "const settled = await settleOpen(workflow, want, view, persisted);" in js
        body = js.split("async function settleOpen")[1].split("\n}\n")[0]
        assert "if (fingerprint() !== want)" in body
        assert (
            "store.openWorkflow(persisted)" in body
            and "app.loadGraphData(workflow, true, true, persisted || null)" in body
        )

    def test_a_graph_without_a_form_falls_back_to_the_canvas_and_says_so(self) -> None:
        body = _JS.read_text().split("function requestedViewFor")[1].split("\n}\n")[0]
        assert "linearData" in body and 'return "graph";' in body
        assert "declares no App view" in body

    def test_asking_for_a_view_re_navigates_a_graph_already_on_screen(self) -> None:
        js = _JS.read_text()
        assert "if (fingerprint() === want && !requestedView)" in js

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the extension's decision logic")
    def test_the_settle_loop_behaves(self, tmp_path: Path) -> None:
        """The pure parts, run under node with a stubbed app: what settleOpen does in each
        measured situation — content right + face right, content right + face wrong, content
        replaced by a late restore, and no face asked for."""
        js = _JS.read_text()

        def fn(name: str) -> str:
            marker = f"function {name}("
            start = js.index(marker)
            start = js.rfind("\n", 0, start) + 1  # include a leading `async `
            return js[start : js.index("\n}\n", start) + 3]

        harness = "\n".join(
            [
                "const log = [];",
                "console.log = (...a) => log.push(a.join(' '));",
                "let nodes = [], store, app;",
                "const sleep = async () => {};",
                fn("fingerprint"),
                fn("requestedViewFor"),
                fn("currentMode"),
                fn("settleOpen"),
                "function setup(showing, mode) {",
                "  nodes = showing; const active = { activeMode: null, initialMode: mode };",
                "  store = { activeWorkflow: active, openWorkflow: async () => { nodes = ['bte']; } };",
                "  const shown = () => active.activeMode || active.initialMode;",
                "  const flip = () => { active.activeMode = shown() === 'app' ? 'graph' : 'app'; };",
                "  app = { graph: { get nodes() { return nodes.map((t) => ({ type: t })); } },",
                "    extensionManager: { workflow: store,",
                "      command: { execute: async (id) => { calls.push(id); flip(); } } },",
                "    loadGraphData: async () => { calls.push('load'); nodes = ['bte']; } };",
                "}",
                "let calls; const out = {};",
                "calls = []; setup(['bte'], 'app');",
                "out.rightAndApp = [await settleOpen({}, 'bte', 'app', {}), calls];",
                "calls = []; setup(['bte'], 'graph');",
                "out.rightButGraph = [await settleOpen({}, 'bte', 'app', {}), calls, currentMode()];",
                "calls = []; setup(['fsk'], 'graph');",
                "out.lateRestore = [await settleOpen({}, 'bte', 'graph', {}), calls];",
                "calls = []; setup(['bte'], 'graph');",
                "out.noFaceAsked = [await settleOpen({}, 'bte', null, {}), calls];",
                "out.decide = [requestedViewFor({extra: {linearData: {inputs: [[1, 'x']]}}}, 'app'),",
                "  requestedViewFor({extra: {}}, 'app'), requestedViewFor({}, 'graph'), requestedViewFor({}, null)];",
                "out.log = log;",
                "process.stdout.write(JSON.stringify(out));",
            ]
        )
        script = tmp_path / "harness.mjs"
        script.write_text(harness)
        run = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
        out = json.loads(run.stdout)
        assert out["rightAndApp"] == [True, []], "nothing to do: no toggle, no reload"
        assert out["rightButGraph"] == [True, ["Comfy.ToggleLinear"], "app"], "one toggle, then the face reads app"
        assert out["lateRestore"] == [
            True,
            [],
        ], "the named document is re-activated (openWorkflow), no toggle for graph"
        assert out["noFaceAsked"] == [True, []], "no view asked: the face is left alone"
        assert out["decide"] == ["app", "graph", "graph", None]
        assert any("declares no App view" in line for line in out["log"])

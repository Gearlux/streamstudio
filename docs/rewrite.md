# streamstudio rewrite — scope proposal

*2026-09-27. A proposal, not an implementation. Every number below was measured on this
machine; the prototypes live outside the repository. Nothing here is built until the scope
is approved, then the tests, then the code.*

## 0. What is already decided

- streamstudio is rewritten as a **Flask app**; ComfyUI is dropped.
- The canvas is **React Flow** (`@xyflow/react`), built with vite like `navigaitor/frontend`.
- The **confluid YAML document is the only truth**; the canvas is a drawing of it.
- An **engine executes and debugs** a graph: run one record through it, stop at a node,
  inspect what every node received and produced, rerun one node with new parameters.
- Nodes are typed from the per-node `Input`/`Output` declarations (`recordstream.algorithm`)
  and the per-graph `GraphContract` (`recordstream.ops.contract`).
- annotaide **mounts** the editor inside its own Flask app; streamstudio also serves it
  standalone.

This document asks you **one** question (§6) and states every other default it takes.

## 1. What changes for you

| You do | Today | After |
| --- | --- | --- |
| press *set source…* in annotaide | a second server boots on port 8188, a waiting room shows for tens of seconds, a ComfyUI tab opens | the editor opens at once as a pane of annotaide, same address |
| save the graph | `source.json` is written, a converter child turns it into `source.yaml`, every start re-checks the copy | the editor patches `source.yaml` in place; there is no `.json`, no converter, no re-check |
| add a parameter to an op class | every saved canvas shifts its values onto the wrong parameters | nothing moves: values are stored by name |
| install a new op | restart the editor, or the canvas converts wrongly | reload the page |
| press *run* on a pane graph | ComfyUI's queue runs hidden preview nodes | the engine runs the record you have selected and shows, per node, what came in, what went out, how long it took |
| a node produced nothing | read the report and edit the YAML by hand | click the node, change a value, rerun it alone; the nodes after it recompute, the ones before do not |
| an LLM investigates a pane | it can call one hand-written route per feature | it gets `read_graph`, `run_graph`, `rerun_node`, `set_param`, `add_step`, `remove_step`, plus one tool per node, all generated from the declarations |

## 2. The four mechanisms, measured

### 2.1 The document is the truth

The editor never keeps a second document. It loads the YAML with the markers intact,
derives a model, and writes changes back **by patching the line the marker came from**.

```python
doc   = confluid.load(path, until="document")     # markers kept, each stamped file:line:col
model = derive(doc)                               # nodes, typed sockets, wires, problems
pos   = positions(model, path.with_suffix(".layout.yaml"))   # sidecar keyed by node NAME
```

Measured on the shipped Bluetooth pane, `waivefront/annotation/bte_view.yaml` (275 lines,
148 of them comments, 24 steps):

```
confluid.load(until='document') 5 ms; derive 8.8 ms
nodes=24  wires=49  by kind={'record': 23, 'gate': 9, 'entry': 17}
gate wires (requires: X -> the earlier step whose flags raise X):
  pick.plain->burst, pick.smooth->smooth, classify.ble->ble, classify.try_weak->weak, ...
entry wires (what a node reads from what an earlier node wrote):
  pick.burst_pick->smooth.burst_pick [BurstPick], gap.burst->content.burst [Burst], ...
```

So the drawing shows three things the ComfyUI canvas never could: which node a record came
from, which earlier decision gates a node, and which node produced the entry a node reads.

**Write-back, PRO and CON side by side.** Re-dumping the document loses the file;
patching the marker's line keeps it:

```
confluid.dump(bte_view):   275 lines / 148 comments  ->  179 lines / 0 comments
                           root: $DATA_ROOT/pets     ->  root: /data/gert/pets   (the variable is baked in)
patch_scalar(size.max_band_hz = 30e6):
  lines differing = 1  ->  [(61, '      max_band_hz: 25.0e6', '      max_band_hz: 30000000.0')]
  comment lines kept = 148, '$DATA_ROOT' kept; reload: everything else identical: True
```

**A new constructor parameter between save and load** — the case that bit you in
practice. The class `Refuse(max_band_hz, max_duration_s, hint)` gains `note` as its
FIRST parameter:

```
saved document:  size: !class:ops_proto.Refuse {max_band_hz: 25.0e6, max_duration_s: 25.0e-3, hint: a Bluetooth packet ...}
saved canvas:    widgets_values = [25000000.0, 0.025, 'a Bluetooth packet ...']     (positional, no names)

after the change:
document -> {'note': '', 'max_band_hz': 25000000.0, 'max_duration_s': 0.025, 'hint': 'a Bluetooth packet ...'}   nothing shifts
canvas   -> {'note': 25000000.0, 'max_band_hz': 0.025, 'max_duration_s': 'a Bluetooth packet ...'}                every value one slot off, hint lost
```

**Wire types are refused at the model, before anything runs:**

```
! cannot wire decode.image (Image) into count.label (Label) at <file>:4:9: the types do not meet
```

Everything a graph needs already has a spelling; nothing is added to confluid:

| need | spelling that exists |
| --- | --- |
| an input the host fills (the selected record, the window) | `GraphContract.inputs` |
| an input the author must fill | `Mandatory[T]` on the class |
| one node reading another node's output | `bind: {low_level: floor.noise_floor_db}` |
| a subgraph reused by several graphs | `include: subflow.yaml` inside `flow:`, or `steps: {include: prep.steps.yaml}` inside a `Subgraph` step (§6b) |
| node positions, notes | the `.layout.yaml` sidecar, keyed by node name |

### 2.2 The engine: run, stop, inspect, rerun

A tracer wraps every node in a probe and runs the graph through recordstream's **own**
kernel, so what it shows is what a real run does. It lives in recordstream
(`recordstream/flow/trace.py`) because it needs the kernel's internals, and it imports no
Flask.

```python
t = Tracer(graph)                    # a Stream or a FlowGraph; nothing built until used
t.check(seed)                        # static: every node's needs against what is available before it
t.run(seed, until="gated")           # stops BEFORE 'gated' with everything so far recorded
t.step(); t.resume()
t.rerun_from("floor", percentile=75) # rebuilds ONE node through its constructor, reruns from there
t.value("gated", "mask")             # the real array, only when asked
t.to_dict()                          # the JSON the page and the MCP tools consume
```

Measured, N=200 records of 64x64:

| | ms per record |
| --- | --- |
| plain `Stream` | 0.107 |
| traced, snapshots by reference | 0.115 |
| traced, deep copies | 0.177 |

One 1024x1024 record through four nodes keeps 5 MiB by reference, 37 MiB deep-copied. No
op in either demo edited its record in place; the trace flags it when one does.

Rerun one node on the six-node IQ graph, which forks at `mag` and re-binds a threshold from
the recomputed floor:

```
rerun_from('floor', percentile=75.0)  1.95 ms   (the full run was 5.94 ms)
generations: {'spec': 0, 'mag': 0, 'floor': 1, 'level': 1, 'gated': 1, 'boxes': 1}
noise_floor_db: -9.03  ->  threshold bound on gated: 2.97  ->  bin_boxes: count 1
```

A bad parameter is refused by the constructor and the trace is untouched:

```
rerun_from('floor', percentile=150) -> ValidationError: percentile Input should be less than or equal to 100
floor params still: {'percentile': 75.0}
```

A node whose input nothing produces is refused **before** anything runs:

```
ChainContractError: image_chain:s0: BackgroundLevel needs the record entry 'image', which nothing
before it produces — the chain has class, picture at that point
nodes reached: [] | result: None
```

One JSON entry of the trace (arrays are summarised, never dumped):

```json
{"node": "gated", "op": "Threshold", "status": "ok", "ms": 0.034,
 "params": {"field": "spectrogram", "output": "mask"}, "bound": {"low_level": 2.626},
 "input":  {"spectrogram": {"type": "Spectrogram", "shape": [256, 511], "min": -32.2, "max": 27.9}},
 "output": {"mask": {"type": "Mask", "shape": [256, 511], "true_fraction": 0.0064}},
 "changed": ["mask"]}
```

Three corrections the critic measured, folded into the scope:

- The static check must carry raised flags across nodes. Checked node by node it refuses
  every gated node of the shipped pane (`SelectBurst is gated on the flag 'plain', which no
  node before it raises`); the whole-chain check passes, and the run itself ran all 24
  nodes in 4.7 ms.
- A rerun rebuilds the node from its **current** values plus the change, through the
  constructor. Rebuilding from the constructor's captured kwargs drops what the host set
  afterwards (annotaide fills the window that way); setting the attribute directly
  bypasses validation and leaves the live op poisoned. Measured both ways.
- The tracer's constructor does no work; parsing and building happen on first use.

### 2.3 The canvas: React Flow

Chosen after measuring fourteen libraries. React Flow 12.12: MIT, 13.5 million weekly
downloads, 112 commits in the last 90 days, React 18, the toolchain navigaitor already
builds with.

| ready-made | we build |
| --- | --- |
| pan, zoom, minimap, snap to grid, box select | socket colours and the type test (about 30 lines) |
| groups that carry their children | one generic node component whose widgets come from the node spec |
| a wire refused while dragging, through one callback | the search palette on double-click and on a dropped wire |
| load and save as plain JSON | undo/redo and copy/paste over the two arrays (about 150 lines) |
| dagre layout from the free example | collapse to subgraph: a node pointing at an `include:` file |

Proof: a 685-line prototype built with navigaitor's exact toolchain (React 18.3, vite 5.4,
TypeScript 5.9), one generic node component for all 400 catalogue classes, driven headlessly
under playwright with real mouse drags. Zero console errors. Measured:

```
typed sockets    isValidConnection Image->Label False | Image->Image True
                 edges after a real drag Image->Label 0 | after Image->Image 1
widgets          number 20->42, combo fast->hybrid, toggle true->false through the DOM
                 'convert to socket' on bandwidth: handles ["in:image","out:image"] -> [...,"in:bandwidth" (Number),...]
palette          double-click -> 400 entries in 6.9 ms; typing 'TuneToBand' -> 16 / 400
                 wire dropped on empty canvas -> 68 / 400 accepting Image; pick -> node placed and wired
groups           group dragged +110,+69 px: both children moved by the same delta, relative positions unchanged
subgraph         a node pointing at document 'sub-1': Expand swaps the canvas, back to main keeps all 8 nodes
undo/redo/paste  history.ts = 77 lines; add, move, undo x2, redo, copy 3, paste 3: count 9 -> 12; ctrl+z undoes the paste
dagre layout     40 nodes, 39 edges: 9 ms, bounding box 5442 x 845 px, ranks 262 px apart
round trip       toDoc -> load -> toDoc: 2278 bytes in, 2278 out, identical
load             60 nodes from 60 classes, production build: 72 ms to all nodes measured (3 runs: 75, 72, 73)
bundle           392 KB JS, 128 KB gzipped, 3 KB CSS; build 1.2 s
```

Two things to carry into the real build: React Flow 12 has no double-click event on the
pane, so the wrapper listens and zoom-on-double-click is off; and pasting a group's child
without its group lands at the parent-relative offset, so paste reads absolute positions.
ELK is not needed: a pipeline is a one-direction DAG, which is exactly dagre's model.

### 2.4 Tools an LLM gets, generated from the declarations

Nothing is hand-written per tool. Graph tools come from `GraphContract`; per-node tools
from `algorithm_spec` or `confluid.input_specs`/`output_specs`. All 24 node tools of the
Bluetooth pane registered on the real `MCPServer` and were advertised:

```json
{"name": "bte__floor_db",
 "input_schema": {"properties": {"run": {"type": "string"},
                                 "percentile": {"type": "number", "default": 25.0, "minimum": 0.0, "maximum": 100.0}},
                  "required": ["run"]}}
```

One real session over a synthetic BLE advertisement (the tool calls and answers are the
prototype's, verbatim):

```
>>> run_graph(graph="bte", record="synthetic_ble_adv", x0=2401e6, x1=2403e6)
<<< classify  ClassifyBluetooth  found: not a Bluetooth burst — envelope spread 0.6 dB ...  flags={'try_weak': True}
    ble       DecodeBleAdvertising  SKIPPED because ble is false (ClassifyBluetooth)

>>> rerun_node(run="run_a4f2b231", node="classify", params={"envelope_spread_db": 6.0})
<<< node_outputs: {"flags": {"ble": true, ...}, "value": "BLE"}
    downstream_diff: ble  before: skipped  after: ran, ADV_IND 31 B, CRC ok
                     weak before: ran      after: skipped (try_weak is false)

>>> set_param(graph="bte", node="classify", param="envelope_spread_db", value=6.0)
<<< written line 179: '      envelope_spread_db: 0.5' -> '      envelope_spread_db: 6.0'   comment lines 148 -> 148

>>> add_step(graph="bte", name="floor_db", after="floor", class_path="...NoiseFloorEstimate", params={"percentile": 30})
<<< refused: NoiseFloorEstimate needs the record entry 'spectrogram', which nothing before it produces
    (file unchanged: 275 lines)
```

Against LangGraph, on the axes you named: the agent can add and remove steps at run time
(LangGraph compiles the graph first and only routes dynamically); the graph is a data file
(LangGraph's is Python); every node declares typed inputs and outputs a checker can refuse
on (LangGraph has one shared state); one node can be rerun alone with its cached input
(LangGraph replays from a checkpoint and re-executes everything after it).

## 3. Where the code lives, what is deleted

| project | new | deleted |
| --- | --- | --- |
| recordstream | `flow/trace.py` (the tracer, 797 lines) | nothing |
| streamstudio | `app.py` (blueprint + `create_app`), `document.py` (derive, patch, history, sidecar), `palette.py` (registry → node specs), `layout.py`, `cli.py` (bare `streamstudio` standalone; `--editor comfy` for ComfyUI until phase 4), `frontend/` (vite + React Flow), tests | `bridge.py`, `graphio.py`, `export.py`, `nodes.py`, `web/*.js`, the ComfyUI clone and launch, 12,857 Python lines and 3,705 JS lines. The HuggingFace pickers and the directory chooser are ported as Flask routes. |
| annotaide | mounts the blueprint; *set source…*, *graph*, *settings* open it in a pane | the resident converter, seeding through the CLI, the graph-copy re-check, the waiting room, tab naming: 743 lines and 30 rules |
| navigaitor | registers the graph tools once and the per-node tools of any graph a session opens | nothing |
| the op author | writes the op class only | |

Node naming is one scheme everywhere: a flow step is named by its key; a member of a list
is `stream.ops[1]`. The engine, the sidecar, the tools and the canvas all use it.

## 4. Templates

17 of the 27 shipped canvases exist only as ComfyUI JSON. All 17 convert with today's
converter and all 17 load again, but none carries a graph contract, and the source and sink
graphs annotaide draws are rooted in one. Phase 0 therefore converts each, wraps the
source and sink graphs in their contract with the converted entries under `delivered:`,
runs `check()` on every one, and keeps JSON and YAML side by side until the flip.

## 5. Defaults this scope takes

- Write-back patches the located line; a value the file left at its default is inserted
  under its node; a re-dump is never used.
- Undo is one text history per open document, shared by canvas edits and agent edits.
- Renaming a node is one editor action: the key, every `from:`/`bind:`/`merge_from:` that
  points at it, and the sidecar entry. No ids are added to the YAML.
- A run has an id; agents refer to it; the page shows the latest run of its graph.
- Per-node tools are registered for the graphs a session has opened, not for every pane
  at once (11 panes times 24 steps would drown the tool list).
- A node that declares only the TYPE it writes, not the entry name, is marked
  "unverifiable" and the run shows the truth; the by-name refusal stays for complete
  declarations. The 12 signal ops that declare tuples are reported to recordstream's
  backlog, not fixed here.
- The `app:` block in 7 shipped pane files stays as the settings form's field list; the
  `__lo`/`__hi` names go.
- A `Stream` with an `ops:` list is drawn as a chain of record wires between the ops.
- A hand-drawn wire is a PROPOSAL until a route can spell it (phase 3 part 1, 2026-09-28): no phase-2 route writes
  a wire — a Stream's record wires ARE its `ops:` order, a flow's are `from:` / `bind:`, a contract's `!ref:` — so
  a drop whose types meet stays dashed on the canvas, a toast names the spelling the document would need, the file
  is untouched, and the next model refresh drops it. A mismatch is refused WHILE dragging and never becomes an edge.
- Two datasets in one graph are the existing `JointStream`, drawn with one socket per stream.
- Taken while landing phase 3 part 2 (2026-09-28): undo is TWO histories under one gesture — a document edit is
  undone by the document's own text history on the server (`POST …/undo`, shared with an agent's edits), a move, an
  arrange or a group by the page, which recorded its before and after; groups live in the sidecar's `groups:` block
  only, stored as the page spelled them (`docs/architecture.md` has both records). A rerun from the run panel sends
  only the values that differ from what the run's node holds, and a value typed there is a trial, never written.
- Taken while closing phase 3 (2026-09-28), gathered from what landed:
  - **The wire rule** stands as written above for EVERY document edit the canvas cannot route — today a drawn wire.
    The page never writes a spelling the document did not state; a missing route is an item in
    `streamstudio/TASKS.md`. Adding to and removing from a `Stream`'s `ops:` list became routes on 2026-09-29 (§6c).
  - **Groups live in the sidecar only**, stored as the page spelled them; the server reads inside a group only to
    follow a rename.
  - **Positions and the layout history are the page's.** The server stores what the page sends (`PUT …/positions`,
    `…/groups`); the before/after of a move, an arrange or a group lives in the page's timeline for as long as the
    page is open, and never enters the document's text history.
  - **The built page is not in the wheel.** `frontend/dist` is gitignored, outside `package-data`, not built by CI
    (navigaitor's precedent); an install without it serves the API and a placeholder naming the build command.
    Shipping it is a release item for the flip (`streamstudio/TASKS.md`).
  - **The palette draws at most 200 rows**; typing narrows them. Paste inserts the copied steps' own YAML text (since
    2026-09-29), so `2.0` stays `2.0` and comments come along.

Taken while landing phase 2 (2026-09-28):

- The standalone editor is bare `streamstudio <graph_dir>` on port 8190, and the ComfyUI editor is
  `streamstudio --editor comfy` on 8188; the two servers coexist on one machine (§6a, decision 3).
- The HuggingFace pickers and the directory chooser arrive in phase 3 as routes beside the page that
  calls them; phase 2 ships no route without a caller. **Not landed with phase 3** (2026-09-28): no node of the page
  calls one yet, so the same rule keeps them out; they are deferred to `streamstudio/TASKS.md` and must land before
  phase 4 deletes the ComfyUI versions.
- The first layout is streamstudio's own layered one (`layout.py`: longest-path layering along the
  wires, one column per layer; a node the sidecar names keeps its place, a new node lands below the
  lowest kept one), not dagre on the server — the page may still run dagre client-side.
- The settings form is `confluid.to_pydantic(cls).model_json_schema()` per node
  (`GET /api/graphs/<name>/form`): an `Interval` is already `minimum`/`maximum` and a `Literal` already
  `enum`, so the form, the palette's widgets and the phase-5 tool schemas cannot disagree.

## 6. The one decision: the class names of a source graph

The source graph must deliver the class vocabulary. Four spellings, measured:

```
1 slot 'class_names', literal list          loaded; check(): ok
2 slot 'class_names', wired producer         LOAD REFUSED: Failed to construct Stream ... StreamConfig
3 slot renamed 'classes', wired producer     loaded; delivered = {'classes': ClassNamesOutput}; the producer declares ['cat', 'dog']
4 slot renamed 'classes', wired to a FilesSource   loaded; that source declares no class names
```

Case 2 fails because `class_names` is also a constructor parameter of `Stream`, so confluid
broadcasts the wired producer into the Stream beside it. That is a name clash, not a
missing feature.

- **A, recommended:** the slot is named `classes`. You may type a list, or wire a source or
  producer that declares its class names; the document keeps the producer, the contract
  reads its declared vocabulary, and case 4 is refused by name: "FilesSource declares no
  class names; wire a source that does, or type them". The contract also refuses a slot
  whose name is a parameter of any class the graph uses, at declaration time.
- **B:** the slot stays `class_names` and only a typed list is accepted. Wiring a producer
  makes the editor evaluate it at save time and write the list, remembering the producer
  in the sidecar. Simpler, but the document then no longer says where the names came from.

**Decided 2026-09-28: A.** The slot is `classes` (`recordstream.ops.contract.CLASS_VOCABULARY_SLOT`,
the one place the name is spelled). Measured after landing, through the ComfyUI canvas export
(`streamstudio/tests/test_graph_contract_canvas.py`):

```
outputs: {stream: Stream, classes: "List[str]"}      exported; check(): classification source: 'classes' is not delivered — wire it in the graph (this graph delivers: stream, classes)
outputs: {stream: Stream, class_names: "List[str]"}  exported; check(): classification source: output 'class_names' shares its name with a parameter of Stream (delivered as 'stream'); confluid would push the slot's value into that parameter — rename the slot (the class vocabulary slot is 'classes')
```

The first line is the open socket a template leaves for its author; the second is the CON case,
refused by name before anything is built.

## 6a. The menu scope — three decisions (decided 2026-09-29)

The editor's menus, Nodes sidebar, right-click menus and groups were proposed with three open questions. The
answer was "go with the defaults" — option A each time. All three landed on 2026-09-29.

| # | the question | decided | what you see now |
| --- | --- | --- | --- |
| 1 | When does an edit reach the file? | **A** — changes stay in the editor until Save (B was: every edit written at once, as in phases 2-3) | `● demo.yaml` while unsaved; File ▸ Save writes; a file changed on disk is refused: `…/demo.yaml changed on disk since the editor read it — Revert to load it, or Save As to keep both` |
| 2 | What do Open and Save As browse? | **A** — a file browser of the machine the editor runs on, starting in the graph folder (B was: a browser upload) | `GET /api/files`; folders first, then `.yaml` files |
| 3 | Which editor does bare `streamstudio` start? | **A** — the canvas editor on 8190; `streamstudio --editor comfy` starts ComfyUI on 8188, and the consuming annotation workspace asks for ComfyUI by name until phase 4 | `streamstudio --comfy-dir /x` → `--comfy-dir only applies to --editor comfy` |

The menu bar (ComfyUI style, only entries that do something):

```
New       Flow graph… | Pipeline… | Source graph (with its contract)…
File      Open… Ctrl+O · Save Ctrl+S · Save As… Ctrl+Shift+S · Revert
Edit      Undo Ctrl+Z · Redo Ctrl+Shift+Z · Cut / Copy / Paste · Delete · Select all · Rename… F2
View      Nodes sidebar N · Fit to view · Arrange · Arrange in columns · Minimap
Graph     Add group · Group selection G · Convert selection to subgraph · Insert template ▸ · Save selection as template… · Delete template ▸ · Check graph
Run       Run Ctrl+Enter · Step · Rerun selected node
```

## 6b. Subgraphs — four decisions (decided 2026-09-29)

A subgraph is written INLINE, as an op: `op: !class:recordstream.flow.subgraph.Subgraph {steps: {…}, result: scaled}`
— no new YAML syntax, no confluid change. Four questions were put, each answered; all four landed on 2026-09-29.

**1. Using one subgraph twice.** The user: *"if a subgraph is needed twice (either copy it, use templates like
comfyui or use the include construction)"*. So three ways and only these: Copy/Paste (an independent copy of the
block, comments kept), templates (a steps file in `~/.streamstudio/templates` or `--templates-dir`, inserted as a
copy), and `steps: {include: prep.steps.yaml}` (Move to its own file… / Insert steps file…; every use gets its own op
objects, and an edit through one use changes every use). YAML anchors and `!ref:` are NOT reuse spellings — measured,
an anchor merged into `flow:` runs first whatever line it is on and a second use vanishes; a `!ref:` makes both uses
one object, so one use's `@output` read the other's value (`0.7145` instead of `0.2855`). Rationale:
`docs/architecture.md` → "Reuse is a copy, a template or an include".

**2. An outer step reading a value inside a subgraph** (`bind: {value: measure.level}`, `level` an inner step of
`measure`). Decided: **A — refused before anything runs**, saying what to do:

```
flow step 'stamp': bind value='measure.level' reads 'level' of 'measure', which is a subgraph — a step outside a
subgraph cannot read a value of a step inside it; move that step out of the subgraph
```

**3. The result line.** Decided: **A — `result: scaled`**. Written `outputs:`, the file's own top-level
`outputs: boxes` is broadcast into a same-named constructor parameter; measured: the run failed with
`flow: outputs 'boxes' does not name a step`.

**4. How an inner node is named.** Decided: **A — `prep/grey`**, on the canvas, in the run panel and in errors (a
Stream member: `stream.ops[1]/grey`; nested: `prep/inner/x`). A step name may therefore not contain `/`:

```
flow: step name 'prep/grey' may not contain '/' (reserved for the nodes inside a subgraph: 'prep/grey' is the step
'grey' inside the subgraph 'prep')
```

The tracer lists `['prep', 'prep/grey', 'prep/scaled', 'mask']`; `run(until='prep/scaled')` pauses inside.

## 6c. Round 2 — four answers (2026-09-29)

The review of 3b put four questions. The user's answers, verbatim, and what each became:

| # | the question | the answer | what you see now |
| --- | --- | --- | --- |
| 1 | `stop` / `restart` drop unsaved edits — keep them? | *"streamstudio stop or restart should save the file to a tmp workspace file (hidden), when opening that same workspace streamstudio should ask to start from the tmp file or the original"* | landed: the stop writes `<folder>/.streamstudio-workspace.json` and says so; the next start prints `Unsaved work from the last session (29 Sep 20:06): demo.yaml — the page asks whether to restore it.` and the page asks **Restore unsaved work** / **Open the saved files** before it opens a graph |
| 2 | a graph made with New… by mistake stays in the list — a "clear workspace"? | *"clear workspace ?"*, then: *"New ... by mistake should not happen. It should ask to save."* | landed in round 3: there is no "clear workspace" — File ▸ New… asks a **Name** and a **Folder** (Browse… for another), shows the file it will write (`→ /home/me/graphs/fresh.yaml`), and **Create** writes the starter at once and opens it with no ●; a file that is already there is refused and left byte for byte — `/home/me/graphs/fresh.yaml already exists — pick another name, or open it with File ▸ Open` |
| 3 | Pipeline graphs: should adding and removing work on `ops` list members? | *"yes"* | landed: a click, a palette pick or a dropped wire adds a member after the selected member, or at the end of the list; Delete / Cut remove members as one edit; later members keep their places |
| 4 | Shift+click: add to the selection like ComfyUI, or keep React Flow's box? | *"match"* | landed: Shift+click adds a card and never removes one; Cmd/Ctrl+click toggles; Shift+drag on empty canvas still draws a box |

How each works, and what it refuses: README → "Unsaved work survives a stop", "New, Open and the file browser",
"Pipelines — adding to and removing from an `ops:` list", and the gesture table; the reasons:
`docs/architecture.md` → "Unsaved work is kept in a hidden file of the served folder", "Changes stay in the editor
until Save" (its 2026-09-29 update: why New… writes at once) and "An op-list member is named by its place".

What a stop still cannot keep, stated in the README: a `kill -9`, a crash, and the undo history. Closing the
editor's terminal (SIGHUP) keeps the work since 2026-09-30 (the user's pick).

## 7. Phases, tests first

Each phase ends with its gates green and the previous path still working.

| phase | tests written first | code | status |
| --- | --- | --- | --- |
| 0 templates | every converted YAML loads, carries its contract, passes `check()` | the 17 conversions | in progress 2026-09-28 |
| 1 engine, landed 2026-09-28 | `recordstream/tests/test_trace.py`: 39 tests — overhead bound, rerun recomputes only downstream, refused parameter leaves the trace untouched, gated chain runs whole, in-place edit is flagged, lazy constructor | `recordstream/flow/trace.py`, exported as `recordstream.flow.Tracer`, user page `docs/trace.md` | tests in progress 2026-09-28 |
| 2 document and app, landed 2026-09-28 | `streamstudio/tests/test_document.py` (58): bte_view derives 24 nodes and 49 wires, patch keeps comments and `$VAR`, new parameter shifts nothing, wire mismatch refused, rename is atomic, sidecar round trip; `test_app.py` (47): every route through Flask's test client; `test_palette.py` (72); `test_layout.py` (28) | `document.py`, `_yamltext.py`, `palette.py`, `layout.py`, `app.py`, `cli.py` | 205 tests green; whole suite `730 passed, 1 skipped in 30.83s`; mypy `Success: no issues found in 50 source files` |
| 3 canvas, landed 2026-09-28 | the playwright proof turned into `tests/test_canvas.py` against the built page: sockets refuse a mismatch, palette of the real 419 types, save produces the same YAML the model came from | `frontend/` | landed 2026-09-28 in two parts — part 1, the scaffold: 14 playwright + 9 vitest; part 2 (palette of the real 447 classes, groups, undo/redo, copy/paste, arrange, rename, run panel, settings): `tests/test_canvas.py` 44 + vitest 57, with `test_app.py` 61, `test_layout.py` 37, `test_document.py` 59. Deferred to `streamstudio/TASKS.md`: a step into a `Stream`'s `ops:`, a route that writes a wire, the HuggingFace pickers and directory chooser; collapse to subgraph landed in 3b |
| 3b menus, groups, subgraphs, landed 2026-09-29 | §6a and §6b: explicit Save and its refusals, New / Open / Save As / Revert and the file browser, copy/paste as text, templates, the subgraph node model and its four routes, the `--editor` switch; `recordstream/tests/test_subgraph.py`, `test_trace_subgraph.py`; `streamstudio/tests/test_app_save.py`, `test_app_files.py`, `test_app_clipboard.py`, `test_app_templates.py`, `test_app_subgraph.py`, `test_subgraph_model.py`, `test_subgraph_edits.py`, `test_cli_editor.py`; 24 more browser tests | `recordstream/flow/subgraph.py` and the tracer's descent; `document.py`, `app.py`, `cli.py`, `layout.py`, `_yamltext.py`; the page's menu bar, Nodes sidebar, right-click menus, groups, levels | landed; see §9 |
| 3c round 2, landed 2026-09-29 | §6c: `tests/test_recovery.py` (40), `test_recovery_process.py` (7, real editor processes), `test_op_lists.py` (50), 21 more in `test_cli_editor.py`, 18 more browser tests, 88 more vitest | `app.py` (the recovery routes, the session guard), `cli.py` (the way out on SIGTERM, what `stop` says, `restart`'s folder), `document.py` (op-list members, the comment rule, `shifts`), `layout.py`; the page's recovery dialog, op-list targets and Shift+click | landed; see §9 |
| 3d round 3, landed 2026-09-29 | §6c item 2: `tests/test_app_files.py::TestNew` (the file written at once, every refusal writing nothing, a file appearing between the check and the write never overwritten), `test_app_save.py::TestRevert` (back to the starter), `test_recovery.py` (an untouched New graph is not kept), `tests/test_canvas.py::TestNewAndOpen` (3 more browser tests), `frontend/src/__tests__/newgraph.test.ts` (9) | `document.py` (`Document.create`), `app.py` (`Studio.new_graph` writes; the recovery rows are changed files only); the page's New dialog (Name, Folder, Browse…, the path line, the refusal in place) | landed; see §9 |
| 4 annotaide | its existing suite, with the converter and seeding pins deleted and the mount pinned | the mount, the deletions |
| 5 agent tools | the transcript of §2.4 as a test against the generated tools | navigaitor registration |

Estimate: four to six weeks, by the comparables of annotaide's own web app (four weeks for
9.5k lines) and its pane shell (two weeks for 1.4k).

## 8. Out of scope

Tracing a parallel run; ELK layout; several people editing one graph; standalone value
tools for Algorithms without a run; any change to confluid's grammar.

## 9. Status

Landed on 2026-09-28 and 2026-09-29, nothing committed. Phase 0, phase 1, phase 2 and phase 3 are complete, and so
are 3b — the menu scope (§6a) and subgraphs (§6b) — 3c, round 2 (§6c items 1, 3 and 4), and 3d, round 3 (§6c item
2: New… writes the file at once). Phase 4 (the consuming workspace mounts the editor) and phase 5
(agent tools) are next; the root `TASKS.md` holds both.

| project | what landed | gate |
| --- | --- | --- |
| recordstream | option A in `ops/contract.py` (`CLASS_VOCABULARY_SLOT = "classes"`, the collision refusal, a wired producer accepted); 62 contract tests; `docs/graph-contract.md`; `flow/trace.py` (the engine: check, run, step, resume, rerun_from, value, to_dict) with `docs/trace.md`, an architecture record and 39 passing tests | whole suite green; mypy clean |
| annotaide | `classification.yaml` delivers `classes`; seven YAML twins under `workspaces/classification/` and `workspaces/detection/`, each rooted in a `contract:` entry; `tests/test_template_twins.py` pins each twin's `check()` outcome; the type-workspace mandate amended | `2056 passed`; mypy clean |
| waivefront | ten YAML twins under `waivefront/annotation/`; `write_annotations.yaml` carries the `app:` block and round-trips it; `tests/test_template_twins.py` | `111 passed`; mypy clean in touched files |
| waivefront, the panes rooted (2026-09-28) | every one of the 16 VIEW documents (8 hand-written sources + 8 converted twins) ends in a `contract:` root — `GraphContract(name="<pane> view", inputs={input: Signal, regions: Regions}, outputs={})`, the host's side declared — so `Document.add_step` of a node whose entry nothing produces is REFUSED (`bte_view.yaml:69:9: … NoiseFloorEstimate needs the record entry 'spectrogram', which nothing before it produces — the chain has input, regions at that point`) where a synthetic `<host>` used to accept it; appended after the last entry because the pane's own `outputs: <step>` line broadcasts into `GraphContract.outputs` when the root comes first (measured: `confluid.load` fails); the old importer drops it and regenerates a byte-identical canvas on all 16 | waivefront `147 passed` (`test_template_twins.py` + `test_annotation_templates.py`), `mypy waivefront`: `Success: no issues found in 200 source files`; streamstudio `test_document.py` `59 passed`, `mypy streamstudio`: `Success: no issues found in 51 source files` |
| streamstudio | the ComfyUI contract canvas pins `classes` and the collision refusal; this page; the phases in the root `TASKS.md` | `93 passed`; mypy clean |
| streamstudio, phase 2 | `document.py` (derive, located patches, undo ring, sidecar), `_yamltext.py` (the patcher over PyYAML's composed tree), `palette.py` (registry → node specs, `to_pydantic` widgets), `layout.py` (layered first layout + sidecar), `app.py` (`editor_blueprint` + `create_app`, `Studio` on `app.extensions`), the standalone command in `cli.py`; `pyproject.toml` declares `flask` and `pyyaml`; `tests/test_document.py` 58, `test_app.py` 47, `test_palette.py` 72, `test_layout.py` 28 | isort/black `left unchanged`; flake8 clean; `mypy streamstudio`: `Success: no issues found in 50 source files`; `pytest -q`: `730 passed, 1 skipped in 30.83s`; `test_packaging.py`: `12 passed`; red run before implementing (modules moved aside): `1 error in 6.04s` |
| streamstudio, phase 3 part 1 | `frontend/` — `src/api.ts` (one function per route), `src/model.ts` (the model's JSON, `modelToFlow`, `compatible` over `GET /api/types`), `src/canvas/GenericNode.tsx` (the one node component), `src/canvas/Canvas.tsx` (React Flow, `isValidConnection`, positions on drag stop, proposals, the status bar), `src/App.tsx` (picker, sidebar text, toasts); `app.py` gains `GET /api/types` (`document.supertypes()`), `web_dist()`, `page_blueprint()`, `create_app(web_dist=)`; `pyproject.toml` declares `node` as a system dependency; the root `.gitignore` no longer ignores `frontend/`; `tests/test_canvas.py` 14 (playwright, real mouse drags), `frontend/src/__tests__/model.test.ts` 9, `test_app.py` 52 | `npm run build`: `338.47 kB │ gzip: 109.55 kB`, `built in 491ms`; `gzip -c dist/assets/*.js \| wc -c`: `109506`; `pytest -q tests/test_canvas.py`: `14 passed in 18.15s` (skipped `1 skipped in 7.76s` before the build — the red run); `npm test`: `9 passed`; `mypy streamstudio`: `Success: no issues found in 51 source files`; the standalone editor on port 8199 → `GET /` `200 text/html` serving the built page, its asset `200 338479 bytes` |
| streamstudio, phase 3 part 2 | `frontend/src/` — `canvas/Palette.tsx` + `steps.ts` (the palette, the step anchor and name), `groups.ts` + `canvas/GroupNode.tsx` (boxes derived from their members), `history.ts` (the layout timeline beside the server's text history), `layout.ts` (`arrange`, dagre), `run.ts` + `panels/RunPanel.tsx` (run, step, inspect, rerun), `panels/SettingsForm.tsx`, `canvas/ParamWidget.tsx` (the one widget of the node, the form and the rerun form), `App.tsx` (toolbar, tabs, shortcuts); `layout.py` gains the `groups:` block (`Group`, `read_groups`, `rename_member`), `document.py` keeps and re-keys groups, `app.py` gains `PUT /api/graphs/<name>/groups`; `tests/test_canvas.py` 44, vitest 57, `test_layout.py` 37, `test_app.py` 61 | `npm run build`: `411.68 kB │ gzip: 134.81 kB` (part 1: `338.47 kB │ gzip: 109.55 kB`); `pytest -q tests/test_canvas.py tests/test_app.py tests/test_layout.py tests/test_packaging.py`: `1 failed, 153 passed` — the one failure is the 24-node bte layer pin the pane's new `contract:` root outdated (another session; tracked); whole suite `1 failed, 797 passed, 1 skipped`; `mypy streamstudio`: `Success: no issues found in 51 source files`; red run before implementing: `8 failed, 35 deselected` |
| streamstudio, phase 3 closed (2026-09-28) | the documentation checklist, no code: README `The rewrite (phases 2-3 landed)` (the build commands, the gestures, every refusal verbatim, what is not there yet, and the mount corrected to one prefix — measured: the old snippet served the placeholder at `/editor/` and `404` at `/editor/api/graphs`), an AGENTS invariants block, the architecture record *The canvas is a React Flow page over the document routes*, CHANGELOG, this page; the root phase-3 item deleted and its deferred parts moved to `streamstudio/TASKS.md`; no environment variable was introduced (`web_dist()` resolves off `__file__`, the port is `--port`) | `pytest -q tests/test_canvas.py`: `44 passed in 41.93s`; `npm test`: `Tests  57 passed (57)`; `gzip -c dist/assets/*.js \| wc -c`: `134831` (`411743` bytes raw); whole suite `1 failed, 797 passed, 1 skipped in 65.89s` — the failure is still the 24-node bte layer pin (`test_layout.py:337`, `assert 25 == 24`, tracked); `mypy streamstudio`: `Success: no issues found in 51 source files`; `test_packaging.py`: `12 passed in 0.47s` (it checks the README's links) |
| recordstream, 3b (2026-09-29) | `flow/subgraph.py` — `Subgraph(steps, result)`, an op in the compose group, running its inside on the flow kernel; `consumes` / `produces` / `flags` derived (`produces` and `flags` from the lineage of `result`); every structural mistake refused before the first record, naming the outer step and the line; `check_chain(raised=…)`; the tracer descends with a child tracer per subgraph step (`prep/grey`, pause inside, step, resume, rerun); the public `StepReferenceError`; README, `docs/graph.md`, `docs/trace.md`, an architecture record | `pytest -q`: `1426 passed`; isort/black/flake8 clean; `mypy recordstream`: `Success: no issues found in 132 source files` |
| streamstudio, 3b backend (2026-09-29) | explicit Save (`save`, `save-as`, `revert`, `check`, the changed-on-disk refusals, `_DISK_LOCK`, merged positions), New / Open / the file browser, copy/paste as text, `POST …/steps/remove`, templates (`--templates-dir`), the subgraph node model (`model.inside`, qualified names, boundary nodes) and its routes (`subgraph`, `extract`, `include`, template insert), the `--editor canvas\|comfy` switch | `pytest -q` without the browser tests: `1076 passed, 1 skipped`; isort/black/flake8 clean; `mypy streamstudio`: `Success: no issues found in 59 source files` |
| streamstudio, 3b page (2026-09-29) | the menu bar and keyboard map (`commands.ts`), the right-click menus, the file / new / check dialogs, ● and `beforeunload`, the Nodes sidebar (Subgraph left out, docstring markup as inline code), groups (Add group, resize, drag in and out), subgraph levels (breadcrumb, Escape, `?level=`, a readable fit on entering, a fit after Arrange), one-request Cut / Delete, paste waiting for its copy | `tests/test_canvas.py`: `83 passed` (58 before the fixes; 20 of the new tests failed on the old build); `npm test`: `Tests  203 passed (203)`; `npm run typecheck` and `npm run build` clean |
| the consuming annotation workspace, 3b (2026-09-29) | its ComfyUI launch names the editor (`python -m streamstudio.cli run --editor comfy`), pinned in two halves (the command and its neutral working directory run without streamstudio installed) | `pytest -q`: `2059 passed`; `mypy`: `Success: no issues found in 99 source files` |
| streamstudio, 3c round 2 (2026-09-29) | §6c items 1, 3 and 4: the recovery file (`Studio.stash` on the way out of SIGTERM and Ctrl+C, `GET /api/workspace`, restore, discard, the session guard, what `stop` says, `restart`'s folder), op-list members added and removed (`OP_LISTS`, `{list}` on `POST …/steps`, `added`, the member comment rule, `shifts`), Shift+click; the page's recovery dialog, op-list targets and selection; `.streamstudio-workspace*.json` ignored in every project whose folders hold editor graphs | `pytest -q` (page built): `1296 passed, 1 skipped in 195.54s`; `tests/test_canvas.py` alone: `101 passed`; `npm test`: `Tests  291 passed (291)`, 18 files; `mypy streamstudio`: `Success: no issues found in 62 source files`; live on port 8196: `stop` printed the kept-work line and the next start the restore line |
| streamstudio, 3d round 3 (2026-09-29) | §6c item 2: File ▸ New… writes the file at once — `Studio.new_graph` through `Document.create` (the file is created or the write fails, never opened over one that is there), `dirty: false`, a file in another folder joins like File ▸ Open (`clip~2`); every refusal before anything is written; the recovery rows are changed files only; the page's New dialog (Name, Folder, Browse…, the path line, the server's refusal shown in place) | `pytest -q` (page built): `1306 passed, 1 skipped`; `tests/test_canvas.py` alone: `104 passed`; `npm test`: `Tests  301 passed (301)`, 19 files; `mypy streamstudio`: `Success: no issues found in 62 source files` |

Two things measured while doing it, both recorded in the root `TASKS.md`:

- The twins' root is keyed `contract:`, never after the graph slot: a top-level `source:` /
  `files:` / `sink:` key is broadcast into a sibling whose constructor has that parameter
  (`Stream.source`, `FilesSource.files`, `DatasetProcessor.sink`). Measured: a root named
  `source` beside a sourceless Stream makes `Stream.source` the contract object.
- A converted twin spells its classes by registry key (`_target_: WindowSignal`), the hand-written
  pane sources by dotted path (`!class:waivefront.processing.WindowSignal`); the registry form loads
  only after the entry-point scan. The rewrite's own writer spells dotted paths.

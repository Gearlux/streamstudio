# Architecture decisions

Design rationale for mechanisms whose existence or shape is not obvious from their name or the
user docs. Each record: Context → Decision → Consequences → Example → What you may change.
User-facing usage lives in the README and the topic pages beside this file (`editor.md`, `settings.md`,
`hosting.md`, `api.md`, `development.md`); agent-facing rules in `AGENTS.md` — this file answers
"why does this mechanism exist?".

## The studio is addressed by its PORT, not by a pid file (2026-08-26)

**Context.** `stop` and `restart` need to find a running studio. The obvious mechanism is a pid file
written at launch and removed at exit, and it is what most single-process servers do.

It does not fit here: the process that holds the port is not always the process anything recorded. A
studio is started from a terminal, by a host app, or by a script, and a wrapper (a shell, a process
manager) may stand between the one that wrote a pid file and the server that holds the port — so a
pid file records whichever of them wrote it, and signalling that one does not free the port. Keeping
a file in sync with a process that can be killed, crash, or be restarted by a third party is state
that goes stale. Worse, a pid file only ever knows about studios THIS launcher started: a studio a
host app spawned detached, or one a developer ran by hand, is exactly what a person reaches for
`restart` to deal with.

**Decision.** The port IS the identity. `_studio_process(port)` scans processes for the one listening
on `127.0.0.1:<port>` and that process is the studio, whatever spawned it and whatever its ancestry.
Nothing is written to disk, so there is no state to go stale.

The scan is per-process rather than whole-system because the whole-system call is privileged:
`psutil.net_connections(kind='tcp')` raises `AccessDenied` for an ordinary user on macOS, while
iterating processes and asking each for its own connections does not.

**Consequences.**

- `stop` / `restart` work on any studio on that port — this launcher's, a host app's, a hand-run one.
- No pid file, no lock file, no cleanup path, and no stale-state failure mode to reason about.
- `psutil` is a hard dependency (imported at module scope by `cli.py`, declared in `pyproject.toml`).
- The scan is O(processes) and touches every process the user owns. That is fine for a command a
  human types; it would not be fine on a hot path, so do not reuse it as one.
- Two studios on two ports are independent, which falls out for free: every command takes `--port`.

**Example.**

```python
proc = _studio_process(8190)          # the LISTENER, whatever started it
proc.terminate()                      # port measured free 64 ms later
proc.wait(timeout=grace)              # deterministic; kill() only if it ignores SIGTERM
```

**What you may change.** Widen the match (a host other than loopback, a port range) inside
`_studio_process`; make `grace` configurable if a slow shutdown ever needs it. Do not introduce a pid
file, a lock file, or any launch-time state as a "faster" lookup — it would reintroduce exactly the
staleness and the this-launcher-only blindness this avoids. Do not reach for the whole-system
`net_connections()`; it is privileged, and the failure is a permission error rather than an empty
result.

**Update, 2026-10-14.** The decision is unchanged by the removal of the second editor, whose launcher started its
server as a child: the owner of the port is the target, whatever started it.

## The graph document is the truth; the canvas is derived and edits are located text patches (`streamstudio.document`, 2026-09-28)

**Context.** An earlier editor, removed before the first release, kept TWO documents of every graph: a canvas file
and the confluid YAML a converter produced from it, re-checked on every start. Three things went
wrong, each measured before the rewrite was scoped (`docs/rewrite.md` §2.1, §6, §9):

- **A canvas file stores a node's values by POSITION.** `widgets_values = [25000000.0, 0.025, 'a Bluetooth
  packet …']` carries no names, so when `Refuse(max_band_hz, max_duration_s, hint)` gained `note` as its
  FIRST parameter every saved canvas re-opened one slot off — `{'note': 25000000.0, 'max_band_hz': 0.025,
  'max_duration_s': 'a Bluetooth packet …'}`, the hint lost — while the YAML, keyed by name, shifted nothing.
- **Re-dumping a loaded document loses what the loader never kept.** `confluid.dump` of the shipped
  24-step Bluetooth pane: 275 lines with 148 comment lines → 179 lines with 0, and
  `root: $DATA_ROOT/pets` → the resolved path, the variable baked in.
- **A graph's boundary needs a root key that collides with nothing.** A top-level `source:` / `files:` /
  `sink:` key beside a class whose constructor has that parameter is BROADCAST into it — a root named
  `source` next to a source-less `Stream` made `Stream.source` the contract object, and a `class_names`
  slot wired to a producer landed in `Stream.class_names` and refused to load. So the root key is
  `contract`, the class-vocabulary slot is `classes` (`recordstream.ops.contract.CLASS_VOCABULARY_SLOT`),
  and the contract refuses a slot named after a parameter of any class the graph uses.

**Decision.** One document. `Document` opens the YAML text and derives a `Model` — nodes with typed
sockets, wires, located problems — from `confluid.load(text, until="document")` (markers kept, each
stamped `file:line:col`) and the declarations that already exist: `recordstream.algorithm_spec`, an
op's `consumes`/`produces`/`flags`/`reports`, `confluid.input_specs`/`output_specs`/`to_pydantic`,
`GraphContract.inputs`/`outputs`, and the `recordstream.items` registry for socket types. Every edit is
a patch of the TEXT at the marker's location through `_yamltext.YamlText`: PyYAML's composed tree
(`yaml.compose` with the SafeLoader — any tag is tolerated, no constructor runs) has a start mark per
node, and those marks coincide with confluid's stamps, so a marker's `file:line:col` names exactly one
composed node and the edit is expressed against its spans (a kwarg's value, a step's lines, a `!ref:`
tag). The patched text is re-derived; if it no longer loads or `check()` gains a problem, the edit is
refused with the located message and nothing is written. **Update, 2026-09-30:** "gains" compares the problems'
messages with every location taken out (`file:line:col`, PyYAML's `line N, column M`), COUNTED — measured before,
the whole message was compared, so a step inserted above an existing problem moved it from `:6:9` to `:9:9` and the
edit was refused for the problem that was already there; counting keeps the other half (a second copy is gained). Positions never enter the document: they
live in a sidecar `<graph>.layout.yaml` keyed by node NAME, and the graph loads and runs with the
sidecar deleted. Node names are the engine's (`recordstream.flow.Tracer`): a `flow:` step by its key,
a list member `path[i]`, a nested marker by its document path, the contract root by its key.

**Consequences.** Comments, `$VAR` spellings, key order and number formats survive every edit (one
line differs per `set_param`; 148 comment lines before and after, pinned). Values are keyed by name,
so a constructor change moves nothing. A refusal is located. The costs are the patcher's own: a
multi-line block VALUE is replaced by one flow-style line, so a comment INSIDE that value is the one
thing a patch may lose; and a node's rename must re-key the sidecar in the same operation, or its
entry is orphaned (`layout.stale_positions` reports what is left). The undo history is a ring of
whole texts per document, shared by canvas edits and agent edits, because both are text patches.

**Example.**

```python
doc = Document.open("pane.yaml")                  # reads the text; parses nothing yet
doc.model.node("size").params["max_band_hz"]      # Param(value=25000000.0, spelled=True, …)
doc.set_param("size", "max_band_hz", 30e6)        # ONE line: '      max_band_hz: 25.0e6' -> '      max_band_hz: 30000000.0'
doc.save()                                        # 148 comment lines still 148; '$DATA_ROOT' still spelled
doc.set_param("count", "field", "image")          # ValueError: cannot wire decode.image (Image) into count.label (Label)
                                                  #             at pane.yaml:4:9: the types do not meet   (file untouched)
```

**What you may change.** The patcher's spellings (block or flow form, how an inserted step is laid
out), which declaration a socket type is read from as long as it is a recordstream or confluid
declaration, the first-layout algorithm. What must hold: the file is never re-emitted from the loaded
object graph; a refused edit writes nothing; node names are the engine's names; positions stay out of
the YAML. Pins: `tests/test_document.py::TestSetParam::test_the_bluetooth_pane_keeps_its_259_comment_lines`,
`tests/test_document.py::TestProblems`, `tests/test_layout.py`.

**Update, 2026-10-01 — the first layout stacks a column by each card's height (`layout.card_height`).**
*Context.* The first layout (`layout.layered_layout`, and `layout.positions` for a node the sidecar does not name)
spaced the cards of one column by a fixed `ROW_DY = 200`, so a card taller than about 180 px was drawn over the next
one. Measured on the shipped Bluetooth pane once its head step became `known` (`CollectKnownAccessCodes`, ten
parameters): `known` at (40, 40) is 373 px tall, and the `contract` root at (40, 240) showed only its last 8 px. Over
the 32 graph levels of the shipped annotation panes and annotation-workspace templates, 8 pairs of drawn cards
overlapped. *Decision.* Within a column the next card's y is the previous card's y plus
`max(ROW_DY, card_height(previous) + CARD_GAP)`, rounded up to the canvas's 10-px snap grid (`layout.GRID`, React
Flow's `snapGrid`: a corner off it jumps on its first drag). `positions` starts fresh nodes below the lowest kept
card's BOTTOM (its top plus that pitch) rather than its top + `ROW_DY`; a subgraph's inside goes through the same two
functions. The server never sees the drawing, so `card_height` ESTIMATES it from what `GenericNode.tsx` draws: 37 for
the title, 24 per input socket, parameter and output socket, 20 for a note line, 26 for a subgraph's inner-steps bar.
The parts were calibrated on the page itself — every graph of a scratch copy of those folders plus a one-subgraph
flow opened headless at 1400x900, each React Flow node's measured height beside its row counts from the model:

| card | rows (in / params / out) | measured | estimate |
| --- | --- | --- | --- |
| `bte_view` `known` | 1 / 10 / 3 | 373 | 373 |
| `bte_view` `contract` | 0 / 4 / 2 | 181 | 181 |
| an op with a note | 1 / 3 / 2 | 201 | 201 |
| a subgraph card | 1 / 1 / 2 | 159 | 159 |
| a subgraph `<in>` pill (a note) | 1 / 0 / 0 | 80 | 81 |
| the classification workspace value | 2 / 34 / 1 | 925 | 925 |

Over all 197 cards the estimate is exact on 195 and 1 px over on the two pills — never under. `CARD_GAP = 17` is what
`ROW_DY` leaves under the TALLEST card it fits — a five-row subgraph card, 37 + 5 × 24 + 26 = 183 px (a six-row op card
is 181) — so every card that fitted the fixed pitch keeps its place: 25 of the 32 shipped levels lay out
byte-identically, the 7 that moved are exactly the ones that overlapped, and 8 overlapping pairs became 0. A gap
measured under the six-row op card alone (19 = 200 − 181) left the subgraph bar out: the card below a five-row
subgraph card moved from (40, 240) to (40, 250) although nothing overlapped (measured on the page, the Bluetooth pane
with two steps made a subgraph). Rejected: letting the page send its measured heights back (the graph answer carries positions before the
page has drawn, and a layout that depends on an open page is no longer a function of the document); placing the
unwired root where no step can be taller (it fixes one pane, not the other six levels). *Consequences.* The estimate
mirrors the card's CSS: a new row type, a taller widget or a changed font makes it stale, and only the browser test
(`tests/test_canvas.py::TestFirstLayout`) sees that, so such a change re-runs the calibration. `layout.py` still
reads only the model's shape — the counts come through `getattr` with a default, and a node without them is a
title-only card that keeps the fixed pitch. The page's *Arrange* (dagre over measured boxes) is unchanged.
*Example.*

```python
layout.card_height(model.node("known"))      # 373 — 37 + 14 rows × 24; the page measured 373
layout.layered_layout(model)["contract"]      # (40.0, 430.0) — was (40.0, 240.0), under `known`
```

*What you may change.* The part heights and the gap (re-calibrated), the rounding. What must hold: the estimate is
never below a drawn card, `ROW_DY` stays the MINIMUM pitch so short cards keep their places, `CARD_GAP` is
`ROW_DY` minus the tallest card shape that fits it (re-derived with every part height —
`::TestTallCards::test_no_card_shape_the_fixed_pitch_fitted_moves_the_next_card` enumerates the shapes), and
`layout.py` imports no document code. Pins: `tests/test_layout.py::TestCardHeight`, `::TestTallCards`,
`::test_bte_views_first_layout_overlaps_no_two_cards_of_a_column`, `tests/test_canvas.py::TestFirstLayout`.

## The canvas page is a build artifact beside the package, and a drawn wire is the server's to spell (`frontend/`, `streamstudio.app.page_blueprint`, 2026-09-28)

**Context.** Phase 3 of the rewrite (`docs/rewrite.md` §2.3) puts a React Flow page over the phase-2
routes. Two things had to be decided before the first line: where the built page LIVES — inside the
wheel (every install has it, every build changes the package) or beside it (an install may lack it) —
and what a wire the user DRAWS means, when no route writes one (a Stream's record wires ARE its `ops:`
order, a flow's are `from:` / `bind:`, a contract's are `!ref:`; phase 2 writes parameters, positions,
names and steps). The May-2026 attempt had gitignored the whole `frontend/` tree, so even the source
was invisible to git; the workspace's other vite frontend commits its source, ignores `dist/`, and
serves the build when it is there.

**Decision.** The source is committed under `frontend/`, the build (`frontend/dist`) is gitignored, not
in `package-data`, and not built by CI. `web_dist()` resolves `<repo>/frontend/dist` off `__file__` and
answers it only when `index.html` is there; `page_blueprint(dist)` serves `/` and `/<file>` from it, or a
placeholder that names the build command and lists the API routes; `create_app(graph_dir, web_dist=…)`
takes the directory so a test asks for either page deterministically. The page is built with
`base: './'` and reaches the API through RELATIVE paths (`api/graphs`), so one bundle serves at `/` and
under any mount prefix. The type test the page applies while dragging is the model's own: `GET /api/types`
serves `document.supertypes()` — the `_meets` rule as a table — and the page's `compatible(a, b)` is
identity, the wildcard, or `types[a]` containing `b`; a mismatch never becomes an edge. What a drop whose
types meet becomes is the SERVER's to decide, never the page's: in phase 3 no route could spell a wire, so the drop
was a dashed edge the document did not hold. *Update 2026-10-04:* a drop is sent to `POST …/wires`, and the server
saves it in the document's own spelling or refuses it in one line — "A drawn wire is saved in the document's own
spelling" below; the page draws no edge of its own any more.

**Consequences.** `pip install streamstudio` gives the API and the placeholder, never a page that is out
of step with its routes; a developer builds once and the checkout serves it (since 2026-10-02 a server's start
builds it whenever its source changed — "The canvas page builds itself at a server's start" below).
*Update 2026-10-04:* where the build lives was reversed — it is committed inside the package, `streamstudio/canvas`,
and every install serves it ("The built page is committed inside the package" below); the rest of this record holds. Every element the page renders is addressable by `data-*` (`data-node`, `data-socket`,
`data-type`, `data-kind`, `data-param`, `data-problem`, `data-toast`, `data-saves`), so the playwright
suite reads the SPEC and never a class name. The page never guesses a spelling: adding a wire to the
document by a rule of the page's own would invent semantics the document never stated, so the server, which reads
the document, is the one place a wire is spelled.

**Example.**

```python
app = create_app(graph_dir)                          # web_dist() -> <repo>/frontend/dist when built, else None
client.get("/")                                      # the built index.html, or the placeholder naming the build command
client.get("/api/types").get_json()["Stream"]        # ["Source"]  — the page's compatible("Stream", "Source") is True
```

```
drag decode.image -> count.label  : isValidConnection False -> no edge, no toast, file unchanged
drag decode.image -> boost.image  : POST …/wires -> 400 "The editor can't save wires in this kind of graph yet." — no edge
```

**What you may change.** Where the build lands (as long as `web_dist()` and the build command agree),
the toolchain versions, the widget mapping, the toast wording. What must hold: the page is derived from
the model the routes serve and writes only through them; the type test is `document.supertypes()`, never
a table typed into the page; a wire is spelled by the server or not written; the source is committed
(and, since 2026-10-04, the build with it). Pins: `tests/test_canvas.py`, `tests/test_app.py::TestTypes`,
`tests/test_app.py::TestMounting`, `frontend/src/__tests__/model.test.ts`.

## Undo is two histories under one gesture: the document's text history on the server, the layout's on the page (`frontend/src/history.ts`, 2026-09-28)

**Context.** The canvas changes two different things. A parameter, a step, a rename change the DOCUMENT — the YAML,
which already keeps a ring of whole texts per open document (`Document.undo`), shared by every client of the routes,
an agent's tools included. A move, an arrange, a group change the LAYOUT — the sidecar, which the document never
sees and which has no history anywhere. The prototype undid both by snapshotting React Flow's two arrays on the page;
that cannot serve here: a page-side copy of the text would restore a document out of step with the file an agent just
edited, and the server cannot undo a move it never recorded as an edit.

**Decision.** One gesture, two histories. A document edit is recorded on the page as a bare MARKER (`{kind:
"document"}`); undoing it is `POST …/undo`, which answers the text the document's own history holds. A layout change
is recorded with its before and after (`{kind: "positions" | "groups", before, after}`); undoing it is the page
writing the before back (`PUT …/positions` / `…/groups`). The page's `Timeline` orders the two so Ctrl/Cmd+Z reverses
the latest thing the person did, whichever kind. When the page holds no entry — after a reload, or for an edit an
agent made — undo still asks the server. A new entry drops the PAGE's redo side; the server keeps its own until its
next edit.

**Consequences.** An agent's `set_param` and a canvas edit sit in one text history, and either side's undo reaches
both. A move is undoable without ever touching the document's history (pinned: one Ctrl+Z restores the moved node
and leaves the edit made before it; the next restores the text and moves nothing). The cost is the seam between the
two: an operation that changes BOTH — a rename re-keys the sidecar — is undone on the server as text only, so the
sidecar keeps the new name (measured, tracked in `TASKS.md`); the page re-keys its own layout entries on a rename so
its half stays consistent.

**Example.**

```
set low_level 0.9 (POST …/param)   timeline: [document]
drag stream.ops[2] by (120, 80)    timeline: [document, positions{before (720, 40), after (840, 120)}]
Ctrl+Z                             PUT …/positions {stream.ops[2]: [720, 40]}      text unchanged
Ctrl+Z                             POST …/undo → low_level 0.5                      sidecar unchanged
Shift+Ctrl+Z, Shift+Ctrl+Z         POST …/redo, then PUT …/positions {… [840, 120]}
```

**Update, 2026-09-28.** The server's history entries carry the RENAME an edit made, beside the texts before
it. Undo and redo re-key the current positions and groups by that rename rather than restoring a stored
layout, because the layout is the page's state: restoring would throw away a move made after the rename.
Measured before: undo restored the text only, the renamed node was laid out afresh and its group named a
member that no longer existed.

**What you may change.** The page's depth (`HISTORY_DEPTH`), which layout changes are recorded, the shortcuts. What
must hold: the page never restores document text of its own; a layout change never enters the document's history.
Pins: `tests/test_canvas.py::TestUndoRedo`, `::TestArrange`, `::TestGroups::test_ctrl_z_undoes_a_grouping_on_the_pages_side_and_redo_puts_it_back`,
`frontend/src/__tests__/history.test.ts`.

## A group lives in the layout sidecar only, stored as the page spelled it (`streamstudio.layout.Group`, `PUT /api/graphs/<name>/groups`, 2026-09-28)

**Context.** A group — a titled, coloured box that carries its members when dragged — is how a person arranges a
drawing; the pipeline does nothing different because two nodes are boxed. The document has no spelling for it, and
giving it one would make boxing two nodes a document edit (an undo entry, a re-validation, a diff in a pipeline
file) and make two graphs that RUN identically differ as files.

**Decision.** The sidecar holds two blocks, `positions:` and `groups:`; a group is `{id, title, color, nodes}` plus
any key the page adds, written as sent and read back as written (the shape — a list of mappings — is checked by line,
nothing else). The server reads inside a group in ONE place, `layout.rename_member`: a rename is one operation over
every reference to a node, its group membership included. On the page the box is DERIVED from its members on every
draw (`groups.withGroups`: their absolute positions and measured sizes, the corner on the 10-px grid) and the
members are React Flow children with RELATIVE positions, so dragging the box moves them for free; the sidecar keeps
ABSOLUTE positions, so ungrouping moves nothing.

**Consequences.** A graph runs, exports and diffs the same with or without its groups, and deleting the sidecar
loses the drawing, never the pipeline. A group naming a node the model no longer has is kept and not drawn. Members
are NOT clamped into their box (`extent: "parent"`): measured, the clamp drew members up to 37 px off their saved
place for 0.1–0.8 s after a load, because the box is sized from assumed card heights until React Flow measures
them — the box follows the members instead, and a member dragged past the edge grows it.

**Example.**

```python
layout.write_sidecar(side, {"a": (40.0, 40.0)}, groups=[{"id": "g1", "title": "front end", "color": "#3b82f6", "nodes": ["a"]}])
layout.read_groups(side)                       # the same list, keys and order as written
layout.rename_member(layout.read_groups(side), "a", "z")[0]["nodes"]   # ["z"]
```

**What you may change.** The group's page-side fields, the colours, the padding. What must hold: groups never enter
the document; the server stores them as sent and interprets them only to follow a rename. Pins:
`tests/test_layout.py::TestGroups`, `tests/test_app.py::TestGroups`, `tests/test_canvas.py::TestGroups`,
`frontend/src/__tests__/groups.test.ts`.

## The canvas is a React Flow page over the document routes, and every editor behaviour is a route call on the document, not a second model (`frontend/`, 2026-09-28)

**Context.** A canvas library that keeps a graph model of its own — a JSON of its own, values by position — is a
second copy of every graph, to be kept in step with the YAML by converters in both directions (the record "The graph
document is the truth" above lists what that cost). The canvas needs the behaviours a node-graph editor is expected to
have — typed sockets refused while dragging, widgets on the node, a search palette on double-click and on a dropped
wire, groups, undo/redo, pan/zoom/minimap/snap/multi-select/copy-paste, auto-layout, titles and colours, JSON in and
out — WITHOUT bringing a document model of its own. Fourteen libraries were measured on 2026-09-27 (licence, release
and commit activity, framework, and those nine behaviours one by one, `docs/rewrite.md` §2.3). The ones with the most
behaviours built in are the ones that carry a model:

- **litegraph** has 8 of the 9, but its last commit is 2024-01-08, and its fork is archived and npm-deprecated
  ("Package no longer supported").
- **Rete v2** keeps its own editor state across 11 separately versioned packages; four were last released 2024-08-30.
- **Baklava** has native subgraphs, but is Vue-only in a React toolchain; **reaflow** lets ELK own every position, so
  the sidecar could not.

**Decision.** React Flow (`@xyflow/react` 12.12: MIT, 13.5 million weekly downloads, 112 commits in the 90 days before
the choice, React 18 — the vite toolchain the workspace's other frontend already builds with). It is a diagram
toolkit, not an editor: it draws two plain arrays, `nodes` and `edges`, and calls back on every gesture; there is no
model inside it to keep in step. The page REDRAWS those arrays from the model the server answers after every document
edit (`modelToFlow`, node id = the document's node name), and each behaviour is a route call on the document:

| behaviour | the prototype (2026-09-27, a document of its own) | the page (2026-09-28, the document) |
| --- | --- | --- |
| typed sockets | a rule typed into the page (identical, or `Any`) | `isValidConnection` over `GET /api/types` = `document.supertypes()` (subtypes meet) |
| widgets | values held in node data | `POST …/param` — one located line patch; the widget shows the server's value |
| palette, wire dropped on nothing | a node pushed into the arrays | `POST …/steps`, wired by the order the insert creates |
| groups | a parent node in the arrays | the sidecar's `groups:` block; the box derived from its members every draw |
| undo/redo | snapshots of the two arrays | the document's text history on the server + the page's layout timeline |
| copy/paste | a copy of array entries | `add_step` per copied node, with the values the document spells |
| auto-layout | dagre over the arrays | dagre on the page (*arrange*) or the server's layering (*columns*), saved as positions |
| save/load | `toDoc`/`load`, a JSON of its own (round trip 2278 bytes in, 2278 out) | none — each route call saves the YAML; `serialize.ts` has no successor |

**Consequences.** Measured, the prototype (685 lines, one generic node component, a synthetic catalogue): palette of
400 entries open in 6.9 ms, typing narrows it; dagre on 40 nodes / 39 edges in 9 ms; 60 nodes loaded in 72 ms; 392 KB
of JS, 128 KB gzipped; build 1.2 s. The page as landed, against the real registry: 447 classes, 200 rows drawn,
6.2 ms from gesture to DOM; 411.7 kB of JS, 134.8 kB gzipped (`gzip -c dist/assets/*.js | wc -c` → `134831`); 44
browser tests and 57 unit tests, every browser test pinned to zero console errors (bar the one `400` line of a
refusal provoked on purpose). The cost of the library: what React
Flow leaves to the caller — the palette, the widget mapping, undo, copy/paste — is ours, about the size the
evaluation estimated; React Flow 12 has no double-click event on the pane, so the page listens itself and
zoom-on-double-click is off. The cost of the document: a gesture the document cannot spell is not available on the
canvas — a drawn wire stays a proposal, a step cannot enter a `Stream`'s `ops:` list, a pasted `2.0` becomes `2` —
and each is a missing ROUTE, tracked in `TASKS.md`, never a page-side model. Collapse to subgraph is a convention in
React Flow (a node standing for an `include:` file) and is deferred.

**Example.**

```ts
// the page never edits its arrays itself: it asks the document, then redraws what the server answered
const graph = await setParam(name, "stream.ops[1]", "low_level", 0.9);   // POST …/param → {model, text, positions, …}
const { nodes, edges } = modelToFlow(graph.model, graph.positions);      // node ids are the document's node names
```

**What you may change.** The library version, the widget mapping, the palette's layout, dagre's options, the bundle's
shape. What must hold: the page keeps no document of its own — no serializer, no page-side text history, no wire or
step it invents without a route — and one generic node component renders every class. A library that carries its own
graph model (LiteGraph, Rete's editor state) is the rejected alternative, not an upgrade path. Pins:
`tests/test_canvas.py`, `frontend/src/__tests__/*.test.ts`.

**Update, 2026-09-29.** Three rows of the table changed with the menu scope, each with its own record below: copy and
paste carry the document's text (so a `2.0` stays `2.0`); nothing reaches the file until Save; and collapse to
subgraph landed as the subgraph node, whose inside the canvas opens in place. A step still cannot be added to, or
deleted from, a `Stream`'s `ops:` list from the palette; paste and Convert selection to subgraph do insert members.
The suites: 82 browser tests, 203 unit tests.

**Update, 2026-10-14.** This page is the package's only editor. The record's subject is unchanged: no serializer, no
page-side text history, no wire or step the page invents without a route.

## Changes stay in the editor until Save, and Save refuses to overwrite a file changed on disk (`Document.save`, `POST /api/graphs/<name>/save`, 2026-09-29)

**Context.** Phases 2 and 3 saved every accepted edit at once: `POST …/param` wrote the file before it answered.
Two things the menu scope asked for cannot live with that: Move to its own file… makes a steps file you have not
asked to write yet, and Revert needs a saved state to go back to. And a file can change under the editor — a teammate, a `git pull`, an agent —
which an editor that writes on every change overwrites without a word. The user chose (the menu
scope's decision 1): changes stay in the editor until Save, and ● says so.

**Decision.** Every editing route changes the in-memory `Document`, and `dirty` says whether any text differs
from what was last written, or positions/groups were set since. `Document.save` first compares each file on disk
with what the editor last read or wrote; any difference refuses the WHOLE save with the path and writes nothing.
So does a file the disk will not take (2026-09-30, the update below). Otherwise it writes every changed file — the
graph's own last — and the sidecar. The trace, the check and every derive run the in-memory
text. Holding text in memory brought four mechanisms, each from a measured break:

- a load hands confluid the graph's path and every file the editor holds, the graph's own text included, as
  `texts=`: confluid parses a held text instead of reading the file, so an `include:` naming a steps file that
  exists only in the editor loads, and nothing is written; saves, disk reads and loads run under one process-wide lock (`_DISK_LOCK`),
  because Flask serves threaded and a save empties a file before it writes the new text;
- an included file enters the held texts only when an EDIT changes it (a Copy or a template read goes to disk),
  and when it first enters, every older history entry gets the text it was read as;
- a positions write MERGES over what the editor holds;
- Save As rebases relative includes (`rebase_includes`) so the new file names the same steps files.

**Consequences.** Measured:

- A value edit: before, the file said `{low_level: 0.9}` the moment the route answered; now it keeps `0.5`, the
  answer says `dirty: true`, and `POST …/save` writes it.
- A save after someone appended a line to the file: `400 …/demo.yaml changed on disk since the editor read it —
  Revert to load it, or Save As to keep both`, nothing written. For an included steps file the message offers
  Revert only: a graph written by Save As would include the same file.
- The held texts: after Move to its own file…, 5 checks and 5 edits leave the graph folder as it was during every
  load (`['demo.yaml']`); with the folder made read-only they still check, edit and trace, and Save writes
  `prep.steps.yaml` once the folder can be written again.
- The lock: graph A saved a steps file 300 times while graph B, which includes it without holding it, was checked
  352 times; without the lock 4 of B's loads read the file half-written (`Subgraph: steps is empty`, a
  `ParserError`), with it none.
- The merge: `PUT {prep: [11, 22]}` then `PUT {mask: [33, 44]}` put `prep` back at its laid-out place; now both
  reach the sidecar.
- The undo rule: after Save, two undos left an included edit in place and the next Save wrote it; now the undo
  restores the text the file was read as.

The costs: a teammate's change is found at Save, not while you edit; what the editor holds lives only in the process
until a stop writes it to the recovery file (the record "Unsaved work is kept in a hidden file of the served folder");
the editor needs confluid 0.4.0, the release whose `load` takes `texts=`.

**Example.**

```
POST /api/graphs/demo/param {node: prep/scaled, param: target_max, value: 2.0}  → dirty: true;  demo.yaml: target_max: 1.0
POST /api/graphs/demo/save                                                       → dirty: false; demo.yaml: target_max: 2.0
(someone appends a line to demo.yaml; the editor holds another edit)
POST /api/graphs/demo/save  → 400 …/demo.yaml changed on disk since the editor read it — Revert to load it, or Save As to keep both
```

**What you may change.** The shape of the dirty signal, the messages, how Save As asks to overwrite. What must
hold: no editing route writes to disk, and no load does either — what the editor holds reaches confluid as
`texts=`; Save never overwrites a file that changed since the editor read it, and writes nothing when one of its
files cannot be written; the graph's own file is written last; the trace runs what the editor shows; saves, disk
reads and loads take the lock. Pins: `tests/test_app_save.py`,
`tests/test_app.py::TestTrace::test_the_trace_runs_what_the_editor_shows_not_the_file_on_disk`.

**Update, 2026-09-29 — File ▸ New… writes its file at once.** Explicit Save covers EDITS; making a graph is not an
edit. Asked whether a graph made with New… by mistake needed a "clear workspace", the user answered: *"New ... by
mistake should not happen. It should ask to save."* A graph that lives only in the editor is exactly what makes a
mistaken New… hard to get rid of: it sits in the list, a stop keeps it in the recovery file, and nothing on disk
shows it. So the New… dialog asks the name and the folder, and Create writes the starter at once
(`Document.create`: it creates the file or fails, and never opens one that is there, so a file someone made after
the check is never written over). Measured, `POST /api/graphs {name: fresh, kind: flow}`:

```
before:  {name: fresh, dirty: true}    no fresh.yaml on disk; ● fresh in the list; a stop kept it in the recovery file
after:   {name: fresh, dirty: false}   fresh.yaml holds the starter; a stop with no edit keeps nothing (stash() == [])
POST /api/graphs {name: demo, kind: flow}   (demo.yaml exists)
         → 400 …/demo.yaml already exists — pick another name, or open it with File ▸ Open    demo.yaml byte for byte
```

The consequences: every graph the editor shows is a file, so Save has one path and the recovery file holds only
changed files; Revert of a graph made with New… goes back to its starter; a file made in another folder joins the
list like one opened with File ▸ Open (`clip~2` when the name is taken). The cost: a mistaken New… leaves a file,
which the person sees and deletes — the point of the user's answer. What must hold: no graph exists only in the
editor; New… never writes over a file; every refusal (the file exists, the folder is missing, is not a folder or
cannot be written, a bad name) comes before anything is written. Pins: `tests/test_app_files.py::TestNew`,
`tests/test_canvas.py::TestNewAndOpen`.

**Update, 2026-09-30 — a file the editor holds never reaches the disk before Save.** confluid resolves an
`include:` to a FILE, so the first cut of Explicit Save wrote each held text into its place for the length of every
load and put the old content back after, under the lock. That kept a steps file made by Move to its own file… in the
graph folder during every check, edit and trace, left it there if the process died mid-load, and made a read-only
folder refuse every edit. The user picked the option that has confluid take the texts: `confluid.load(..., texts=)`
parses a held text in place of the file — through the same search, with every `file:line:col` naming the real
path — so `_load_document` hands over the graph's path and every held file, the graph's own text included, and
writes nothing; every include, in the graph or in a held file, is found beside the file that names it by confluid
itself (the update below). Measured through the routes, a flow
holding one subgraph, Move to its own file…, then 5 checks and 5 edits with nothing saved:

```
before  the folder during those loads:   ['demo.yaml', 'prep.steps.yaml']
        the same, folder chmod 555:      edit 400 …/prep.steps.yaml:4:7: …/demo.yaml: does not load:
                                         PermissionError: [Errno 13] Permission denied: '…/prep.steps.yaml'
after   the folder during those loads:   ['demo.yaml']
        the same, folder chmod 555:      edit 200
both    the folder afterwards:           ['demo.yaml']        after Save: ['demo.yaml', 'prep.steps.yaml']
```

The lock stays, for what still needs it: a load reads the includes the editor does NOT hold from disk, and a save of
another graph may be writing one of them (the lock bullet above). The rejected alternative is the first cut itself —
writes into the person's folder that only the lock kept from colliding — measured in confluid's own record ("A
caller can hand `load` the texts of the files it holds"). Pins:
`tests/test_app_save.py::TestAHeldTextNeverTouchesTheDisk` — a read-only folder, the folder listed during every
load, inner nodes located in the held file, a nested include beside a held file, a held text over a different file
on disk.

Three consequences, found by the review of that change and settled in the same round:

- **A Save writes all of its YAML files or none.** With loads no longer writing, Move to its own file… into a folder
  that cannot be written is ACCEPTED (it used to fail at the staging write), and the first Save after it wrote the
  graph, then met the steps file: `400 …/parts/prep.steps.yaml cannot be written: Permission denied`, with
  `run.yaml` already saying `steps: {include: parts/prep.steps.yaml}` and a fresh `confluid.load(run.yaml)` raising
  `ConfigFileNotFoundError`. Save As wrote `other/run2.yaml` the same way. `Document._refuse_unwritable` now checks
  every file before the first write (`_unwritable`: an existing file must be writable, a new one needs a folder
  that exists and can be written) and refuses in the words a failed write gives; and the graph's own file is written
  LAST, so a failure no check can foresee (a full disk) still never leaves a graph naming a file that is not there.
  The rejected alternative — refusing Move to its own file… in a folder that cannot be written — would take back
  what this update is for: working in a folder you may only read until you choose to save.
  The positions sidecar is written after the YAML files and is not in the check yet (`TASKS.md`).
- **The graph reaches confluid as its path, with its own text among the `texts=`.** The first cut parsed the
  graph's text itself and handed confluid the parsed data, rewriting each relative include beside the graph so
  confluid would find it. confluid names parsed data `<cwd>/string.yaml`, so an include the rewrite could not
  find — a missing file — was searched for in the editor's working directory. Measured, `graphs/demo.yaml` with
  `steps: {include: prep.steps.yaml}` and no such file, the editor started in `streamstudio/`:

  ```
  before  …/graphs/demo.yaml: does not load: ConfigFileNotFoundError: …/streamstudio/string.yaml includes
          prep.steps.yaml: Not found: prep.steps.yaml (searched: …/streamstudio/prep.steps.yaml,
          …/streamstudio/prep.steps.yaml, …/streamstudio/config/prep.steps.yaml, ~/.config/…, /etc/xdg/…)
          at …/graphs/demo.yaml
  after   …/graphs/demo.yaml: does not load: ConfigFileNotFoundError: …/graphs/demo.yaml includes
          prep.steps.yaml: Not found: prep.steps.yaml (searched: …/graphs/prep.steps.yaml,
          …/streamstudio/prep.steps.yaml, …/streamstudio/config/prep.steps.yaml, ~/.config/…, /etc/xdg/…)
          at …/graphs/demo.yaml:5:18
  ```

  Handing confluid the path makes ONE resolver — confluid's search tiers, the including file's folder first —
  decide every include, the graph's and a steps file's alike; the editor keeps no second copy of that rule. The
  location of the include line is the editor's (`_include_loc`: the entry the message names, in a file the
  editor holds). Every load now passes `texts=`, so the editor needs confluid 0.4.0; a load that passed it only
  when a steps file was held was dropped with the first cut — before 1.0 nothing is kept for an older confluid.
  The cost, measured: a graph opened through a symbolic link names its REAL file in its nodes' locations
  (`real/demo.yaml:3:9`, where the first cut said `link/demo.yaml:3:9`), because confluid names every file it
  loads by its resolved path — as the graph's steps files always were; the messages keep the path the graph was
  opened by, and every edit compares files resolved. For a graph that is a symbolic link to a FILE the choice
  reaches further: confluid reads its includes from the REAL file's folder, so every place the editor reads an
  include of the graph's own text itself — Copy's inlining, Save as template — starts there too
  (`Document._folder`). Measured on the first cut, `served/demo.yaml` → `shared/demo.yaml` with a different
  `prep.steps.yaml` in each folder: the canvas showed `shared/`'s (`mode: L`), Copy and the template took
  `served/`'s (`mode: RGB`), and with none in `served/` both were refused for a graph that loaded clean. One folder
  for all of them is the rule; the link's folder was never the one confluid read.
  The location of a missing include matches the entry by every name confluid gives it — as written, and as its own
  `resolve_config_path` made it (`~` expanded; a folder its search found, by its full path) — because confluid's
  message names what it RESOLVED, not what was written: `include: ~/nope.steps.yaml` reads
  `… includes /home/me/nope.steps.yaml: Not found`, and the first cut, comparing the text as written, pointed at
  the bare graph file.
- **A held file is matched by the path its include resolves to, spelled as written.** On a disk that ignores case
  the staged file used to be found under any spelling; a held one is not: an include respelled
  `PREP.steps.yaml` for a held, unsaved `prep.steps.yaml` is refused (`… includes PREP.steps.yaml: Not found`)
  until Save puts the file on disk. The editor keys an included file by the spelling its include resolves to, so an
  edit of `Prep.steps.yaml` included as `prep.steps.yaml` is what runs and what Save writes. Matching held files by
  their identity on disk would give confluid a second identity for a file, which its own record rules out; the case
  is a documented limit.

Pins: `tests/test_app_save.py::TestASaveTheDiskRefusesWritesNothing`, `::TestAnIncludeSpelledInAnotherCase`,
`tests/test_document.py::TestEveryLoadHandsConfluidTheGraphsPath`, `::TestAMissingIncludedFile`,
`::TestAGraphFileThatIsASymbolicLink`, `tests/test_app_clipboard.py::TestAGraphFileThatIsASymbolicLink`.

## A subgraph is one node outside and a nested model inside, its steps named `prep/grey` (`Model.inside`, 2026-09-29)

**Context.** The engine gained `recordstream.flow.subgraph.Subgraph` — a `flow:` mapping used as one op, written
inline in the step that uses it. Drawn the way every other op was, a subgraph step was ONE opaque node whose `steps`
parameter was a form field, and three things broke, measured:

- a form edit of `steps` replaced the whole block with one flow-style line: 18 → 11 lines, 4 → 3 comments;
- one subgraph step made the whole flow's canvas check answer `[]`: an unbuilt marker switched the check off, so a
  real defect two steps later went unreported;
- renaming an OUTER step `grey` also rewrote an inner `from: grey`, and the saved file failed with
  `flow step 'mask': from: 'mono' does not name an EARLIER step`.

The design decisions (`docs/rewrite.md` §6b) fixed the vocabulary: the result line is `result:`, inner names are
`prep/grey`, and an outer step may not read a value inside.

**Decision.** A `Subgraph` marker — recognised by the engine's class attribute `FLOW_SUBGRAPH` — is one node of
kind `subgraph`: `record` in and out, plus the entry sockets the engine derives, and ONE parameter, `result`. Its
inside is a nested `Model` in `model.inside[<node>]`, derived by the same builder, so every inner node carries its
real `file:line:col` (the steps file's line when the steps are included). Inner names are the engine's, qualified
(`prep/grey`; a Stream member `stream.ops[1]/grey`, which the trace maps to the engine's `ops[1]/grey`), and each
inside gains two boundary nodes, `prep/<in>` and `prep/<out>`, where the record enters and leaves. The deriver
BUILDS a subgraph marker, so the chain check stays on. A step name is scoped to its own steps mapping: the text
patcher follows no `from:` / `merge_from:` / `bind:` into a marker's `steps:`, and a rename stays on its level. An
inner problem is reported once, on its inside and at the top; the engine's refusal of the whole subgraph only when
the inside derived clean.

**Consequences.** Editing `target_max` of `prep/scaled` changes one line and keeps every comment (4 → 4, pinned).
The check stays on: the defect after a subgraph is reported at its node (`:floor: NeedsSpectrogram needs the record
entry 'spectrogram', which nothing before it produces …`). Renaming the outer `grey` changes only its own key line,
and the file runs. The costs: `/` is reserved in step names (the engine refuses it); the patcher tells a subgraph by
its `!class:` tag, so a `_target_:`-spelled Subgraph is not scoped (a documented limit); a subgraph held in another
op's slot (`maybe.op`) is drawn and edited like any other, but the engine does not descend there, so its inside has
no trace rows. Deriving the 25-node Bluetooth pane costs what it did (27.9 ms before, 28.1 ms after, best of 7).

**Example.**

```python
doc = Document.open("demo.yaml")                        # the flow of prep (a Subgraph of grey, scaled) and mask
[(n.name, n.kind) for n in doc.model.nodes]             # [('prep', 'subgraph'), ('mask', 'op')]
[(n.name, n.kind) for n in doc.model.inside["prep"].nodes]
# [('prep/<in>', 'boundary'), ('prep/grey', 'op'), ('prep/scaled', 'op'), ('prep/<out>', 'boundary')]
list(doc.model.node("prep").params)                     # ['result']
doc.set_param("prep/scaled", "target_max", 2.0)         # one line of demo.yaml changes; doc.dirty is True
```

**What you may change.** How an inside is laid out and drawn, the boundary nodes' spelling, the separator — only
together with the engine's (`recordstream.flow.steps.SUBGRAPH_SEPARATOR`). What must hold: `steps` is never a form
field; the deriver builds a subgraph marker; a name is scoped to its level; the editor's inner names are the
engine's. Pins: `tests/test_subgraph_model.py`, `tests/test_subgraph_edits.py`, `tests/test_app_subgraph.py`.

## Reuse is a copy, a template or an include — never a YAML anchor or a `!ref:` (2026-09-29)

**Context.** A subgraph may be needed in two places. The YAML already has two ways to say "the same thing twice" —
anchors (`&` / `*` / `<<`) and confluid's `!ref:` — and a third exists for files, `include:`. Each was measured on the
same two-step subgraph (mask mean 0.2855 on a test image):

- **A merge key** (`flow: {<<: *prep, mask: …}`) puts the merged steps FIRST whatever line it is written on
  (`['grey', 'scaled', 'first', 'mask']`); a second `<<: *prep` in one flow silently vanishes
  (`['grey', 'scaled', 'mask']`); and a host step that shares a name with an inner step silently REPLACES it
  (`ConnectedComponents expects a 2-D mask; got shape (120, 160, 3)`, while the canvas check said `[]`).
- **`!ref:`** makes both uses ONE object: after a run the first use's `@output` read `0.7145` — the other use's
  value — instead of its own `0.2855`; `!ref:thr {low_level: 0.2}` written on one use set BOTH uses to 0.2, and the
  same override on a dotted target silently did nothing.
- **`steps: {include: prep.steps.yaml}`** in two uses built two SEPARATE sets of op objects
  (`same inner op objects across uses: False`) and ran right (mask mean `0.2855`).

The user's rule: "if a subgraph is needed twice (either copy it, use templates … or use the include
construction)".

**Decision.** Three ways, each a text operation: **copy** (the clipboard holds the step's own text; a subgraph
whose steps are included is copied with those steps inline — next record); **templates** (a steps file in a
templates folder outside every graph, written at once; Insert template adds a copy, and nothing links back);
**include** (Move to its own file… writes the steps to `<name>.steps.yaml` and `steps: {include: …}` in their place;
Insert steps file… adds another use). Nothing in the editor writes an anchor or a `!ref:` as a reuse spelling.

**Consequences.** A copy and a template are independent: editing one never changes another. An include is shared on
purpose: an edit through one use lands in the steps file and changes every use — tuning one use alone is what a copy
is for. A steps file is a bare steps mapping, so the graph list leaves `*.steps.yaml` out. Copying an included
subgraph used to paste `steps: {include: prep.steps.yaml}`, which bound silently to whatever file of that name sat
beside the target graph (measured: the paste read `mode: RGB` where the original was `L`); it now pastes the steps
inline. Anchors and `!ref:` stay valid in a hand-written file — the editor reads one; it never writes one for reuse.

**Example.**

```yaml
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps: {include: prep.steps.yaml}
      result: scaled
  mask:
    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
  prep2:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps: {include: prep.steps.yaml}
    from: mask
outputs: prep2
```

```
POST …/param {node: prep/scaled, param: target_max, value: 0.5}
→ prep/scaled 0.5 and prep2/scaled 0.5 — both located at prep.steps.yaml:4:7
```

**What you may change.** Where templates live (`--templates-dir`), how a copy names itself (`_2`), the template's
first line. What must hold: three ways and no fourth; a template insert and a paste are copies; the editor never
writes an anchor or a `!ref:` for reuse. Pins: `tests/test_app_clipboard.py`, `tests/test_app_templates.py`,
`tests/test_subgraph_model.py::TestUsingOneStepsFileTwice`.

## Copy and paste carry the document's TEXT (`POST …/copy`, `POST …/paste`, 2026-09-29)

**Context.** Phase 3 pasted a node as `add_step(class, params)` with the params read off the model's JSON. JSON has
one number type, so a copied `{gain: 2.0}` pasted as `{gain: 2}`; comments and `$VAR` spellings were lost; and a
subgraph — a whole nested block — cannot be rebuilt from a class and a flat parameter map. The user asked that
copying a subgraph node paste "a full, independent copy of its block".

**Decision.** `POST …/copy {nodes}` answers `{text, kind}`: the copied flow steps' own lines as a steps mapping
(dedented, comments kept), or the copied `ops:` members as a YAML sequence. The page's clipboard holds that text.
`POST …/paste {text, after}` inserts it after `after`, on `after`'s level: a taken name is renamed (`grey` →
`grey_2`) and references among the pasted steps follow; a pasted step that read a step NOT pasted reads the one
before it, so the block lands as a chain; the step that read `after` now reads the last pasted one; ONE undo entry.
A subgraph whose steps are included is copied with those steps inline.

**Consequences.** `2.0`, comments and `$VAR` survive; a copy pastes into another graph, and into a Stream as list
members. The clipboard is filled by the server's answer, so a paste pressed before the copy answered must wait for
it — it does (measured before: `data-saves` stayed 0 and the paste was dropped). A pasted text holding an
`include:` key is refused.

**Example.**

```
POST /api/graphs/chain/copy {nodes: [prep]}
→ {kind: steps, text: "prep:\n  op: !class:recordstream.flow.subgraph.Subgraph\n    steps:\n      grey:\n        op: !class:… {mode: L}   # to one channel\n…"}
POST /api/graphs/chain/paste {text: <that text>, after: prep}
→ pasted: [prep_2]; the nodes are prep, prep_2, mask
```

**What you may change.** The rename rule, how an outside reference is chained. What must hold: the clipboard is
document text, never a page-side model; a paste is one edit and one undo entry. Pins: `tests/test_app_clipboard.py`,
`tests/test_canvas.py::TestCopyPaste`, `tests/test_canvas.py::TestPasteRightAfterCopy`.

## The CLI serves one editor and refuses an option that reaches nothing (`streamstudio.cli`, 2026-10-14)

**Context.** `streamstudio` is a liquifai app with three commands: `run` (the default — bare `streamstudio`), `stop` and
`restart`. It once launched two editors, chosen by an option; a second, older editor was removed before the first
release, and with it that option. What is left is a launcher, and a launcher has a failure a configuration-driven
command does not: it materializes nothing, so liquifai's usual leniency — a key that matches no parameter is carried on
as configuration, and a warning speaks only when something was materialized from it — would make a mistyped option
indistinguishable from one that worked (`--templates-dri /x` would start the editor on the default templates folder and
say nothing). Three faults of the same family were measured while the CLI took its shape: a folder given relative was
handed to the editor as written (`streamstudio graphs` listed `graphs/chain.yaml` while the file browser looked in
`graphs/graphs`; `streamstudio .` opened both dialogs on `/`, where Save As would have written to the filesystem root);
and the busy-port hint suggested `streamstudio --port 8195`, which served the CURRENT directory with the default
templates folder, not the one the person had asked for.

**Decision.** (1) The app is declared with `strict_flags=True`: every command takes PLAIN values, there is no
configuration document for an override to fall through to, so an option that names no parameter of the command is an
error that names it (and the nearest declared parameter, when there is one) before anything starts. (2) The folders are
resolved to absolute paths ONCE, in `_canvas_folders`, before any work — and before `restart` stops anything, so a
folder that is not there is refused naming it while the running editor is still running. liquifai binds CLI values
verbatim as strings, so they are coerced there; every graph path the editor then answers is absolute, and a path from
`GET /api/graphs` can be sent back. (3) Every command a message prints must parse back: the busy-port hint
(`_report_port_in_use`) prints the SAME folder and templates folder one port up, absolute, shell-quoted (`shlex.join`),
the folder first because liquifai binds positionals up to the first flag; the address is printed bare with `print()` on
a line of its own, so a terminal linkifies it. (4) A taken port is refused before any work, and asked again after the
page was built (a build takes seconds, and another start may take the port meanwhile). (5) `stop` and `restart` default
to the editor's port, 8190 (`EDITOR_PORT`), and find the server by that port ("The studio is addressed by its PORT");
the way out of a start keeps the person's unsaved work ("Unsaved work is kept in a hidden file of the served folder").

Rejected: a lenient mode that warns (the warning is the thing a launcher cannot raise, having materialized nothing);
argparse's spellings (liquifai's `strict_flags` gives the same refusal with its own boolean and list spellings).

**Consequences.** Measured 2026-10-14, each output beside the command that produced it:

```
$ streamstudio graphs --templates-dri x
Error: streamstudio: unknown option 'templates_dri' — it names no parameter of
the 'run' command and no key in the configuration. Did you mean 'templates_dir'?
Declared parameters: graph_dir, port, templates_dir.                              (exit 1, nothing served)

$ streamstudio graphs --port 18197                     (a program already listens on 18197)
Port 18197 is already in use — StreamStudio may already be running there.

    Open it:  http://127.0.0.1:18197

If that is a different program, run StreamStudio elsewhere:  streamstudio /abs/graphs --port 18198

$ streamstudio restart grpahs --port 18198
Graph directory not found: /abs/grpahs                                            (nothing was stopped)
```

The cost: a start script that spells an option the command does not declare fails at once — which is the point.

**Example.**

```python
from streamstudio import cli

sorted(cli.app._commands)        # ['restart', 'run', 'stop']  — liquifai's registry of the three commands
cli.EDITOR_PORT                  # 8190                        — the port all three default to
```

**What you may change.** The port, the option spellings, the layout of the busy-port hint, the wording of the
refusals. What must hold: an option no command declares is refused, never ignored; the folders are resolved once,
before any work, and `restart` checks its folder before it stops anything; every command a message prints is one the
CLI accepts (pinned by parsing it back). Pins: `tests/test_cli_editor.py::TestTheCommands`, `::TestServingTheCanvas`,
`::TestStopAndRestart`, `tests/test_cli_port.py`.

## Unsaved work is kept in a hidden file of the served folder when the editor stops, and the PAGE asks what to do with it (`Studio.stash`, `GET /api/workspace`, 2026-09-29)

**Context.** Explicit Save (above) made the editor hold edits in memory until Save. `streamstudio stop` and
`restart` stop the editor with SIGTERM, and measured at the start of this round SIGTERM ended it at once (exit
-15): every unsaved edit was gone, without a word. The user: *"streamstudio stop or restart should save the file to
a tmp workspace file (hidden), when opening that same workspace streamstudio should ask to start from the tmp file
or the original"*. That leaves four questions: where the work is kept, when it is written, who asks, and what may
never be lost.

**Decision.**

- **Where: one hidden JSON file in the folder the editor was started on**, `.streamstudio-workspace.json`, holding
  every graph with unsaved work, graphs opened from other folders included. Three
  alternatives were rejected:
  - *writing the unsaved text into the graph files* (an autosave) undoes Explicit Save: a stop would write what
    nobody chose to save, and "Open the saved files" would have nothing left to open;
  - *a file beside each graph* (`demo.yaml.unsaved`): work from other folders would land where the next start
    does not look, and the graph list and the file browser would need a second hidden kind;
  - *a folder under the home directory*: the work would be cut off from the folder it belongs to, and nobody looking
    at the folder would see that something waits. In the served folder, "the same workspace" is the same folder.

  JSON, not YAML: it is editor state (texts, what each file held when read, positions), not a confluid document,
  and nothing should ever load it as a graph.
- **When: only as the process stops, and only when something is unsaved.** The CLI turns SIGTERM into the Ctrl+C
  way out and writes in a `finally`. There is no periodic autosave: it was not asked for, and a timer writing into a
  folder under version control makes files nobody asked for.
- **Who asks: the page, not the terminal.** The editor often starts where nobody watches the terminal (in the
  background, from a `restart` another tool ran), and a terminal prompt would block the start until answered. The
  CLI prints one line; the page asks before it opens any graph, because opening one reads the file on disk and the
  next edit would fork from the saved text. The answers are routes of `Studio`, so an agent's tool can answer the
  same question.
- **What is never lost.** A file nobody answered yet is merged into, never overwritten: a second stop (two editor
  processes on one folder, a restart before anyone answered) keeps the earlier graphs, and one file kept twice is two
  entries. A file the editor cannot read is moved aside, never deleted. Restore opens a kept version BESIDE a graph
  that holds other unsaved changes (`demo~2`). The write goes through a temporary file of its own, renamed over the
  real one under a `flock` on the folder, so two stops at once, or a stop cut short, leave a whole file. A browser
  tab of an earlier editor cannot write into the new one: each editor has a session, and a request from another
  session is refused with `409` before anything changes.

**Consequences.** Measured:

```
before:  streamstudio stop  →  the editor ends at once (exit -15); no file; the unsaved edit is gone
after:   $ streamstudio stop --port 8196
         Stopped StreamStudio on port 8196 (pid 57474). Its unsaved work (demo.yaml) is kept in …/graphs/.streamstudio-workspace.json — the next start asks what to do.
         $ streamstudio …/graphs --port 8196
         Unsaved work from the last session (29 Sep 20:06): demo.yaml — the page asks whether to restore it.
```

- Two editor processes on one folder, stopped at once: before, work was lost in 4 of 4 trials and one file was not JSON;
  after, both editors' graphs were kept in 4 of 4.
- A tab left open across a restart: before, its next edit went to the new editor's document, read from disk, and
  the kept value (0.8) was gone for good; after, the edit is refused (`409`, nothing changed) and the tab asks the
  question, whose Restore brings 0.8 back.
- A graph file of the kept work that cannot be read (`chmod 000`, or not UTF-8): before, every start died with a
  `PermissionError`; after, the start comes up and the dialog's row names the file and the reason
  (`…/other/away.yaml cannot be read: Permission denied`).

The costs: the file lives in the person's folder, so a repository holding editor graphs must ignore
`.streamstudio-workspace*.json`. The undo history is not kept. `stop` asks the editor first and waits up to 5 s,
so a listener that never answers HTTP stops up to 5 s later. A `kill -9` and a crash
keep nothing. **Update, 2026-09-30:** SIGHUP — the terminal window the editor runs in closed — takes the same way out
as SIGTERM (the user's pick); measured before, it ended the editor at once (exit -1) with no file written, and a first
version kept the work but exited 120 because the final flush to the vanished terminal failed.

**Example.**

```
POST /api/graphs/demo/param {node: stream.ops[2], param: low_level, value: 0.8}   → dirty: true
(the editor stops: stop / restart / Ctrl+C / terminal closed)                            → .streamstudio-workspace.json; demo.yaml unchanged
GET  /api/workspace          → {folder, session, recovery: {saved_at, error: null, aside: null,
                                  graphs: [{name: demo, disk: unchanged, changed_on_disk: false, …}]}}
POST /api/workspace/recovery/restore   → [{name: demo, dirty: true, restored: true}, …]
POST /api/workspace/recovery/restore   → 404 no unsaved work of an earlier session in …/graphs
```

**What you may change.** The file's inner layout, the wording of the lines and the dialog, how rows are named.
Adding a periodic autosave is a new decision to put to the user (it writes files unasked). What must hold: a stop never writes a graph file; the recovery file is written only when something is
unsaved and removed only by an answer; an unanswered or unreadable file is never overwritten or deleted; no graph
opens before the question is answered; a stop never claims work was kept without reading the file back. Pins:
`tests/test_recovery.py`, `tests/test_recovery_process.py`, `tests/test_cli_editor.py::TestWhatStopSays`,
`tests/test_canvas.py::TestUnsavedWorkOfTheLastSession`, `tests/test_canvas.py::TestATabLeftOpenAcrossARestart`.

## An op-list member is named by its place, so an edit re-keys the members after it (`Document._shift_members`, the `shifts` answer, 2026-09-29)

**Context.** A `flow:` step has a key; a member of a `Stream`'s `ops:` (a `Pipeline`'s `transforms:`) has only its
place, so the canvas names it `stream.ops[2]` — the engine's name. Adding or removing a member (the user's answer
"yes", 2026-09-29) changes the place of every member after it. The sidecar keeps positions and groups by name, the
page keeps card sizes and its level by name, and the undo history brings names back. Measured before the re-key was
complete: a member added between two others was drawn on the sidecar's place of the member it pushed down (both at
`(200, 0)`), the undo of a removal brought the member back outside its group, and Ctrl+Z inside a subgraph member
showed another subgraph under the same breadcrumb.

**Decision.** Names stay positional — no ids are written into the YAML (the document-is-the-truth record). Every
add, remove, undo and redo re-keys the positions and group entries of the LATER members in the same operation;
the sidecar's positions are merged under the ones the editor holds first, so a missing name means "lay it out
afresh", never "use what the sidecar kept for that name". `POST …/undo` and `…/redo` answer the renames they made,
`shifts: [{list, first, by}]`, and the page moves its own per-name state through them instead of guessing.

**Consequences.** A later member keeps its place and group across an insert (`stream.ops[3]` → `stream.ops[4]`),
and an undo moves the names back. The cost is one more field in two answers and one rule every per-name map must
follow: a new per-name map on the page must be re-keyed through `shifts`, or it drifts after an undo.

**Example.**

```
POST …/steps/remove {nodes: [stream.ops[2], stream.ops[4]]}   → both lines gone, one undo entry
POST …/undo  → changed: true, shifts: [{list: stream.ops, first: 2, by: 1}, {list: stream.ops, first: 4, by: 1}]
```

**What you may change.** How the page applies `shifts`, the layout of a fresh member. What must hold: member names
are the engine's positional names; every edit that moves a member re-keys what is kept by name in the same
operation; undo and redo say how they renamed. Pins: `tests/test_op_lists.py::TestPositionsFollowTheMembers`,
`tests/test_op_lists.py::TestTheRoutes`, `tests/test_canvas.py::TestAnUndoInsideASubgraphMember`.

## A host mounts the editor through three methods, and the editor follows it (`app.EditorHost`, `editor_blueprint(host=…)`, 2026-10-01)

**Context.** A consuming workspace shows the editor in a panel of its own page, mounted in its own Flask app (no
second process, no second tab). Three things it needs could not be expressed by mounting the blueprint over a
folder: its graphs sit in a STATE folder it moves on its first save (and copies on a save-as), so a folder fixed at
registration would list and save into a folder that no longer exists; its panes are graphs WITHOUT a source — the
record is whatever the person has selected in the workspace's page, with the window and band it shows filled into
the chain's ops — so the run refused them ("a host supplies the record (mount the editor in one) or wire a source")
with nothing behind the sentence; and it re-runs its panes when a graph file changes, which with an explicit Save
means "when a Save wrote one". A bespoke route per consumer would spell the same three needs differently each time.

**Decision.** The mounting app passes a HOST — a `typing.Protocol` of three methods and a `name`, no base class:
`folder() -> Path` (asked at each request), `record(graph, path) -> HostRecord` (a Run of a graph that has no
source of its own) and `saved(paths)` (after every Save that wrote a document file); `name` is what the page calls
the host, and `GET /api/workspace` answering `host: {name}` is how the page knows it is mounted. `editor_blueprint` takes a
folder OR a host, never both. The folder is a property of the `Studio` that asks the host and, when the answer
changed, rebases every registry entry and every held document under the old folder — the document's texts, its
saved baseline and its undo history re-keyed, nothing read or written (`Document.moved`). A host's record runs
through the texts the editor holds on recordstream's tracer, with the host's `fills` set on the top-level ops that
hold every attribute of a group — the same rule a consuming workspace applies to its own chains — so pause, step and
rerun work as for a graph's own record, and the run says what it filled. `Document.save` answers the files it wrote
so `saved` gets exactly those; a host failing on the news never turns a written file into a refused Save.

Rejected — the two larger alternatives first:

- **A second editor in the consuming workspace** (an editor of its own, with a copy of each document in a format of its
  own, or a form, run and save grown over the same YAML). Two editors over one document is two answers to "what does
  this graph run": the measurement that started this phase was exactly that — an edit saved in the canvas editor
  (`fft_size` 1024 → 2048) was not what the pane ran (it read the JSON copy the other editor wrote, its cache keyed on
  that file's time) and was thrown away at the next start when the copy check rebuilt the YAML from the JSON. A second
  editor also brings back what the rewrite removes: a second process, a JSON copy and a converter between them. One
  editor mounted where the person works keeps one document, one save and one undo history.
- **An iframe-only bridge** (the page asks the host's PAGE for the record and the folder through `postMessage`, and
  tells it of a Save the same way; no server-side host). The record a pane runs is a server-side object — a capture's
  samples as a numpy array, the regions drawn on it — so every Run would serialise it into the browser and back into the
  server it came from; the folder a Save writes into would be whatever a page last said, while the host moves that
  folder on the server; and a Save made through the routes by a tool (phase 5's agent tools) or a second tab would go
  unheard. The page does speak to its host page through its messages ("The page in a host's panel" below), but
  only for what a page decides — which graph and tab to show, and that a Save happened, so the host's own page can
  re-draw; what runs and where files go is the server's, through these three methods.

And three narrower ones: re-opening the documents of a moved folder (loses the undo history of exactly the graphs the
person was editing); reading a record from the host over HTTP (the host is in-process — a route would add a second
protocol for one function call); a fill table keyed by class (a closed list that drifts — attributes are what the ops
declare, and "every attribute of the group" keeps a window from half-applying to an op with only a `start_s`).

**Consequences.** A standalone editor is unchanged (no host: the old refusal, word for word). The host's `folder()`
is called on every request, so it must be cheap. Fills reach the graph's own level only — an op inside a subgraph
step keeps its own values (a documented limit). The stop of a host is the host's: it calls
`studio_of(app).stash()`, the same method the CLI calls, so the recovery file and the page's restore question are
one mechanism for both.

**Example.**

```python
class Host:
    name = "the workspace"
    def folder(self): return workspace.state_dir()
    def record(self, graph, path):
        return HostRecord({"input": capture, "regions": regions}, fills=({"start_s": 0.001, "stop_s": 0.003},))
    def saved(self, paths): workspace.rerun_panes_reading(paths)

app.register_blueprint(editor_blueprint(host=Host()), url_prefix="/studio")
# POST /studio/api/graphs/view_psd/trace {}  ->  record: null,
#      host: {label: "…", filled: {"stream.ops[0]": {start_s: 0.001, stop_s: 0.003}}}, statuses: {… "ok"}
```

**Update 2026-10-01 (the review of the mount).** Five things a host met, each measured, and the rule each became:

- **One spelling for a path.** `record()` got `<folder>/view_x.yaml` as the host spelled its folder, `saved()` the
  resolved `/private/…/view_x.yaml` — not under `folder()` for a macOS temporary folder, so a host matching the two
  missed every save. Every path the host is handed is now spelled under `folder()` as `folder()` spells it
  (`Studio._spelled`; resolved only for a file outside the folder). Rejected: resolving everything (the host would
  have to resolve its own folder before every comparison — the trap it fell into).
- **New… is a write.** It writes its starter at once, so the host hears it through `saved()` like a Save (a host
  finding its panes through `saved` never heard of a new one).
- **The host writes files too.** A conversion, an edit of its own source graph: the editor kept the first text it
  read for the process's life. A document holding NOTHING unsaved is now read again when its file changed on disk
  (`Studio.document`); a document holding unsaved work keeps it, and its Save refuses as before. Rejected: a host
  call "I wrote this file" (a contract for the host to forget; the disk is the truth either way).
- **A move rewrites includes out of the folder.** Insert steps file… writes an include relative to the graph's
  folder; after the host moved that folder the include named nothing, and Save wrote a graph that did not load. A
  move now rewrites the held texts' relative includes the way Save As does (`document.follow_includes`): read
  lexically from the old folder (gone by then), a target inside moved with it, one outside stayed. The graph is then
  unsaved — the file on disk still names the old way — and Save writes the corrected one.
- **Every failure of the host is words.** A `record()` raising anything but `ValueError`, or answering no
  `HostRecord`, reached the page as werkzeug's HTML 500 (a `KeyError` as a 404 `'rec_0042'`); a `folder()` raising
  broke every request the same way. Both are now the request's 400 naming the host and the error. A graph whose own
  source yields NOTHING (a source the host fills when IT runs the graph — the files graph) says so instead of
  `the source yields only 0 records`: the editor runs the source as the document spells it.

**Update 2026-10-05 — a fourth method, `shipped()`.** A host may ship graphs from outside its folder (a package's
templates); the editor lists and opens them while the folder holds no file of their name, and writes the copy only on
the first Save of a change to the graph itself. The record "A graph's settings live in a file of their own" has why.

**Update 2026-10-14 — mounting costs an import and nothing else.** `import streamstudio.app` takes about 0.2 s and
loads no torch (measured: 0.20–0.22 s in three fresh processes, `import streamstudio.cli` 0.10 s); the package root
answers `__version__` and the HuggingFace lookups the pickers call, registers nothing, and never changes the host's
environment (no `NO_COLOR`, no colour setting). A host that mounts the editor pays for it only when it serves one. Pins:
`tests/test_lazy_package.py`,
`tests/test_logflow_colorize_default.py::test_a_host_mounting_the_editor_keeps_its_environment_and_its_colors`.

**What you may change.** What a host adds beyond the four methods, how a run labels the record, the fill rule's
reach (into subgraphs, once the tracer exposes their ops). What must hold: the editor never caches the host's folder
across requests; a moved folder keeps the held documents' edits and history; a host's record runs the held texts,
never the files; `saved` hears only of document files actually written, every path spelled one way; a clean
document never shows an older text than its file. Pins: `tests/test_host.py`.

## The page in a host's panel speaks through its link and three messages (`?embed=1`, `frontend/src/host.ts`, `link.ts`, 2026-10-01)

**Context.** A consuming workspace shows the page in a panel of its own page (an `<iframe>`), not in a second tab. Its
buttons must open a graph on a given tab ("graph", "settings"); before it closes the panel it must know whether the
person holds unsaved edits; and the moment a Save writes a graph its own panes must re-run. Measured before: the
page always opened on the text tab (no link could ask for the settings), a `?graph=` naming a graph the folder did
not hold opened the FIRST graph without a word, and a switch by setting the iframe's `src` reloads a page that may
hold unsaved work — the browser then asks "Leave site?".

**Decision.** The link says what to show — `?graph=`, `?level=`, `?tab=text|settings|run`, and `?embed=1` for the
panel (chrome removed, every function kept) — and follows what the page shows, so a reload opens the same view. A
graph the list does not hold opens NOTHING: the canvas names it and the graphs there are. Three `postMessage`
messages, same origin only, between the page and its OWN parent window: the page posts `state` (the graph shown,
dirty, every unsaved graph, the tab) on every change and `saved` (graph, path) after a Save; it takes one command,
`open` (graph, tab, level). Run on a graph without a source of its own sends no record number and the server asks
the host (the server decides; the page's `hasOwnSource` only words the panel).

Rejected: switching graphs by setting the iframe's `src` (the "Leave site?" question over unsaved work, and the
page's own state — the run, the undo of the layout — lost); a JavaScript API the host calls into the iframe (couples
the host to the bundle's internals, and a cross-origin mount could not call it); telling the host of a Save only
through `postMessage` (a Save made by a tool through the routes would go unheard — the server's `EditorHost.saved`
is the authoritative one; the message is for the host's PAGE).

**Consequences.** A host needs no code of the editor's: an iframe, a listener and one `postMessage` call. The
messages are a contract with the hosts that read them — a field renamed here is a host broken there. A message from
any window other than the parent, or another origin, is ignored.

**Example.**

```js
// the host's page
studio.src = "/studio/?graph=view_bte&tab=settings&embed=1";            // first open
addEventListener("message", (e) => e.data?.type === "saved" && rerunPanes(e.data.path));
studio.contentWindow.postMessage({target: "streamstudio", type: "open", graph: "view_fft", tab: "run"}, location.origin);
// the page posts: {source: "streamstudio", type: "state", graph: "view_fft", dirty: false, unsaved: [], tab: "run"}
```

**Update 2026-10-01 (the review of the panel).** An `open` arrives while the person is doing something in the panel,
with no click inside it — measured before: a value typed into `gain` and not left was committed into the NEXT graph's
`gain` (the card is the same React component, and its draft survived), and Set source… stayed up over the next graph
and wrote its source THERE (every Stream names its stream `stream`). The rule is that a gesture belongs to the graph
it began on: an `open` first blurs the focused field (its commit goes out through `graphRef` to the graph shown),
then closes Set source… with the palette and the context menu; an answer arriving for a graph no longer shown only
marks the list (`accept`); a node's widgets are keyed by the graph. An `open` of the graph already shown reads it
again (the host may have rewritten it), and every `open` (and New…) asks the editor's folder afresh — the page used to
read it once, so after a move New… named one folder and wrote into another. The panel opens a graph's top level
fitted: at zoom 1 from the top-left a 900x700 panel showed 1 of the Bluetooth pane's 30 cards. Rejected: queueing the
host's `open` until the person is done (the host's page has moved on — its pane, its selection; the panel must follow
it), and refusing it while a dialog is up (the host cannot see the panel's dialogs).

**Update 2026-10-02 (a page that is not the editor).** When there is no editor to serve — the page is not built,
Node was missing at the server's start, or the start's build failed — `/` answers a short page that says which and
what to do. In a host's panel that page used to say nothing to its parent, and a host that covers its panel until
the editor's first `state` (so a graph is never shown under another's name) covered it for good — measured: the
panel read `loading sink…` over `The editor's page is not built and Node is not installed — run aisland setup`.
That page now posts a fourth message, `{source: "streamstudio", type: "unavailable"}`, under the same rules (`?embed`
only, its parent, same origin): no editor will answer here, so the host uncovers the page and, to show another graph,
loads the frame again rather than posting `open`. Rejected: posting `state` from it (the host would then post `open`
to a page that cannot take it, and the next button would hang the same way), and having the host read the frame's
document to guess whether it is the editor (couples the host to the bundle's markup).

**What you may change.** The panel's styling, what `state` adds (a new field is harmless to a host that ignores it),
further commands. What must hold: the same-origin and own-parent checks; an `open` never reloads the page; an unknown
graph is said, never replaced by another; the server, not the page, decides whose record a Run runs; nothing begun on
one graph lands in another. Pins:
`tests/test_canvas.py::TestTheHostHearsAndFollows::test_the_hosts_panel_hears_state_and_saved_and_opens_a_graph_by_command`,
`::TestMountedInAHost`, `::TestTheHostsOpenKeepsEachEditInItsGraph`, `::TestTheEmbeddedPanelOpensFitted`,
`frontend/src/__tests__/host.test.ts`, `link.test.ts`.

## A short class name is looked up one entry point at a time, and the page asks for the catalogue on first need (`document._import_until`, `frontend/src/catalogue.ts`, 2026-10-02)

**Context.** A document may name a class by its registry key (`!class:AnnotationUpdateSink`), and a key resolves only
once the module defining it has been imported. On a miss the editor imported EVERY `confluid.configurables` entry
point (`confluid.load_configurables`) — 127 of them on the development machine, torch, transformers and every
training and detection backend among them. Measured 2026-10-02, the editor mounted in a host app over a sink graph
naming one class from a signal domain's sinks module: the first `GET /studio/api/graphs` took 13.9 s on a cold disk cache, 6.0 s
on a warm one, all of it that import. Second, the page asked `GET api/palette` as it opened — the whole catalogue,
which runs the same full import in another request thread. The two contend for the import locks: with only the
lookup narrowed, the first graph list still took 7.2 s in one run of two while the catalogue was being built beside
it (1.2 s alone). A page opened to look at a graph or change a value paid for both.

**Decision.** Two halves, because either alone leaves the wait.

1. **Server.** A short-name miss imports the entry points ONE AT A TIME and stops at the first after which the name
   resolves (`document._import_until`). Entry points of a package that is already imported go first — their
   dependencies are loaded, and the key usually lives there — then the rest in their listed order. An entry point
   whose module is already imported is loaded all the same: that costs nothing, and it WAITS when another thread is
   still running the module's body — Python lists a module in `sys.modules` before its body runs, and the catalogue
   imports every entry point in its own request thread. A failing one is skipped with the warning line
   `confluid.load_configurables` writes for it. Each entry point is imported at most once per process: the walk
   takes it off one shared list, so a later miss starts where the last stopped, and a name nothing defines ends with
   all of them imported once and costs nothing afterwards. The walk holds a lock, and a request that waited for it
   looks the name up again before walking on: the walk it waited for may have registered the name and taken its entry
   point off the list.
2. **Page.** The catalogue is asked for when something that lists it is first shown — the Nodes sidebar (also when
   the page starts with it open), Add Node ▸, the palette, Set source… — once per page; each shows "loading the
   catalogue…" until it arrives. A failed answer is a toast and the next need asks again. `data-palette-ready` still
   means "the catalogue has arrived".

Rejected: a background thread that warms the catalogue at server start (the same import-lock contention, moved to
the first seconds of every session, and paid by a host that never opens the editor — a helper PROCESS started when the
page opens has neither cost: "The catalogue is built by a helper process", 2026-10-05); a persisted name → module index
(a second source of truth that goes stale when a package is installed — not decided, see below); keeping the page's
fetch at open and only narrowing the server's lookup (measured: 7.2 s for the graph list while the catalogue builds
beside it).

**Consequences.** Measured the same day, warm cache, a fresh host process each run: the first graph list 6.0 s →
1.1–1.3 s (the lookup imports 15 of the 127 entry points and stops at the sink's); the first graph read after it
0.002 s either way; the catalogue costs its 6.5–8.4 s only when a list of it is opened. **What it does not fix:** an
editor started on its own, whose graphs name a class in a package nothing has imported yet. Over the 21 shipped
annotation panes the first graph list stays at 6.1 s (5.7 s before): `WindowSignal`'s package is not imported yet,
so nothing ranks its entry points first, and the walk loads 85 entry points — every backend listed before them —
until the signal domain's detection package, the 116th listed, registers it. **A short name two installed classes share** resolves to
the first one the walk imports, because the other is not imported yet: before, every entry point was imported first,
so the name was ambiguous and resolved to nothing. With every package installed 18 short names are shared (measured
2026-10-02, e.g. `FourierOp` in a signal domain's `fourier` and `fourier_torch` modules); no shipped graph spells one.
Measured on `FourierOp` in a fresh process:

```
first lookup:          <class 'domain.fourier.fourier.FourierOp'>
after the catalogue:   None                     (the graph: cannot resolve the class 'FourierOp')
a run (strict):        AmbiguousClassError 'FourierOp' is registered by 2 classes, so a bare lookup cannot choose …
```

Walking on through the rest of the defining package (`domain.*`) would settle the 14 shared within one package
(4 are shared between two packages) and costs the sink lookup 0.12 s on a warm disk, 9.5 s on a cold one (the
torchsig backend); not done — the shared name is a documented limit, and its dotted path is the spelling that works
everywhere.
**Two lookups at once**, both measured before the fix with module bodies held half-way by events: a lookup beside the
catalogue's import of the defining module skipped it as imported and answered "cannot resolve" (`walk answered:
None`, the class registered a moment later), and a request that waited for another's walk answered "cannot resolve"
when nothing was left to walk, or imported an entry point nobody needed when something was. A test that counts what
a document load imports must reset `_pending_entry_points` (and serve its own entry points), since a walk earlier in
the process has already consumed part of the list.

**Example.**

```
# a host app, its sink graph spelling !class:AnnotationUpdateSink, first GET /studio/api/graphs
before:  confluid.load_configurables()          127 entry points imported      6.0 s
after:   document._import_until("AnnotationUpdateSink")
           domain-lte, recordstream-ops-predict, …, domain-sinks  ->  found after 15     1.1 s

# the page
before:  page open  ->  GET api/graphs, GET api/types, GET api/workspace, GET api/palette
after:   page open  ->  GET api/graphs, GET api/types, GET api/workspace
         N (Nodes sidebar)  ->  GET api/palette   (once; the sidebar, Add Node ▸, the palette and Set source… share it)
```

**What you may change.** The order of the walk (a better guess of which entry point defines a key — a persisted
index, say — is the open question for the standalone case), and which surfaces count as a need. What must hold: a
miss never imports an entry point twice in one process; an already-imported module's body never runs again; a lookup
never answers "not found" for a name another request's import is about to register; the page
asks for the catalogue at most once while an answer is on its way or has arrived, and never while it opens. Pins:
`tests/test_short_name_lookup.py`, `tests/test_canvas.py::TestTheCatalogueIsAskedForOnFirstNeed`,
`frontend/src/__tests__/catalogue.test.ts`.

## The canvas page builds itself at a server's start when its source changed (`streamstudio.page.ensure_page_built`, 2026-10-02)

**Context.** The page is TypeScript/React in `frontend/` (committed), built by `npm run build` into `frontend/dist`
(gitignored, never in the wheel — the 2026-09-28 record above). Nothing rebuilt it but a person: after a `git pull`
that changed the page, every start on that machine served the page built BEFORE the pull — its routes and its page
out of step — and a machine that had never built it served the placeholder `The canvas page is not built. Build it
once — cd streamstudio/frontend && npm install && npm run build — and reload`. A host app that mounts the editor showed
the same stale page in its panel. And `page_blueprint` decided ONCE, when it was made, whether a page existed, so a
page built while the server ran was never served. A packaged editor sidesteps all of this by shipping its page
prebuilt inside a Python package; a source checkout
cannot — the page in it is whatever the last build left.

**Decision.** A server's START builds the page when what the build reads changed since the last build.

1. ONE public function, `ensure_page_built()` (`streamstudio/page.py`, re-exported by `streamstudio.app`), called by
   the editor's CLI after a taken port is refused and before its app is made (the port is asked again after it), and
   by a host app before it announces its server anywhere and before its app is made. Never at app or blueprint
   creation: tests make apps by the hundred.
2. The source fingerprint is a SHA-256 over every input's relative path and bytes — `src/` and `public/` minus the
   page's tests and hidden files, plus `index.html`, `package.json`, `package-lock.json`, `vite.config.*`,
   `tsconfig*.json` and the two `.env` files a clone carries that `vite build` loads in production mode (`.env`,
   `.env.production`) — and the STAMP is
   `dist/.built-from`, written after a successful build. vite empties `dist` as a build starts, so an interrupted
   build, or one run by hand, leaves no stamp, and the next start builds. (Since 2026-10-04 the stamp is
   `streamstudio/canvas/.built-from`, committed, and a build by hand writes it too — the record below.)
3. `npm ci` runs first only when `node_modules/.package-lock.json` — npm's own record of its last install — is
   missing or older than `package-lock.json`. By time: npm keeps no fingerprint of the lock it installed from.
4. Nothing about it stops a start: without Node, or when npm fails or outlives its time limit (300 s for `npm ci`,
   120 s for the build; npm runs in a session of its own and its whole process group is stopped), the start prints
   why, and the page at `/` — decided per request now — says the same instead of serving an out-of-date page. A page
   with NO stamp and no Node to rebuild it is served as it is (it may well be current). Ctrl+C during the check stops
   npm and ends the start in one line.
5. An answer that did not make the page current is kept per page folder with the fingerprint it was given for: a
   second check in the same process repeats neither npm nor its lines, and a server process the start launches takes
   it over from its environment (`page_check_environment()`) instead of running npm where nobody sees the output.
6. Only a build takes an exclusive `flock` on the page folder (macOS and Linux), so two starts from one checkout build
   once; the second says it waits.

Rejected: **committing `dist`** (every page change becomes a second, generated diff that merges badly, and a commit
that forgets it ships a page out of step with its routes — the very drift this removes; *reversed 2026-10-04* with a
test that catches the forgotten commit and a binary merge rule — "The built page is committed inside the package"
below); **building in `aisland
setup`** (setup runs once per machine, not after each pull, so the stale page after a pull stays); **building by hand**
(what existed — the measured stale page and placeholder); **building at app creation** (a test suite makes hundreds of
apps and must never run npm); **deciding whether the PAGE is current by modification times instead of contents** (a
branch switched away and back changes every time and nothing in the page). The install decision (item 3) does use
times, because the only record of what npm installed is npm's own file; its cost is one extra `npm ci` when a lock is
rewritten with the same bytes and the page needs a build anyway.

**Consequences.** Measured on a copy of the page folder, the real npm: a start after one source file changed prints
two lines and serves 2.0 s later; an unchanged start prints nothing and the check costs 2.1 ms (56 inputs, 570 KB); a
start without Node prints one line and starts at once; two starts at once from the same folder build once — the
second printed `another start is building the editor's page — waiting for it` and served at the same moment as the
first. The first install of the page's libraries takes about a minute and is said before it starts. A page built by
hand had no stamp, so the next start built it once more (2 s) — since 2026-10-04 `npm run build` writes it. Each start that FAILED to build tries again; the
failure's last 20 lines are printed and shown, and `/` keeps saying so until the next start (a page built by hand
meanwhile is not served — the page tells the person to start the server again). A host that registers its server
before checking would make a server still building look gone to anything that prunes unanswered servers — so the
check comes first. Two starts on ONE port released together by the lock still race between the second port probe and
the bind, exactly as two plain starts always did. Where `fcntl` does not exist (Windows) there is no lock between two
starts.

**Example.**

```python
import os, subprocess
from streamstudio.app import ensure_page_built, editor_blueprint, page_blueprint, page_check_environment

def serve(app, host):                       # a host app's start
    ensure_page_built()                     # silent when the page is current — before the server is announced
    app.register_blueprint(editor_blueprint(host=host), url_prefix="/studio")
    app.register_blueprint(page_blueprint(), url_prefix="/studio")
    app.run(port=8195)

def serve_in_background(argv):              # the start checks in the terminal; the child takes its answer over
    ensure_page_built()
    subprocess.Popen(argv, env={**os.environ, **page_check_environment()}, stdout=subprocess.DEVNULL)
```

```
# after a pull that changed frontend/src/toast.ts
streamstudio: the editor's page changed since its last build — building it
streamstudio: built the editor's page in 2.0 s
# the next start: nothing
# the same change, no Node on PATH: the server starts, and GET / answers
The editor's page is out of date and Node is not installed — run aisland setup, then start this server again.
```

**What you may change.** The list of inputs (as long as it covers everything the build reads — a missed input is a
stale page that looks current), the stamp's name and place inside `dist`, the time limits, the wording, how many of
npm's lines are shown. What must hold: only a START checks, never the making of an app; a current page costs no
process, no lock and prints nothing; a start never fails because of the page (Ctrl+C is the person ending it); the
page at `/` never shows a page the start knows is out of date; two starts never build in one folder at once; a folder
without page source is never checked; an answer is never repeated in the process that gave it. Pins:
`tests/test_page_build.py`.

## The built page is committed inside the package (`streamstudio/canvas`, user decision B, 2026-10-04)

**Context.** The page built into `frontend/dist`, gitignored and outside `package-data` (the two records above). So
the page existed only where someone had built it: an install from GitHub — `uv pip install --no-deps
git+https://github.com/Gearlux/streamstudio.git@main`, which is how a consuming app's CI installs the editor — carries
no `frontend/` and no build, `ensure_page_built()` answered `no-source`, and `GET /` served the placeholder `The canvas
page is not built. Build it once — cd streamstudio/frontend && npm install && npm run build …`. Measured 2026-10-04 in
a consuming app's CI mirror (streamstudio installed non-editable from today's `main`): `web_dist()` → `None`, and the
app's ten tests that drive the real editor in its panel SKIPPED (`30 passed, 10 skipped`). A checkout without Node
served the "not built and Node is not installed" page. The build is small — `index.html` (0.4 KB), one js bundle
(511 KB), one css (45 KB); the 1.95 MB source map was the bulk.

**Decision.** B — commit the built page, inside the package.

1. vite writes to `streamstudio/canvas` (`outDir: "../streamstudio/canvas"`, `emptyOutDir: true`; `page.OUT_DIR` says
   the same, `page.built_folder()` derives the folder from the page's source folder). The name collides with
   nothing: not `page` (the module `streamstudio/page.py`), not `static` (Flask serves a `static` folder beside
   `app.py` at `/static/`), not `dist`/`build` (the repository's `.gitignore` drops every folder of those names —
   they are Python's build output).
2. The build record `.built-from` — the fingerprint of the source, `page.fingerprint` — is committed with the build.
   A checkout whose committed page matches its source is `current`: it starts silently and needs no Node. A checkout
   whose page source changed rebuilds at its start as before (Node needed — that is the person editing the page). An
   install with no page source serves the page its package carries; the placeholder is left only for a package with
   no build at all.
3. `package-data` carries `canvas/index.html`, `canvas/.built-from`, `canvas/assets/*.js`, `canvas/assets/*.css`.
   No source map is built (`sourcemap: false`): a committed `.js` would end in `//# sourceMappingURL=…map` for a map
   no install carries, and every page change would add ~2 MB to the history. `npm run dev` still serves the sources
   with maps. A map made by hand is gitignored (`/streamstudio/canvas/**/*.map`) and matches no package-data pattern.
4. THE GUARD, `tests/test_committed_page.py::test_the_committed_editor_page_was_built_from_its_current_source`,
   compares the committed record with `page.fingerprint(page.FRONTEND)` and fails, naming the one build command and
   the folder to commit. It needs no Node, so it runs in CI. It calls the ONE fingerprint; so does the hand build,
   whose `npm run build` is `python3 ../streamstudio/page.py begin && npm run build:page && python3
   ../streamstudio/page.py` (`begin_build()` and `record_build()` — `page.py` imports the standard library only, so a
   `python3` (3.8 or later) command runs it; measured under macOS's `/usr/bin/python3` 3.9.6). A second
   implementation (a vite plugin hashing in JavaScript) was rejected: two hashes drift, and the guard would compare
   one with the other.
   - **`begin` takes the fingerprint BEFORE vite reads a file** (into `node_modules/.cache/streamstudio-page-source`,
     which git ignores and vite's emptying of the built folder does not reach); the last step writes THAT value as the
     record and refuses (`SourceChanged`, exit 1, no record) when the source no longer has it. A record taken after
     the build vouched for a line saved during its ~3 s: measured, a line appended at `rendering chunks` was in the
     source and in the record, not in the bundle (`grep` 1 / 0), the guard passed and every start answered `current`.
     A start already fingerprints before it builds (`_build`), so only the hand build needed the note. Run alone
     (without `begin`) the last step records the source as it is now.
   - **A start runs `npm run build:page`, never `build`** (`page._STEPS`): `build:page` is `tsc --noEmit && vite
     build`, with no `python3` in it, and the start writes its own record. Measured: with `build` ending in
     `python3 …`, a machine with Node on `PATH` and no `python3` command built the page and was told `npm run build
     exited with code 127` (`sh: python3: command not found`), and `/` served the failure page. python.org's Windows
     installer provides `python` and `py` only, and `page.py` supports Windows. The hand build is the developer's, and
     it needs a `python3`.
5. The fingerprint is the same on every machine for the same source: relative POSIX paths and bytes, never mtimes or
   absolute paths — and EVERY input is a file a clone carries: `.env.local`, `.env.production.local` and any `*.local`
   under `src/` or `public/` are `frontend/.gitignore`'s own pattern and are never inputs (`page._read_by_the_build`;
   `tests/test_committed_page.py` fails when one input is git-ignored or when a skipped name is not). Measured with
   a `VITE_DEBUG_PANEL=1` `.env.local` on the developer's machine: the record moved, the guard passed there, a fresh
   clone (without the file) FAILED the guard, and the same clone with no Node was refused the page (`no-node`) though
   nothing of it had changed. The page reads no `import.meta.env` (`grep` finds none), so such a file changes nothing
   the browser runs; if the page ever reads one, the value must come from the committed `.env` / `.env.production`. Bytes means line endings count, so `frontend/.gitattributes` (`* text=auto eol=lf`) checks the
   source out with LF everywhere — a Windows checkout with `core.autocrlf` would otherwise find the page out of date.
6. `.gitattributes` marks `streamstudio/canvas/**` `binary linguist-generated=true`: no diff of minified bundles, no
   line-ending conversion, and a merge of two page changes is a conflict, resolved by building again.

Rejected, each measured or counted against the same consuming CI:
- **A — build only in the release workflow** (Node on the release runner, the page in the wheel): helps nobody until
  a release, and a consuming app's CI installs from GitHub `main`, so its ten editor tests keep skipping.
- **C — build at install** (a setuptools build step running npm): every install, a consumer's CI included, needs
  Node and the npm registry, and is about a minute slower (`npm ci`'s first install takes about a minute).
- **D — keep the honest placeholder**: every install from GitHub stays without an editor.

**Consequences.** Measured 2026-10-04:
- in a copy of the tree (`git ls-files -co`) with no Node on `PATH`, `streamstudio <graphs> --port 8194` printed
  nothing about the page and `GET /` served the committed `index.html` byte for byte — before, the same start printed
  `streamstudio: the editor's page is not built and Node is not installed — run aisland setup` and served that line;
- in the consuming app's CI mirror (streamstudio installed non-editable, `--no-deps`, from a copy), `web_dist()` named
  `…/site-packages/streamstudio/canvas`, `page.FRONTEND` did not exist, and the editor tests went from `30 passed, 10
  skipped` to `40 passed`;
- in a scratch copy, one changed line in `frontend/src/canvas/Palette.tsx` failed the guard with its message; a start
  with Node printed `the editor's page changed since its last build — building it` / `built the editor's page in
  5.5 s`, the guard passed, and `git status` listed `.built-from`, `index.html` and the two bundle names — what a page
  change commits;
- review round, same day, real npm in a scratch copy: with only node, npm and `/bin` on `PATH` a start's rebuild
  printed `the editor's page changed since its last build — building it` / `built the editor's page in 10.0 s` (before:
  `npm run build exited with code 127`); a line saved during `npm run build` ended it with `streamstudio: the editor's
  page's source changed since its build began — build again (npm run build)`, left no record, the guard failed, and the
  next start printed `the editor's page has no record of the source it was built from — building it` and the bundle
  held the line; with an ignored `.env.local` the guard passed on the developer's machine AND in a fresh clone, and the
  clone without Node answered `current`.
Every page change now carries a second, generated change (the bundles, ~0.5 MB), which is the price; the guard turns
the forgotten one into a red CI run instead of a page out of step with its routes.

**Example.**

```python
from streamstudio import page
from streamstudio.app import web_dist

page.built_folder()                     # <repo>/streamstudio/canvas — in an install: <site-packages>/streamstudio/canvas
web_dist()                              # the same folder: every install serves the page
page.ensure_page_built().state          # "current" in a checkout whose committed page matches its source; no Node needed
assert page._built_from(page.built_folder()) == page.fingerprint(page.FRONTEND)   # the guard
```

```
$ cd frontend && npm run build          # by hand: note the source, vite into streamstudio/canvas, then the record
…
✓ built in 3.9s
streamstudio: recorded the source of the editor's page in …/streamstudio/canvas/.built-from
```

**What you may change.** The folder's name (with `OUT_DIR`, vite's `outDir`, `package-data`, `.gitignore`,
`.gitattributes` and the release smoke test together), the guard's wording, the attributes. What must hold: the
committed build matches its committed source (the guard, in CI); ONE fingerprint, in `page.py`, over files a clone
carries; the hand build notes it before vite and records it after; a start rebuilds with `build:page` and so needs no
`python3`; no source map in git or a wheel; every install serves the page without Node; a checkout rebuilds only when
its page source changed. Pins:
`tests/test_committed_page.py`, `tests/test_page_build.py::TestAnInstallWithoutPageSource`, `::TestABuildByHand`,
`::TestWhatTheBuildReads`, `tests/test_packaging.py::test_the_wheel_built_from_a_checkout_serves_the_page`,
`::test_every_file_of_the_built_page_is_shipped_and_no_source_map`.

## A drawn wire is saved in the document's own spelling, and a nested producer moves out to a top-level key (`Document.add_wire`, `POST /api/graphs/<name>/wires`, 2026-10-04)

**Context.** Until now a wire drawn on the canvas was never written (the record above): the user wiring a host
workspace's seeded source graph — `stream.source.class_names → contract.classes` — got a dashed edge and a toast
saying the editor "cannot write" `classes: !ref:stream.source`, and the graph stayed undeliverable. Two things stood in
the way. A wire is not ONE spelling: into a root's output slot it is a `delivered:` entry, into a card's slot a
`!ref:`, and into a record entry of the root (the root reads `input`, the source writes `image`) it is a plain key.
And a `!ref:` names a TOP-LEVEL key only — confluid refuses `!ref:stream.source`, attribute references having been
removed — while the seed writes its source INSIDE the Stream's slot.

**Decision.** The server spells every wire, in the document's own words, as one text patch held until Save:
`delivered: {<slot>: !ref:<P>}` into a root's output slot (adding `delivered:` when there is none),
`<slot>: !ref:<P>` into any other card's single slot (`YamlText.set_marker_edit`), and `delivered: {<entry>: <key>}`
from a record-entry output — a source's `produces`, an op's — into a root's record entry. `P` is the producer's
top-level key; a producer written inside another card's slot is MOVED OUT first (`YamlText.hoist_edits`): its text
whole becomes a top-level key named after the slot, inserted above the key that held it, and the slot reads `!ref:P`.
The name must be one no card takes as a constructor parameter: confluid hands a top-level key to every same-named
parameter, so a top-level `source` reached a second, source-less Stream and overrode the `source` a Stream ABOVE it
spelled (measured 2026-10-04, last spec wins) — `Document._free_key` takes `source_2` then. The page sends only the two
ends and draws the answer; it never draws an edge of its own. Every edit re-derives and must draw exactly the wire
drawn (`_drawn`), so a spelling that would draw another wire — a bare `!ref:` is drawn from P's FIRST fitting output —
is refused, never written. Removing is the same patch backwards; the one problem a removal may gain is the required
input it leaves open (`_commit(allow=…)`). Everything with no spelling yet is refused in one plain line
(`document.WIRE_*`): a Stream's record wires ARE its `ops:` order, and a flow's wires are its steps' `from:` / `bind:`
/ gates.

**Consequences.** A wire drawn on the seeded source graph is saved, reopens drawn, and the file passes the host's
`GraphContract.check()`. A card can change its NAME when it is wired (`stream.source` → `source`): the move is a rename
in the history — its sidecar position and groups follow, those of the cards under it too, a laid-out card keeps the
place it was drawn at (`Document._pin`), and one undo puts it back in the slot. A root card grows one input per record
entry and a source card one output per entry it declares, read off the BUILT source, since a source may answer
`produces` from its settings. A `!ref:` written into a one-line card needs a space before `}` (`{thing: !ref:source }`)
— PyYAML reads `!ref:source}` as one tag.

**Example.**

```yaml
# drawn: stream.source.class_names → contract.classes, source.image → contract.input
source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}   # moved out of the slot
stream: !class:recordstream.core.stream.Stream
  source: !ref:source
  ops: []
contract: !class:recordstream.ops.contract.GraphContract
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
    classes: !ref:source
    input: image
```

```python
document.add_wire("stream.source", "class_names", "contract", "classes")   # → "source" (the card's name now)
document.add_wire("stream.ops[0]", "record", "stream.ops[1]", "record")
# ValueError: This wire would change the order of the steps — the editor can't save that yet.
```

**What you may change.** The refusal wording, where a moved-out key is inserted, the suffix rule of a taken name. What
must hold: the server spells a wire, never the page; a patch, never a re-dump; a `!ref:` only to a top-level key; a
moved-out name no card takes as a parameter; a wire edit draws exactly the wire asked for or writes nothing. Spelling
the wires still refused — a Stream's `ops:` order, a flow's `from:` / `bind:` — is a new route each, decided with the
user (`TASKS.md`). Pins: `tests/test_wires.py`, `tests/test_app.py::TestWires`, `frontend/src/__tests__/wires.test.ts`,
`tests/test_canvas.py::TestWiringTheSourceGraph`.

**Update, 2026-10-05 — the moved-out card is named after its CLASS (`Document._moved_key`).** Named after the slot,
every source moved out of a Stream was `source`, `source_2` …: the key said where the card had sat, not what it is,
and the user's saved graph read `source: !ref:source`. A moved-out card now takes its class name in snake case
(`document._snake`, the rule generated step names already used): `HuggingFaceSource` → `hugging_face_source`,
`FilesSource` → `files_source`, `HTTPSource` → `http_source`, then `_2`, `_3` … while it is taken. TAKEN now counts
the parameters of EVERY card, the slot's owner included — it was exempt, but a key named like the owner's slot flows
back into that slot through confluid once the person removes the wire (measured 2026-10-05: a top-level `source:`
card was handed to a second, source-less Stream — `second (no source written) got: FilesSource ['a.png']`). The
user's saved graph now reads `hugging_face_source: !class:…HuggingFaceSource {path: ylecun/mnist}`, `source:
!ref:hugging_face_source`, `classes: !ref:hugging_face_source`. Pins:
`tests/test_wires.py::TestTheMovedOutCardIsNamedAfterItsClass`, `::TestTheUsersSourceGraph`.

```python
document.add_wire("stream.source", "class_names", "contract", "classes")   # → "hugging_face_source"
# a class named like a parameter of any card — Source, in a Stream's `source` slot — takes a suffix:
document.add_wire("stream.source", "stream", "holder", "thing")             # → "source_2"
```

## The catalogue is built by a helper process, and a class's settings are asked when it is selected (`streamstudio.catalogue`, `GET api/palette/<class>`, 2026-10-05)

**Context.** The catalogue — every class the page can list — was built inside the editor's process on the first
`GET api/palette`: import every `confluid.configurables` entry point, then describe every class with its settings.
Measured 2026-10-05 with every backend installed: 21 s at load 23 — 12 s of imports (`raidar.ultralytics` 5.7 s, which
is torch and torchmetrics, the latter importing `transformers` on its own; `deltaid.tensorflow_segmenter` 2.2 s;
`deltaid.unet.flax_unet` 1.1 s), 6 s building each class's settings (`to_pydantic` and pydantic's JSON schema), the
rest describing kinds and sockets — and an 11.6 MB answer, 94 % of it settings. After it the editor held 1.17 GB.
Importing those backends lazily was measured and rejected: ultralytics itself imports in 0.07 s — the cost is torch
and torchmetrics, which every torch package pays — and the TensorFlow and Flax classes inherit from their framework's
own base classes, so they cannot be declared without it; at most 3 s for a refactor across two projects. Building the
catalogue earlier, in a thread of the editor, was rejected on 2026-10-02 (the record above): it contends with the
page's own requests for the import locks.

**Decision.** Two halves.

1. **A helper process builds it** (`python -P -m streamstudio.catalogue`). It imports every entry point and answers
   twice on its stdout, one JSON line each: first the LIST — every class without its settings (`palette.listed`,
   `describe(settings=False)`) — then every class's full spec. It points its file descriptor 1 at stderr before
   importing anything, so what an imported package prints reaches the editor's log, never an answer; `-P` keeps the
   working directory from shadowing an installed package. Serving the editor's page (`page_blueprint`'s `/`) starts
   it; a catalogue request starts it if nothing has. ONE helper per process (`catalogue.CATALOGUE`), the one piece of
   module-level state the editor keeps: the catalogue describes the installed packages, the same for every app the
   process mounts (the palette's own cache was always per process). `?refresh=1` runs a new helper, which — a fresh
   interpreter — also sees packages edited since. A helper that stops before answering is a `503` naming its exit
   code, and the next request runs a new one. The editor adds the classes only ITS registry holds (a host's or a
   test's, no entry point naming them), described in its process.
2. **A class's settings are asked when it is selected** — picked in Set source…, its card shown in the Nodes sidebar
   (`GET api/palette/<class>`, the page's `api.settingsOf`, once per class for the page's life). The answer comes from
   the helper's second line, or at once from the editor's own process when the class's module is already imported
   there (no import, no wait). The list and the full spec must agree on a class's kind, sockets and problem, but the
   list does not build settings — and building them is where a class whose constructor annotations do not resolve
   fails (torch's `DataLoader`). So the list makes confluid's own cheap check (`inspect.signature`,
   `get_type_hints`) and, only when it fails, calls `to_pydantic` for confluid's own words (`palette._check_settings`):
   1.8 s for 825 classes against 6.5 s for building every model (load ~70).

Rejected: settings described in the editor on selection, whatever is imported (measured on a first pick in a fresh
editor: `YOLO26Model` 10.8 s, `TensorFlowSegmenter` 9.3 s at load 112 — each first pick of a torch or TensorFlow
class would load that framework into the editor); the full catalogue persisted on disk between runs (instant when
nothing changed, 21 s again after any edit to a package that declares classes, and a second copy to keep true); one
helper per editor app (a test session making many apps would run one each).

**Consequences.** Measured 2026-10-05, one fresh editor process per line, over the six shipped pane graphs:

```
                                               before (in the editor)     after (helper process)
editor opened, Set source… at once — the list  55.1 s, 11.7 MB  (load 128)  31.0 s, 0.60 MB  (load 120)
a picked class's settings                      in the list                FilesSource 31.0 s (with the list),
                                                                          YOLO26Model 44.2 s (the second answer)
the list asked once the helper answered        —                          3 ms
the editor's memory afterwards                 1.17 GB                    0.13 GB
the first graph list (load ~104, two runs)     4.3 s, 4.4 s               4.1 s, 4.1 s with the helper running
```

Every editor that serves its page runs the helper once — about 20 s of one core at load 23 — even when nobody opens
the catalogue. A short class name two packages share (`FourierOp`, the record above) is no longer refused by name once
the catalogue has been opened: the editor never imports every package now, so a card draws the first the lookup
imports, as before the catalogue, while a run still refuses the name.

**Example.**

```
GET /studio/                                   -> the page; the helper starts
GET /studio/api/palette                        -> [{"class_path": "recordstream.sources.files.FilesSource",
                                                    "kind": "source", "inputs": [...], "outputs": [...], ...}, ...]
                                                  (no "params_schema", no "params_defs")
GET /studio/api/palette/recordstream.sources.files.FilesSource
                                               -> {"class_path": ..., "params_schema": {"files": {...}, "root": {...},
                                                    "pattern": {...}, ...}, "params_defs": {}, ...}
```

```python
from streamstudio.catalogue import Catalogue

catalogue = Catalogue()          # stores only; the helper runs on the first start() or entries()
catalogue.start()                # what serving the page does
entries = catalogue.entries()    # waits for the helper's first answer
spec = catalogue.spec("recordstream.ops.numpy.Threshold")   # at once: recordstream is imported here
catalogue.stop()
```

**What you may change.** When the helper starts (another trigger than the page — a host that opens the editor with
the catalogue in view, say), how the answers travel (two lines on stdout today), and which surfaces count as
selecting a class. What must hold: the editor's process never imports every entry point for the catalogue; the list
carries no settings; a list entry and its full spec agree on kind, sockets and problem; a helper never outlives its
editor; one helper at a time per process. Pins: `tests/test_catalogue.py`,
`tests/test_app.py::TestPaletteAndForm::test_the_palette_lists_at_least_recordstreams_own_classes`,
`frontend/src/__tests__/catalogue.test.ts`.

## A graph's settings live in a file of their own, joined to the graph by confluid's `include:` rule (`streamstudio.settings`, `EditorHost.shipped`, 2026-10-05)

**Context.** A host copies every graph it offers into its own folder before the editor opens it — measured on a signal
workspace (2026-10-05): opening the type copied 19 graphs into its `templates/`, the first draw of one pane copied
that pane's graph, and changing one threshold in a pane's settings tab saved the whole 457-line graph. From then on
the workspace runs its frozen copy, and a fix shipped later in the package never reaches it. Most of those changes are
VALUES the graph's `app:` block already names as its settings; very few change the graph itself. The user's design:
a graph and its settings are two files, a graph is copied only when its structure is edited, and the settings a
person made stay in force on the edited copy.

**Decision.**

- **The settings file** sits beside the graph (`view_bte.yaml` → `view_bte.settings.yaml`) and holds one line per
  setting. The editor writes there every value of a field the `app:` block names (`Document.set_param` →
  `settings.set_setting`), and nothing else; every other edit goes to the graph as before.
- **One document, by the rule confluid already has.** The two are loaded as the HELD document
  `include: [<graph>, <settings>]` (`settings.composed`, passed with `texts=`, never written): a key below an include
  wins over it, so a setting wins over what the graph spells, and every location names the real file. Rejected: a
  `configure()` call on the built objects (it cannot address a flow step by name), a settings block inside the
  workspace file (it travels with no graph), and any new confluid spelling or precedence (root ruling "A configuration
  utility, not a compiler").
- **A setting names its node by NAME** — the user's choice — `flow.<step>.op.<param>` for a flow step,
  `<key>.<param>` for a top-level object, so two nodes of one class are set apart; a node with no name (an `ops:` list
  member) falls back to `<Class>.<param>` only while the class is that node's alone. Measured: a nested
  `flow: {first: {op: …}}` block is merged by confluid by MOVING the step to the end of the flow
  (`from: 'first' does not name an EARLIER step`), and a dotted index into a list is refused
  (`a dotted write cannot index a list`) — so neither is a spelling. A parameter ONE node of the drawing alone has
  may be written bare (`floor_db: -60.0`, 2026-10-14): that is confluid's own broadcast spelling, and measured on two
  review graphs it built the one step with the value, whether the graph owns its flow or includes it — so the check
  lets it through where it is exact (`Declared.bare`) and refuses it where two nodes share the parameter, naming the
  full keys (`Declared.ambiguous`). Nothing is added to confluid.
- **A line that is none of the graph's settings is refused where it is written.** Confluid by itself warns and ignores
  an unknown key: measured, a misspelt `line_dbb` loaded with a warning and the pane ran the graph's own value. The
  editor's check and `settings.load_graph` (what a host runs) refuse it at its `file:line:col`.
- **The model applies the settings itself.** A class line reaches its node only at confluid's `settled` stage, after
  the `document` stage the model is derived at — measured, the canvas showed `-90.0` while a run used `-60.0` — so the
  derivation sets each resolved setting on its node (`document._apply_settings`).
- **A host may SHIP a graph** (`EditorHost.shipped()`): listed and opened from the shipped file while the folder holds
  none (`Document.open_shipped` holds the shipped text as unchanged), so a settings change writes only the settings
  file and the first Save of a change to the graph writes the folder's copy.
- **A graph may offer its settings only** (`app: {graph: settings-only}`): every other change is refused first
  (`@_structural`), so a host can show a graph's settings without offering its canvas.

**Consequences.** A host that ships its graphs keeps only what a person changed: settings files, and the graphs whose
structure was edited; a package update reaches every graph not copied, the settings staying on top. A setting naming a
parameter the package later renamed is a located refusal, never a silent fallback. The settings file is a second
text of every document (undo, the recovery file, a folder move and Save As carry it). Removing or renaming a node a
setting names is refused while the setting is there — the line would name nothing.

**Example.**

```python
from streamstudio.settings import load_graph

# view_pane.settings.yaml (in the host's folder):   flow.second.op.level: 0.9
built = load_graph(package / "pane_view.yaml", folder / "view_pane.settings.yaml")
built["flow"]["second"]["op"].level        # 0.9 — the graph spells nothing, the class default is 0.5
```

**What you may change.** How a value is spelled on its line, which edits re-key a setting instead of refusing (a
rename could follow its node), and a gesture that drops a setting back to the graph's value. What must hold: a setting
never goes into the graph; the composition stays confluid's `include:` rule; a key that is none of the graph's
settings is refused, never ignored; a shipped file is never written. Pins: `tests/test_graph_settings.py`,
`tests/test_canvas.py::TestTheSettingsOnlyPage`.

## What a host hands a rooted graph is one HOST card, and where the host puts it is a derived dashed wire (`_Builder.root_host`, `_Builder.host_wires`, 2026-10-05)

**Context.** A host workspace has two graphs that deliver records: a SOURCE graph and a FILES graph — the files a
person drops. The user asked for the files graph to work like the source graph (approved from a picture, 2026-10-05):
a `FilesSource` card the host fills, a Stream reading each file as an image, a root that delivers the stream, reads
`input` from `image` and MAY deliver a class list (recordstream's `GraphContract.optional`). Until then the root drew
the contract's `inputs` as its OWN outputs: the files graph's root showed a `files · List[str]` output that nothing in
the document wires and nothing could be wired from, while the host's real rule — it fills a list-valued `files` on an
object in the delivered stream's source chain, BY SHAPE — showed nowhere. A pane's root carried `input · Signal` the
same way, and the wires its steps read left the root, a card that is otherwise only wired INTO.

**Decision.** The host's side of a rooted graph is ONE card, the synthetic `<host>` a graph without a root already had
(kind `host`): one output per slot the contract's `inputs` declare — an item-typed slot (`input · Signal`) is an ENTRY
of the record the host hands in, which a step reads by name; any other (`files · List[str]`) is an object — and the root
has no outputs. Where the host PUTS an input is drawn as a wire of its own kind, `host`, mirroring the host's rule: for
each host output naming a constructor PARAMETER of exactly one card of the delivered stream's source chain (the stream
the root delivers, the card in its `source` slot, that card's own `source`, …), a wire from the host card into that
parameter; none or several such cards, no wire. The wire is DERIVED from the document's shape and spelled by no text,
so it is never saved and its removal is refused in one line (`document.WIRE_FROM_THE_HOST`). The card is never a node
of the document: no parameters, Rename and Delete refused (`document.HOST_NOT_A_NODE`), never written. The page titles
it after the host it is mounted in (`from <host>`, `from the host` standalone; `host.hostCardTitle`) and draws the
`host` wire dashed (`.wire-host`); a Delete on it is sent to the server like any wire, whose refusal is the toast — the
page decides nothing.

**Consequences.** Every rooted graph that declares `inputs` gains the card — the signal domain's panes too: the
Bluetooth pane is 31 nodes, its 59 wires unchanged (no step reads `input` or `regions` by name). The chain check still
starts from the declared inputs (`_Derivation.provided`), and `_contract_problems` reads the declaration from the root's
parameters, not from the root's sockets. A root with no `inputs` draws no card, so an entry nothing produces stays the
chain check's refusal, never a host that supplies anything. An `optional` slot is an input without the required mark
after the record entries, wired and removed like an output, never a problem while unwired.

**Example.**

```yaml
file_source: !class:recordstream.sources.files.FilesSource {}
stream: !class:recordstream.core.stream.Stream
  source: !ref:file_source
  ops:
    - !class:recordstream.ops.image.ReadImage {field: file, output: image}
contract: !class:recordstream.ops.contract.GraphContract
  name: classification files
  inputs: {files: "List[str]"}
  outputs: {stream: Stream}
  optional: {classes: "List[str]"}
  records: {input: Image}
  delivered:
    stream: !ref:stream
    input: image
```

```python
model = Document.open(path).model
[(w.kind, w.source, w.source_socket, w.target, w.target_socket) for w in model.wires]
# [('list', 'stream.ops[0]', 'op', 'stream', 'ops[0]'), ('ref', 'file_source', 'stream', 'stream', 'source'),
#  ('delivered', 'stream', 'stream', 'contract', 'stream'), ('delivered', 'stream.ops[0]', 'image', 'contract', 'input'),
#  ('host', '<host>', 'files', 'file_source', 'files')]
document.remove_wires([("<host>", "files", "file_source", "files")])
# ValueError: The host fills this when it runs the graph — it isn't a wire you can remove.
```

**What you may change.** The card's title and note, the dash, the refusal wording, and the chain the host wire follows
— together with the host's own rule, which it mirrors. What must hold: the host card is drawn, never written; a `host`
wire is never saved and its removal is refused; one card takes an input or none is drawn; the root has no outputs.
Pins: `tests/test_wires.py::TestTheFilesGraph`, `::TestTheHostWire`, `::TestAnOptionalSlot`,
`tests/test_document.py::TestDerive::test_a_rooted_flow_reads_an_item_typed_input_from_the_host_card`,
`frontend/src/__tests__/{host,model,wires}.test.ts`, `tests/test_canvas.py::TestTheFilesGraphsHostCard`.

## A class that is not an op is a card of its own, named like a card moved out of a slot (`Document.add_node`, `POST /api/graphs/<name>/nodes`, 2026-10-05)

**Context.** Adding a class knew two places: a member of an op list (`Document.add_step` → `_add_member`) or a flow
step. A class that is neither — a value like `ClassNamesScan`, a source, an engine, a sink — had no place in a Stream
document, and the page took the host card (and the root) as the node to add after. Measured on the files graph a host
workspace seeds, right-click ▸ Add Node ▸ ClassNamesScan: with nothing selected `add
recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at …/files.yaml:12:7: cannot wire
stream.ops[1].object (object) into stream.ops[1] (Op) …`; with the host card selected `a new flow step needs a name —
after '<host>', it has none`. The same path broke the host's own instruction: drag from the root's `classes` socket
onto empty canvas and pick the class-names card.

**Decision.** A class whose node kind is not `op`, `algorithm` or `subgraph` is a CARD of its own (the user's design,
2026-10-05): a NEW top-level key `<key>: !class:<path> {<params>}`, ONE edit. The key follows the move-out rule — the
class name in snake case, `_2` … when it is a top-level key, a card, or a constructor parameter of any card — because a
card added on its own and a card moved out of a slot are the same thing in the text, and both meet confluid's broadcast:
a top-level key is handed to every same-named parameter (`_free_key`, shared with `_moved_key`). The line goes directly
above the root and the comment lines written over it (`YamlText.top_key_line`), so the root stays last, else at the
end. A wire dropped on empty canvas is saved to the new card in the SAME edit (`add_wire`, inside `_one_edit`), or
nothing is added. Which classes are ops is the DOCUMENT's word (`_kind_of`): the catalogue lists 35 classes as values
that the document derives as ops (`recordstream.core.wrappers.FilterOp`, `WrappedOp`, Keras and fastai losses —
measured 2026-10-05 over 829 classes), so `add_step` hands a non-op to `add_node` and the page sends every add through
`POST …/steps`, deciding only how to PLACE the answer (`cards.addPlan`). The host card and the root are never an
anchor. A flow graph and a subgraph's inside refuse a card in the user's line — their top level is steps, and a card
there would be drawn on no level the person is looking at.

**Consequences.** The user's gesture writes `class_names_scan: !class:…ClassNamesScan {}` above the root's comment and
Check graph finds nothing. A rooted graph keeps its root last whatever is added. A non-op class picked with an op's
place (`after: stream.ops[0]`) is a card, not a refusal; an op whose catalogue entry says value still goes into the
list. One known limit, unchanged by this: confluid also hands an EXISTING top-level key to a new card's same-named
parameter (a top-level `source:` reaches a new `ClassNamesScan`'s `source` at load, drawn as no wire — `TASKS.md`).

**Example.**

```python
document = Document.open("files.yaml")                       # the files graph, its root under a comment line
document.add_node("recordstream.ops.contract.ClassNamesScan", {})   # → "class_names_scan", above the root's comment
document.add_step("", "<host>", "recordstream.ops.contract.ClassNamesScan", {})   # the same: no anchor, no op
document.add_node("recordstream.ops.contract.ClassNamesOutput", {}, wire=("", "", "contract", "classes"))
# → "class_names_output"; delivered gains `classes: !ref:class_names_output` — one undo takes both
Document.open("flow.yaml").add_node("recordstream.ops.contract.ClassNamesScan", {})
# ValueError: The editor can't add a value card to a flow graph yet — pick an op.
```

**What you may change.** Where the line goes when there is no root, the refusal wording (the flow line is the user's),
which kinds count as ops — together with `_STEP_KINDS` and `cards.STEP_KINDS`. What must hold: one edit and one undo,
card and wire together or nothing; the move-out naming rule; the root stays last; the document's kind decides, never
the catalogue's; the host card and the root are never an anchor. Pins: `tests/test_add_node.py`,
`tests/test_app.py::TestAddingACard`, `frontend/src/__tests__/{cards,steps}.test.ts`,
`tests/test_canvas.py::TestACardOfItsOwn`.

## A plugin supplies the list beside a field, and a host asks the page to commit before it reads (`streamstudio.lookups`, the widget-kind stamp, `GET /api/lookups/<tag>`, the `commit` command, 2026-10-08)

**Context.** Some producers integrate with a vendor-specific platform — a dataset picker backed by a credentialed
platform API, for example. The editor is a generic package and carries none of that knowledge, yet the integration
needs two things of it: a per-parameter tag saying which field to decorate, and a place for the integration's code to
be found. With nothing behind the tag, a field whose valid values live in a remote service (a dataset on a platform,
the versions of that dataset) could only be typed: a tag the editor did not know was drawn as a plain text field.
Separately, a host that runs something with a graph's settings (a run button beside its settings panel) read the
settings file while the field the person had just typed in was still being written: a field writes when it is LEFT, and
pressing the host's button leaves it — measured, the run read the old value.

**Decision.** (1) *The seam.* A producer stamps a class with `__taidal_widget_params__: Mapping[str, str]` (parameter
name → opaque tag). `palette.picker_of` reads it FIRST — before the HuggingFace conventions and the file/folder name
rules — and the form schema carries the tag verbatim as `picker`: there is no registry of tags, and the page draws the
pickers it knows and a plain field for any other. A plugin is a module an installed package names in the
`streamstudio.plugins` entry-point group; one that fails to import is skipped with a `warning` — louder than the
debug-level scan of `confluid.configurables`, because a declared plugin is an explicit opt-in and its failure is
actionable. (2) *The list.* A plugin module may declare `LOOKUPS = {tag: fn}`, `fn(values) -> (names, reason)`, read
once per process. The palette marks a stamped field whose tag is listed `lookup: true` (the tag stays in `picker`), the
page draws the same `LookupPicker` the HuggingFace lists use, and asks `GET /api/lookups/<tag>` with the node's current
scalar values — the one input a dependent list needs (a version list needs the dataset named beside it). Python, not
JS, because the editor serves no plugin code; a list function is the whole of what a plugin needs to say. `answer`
never raises and bounds a list at `TIMEOUT_S` on a daemon thread: a remote service may hang on a login. (3) *Commit.* A
host's `commit` command: the page blurs the active field, waits for every parameter write on its way (`commitParam`
registers each in `writes`), and posts `committed`.

Rejected: a registry of tag kinds (nothing consumes kinds centrally — it would be bookkeeping with no reader); a
class-NAME gate for a vendor's picker (the editor would carry the vendor's name, and a class whose name merely
resembles a convention would get a picker it never asked for — only a stamp triggers one); a delay in the host (a guess
at the round trip — the test that measured the race fails with any fixed wait under load); reading the page's
`state.dirty` (the dirty state arrives only after the write's request returns, so the host saw "clean" first).

**Consequences.** This package carries no vendor knowledge, and every platform integration is a plugin, not a special
case. A dependent list re-asks when any field of its node changes (the cache key holds every value) — a list cheap to
ask is assumed. The `committed` message joins the host contract (`state`, `saved`, `unavailable`); a host that never
sends `commit` is unaffected. The lists are process-level state like the catalogue.

**Example.**

```python
# the producer stamps the parameter with a tag of its own
@configurable(category="value")
class ShopDataset:
    __taidal_widget_params__ = {"name": "SHOP_DATASET"}
    def __init__(self, name: str = "", project: str = ""): ...
# palette.picker_of(ShopDataset, "name", str)  ->  "SHOP_DATASET"
```

```toml
# the plugin package's pyproject.toml
[project.entry-points."streamstudio.plugins"]
my-plugin = "my_plugin.studio"          # a module with LOOKUPS
```

```python
# my_plugin/studio.py — the form marks `name` lookup: true because this table lists its tag
LOOKUPS = {"SHOP_DATASET": lambda values: (shop.datasets(values.get("project", "")), "")}
```

```js
// a host about to run what the settings panel wrote
frame.contentWindow.postMessage({target: "streamstudio", type: "commit"}, location.origin);
// … then, on {source: "streamstudio", type: "committed"}: read the settings file, run.
```

**What you may change.** The label, the timeout, which values are sent (keep every one the list may depend on in the
cache key); further optional, `getattr`-read attributes of the plugin contract; the stamp's value type if a field ever
needs more than a tag (on the stamp, never with a name rule). What must hold: a list never fails a request; a tag no
plugin lists stays a plain field; no vendor name, class-name gate or second discovery mechanism beside the entry-point
group; `committed` is never posted before the writes land. Pins: `tests/test_plugin_lookups.py`,
`tests/test_pickers.py::TestWhichParameterGetsWhichPicker::test_a_producers_stamp_wins`,
`frontend/src/__tests__/{pickers,api,host}.test.ts`,
`tests/test_canvas.py::TestTheHostHearsAndFollows::test_commit_writes_the_field_being_typed_in_and_answers_committed`.

**Update, 2026-10-14.** The stamp and the entry-point group, first decided on 2026-08-25, are decided here: a plugin
supplies lists, and nothing else is read from its module.

## A field another parameter does not ask for is grayed, by the value the HOST runs (`settings.offReason`, the `values` command, 2026-10-08)

**Context.** A class whose parameters belong to one choice among several (a detector's rule: a margin for most rules,
an absolute level for one, CFAR cells for another) showed every field live in the settings form whatever the choice,
so a person tuned a value the run never read (user request 2026-10-08: "setting the method must gray out the settings
… if not applicable"). And a host may RUN a graph with values the graph does not spell — knobs on its own pane,
applied by name and never written — so the graph's own value of the choice is not always the one that runs.

**Decision.** (1) The dependency is DECLARED on the parameter, in its schema: `applies_when: {<parameter>: [values]}`,
an `Annotated` pydantic `Field(json_schema_extra=…)` that `confluid.to_pydantic` already keeps — nothing new in the
server, nothing new in confluid. The form grays the field (a `fieldset disabled`, so every input a widget draws goes
with it) while the named parameter holds none of the values; its title says which value it waits for. (2) A host's
third command, `values` (`{graph, values}`), says what it runs the graph with; the form compares the host's value
first, the node's otherwise. Rejected: HIDING the field (switching back to the choice that reads it, the person no
longer sees the value that returns); refusing a value set on a grayed field (it is a setting the next choice reads);
a list of dependencies kept by the form or the host (it drifts from the signature, and every other form — an MCP
schema — would need its own); an expression language in the declaration (a configuration utility, not a compiler —
a list of values per parameter, all of them holding, is the whole grammar).

**Consequences.** Any form that reads the schema can gray the same way; one that does not (an older client) shows the
field live, which is what it did before. A malformed declaration grays nothing rather than taking a field away. The
`values` message joins the host contract (`open`, `commit`); a host that never sends it gets the node's values. Values
are compared as text, because a host sends what its own field holds.

**Example.**

```python
class Detector:
    def __init__(
        self,
        rule: Literal["fixed", "median"] = "median",
        level: Annotated[float, Field(json_schema_extra={"applies_when": {"rule": ["fixed"]}})] = -150.0,
    ) -> None: ...
```

```js
// the host's pane runs rule "fixed" although the graph says "median": the form follows the pane
frame.contentWindow.postMessage({target: "streamstudio", type: "values", graph: "view_scan",
                                 values: {rule: "fixed"}}, location.origin);
// offReason(levelSchema, {rule: "median"}, {rule: "fixed"}) === ""            — live
// offReason(levelSchema, {rule: "median"})  === "rule is median — read only while rule is fixed"
```

**What you may change.** The wording of the reason, how a grayed field looks. What must hold: grayed is never hidden
and never refused; the host's value wins over the node's; a declaration the form cannot read grays nothing; `values`
writes nothing. Pins: `frontend/src/__tests__/settings.test.ts` (`offReason`), `frontend/src/__tests__/host.test.ts`
(`values`), `tests/test_document.py::TestDerive::test_a_params_schema_keeps_which_values_of_another_param_read_it`.

## A value node that IS a number offers a number (`palette._built_socket`, `document._socket_of_built`, 2026-10-09)

**Context.** A setting a person types with its unit — a rate, `2.5 MSa/s` — is read by a node of its own (recordstream's
`Quantity`, a `float` subclass) wired into the number setting, so a settings form shows the node's field where the
setting was (user request 2026-10-09: "only the unit node exposes its settings, make this node generic"). Every
parameter row already offered a handle a wire lands on, and `add_wire` already saved `<param>: !ref:<P>` into a
parameter whose schema type the output meets. But a value node offered its instance as `object`, which meets no
`number`: measured, `up_rate.object (object) cannot be wired into upsample_rate.samplerate (number): the types do not
meet`.

**Decision.** A value node's `object` output carries the scalar its class IS — `bool`, `int`, `float`, `str`, tried in
that order (a bool is an int) — else `object`. Any node whose result is a number meets a number setting (user pick
2026-10-09: any node, not the one unit node). The output keeps its name, so saved wires and sidecars are untouched;
the meet rule is unchanged (an integer still meets a number, a number never an integer). The same rule is written in
the palette (`_built_socket`) and the document model (`_socket_of_built`), which keep their own scalar tables.
Rejected: a builder FUNCTION returning the number — `_resolve` answers classes only, so the model could neither draw
nor set it (`up_rate: cannot resolve the class 'qty_probe.quantity_probe'`); a socket per number setting on every
card — the parameter rows already take a wire.

**Consequences.** A graph wires a written unit into any number setting with no change to the class it feeds. A value
node whose class is no scalar offers an `object` as before.

**Example.**

```yaml
rate: !class:recordstream.quantity.Quantity {value: 2.5 MSa/s, kind: rate}   # its output: object (number)
taker: !class:my_pkg.Taker {rate: !ref:rate}                                   # a wire drawn on `rate` saves this
```

**What you may change.** Which scalars count, the output's doc. What must hold: the type follows what the instance IS,
never a name or a category; the output keeps the name `object`. Pins: `tests/test_number_wires.py`.


## One editor request at a time (`Studio.lock`, `_take_turn`/`_end_turn`, 2026-10-13)

**Context.** A host serves the editor's panel and its own routes from ONE threaded server, and both reach the one
`Studio`: its graph list (`_paths`, `_shipped`) and its open documents (`_documents`) are plain dicts every request
reads and rebuilds. Nothing made two requests take turns. Measured on a host's page test (a remembered tool setting
picked while the settings panel was still loading): about one run in fifteen died in the editor's own scan,
`RuntimeError: dictionary changed size during iteration` at `Studio._scan`, the panel's `GET /api/graphs` rebuilding the
list on another thread. A host's read-change-save (`document()`, `set_param`, `save(only="settings")`) can also be cut
in two by the panel's own save.

**Decision.** One reentrant lock per editor, `Studio.lock`. Every editor request holds it from its first hook to its
teardown: `_take_turn` is the blueprint's FIRST `before_request`, so no other hook reads the editor before it, and
`_end_turn` lets go in `teardown_request` — which runs for a refusal (another session's `409`) and a raise alike, and
only for a request whose turn was taken (`g.streamstudio_turn`). `stash()` takes it, so a stop keeps what a request in
flight leaves. A host holds the same lock across a read-change-save of its own. Reentrant because a request's save
tells the host (`saved`), which may ask the editor again on the same thread. Rejected: a lock inside `_scan` and
`document()` only — the crash goes, but a host's read-change-save and the panel's save still interleave; a lock per
document — the graph list is shared by all of them, and the ordering between two locks is one more thing to get wrong.

**Update 2026-10-13 (same day) — a request that waits outside the editor takes no turn.** The catalogue's helper
process imports every installed package; one of them waited forever on a stale torch extension lock, and
`GET /api/palette` waited for the helper HOLDING the lock — every other editor request (the settings panel, a host's
knob save, a tool run, a history pick) hung behind it, where before the lock they had kept working. So the routes that
wait on something outside the editor and read none of its graphs — the catalogue, a class's settings, `types`, plugin
lists, the HuggingFace routes — are `TURN_FREE` and take no turn; a Run (`trace`, `rerun`) is too, and `Studio.trace`
takes the lock itself only to read and build the document and to keep the run, never across the host's `record()` or
the run itself.

**Consequences.** Editor requests that read the graph list or a document are served one after another; a Run's long
part, the catalogue and the lookups run beside them. The lock order is always the editor's first, then whatever the host takes inside `saved` or
`folder` — a host never calls into the editor while holding a lock of its own that those callbacks take. The page
blueprint (static files) takes no lock.

**Example.**

```python
from streamstudio.app import studio_of

studio = studio_of(app)
with studio.lock:                       # the panel's request, if one is being served, finishes first
    document = studio.document("tool_move")
    document.set_param("mover", "folder", "/data/tier2")
    studio.save("tool_move", only="settings")
```

**What you may change.** Where the turn is taken (a decorator per route instead of the hooks), the lock's kind if a
reentrant call goes away, which waits are `TURN_FREE`. What must hold: no editor request reads the graph list or a
document without the lock; nothing waits on a process, a plugin or the network while holding it; a refusal and a raise
let go of it; a host's read-change-save is one turn. Pins: `tests/test_studio_threads.py`.

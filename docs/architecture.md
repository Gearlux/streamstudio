# Architecture decisions

Design rationale for mechanisms whose existence or shape is not obvious from their name or the
user docs. Each record: Context → Decision → Consequences → Example → What you may change.
User-facing usage lives in the README; agent-facing rules in `AGENTS.md` — this file answers
"why does this mechanism exist?".

## The plugin seam: `streamstudio.plugins` + the widget-kind stamp (2026-08-25)

**Context.** StreamStudio is published, but some node producers integrate with private,
vendor-specific platforms — a dataset picker backed by a credentialed platform API, for example.
Such an integration needs three things a plain node cannot carry: a backend route on the editor's
server, a frontend JS extension, and a per-param widget tag telling that JS which field to
decorate. All three used to be hard-coded here (a guarded vendor import, an inline route, a
shipped JS file, and a class-NAME gate in `_widget_for`) — publishable code carrying vendor
knowledge, and a bridge that grew a special case per integration.

**Decision.** Three seams, none naming any consumer:

1. **Widget kinds are a class stamp, not a bridge rule.** `__taidal_widget_params__: Mapping[str,
   str]` (param name → opaque tag) joins the existing cross-package stamp protocol
   (`__taidal_display_name__`, `__taidal_op_params__`, `__taidal_op_list_params__`,
   `__taidal_model_params__`). `_widget_for` reads it via `getattr` FIRST — before the
   HuggingFace name conventions and before `_map_type`'s path-name rule — and emits a free-text
   STRING carrying `taidal_widget=<tag>`. No registry: the tag flows verbatim to the frontend,
   and only the producer's own JS attaches meaning to it.
2. **Routes and web assets ride a `streamstudio.plugins` entry point** whose value is a module.
   `ROUTES` (an aiohttp `RouteTableDef`) is merged onto `server.PromptServer.instance.routes` by
   re-registering each RouteDef with first-wins per-(method, path) dedup — aiohttp has no public
   table append. `WEB_DIRECTORY` is inserted into ComfyUI's `nodes.EXTENSION_WEB_DIRS` under the
   plugin's *distribution* name; ComfyUI's `/extensions` listing globs that dict per request and
   its static routes are added at server start-up, *after* custom nodes (and therefore this
   package, and therefore plugin discovery) have loaded — which is what makes import-time
   registration timing-safe, and what serves plugin JS at the same URL depth
   (`/extensions/<dist>/…`) as any extension, keeping relative `../../scripts/*.js` imports valid.
3. **Failure isolation is per entry, at warning level** — deliberately louder than the
   debug-level `confluid.configurables` scan, because a declared plugin is an explicit opt-in and
   its failure is actionable, while most configurable modules legitimately aren't node modules.

Rejected alternatives: a `taidal_widget` kind *registry* (nothing consumes kinds centrally — a
registry would be bookkeeping with no reader); making each plugin its own ComfyUI custom-node
package (requires launcher symlinking and a second discovery mechanism when entry points already
exist); serving plugin JS from a streamstudio-owned route (breaks the relative-import depth that
`EXTENSION_WEB_DIRS` preserves for free).

**Consequences.** This package carries zero vendor knowledge and every future platform
integration is a plugin, not a bridge special-case. The behavior change is deliberate: a class
whose *name* merely resembles a convention gets no picker any more — only a stamp triggers one.
Plugins depend on two ComfyUI internals (`PromptServer.instance.routes` before `add_routes()`,
and the `EXTENSION_WEB_DIRS` dict) that the existing route registration already relied on; if
ComfyUI moves them, the registrars in `streamstudio/__init__.py` are the single place to adapt.

**Example.** A producer stamps a param and ships the picker beside it:

```python
@configurable(category="value")
class PlatformDataset:
    __taidal_widget_params__ = {"name": "PLATFORM_DATASET_ID"}
    def __init__(self, name: str = ""): ...
# _widget_for(name) → ("STRING", {"default": "", "taidal_widget": "PLATFORM_DATASET_ID"})
```

```toml
[project.entry-points."streamstudio.plugins"]
my-plugin = "my_plugin.studio"        # module with ROUTES + WEB_DIRECTORY (see README → Plugins)
```

**What you may change.** Add module attributes to the plugin contract (keep them optional and
getattr-read); tighten dedup logging; extend the stamp's value type if a widget ever needs more
than a tag (do it on the stamp, not with a name rule). Do not reintroduce name-based widget
gates, per-vendor branches in `_widget_for`, or a second discovery mechanism beside the
entry-point group.

## Node-shape classification: the category tag outranks the structural probe (2026-08-26)

**Context.** `create_comfy_node` must decide which of the seven node shapes a discovered callable
becomes. Most of that decision is structural — does the class implement a no-arg `run()`, does its
`__call__` take a record, does its iteration yield records — which is deliberate: a producer should
not have to learn a bridge vocabulary to get a sensible node. But structural probes read
annotations, and an annotation is a promise about a *value*, not a statement about a *role*.

The two collide at the stream engines. A leaf source materialises its own data, so it can honestly
annotate `__iter__ -> Iterator[Record]`. An engine wraps a source that is duck-typed on purpose —
"any iterable" — so the only honest annotation it can carry is `Iterator[Any]`. The source probe
therefore split three siblings that are the same kind of thing: the one whose sub-streams are
themselves engines kept the `Record` annotation and built a source node; the two that accept an
arbitrary iterable fell through to the generic builder, where a `source` parameter becomes a text
widget and the node returns a bare `STRING`.

That failure is silent and total at the canvas. ComfyUI connects two sockets only when their type
strings are EQUAL, so a source's `RECORDSTREAM_SOURCE` output had nothing to dock into on the engine
node: dragging the wire simply did nothing, with no error in the log, no failed validation, and no
diagnostic to grep for. Nothing downstream could detect it either — the graph was well-formed, it
just could not be drawn.

**Decision.** Where a class carries a `@configurable` category that names its canvas shape, the
CATEGORY decides and the structural probe is not consulted. `category="model"` already worked this
way (a model's `__call__` takes a record, so the op probe would otherwise claim it); `category="engine"`
now does the same for the source probe, via `_is_engine_class`. Structural probes remain the rule for
UNtagged callables, which is most of the palette.

The producer keeps its honest annotations. The bridge stops inferring a role from a type.

**Consequences.**

- An engine is a source node by definition, not by annotation: its `source` param rides a
  `RECORDSTREAM_SOURCE` socket, an `ops` param seeds the dynamic `op_N` `RECORDSTREAM_OP` family, and
  it returns `RECORDSTREAM_SOURCE` — so engines chain into each other and into runnables.
- A producer cannot lose its node shape by refining a type hint. This is the property the probe
  lacked, and the reason the regression survived a rename, a data-model migration, and a package
  split without anyone noticing.
- The tag is now load-bearing, so an untagged engine-like class is invisible as one. That is the
  intended trade: the registration allowlist is already category-driven, so a class with no category
  is not a palette node at all.
- A structural probe that contradicts a tag is a bug in the probe, not a tie to break at the call
  site. Do not add a "probe wins when it is more specific" rule.

**Example.**

```python
@configurable(category="engine")
class Stream:
    def __init__(self, source: Optional[Iterable[Any]] = None, ops: Optional[List[Any]] = None): ...
    def __iter__(self) -> Iterator[Any]: ...      # honest: the source is duck-typed

# probe:  _is_source(Stream) is False           -> generic node, `source` a STRING widget
# tag:    marks(Stream).category == "engine"    -> source node:
#   optional: {"source": "RECORDSTREAM_SOURCE", "op_0": "RECORDSTREAM_OP"}
#   RETURN_TYPES: ("RECORDSTREAM_SOURCE",)   RETURN_NAMES: ("source",)
```

**What you may change.** Give another category the same treatment when its structural probe cannot
answer honestly — add a `_is_<category>_class` predicate beside the existing ones and place it in the
documented dispatch order. Do not widen the structural probes to cover a tagged class (accepting
`Iterator[Any]` as record-yielding would sweep in every unrelated iterable), and do not change a
producer's annotations to satisfy this bridge — the annotation belongs to the producer's own type
contract, and bending it to move a socket is how the two got coupled in the first place.

## The studio is addressed by its PORT, not by a pid file (2026-08-26)

**Context.** `stop` and `restart` need to find a running studio. The obvious mechanism is a pid file
written at launch and removed at exit, and it is what most single-process servers do.

It does not fit here, for a reason specific to this launcher: the process that holds the port is not
the process the launcher is. `run` blocks in `subprocess.run` while the server is the ComfyUI it
spawned — a grandchild by the time the shell is counted — so a naive pid file records the launcher,
and signalling it does not free the port. Recording the child's pid instead means restructuring the
launch to `Popen` and keeping a file in sync with a process that can be killed, crash, or be
restarted by a third party. Worse, a pid file only ever knows about studios THIS launcher started:
an editor's launch button spawns one detached, and a developer may simply run `python main.py`. Those
are exactly the studios a user reaches for `restart` to deal with.

**Decision.** The port IS the identity. `_studio_process(port)` scans processes for the one listening
on `127.0.0.1:<port>` and that process is the studio, whatever spawned it and whatever its ancestry.
Nothing is written to disk, so there is no state to go stale.

The scan is per-process rather than whole-system because the whole-system call is privileged:
`psutil.net_connections(kind='tcp')` raises `AccessDenied` for an ordinary user on macOS, while
iterating processes and asking each for its own connections does not.

**Consequences.**

- `stop` / `restart` work on any studio on that port — this launcher's, an editor's, a hand-run one.
- No pid file, no lock file, no cleanup path, and no stale-state failure mode to reason about.
- `psutil` becomes a hard dependency. It is already ComfyUI's own, so the practical cost is zero in
  any environment that can run a studio, but the declaration is not optional.
- The scan is O(processes) and touches every process the user owns. That is fine for a command a
  human types; it would not be fine on a hot path, so do not reuse it as one.
- Two studios on two ports are independent, which falls out for free: every command takes `--port`.

**Example.**

```python
proc = _studio_process(8188)          # the LISTENER, not the launcher that spawned it
proc.terminate()                      # port measured free 64 ms later
proc.wait(timeout=grace)              # deterministic; kill() only if it ignores SIGTERM
```

**What you may change.** Widen the match (a host other than loopback, a port range) inside
`_studio_process`; make `grace` configurable if a slow shutdown ever needs it. Do not introduce a pid
file, a lock file, or any launch-time state as a "faster" lookup — it would reintroduce exactly the
staleness and the this-launcher-only blindness this avoids. Do not reach for the whole-system
`net_connections()`; it is privileged, and the failure is a permission error rather than an empty
result.

## A subgraph boundary is DECLARED by the document, not inferred from it (2026-08-26)

**Context.** Packaging an imported document as a reusable subgraph needs two things the flat graph
does not carry: which sockets cross the boundary, and in which direction. The first implementation
inferred both. Inputs came from unresolved `!ref:NAME` markers — a dangling reference reads as "this
is supplied from outside" — and outputs from the pipeline roots, the nodes nothing else consumes.

Inference failed in both directions. The input rule is unreachable: the importer resolves the
document before the builder sees it, and resolution is precisely what rejects an unresolved
reference, so the marker the rule looks for can never survive to be read. The output rule is a guess
that happens to be right for a straight chain and silently wrong for anything else, and it cannot
express an output that is *declared but not yet connected* — the case that matters most, because an
empty boundary socket is the affordance telling a user where to connect.

Meanwhile a `flow:` document states its boundary outright and always has. Its head step has no `from:`,
so nothing feeds that step's record — that is the input. Its `outputs:` key names the step whose
record leaves — that is the output. Nothing needs deriving.

**Decision.** Where the document declares its boundary, read the declaration. The flow path builds
its slots from the head step and `outputs:`; the older inference stays only for whole-config
documents, which declare nothing.

Slots are emitted whether or not anything is wired to them. A boundary that appears only once it is
connected is invisible exactly when a user needs to see it.

The importer additionally places the op terminator on the final step and promotes its output, so the
instance carries an op connector. This is not boundary inference — it is the same "make the
affordance exist" rule: the capability was already there, reachable only by knowing which node to add.

**Consequences.**

- A blueprint's boundary is a property of its source document, so it is stable and reviewable. Two
  imports of the same document produce the same sockets.
- The two wrap modes share one envelope and one slot builder; only the slot LISTS differ. A third
  mode supplies lists, it does not copy the envelope.
- Taps ride alongside the spine for free. The terminator compiles its record *ancestor* cone, and a
  tap is a descendant of a spine step rather than an ancestor of the terminator, so it is outside the
  cone without any rule about taps existing anywhere. Only a fork that re-merges *inside* the cone is
  refused, and it names the step it refused at.
- A tap cannot be carried in the document, because a document expressing one is branchy and the flow
  importer replays a sequence. That limit belongs to the importer, not to this boundary rule.

**Example.**

```yaml
flow:
  image: {op: !class:...RecordContract {name: image, fields: {image: Image}}}   # head: nothing feeds it
  class: {from: image, op: !class:...RecordContract {name: class, fields: {class: Label}}}
outputs: class                                                                  # the record that leaves
```

```
instance in : [('record', 'RECORDSTREAM_RECORD', None)]        # declared, unconnected, visible
instance out: [('record', 'RECORDSTREAM_RECORD', []),
               ('op',     'RECORDSTREAM_OP',     [])]          # terminator placed + promoted
```

**What you may change.** Read further declarations the flow grammar grows (a multi-output `outputs:`
would become several record slots). Do not reintroduce inference on this path, and do not "fix" the
`!ref:` rule by loosening resolution globally — settling exists to catch a genuine typo in a
reference, and turning it off to make one branch reachable would trade a located load error for a
silent, mysterious boundary socket.

## The App view is declared in the source document, not pasted into the generated one (`app:`, 2026-09-05)

**Context.** ComfyUI's App view turns a workflow into a FORM: `extra.linearMode` plus
`extra.linearData` naming `[[node id, widget name], …]` inputs and the output nodes the Run button
drives. It is how a template exposes one parameter without the user ever seeing nodes.

The importer wrote `"extra": {}` unconditionally. So a template that wanted an app view had the block
added AFTER generation, and the file's own regenerate header carried a step saying so — which is the
hand-edit-the-generated-artifact trap the template-first rule exists to prevent. Measured on the
shipped detection sink before this change:

```
SHIPPED    : extra = {"linearMode": true, "linearData": {"inputs": [[5, "store"]], "outputs": ["1"]}}
REGENERATED: extra = {}
```

**Decision.** A source document declares its App view in an `app:` block, and the importer emits the
`extra` from it. Nodes are addressed by CLASS NAME, which is the only spelling that reads the same in
both document shapes a template can have — a `flow:` step graph keys its ops by step name, a
whole-graph config nests them under arbitrary keys — and unlike a node id it survives regeneration.

An unusable declaration is REFUSED at import: a class no node carries, a class on two nodes, a widget
the class does not have, a wired socket named as a field. That is the point rather than a nicety,
because the frontend fails SILENTLY — `pruneLinearData` drops an entry whose node does not resolve and
the App-view renderer drops one whose widget does not, so a wrong name yields an empty form with no
error anywhere.

`app:` on a `--as_ subgraph` import is refused too. Subgraph packaging moves every node inside
`definitions.subgraphs[]`, and `resolveNodeWidget` renders a subgraph-internal widget only once it has
been PROMOTED onto the instance node — so ids that point inside a definition are skipped and the form
comes up empty. A template wanting an App view imports flat.

**Consequences.** Regenerating a declaring template reproduces its app view; the shipped detection
sink's JSON is byte-identical before and after, and its header lost the manual step. A template's App
view is now readable in its source rather than only in the generated artifact. The cost is that a
class appearing twice on one canvas cannot be addressed at all — deliberately, because a class name
cannot say which one and guessing would ship the wrong field.

**Example.**

```yaml
app:
  inputs:
    - [ClassifyBluetooth, envelope_spread_db]
    - [ClassifyBluetooth, ble_index]
  outputs: [BluetoothVerdict]
```

```
AppViewError: app: MeasureSymbolClock.windows_hz is a wired socket, not a widget — an App view
shows fields, so expose one of: max_points, inst_freq, output, findings
```

**What you may change.** The block's keys, and which failures are refused. What must hold: the
declaration lives in the SOURCE so regeneration preserves it, nodes are addressed by something stable
across a regeneration, an ambiguous or unrenderable declaration is refused rather than emitted, and
the subgraph combination stays refused until promoted widgets are emitted with it.

## An unregistered `Taidal_*` node is a refusal, not a comment (`graphio.workflow_to_yaml`, 2026-09-06)

**Context.** The exporter is deliberately forgiving: a canvas holds viewers, primitives and
canvas-only adapters that have no Confluid form, and failing on them would make export
useless. So every node the registry did not know was listed in a leading comment block and
skipped. That reasoning is sound for a node that is genuinely not ours — and wrong for one
that is. The node registry is a SNAPSHOT taken when the server starts, so a canvas naming an
op added since converts to a shortened chain that looks like a valid document. Measured: an
editor six minutes older than a new op turned a twelve-node chain into one node, and the
failure surfaced three layers downstream as "no record chain to run".

**Decision.** Split the branch on the node KEY. `bridge._node_key` mints every discovered
node as `Taidal_<package>_<category>[_<group>]_<Class>`, so the prefix is a reliable statement
that the canvas thought this was one of ours. An unregistered name carrying it means the
INSTALLATION is out of step with the canvas; an unregistered bare name is a real ComfyUI
node. The first raises, naming every missing node and the remedy; the second still drops.

The refusal is deliberately narrow, and the boundary is registration rather than the prefix:
the hand-written adapters (`Taidal_SubgraphOp`, `Taidal_MixRecords`, `Taidal_Value`,
`Taidal_Math`) carry the prefix and ARE registered, so they never reach it — which matters,
because every shipped subgraph canvas contains one.

**Consequences.** A stale server now says so in the first message a reader sees, at the layer
that knows why. The CLI export fails where it used to write a partial YAML, which is the
intended trade: a partial chain is not the canvas. No opt-out flag exists — one would be a
second spelling for "give me the wrong answer", and the message already names the fix. A
canvas that genuinely outlives an op (the package was removed) also refuses, and the message
says that case out loud rather than pretending a restart will help.

**Example.**

```python
from streamstudio.graphio import workflow_to_yaml
from streamstudio.export import UnsupportedTopologyError

stale = {k: v for k, v in NODE_CLASS_MAPPINGS.items() if "measure" not in k}
try:
    workflow_to_yaml(canvas, stale)
except UnsupportedTopologyError as refusal:
    print(refusal)
# this StreamStudio does not register 11 node(s) the canvas names: node 2
# (Taidal_waivefront_op_measure_TuneToBand), … Restart it (`streamstudio restart --editor comfy`) …
```

**What you may change.** The wording, and which reasons are listed together. What must hold:
an unregistered `Taidal_*` node never reaches the output document, a registered adapter never
raises, and a bare ComfyUI node still drops with its comment.

## The App view's Run for a pane graph is answered by the workspace, not the editor (`Preview on Selection`, 2026-09-06)

**Context.** A pane graph is a chain of ops with no source: the workspace that shows the pane
feeds the record and fills the time and frequency window by shape. Its App view is a form of the
thresholds, which is what makes it tunable without seeing nodes, but the App's Run button queues
the canvas through ComfyUI, where nothing feeds the head op, and the output panel lists media
outputs only. Without an output node the form is blind: the effect of a change is visible only
after a save, in the workspace.

**Decision.** Two hand-written output nodes, placed by the importer when the source document's
`app:` block says `preview: true`. `Preview on Selection` reads the chain feeding it off the hidden
prompt, converts it with the SAME converter the workspace applies to the saved canvas (so the
form's unsaved values are in the document), and posts it to the workspace, which runs it over the
record and window its page currently shows and answers the pane's payload; the node renders the
payload as one PNG. `Selection` shows that window as the workspace renders it. The editor knows the
workspace by three routes and an address the launcher hands over, nothing else.

**Consequences.** Run previews without saving, on exactly what the analyst is looking at. The
editor never executes the ops (the preview's record input is lazy), so a canvas without a source
runs nowhere but where the record is. The chain must convert to the same document the pane runs,
which is why the display nodes are canvas-only adapters the converter skips. A refusal is an image
that says why. The workspace must be running with a record selected; the nodes carry its address
as a widget rather than discovering it, so two workspaces cannot be confused.

**Example.**

```yaml
app:
  inputs: [[ClassifyBluetooth, ble_index]]
  outputs: [BluetoothVerdict]
  preview: true
```

```text
GET  /api/state                                   -> {record: "files#0", window: {x0, x1, y0, y1}}
POST /api/preview {document: <converted canvas>}  -> {record, window, payload: {note, cycle_hz, power_db, steps}}
```

**What you may change.** The renderer (any function from payload to image); the placement of the
two nodes; the default address. Not the protocol's meaning (state, preview, thumbnail) without
changing the workspace that serves it in the same change, and not the lazy record input: pulling
it would execute a sourceless chain in the editor.

## A decision is DRAWN as outcome sockets and a join, and RUN as a gated chain (`fold_branches`, 2026-09-06)

**Context.** The Bluetooth probe became a chain of nodes with three gated decoders, each carrying
`requires: <flag>` in the flow document. Imported, that is a straight row of twelve nodes with a
text box on each decoder — the user's reaction: *"I thought we made a decision to support a real
DAG graph … `requires` is a technical implementation and not something I should worry about …
the graph is the truth and should visually represent what is executed with branches and test."*
The record engine, meanwhile, runs the chain on purpose: the branches are exclusive, so a gated
node that passes the record through costs one flag check, and nothing is computed just in case.

**Decision.** Two spellings of one thing, meeting in exactly one function. On the CANVAS a test
node (an op declaring `flags`) has one record output per outcome; a branch hangs off an outcome;
branches rejoin in `Join branches`; the gate is neither widget nor socket. In the DOCUMENT the
chain stays linear and gated (`requires:`), unchanged. `export.fold_branches` turns the drawing
into the chain for both exporters (members gated by their branch's outcome unless already gated
— a nested decision keeps its own — branches in the test node's outcome order, the join dropped,
innermost decisions first); `graphio.flow_yaml_to_workflow` draws the chain back (a gate names
an earlier step's outcome; an ungated step closes the open decisions through one join). A
straight exported chain is also run through the engine's `check_chain`, so a wire that skips a
producer is refused where it is drawn.

**Consequences.** The canvas shows the fork and its condition as wires, with no domain word in
the editor; the runtime and the shipped `bte_view.yaml` are untouched; a decision added to any
future test node needs no editor change. The costs: a canvas saved before this carries a widget
value that no longer exists (positional shift on its next save — regenerate or merge templates);
canvas EXECUTION of a nested decision's untaken outcome leaves the outer join empty where the
chain would still run the continuation (the canvas preview is not the runtime); the join takes
six branches.

**Example.**

```
ClassifyBluetooth ├ ble ─▶ DecodeBleAdvertising ─┐
                  ├ bredr ─▶ DecodeClassic… ─────┼─▶ Join branches ─▶ BluetoothVerdict
                  └ try_weak ─▶ CorrelateAccess… ┘
```

```yaml
# what fold_branches exports — and what flow_yaml_to_workflow draws the picture back from
ble:     {op: !class:…DecodeBleAdvertising {requires: ble},          from: classify}
classic: {op: !class:…DecodeClassicAccessCode {requires: bredr},     from: ble}
weak:    {op: !class:…CorrelateAccessAddress {requires: try_weak},   from: classic}
verdict: {op: !class:…BluetoothVerdict {},                           from: weak}
```

**What you may change.** How branches are laid out, how many inputs the join offers, what the
refusals say. What must hold: the document form stays the gated linear chain (the engine's
runtime), the canvas never shows the gate, a nested decision keeps its own gate, and both
exporters fold through the one function.


## A graph that is already open is identified by its DOCUMENT, never by its node types (`open_graph.js`, 2026-09-11)

**Context.** Opening a graph is idempotent by design: the extension re-uses the canvas when it
already holds the graph it was asked for, rather than stacking a duplicate workflow tab. The
test for "already holds it" was the sorted list of TOP-LEVEL node types. That test was already
weak and became worthless once graphs were drawn as subgraphs: the top level of such a document
is `Stream` plus one subgraph id, and subgraph ids are shared between panes of one workspace
AND between workspaces that seeded from the same template. Measured on a user's machine across
four annotation workspaces: SIXTEEN different graphs — every pane of every workspace — carried
one identical node-type list, and the six model graphs of three workspaces carried another.

With one editor serving three annotation servers at once, the consequence was constant: the
second open of any graph was skipped as "already shown", the canvas kept the PREVIOUS
workspace's document, and the user was looking at another server's graph. The preview nodes
went with it — the step that re-points them at the server that launched the open sat after the
skip, so they kept asking the previous one for "the selected record". The user's report was
*"since 3 annotaide servers are running, streamstudio connects to the wrong ones"*, and the
damage is not only what you see: edit the graph you were handed and save, and the mirror writes
it into the OTHER workspace's file.

**Decision.** The identity is the ComfyUI document path — `workflows/taidal/<workspace>/<stem>.json`,
which `workflowPathFor` already builds and the store already reports as `activeWorkflow.path`.
`activeWorkflowPath()` reads it; `holdsOurGraph(want, wantPath)` prefers it and falls back to the
node-type fingerprint only when no document is named (a canvas-only load, where the fingerprint
is all there is). Both the skip test and `settleOpen`'s verification use it. The preview re-point
now also runs on the skip path — that path is precisely the case where the canvas came from
another server — and it walks into subgraphs, where a graph drawn as one keeps its preview nodes.

**Consequences.** Two graphs that differ only in their workspace are told apart, which the node
types cannot do. A canvas-only fallback behaves exactly as before. A skipped open costs one
extra walk of the graph's nodes to re-point the previews, which is the point of the skip path
existing at all.

**Example.**

```js
// both workspaces' `view_fft` graphs: `Stream` + the SAME subgraph id
fingerprint() === want                                  // true for BOTH — the old test
holdsOurGraph(want, "workflows/taidal/alpha.workspace/view_fft.json")   // true  — it is on screen
holdsOurGraph(want, "workflows/taidal/beta.workspace/view_fft.json")    // false — open it
```

**What you may change.** How the document path is spelled (it comes from one function), and the
fallback. What must hold: the node types never decide it while a document is named; the preview
nodes are re-pointed on EVERY path that concludes the canvas shows the asked-for graph. Pins:
`tests/test_open_graph_mirror.py::TestTheAlreadyOpenCheckIsTheDOCUMENT`,
`tests/test_preview_nodes.py::TestTheOpenerPointsTheNodesAtTheLaunchingWorkspace`.


## The graph document is the truth; the canvas is derived and edits are located text patches (`streamstudio.document`, 2026-09-28)

**Context.** The ComfyUI extension kept TWO documents of every graph: the canvas (`workflow.json`)
and the confluid YAML a converter produced from it, re-checked on every start. Three things went
wrong, each measured before the rewrite was scoped (`docs/rewrite.md` §2.1, §6, §9):

- **A canvas stores a node's values by POSITION.** `widgets_values = [25000000.0, 0.025, 'a Bluetooth
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
the YAML. Pins: `tests/test_document.py::TestSetParam::test_the_bluetooth_pane_keeps_its_230_comment_lines`,
`tests/test_document.py::TestProblems`, `tests/test_layout.py`.

## The canvas page is a build artifact beside the package, and a drawn wire is a proposal until a route can spell it (`frontend/`, `streamstudio.app.page_blueprint`, 2026-09-28)

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
identity, the wildcard, or `types[a]` containing `b`; a mismatch never becomes an edge. A drop whose types
meet becomes a dashed PROPOSAL edge with a toast naming the spelling the document would need; nothing is
written and the next model refresh drops it.

**Consequences.** `pip install streamstudio` gives the API and the placeholder, never a page that is out
of step with its routes; a developer builds once and the checkout serves it. The wheel does not carry
the page — the deferred item in `TASKS.md` is to build it in the release workflow, which needs node on
the runner. Every element the page renders is addressable by `data-*` (`data-node`, `data-socket`,
`data-type`, `data-kind`, `data-param`, `data-problem`, `data-toast`, `data-saves`), so the playwright
suite reads the SPEC and never a class name. The proposal rule means the canvas can show one thing the
document does not have — visibly dashed, and only until the next edit; the alternative (adding the wire
to the document by guessing a spelling) would invent semantics the document never stated.

**Example.**

```python
app = create_app(graph_dir)                          # web_dist() -> <repo>/frontend/dist when built, else None
client.get("/")                                      # the built index.html, or the placeholder naming the build command
client.get("/api/types").get_json()["Stream"]        # ["Source"]  — the page's compatible("Stream", "Source") is True
```

```
drag decode.image -> count.label  : isValidConnection False -> no edge, no toast, file unchanged
drag decode.image -> boost.image  : one dashed edge; toast "wires.yaml: the wire decode.image → boost.image is drawn as a
                                    proposal, not written — on boost it would be spelled 'from: decode' …"; file unchanged
```

**What you may change.** Where the build lands (as long as `web_dist()` and the build command agree),
the toolchain versions, the widget mapping, the toast wording. What must hold: the page is derived from
the model the routes serve and writes only through them; the type test is `document.supertypes()`, never
a table typed into the page; a wire the document cannot spell is never written; the source is committed
and the build is not. Pins: `tests/test_canvas.py`, `tests/test_app.py::TestTypes`,
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

## The canvas is a React Flow page over the document routes; every ComfyUI-era behaviour is rebuilt on the document, not on a second model (`frontend/`, 2026-09-28)

**Context.** The ComfyUI canvas was a second model of every graph: LiteGraph's own JSON (`workflow.json`, values by
position), kept in step with the YAML by converters in both directions (the record "The graph document is the truth"
above lists what that cost). Retiring it needed a canvas that brings the behaviours people used there — typed sockets
refused while dragging, widgets on the node, a search palette on double-click and on a dropped wire, groups, undo/redo,
pan/zoom/minimap/snap/multi-select/copy-paste, auto-layout, titles and colours, JSON in and out — WITHOUT bringing a
document model of its own. Fourteen libraries were measured on 2026-09-27 (licence, release and commit activity,
framework, and those nine behaviours one by one, `docs/rewrite.md` §2.3). The ones with the most behaviours built in
are the ones that carry a model:

- **litegraph** has 8 of the 9 (it IS ComfyUI's base), but its last commit is 2024-01-08, and the ComfyUI fork is
  archived and npm-deprecated ("Package no longer supported").
- **Rete v2** keeps its own editor state across 11 separately versioned packages; four were last released 2024-08-30.
- **Baklava** has native subgraphs, but is Vue-only in a React toolchain; **reaflow** lets ELK own every position, so
  the sidecar could not.

**Decision.** React Flow (`@xyflow/react` 12.12: MIT, 13.5 million weekly downloads, 112 commits in the 90 days before
the choice, React 18 — the vite toolchain the workspace's other frontend already builds with). It is a diagram
toolkit, not an editor: it draws two plain arrays, `nodes` and `edges`, and calls back on every gesture; there is no
model inside it to keep in step. The page REDRAWS those arrays from the model the server answers after every document
edit (`modelToFlow`, node id = the document's node name), and each ComfyUI-era behaviour is rebuilt as a route call on
the document:

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

## Changes stay in the editor until Save, and Save refuses to overwrite a file changed on disk (`Document.save`, `POST /api/graphs/<name>/save`, 2026-09-29)

**Context.** Phases 2 and 3 saved every accepted edit at once: `POST …/param` wrote the file before it answered.
Two things the menu scope asked for cannot live with that: Move to its own file… makes a steps file you have not
asked to write yet, and Revert needs a saved state to go back to. And a file can change under the editor — a teammate, a `git pull`, an agent —
which an editor that writes on every change overwrites without a word. The user took the ComfyUI default (the menu
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

The user's rule: "if a subgraph is needed twice (either copy it, use templates like comfyui or use the include
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

## One CLI serves two editors, chosen by `--editor canvas|comfy` (`streamstudio.cli`, 2026-09-29)

**Context.** Until the rewrite's phase 4 there are two editors: the canvas editor, and the ComfyUI extension that a
consuming annotation workspace still launches. During phases 2 and 3 the canvas editor had a command of its own
while bare `streamstudio` launched ComfyUI. With the menu scope the canvas editor became the one a person starts,
so the default had to move (the menu scope's decision 3). Each editor has options the other does not (ComfyUI:
`--comfy-dir`, `-b`, the palette flags; the canvas editor: a graph folder, `--templates-dir`) and its own port.

**Decision.** `run` (the default command) takes `--editor`, a closed `Literal["canvas", "comfy"]`, default
`canvas`; `stop` and `restart` take it too, and every port default follows it (`EDITOR_PORTS`: 8190 / 8188). An
option that belongs to the other editor is refused by name before anything starts — never ignored, since an ignored
`--comfy-dir` would start the wrong editor without a word. The canvas folders are resolved to absolute paths once,
in the CLI, before any work (and before `restart` stops anything). The busy-port hint prints the SAME editor,
folder and templates folder one port up, in the order the CLI parses. The earlier command has no alias (pre-1.0).

**Consequences.** Measured:

```
$ streamstudio --comfy-dir /path/to/ComfyUI    →  --comfy-dir only applies to --editor comfy
$ streamstudio restart -b                      →  --background only applies to --editor comfy
$ streamstudio graphs          (8190 taken)    →  … run StreamStudio elsewhere:  streamstudio /abs/graphs --port 8191
$ streamstudio restart grpahs                  →  Graph directory not found: /abs/grpahs      (nothing was stopped)
```

A consumer that needs ComfyUI names it (`python -m streamstudio.cli run --editor comfy`). Every remedy a message
prints names the editor: the ComfyUI export refusal says `streamstudio restart --editor comfy`, because a bare
restart would restart the canvas editor on 8190 — its unsaved work going to the recovery file — and leave the stale
ComfyUI registry running. Before the folders were resolved, `streamstudio graphs` listed `graphs/chain.yaml` and the file browser
looked in `graphs/graphs`; `streamstudio .` opened both dialogs on `/`, where Save As would have written to the
filesystem root.

**Example.**

```python
from streamstudio.cli import EDITOR_PORTS
EDITOR_PORTS                                   # {'canvas': 8190, 'comfy': 8188}
```

**What you may change.** The ports, the option spellings, the layout of the hint. What must hold: one closed set of
editors; an option is never silently dropped; every command a message prints is one the CLI accepts (pinned by
parsing it back); phase 4 removes `comfy` together with the ComfyUI code. Pins: `tests/test_cli_editor.py`,
`tests/test_missing_node_refusal.py`.

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
- **What is never lost.** A file nobody answered yet is merged into, never overwritten: a second stop (two editors
  on one folder, a restart before anyone answered) keeps the earlier graphs, and one file kept twice is two
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

- Two editors on one folder, stopped at once: before, work was lost in 4 of 4 trials and one file was not JSON;
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

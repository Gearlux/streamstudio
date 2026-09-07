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
# (Taidal_waivefront_op_measure_TuneToBand), … Restart it (`streamstudio restart`) …
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


# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Bare `streamstudio` starts the canvas editor**, on port 8190, over the folder it is given (default: the
  current directory): `streamstudio path/to/graphs`. **`streamstudio --editor comfy`** starts the ComfyUI editor on
  port 8188, with every ComfyUI option (`--comfy-dir`, `-b`, `--default-nodes-`, `--only-taidal-nodes`,
  `--palette-config`, …). `stop` and `restart` take `--editor` too, and their default port follows it. An option of
  the other editor is refused by name — `streamstudio --comfy-dir /x` prints `--comfy-dir only applies to --editor
  comfy` — and `restart` checks the graph folder before it stops anything. A taken port suggests the same command
  one port up (`streamstudio /abs/graphs --port 8191`). The ComfyUI export refusal names
  `streamstudio restart --editor comfy`. `streamstudio restart` without a folder serves the folder the running
  canvas editor serves.

### Fixed

- **An edit is no longer refused for a problem that was already there.** A step inserted above an existing problem
  (a broken `bind:`) moved it from `flow.yaml:6:9` to `flow.yaml:9:9`, and the edit was refused as if it had made
  that problem; problems are now compared without their locations, and counted — a second copy is still refused.
- **The editor's check reports a document that derives but does not build.** A top-level key that confluid
  hands to a same-named constructor parameter used to pass `check()`; it is now one located problem,
  `does not build`, and an edit or a PUT producing such a text is refused and not written.
- **Undoing a rename gives the position and the group memberships back to the old name.** The history
  restored the text only, so the node was laid out afresh and its group kept a member that no longer existed.
- **A chained op's op-list fan-in survives the whole-graph export.** The folded-chain
  path of `workflow_to_yaml` assigned each `op_N` wire to the same kwarg — last wire
  wins — so an op with several nested compute ops (a `ConfigureOp` drawn with two)
  exported with only the second, orphaning the first and failing the re-load. The
  chain path now accumulates dynamic op-list wires in slot order, like the
  standalone-node path always did.
- **A ComfyUI App-mode save now mirrors to the workspace graph file.** A graph opened via
  `?taidal_graph=` whose `extra` carries `linearMode` presents as a ComfyUI APP (a form of
  exposed parameters, no node canvas) and saves as the `<stem>.app.json` derivative — a
  path the save mirror did not map, so a parameter typed into the app form (an update
  sink's output directory) never reached the workspace's graph file. Both paths now map to
  the same file.

### Added

- **The graph editor without ComfyUI — phase 2 of the rewrite (`docs/rewrite.md`).** `streamstudio.document`
  derives a typed node/wire model from a confluid graph document and edits it by patching the marker's own line
  (`set_param`, `add_step`, `remove_step`, `rename_node`, undo/redo) — comments and `$VAR` survive, a refused edit
  writes nothing; `streamstudio.palette` describes every configurable class as a node spec read off its declarations;
  `streamstudio.layout` keeps positions in a `<graph>.layout.yaml` sidecar keyed by node name; `streamstudio.app`
  serves them as a Flask blueprint (`/api/graphs…`, `/api/palette`, `/api/runs…`) — standalone through bare
  **`streamstudio <dir>`** (port 8190), or mounted by a consuming Flask app.
- **The canvas page — phase 3, part 1.** `frontend/` (vite + React Flow + TypeScript) draws the document's nodes
  and wires from the model the routes serve — one component for every class, typed coloured sockets, widgets from
  the constructor's schema — edits parameters through `POST …/param` (a refused value shows the located message and
  reverts), records node positions on drag, and keeps a hand-drawn wire the document cannot spell yet as
  a dashed proposal that is never written. Built with `cd frontend && npm install && npm run build`, served at `/`
  by `streamstudio` when the build exists (a placeholder names the command otherwise), mountable beside the
  API through `streamstudio.app.page_blueprint`. `GET /api/types` lists what each socket type may be wired into,
  so the page's type test is the model's meet rule.
- **The canvas page — phase 3, part 2: the gestures of a graph editor, each a route call.** Double-click the canvas
  (or drop a dragged wire on nothing) for a searchable list of every class the palette describes, narrowed to the
  ones that fit the wire; a pick inserts a `flow:` step after the selected node. Select nodes and press G for a titled,
  coloured group that moves its members with it. Ctrl/Cmd+Z / Shift+Z undo and redo — a document edit through the
  document's own text history (so an agent's edit is undone the same way), a move, an arrange or a group on the page.
  Ctrl/Cmd+C / V copy and paste steps as their own YAML text. *arrange* lays the graph out left to right by the
  cards' sizes; *columns* asks the server's layout. Double-click a node's name to rename it everywhere. The **run**
  tab traces one record of the graph's own source — every node shows its status, wall time and generation, *step*
  pauses one node further, a node's entry lists what came in and went out, and *rerun this node* recomputes it and
  what follows with trial values the document never sees. The **settings** tab is the form of every node, through
  the same write as the widgets. `PUT /api/graphs/<name>/groups` stores the groups.
- **The layout sidecar holds two blocks**, `positions:` (`{node name: [x, y]}`) and `groups:` (`[{id, title, color,
  nodes, bounds?, parent?}]` — `bounds` for a box sized by hand, `parent` for the subgraph level it lives on); a
  rename re-keys both.
- **Explicit Save.** An edit changes the editor's copy only; ● on the title, the file name and the graph list says a
  graph holds unsaved changes, and leaving the page asks first. File ▸ Save (`POST /api/graphs/<name>/save`) writes
  every changed file and the sidecar, and refuses — writing nothing — when a file changed on disk since the editor
  read it: `…/demo.yaml changed on disk since the editor read it — Revert to load it, or Save As to keep both`.
  Save As (`…/save-as {path, overwrite?}`) writes the graph elsewhere, rewriting relative `include:` entries so they
  name the same files; Revert (`…/revert`) reads the file again. The run traces what the editor holds.
- **New, Open and a file browser.** New ▸ Flow graph… / Pipeline… / Source graph (with its contract)… asks a
  **Name** and a **Folder** (the editor's own, a typed one, or Browse…), shows the file it will write
  (`→ /home/me/graphs/fresh.yaml`), and **Create writes the starter at once** (`POST /api/graphs {name, kind, dir?}`,
  answered with `dirty: false`) — every graph the editor shows is a file on disk. Refused before anything is
  written, the dialog staying open with the reason: a file that is already there
  (`…/fresh.yaml already exists — pick another name, or open it with File ▸ Open`, the file left byte for byte), a
  folder that does not exist, is not a folder or cannot be written, and a name the file-name rule refuses. File ▸
  Open… (`POST /api/graphs/open`) adds any `.yaml` on the editor's machine to the list, through a browser of its
  folders (`GET /api/files`). The list shows every `*.yaml` of the folder, every opened file and every file New…
  made in another folder (a name already listed becomes `clip~2`), and leaves out `*.layout.yaml` and
  `*.steps.yaml`.
- **A menu bar, a Nodes sidebar and right-click menus.** New / File / Edit / View / Graph / Run, each item with its
  shortcut and greyed when it cannot act. `N` opens the Nodes sidebar: every class, grouped by package and kind,
  searchable, each with a one-line description, chips for what it reads and writes, and a hover card of its
  parameters; click adds after the selected node, drag places it where you drop it. Right-click on empty canvas, a
  node or a group title for the actions that fit there. Graph ▸ Check graph lists every problem with its line.
- **Groups can start empty, be resized, and be filled by dragging.** Add group drops an empty box; a box resizes by
  its corner; dragging a node into a box adds it, out of it removes it.
- **Subgraphs on the canvas.** A `recordstream.flow.subgraph.Subgraph` step is ONE node; double-click it to edit its
  inside (`demo › prep`, `<in> → grey → scaled → <out>`), Escape or the breadcrumb to go up, and `?level=prep` keeps
  the level in the address. Inner nodes are named `prep/grey` everywhere — the routes, the run panel, the sidecar.
  Graph ▸ Convert selection to subgraph (`POST …/subgraph`) moves consecutive steps inside, keeping their lines and
  comments; Move to its own file… (`…/subgraph/<node>/extract`) moves the steps to a `*.steps.yaml` the graph
  includes; Insert steps file… (`…/include`) adds a step that includes one. A run pauses inside a subgraph and
  reruns an inner node.
- **Using a subgraph twice: copy, templates or include.** Copy / Paste (`POST …/copy`, `…/paste`) carry the steps'
  own YAML text, so comments, `2.0` and `$VAR` survive and a copy pastes into another graph. Save as template…
  (`POST /api/templates`) writes a steps file to `~/.streamstudio/templates` (or `--templates-dir`) and Insert
  template ▸ (`…/templates/<t>/insert`) adds a copy. An included steps file is shared: an edit through one use
  changes every use.
- **`POST /api/graphs/<name>/steps/remove {nodes}`** removes several steps or op-list members as ONE edit, all or
  nothing — Cut and Delete use it, and one undo brings every one back.
- **Unsaved work survives a stop.** When the canvas editor stops with unsaved work — `streamstudio stop`,
  `streamstudio restart`, Ctrl+C, or closing the terminal window it runs in — it writes that work to the hidden `.streamstudio-workspace.json` in the folder it
  serves, and `stop` says so: `Stopped StreamStudio on port 8190 (pid 57474). Its unsaved work (demo.yaml) is kept in
  /home/me/graphs/.streamstudio-workspace.json — the next start asks what to do.` The next start prints
  `Unsaved work from the last session (29 Sep 20:06): demo.yaml — the page asks whether to restore it.`, and the page
  asks **Restore unsaved work** (the graphs open with their unsaved text, positions and groups, marked ●) or **Open
  the saved files** (the kept work is discarded after one more question). Nothing is kept after a `kill -9`, a crash
  or a closed terminal, and the undo history is not kept. Routes: `GET /api/workspace`,
  `POST /api/workspace/recovery/restore`, `POST /api/workspace/recovery/discard`. A request from a page of an earlier
  editor (sent with its `X-Streamstudio-Session`) is refused with `409` and changes nothing.
- **Op lists are editable.** On a Pipeline (`stream: Stream {ops: [...]}`), a click in the Nodes sidebar, a palette
  pick or a dropped wire adds a member after the selected member, or at the end of the list with nothing selected
  (`POST …/steps` with `{after: "stream.ops[1]"}` or `{list: "stream.ops"}`; the answer names the new member in
  `added`). Delete and Cut remove members as one edit; removing every member leaves `ops: []`. A `Pipeline`'s
  `transforms` work the same way. The list keeps its own style, a comment written above a member goes with that
  member, and later members keep their places and groups; `POST …/undo` / `…/redo` answer `shifts`, how members were
  renumbered.
- **Shift+click adds a card to the selection**, as in ComfyUI, and never removes one; Cmd+click (macOS) / Ctrl+click
  toggles; Shift+drag on empty canvas still draws a selection box, and dragging a selected card moves the whole
  selection.
- **Save refuses a file it cannot read**, naming it — `…/demo.yaml cannot be read: Permission denied — the editor
  never writes over a file it cannot compare with what it read; …` — and a route that meets a file error answers
  `400` naming the file.
- **`streamstudio.__version__`** reports the installed version, read from the package metadata
  (`importlib.metadata.version("streamstudio")`), so it always matches `pyproject.toml`; an uninstalled
  source tree reports `0.0.0.dev0`.
- **Item types are socket types, and each gets a passthrough node.** Every registered
  `recordstream` item type (`Image`, `Label`, `Boxes`, `Mask`, `MultiLabel`, plus whatever a
  domain package registers) now has a socket type `RECORDSTREAM_ITEM_<NAME>`, an output on
  *Extract from Record* carrying the item verbatim (`item_image`, `item_label`, …), and a
  generated `<Item> Type` passthrough node. Since ComfyUI connects on socket-type equality, a
  drawn graph can now STATE a contract: an entry declared `Image` only docks into something
  taking an Image, and a subgraph boundary fed by one inherits that type. The family is built
  after node discovery, not at import — item types are registered BY the scan, so building
  earlier carried only the record engine's own and silently missed every domain one.
- **`--as_ pipeline`: a contract document becomes a whole starting canvas.** A `flow:` of
  `RecordContract` steps is read as a DECLARATION rather than laid out as nodes: it becomes a
  subgraph with a `record` input and one typed output per required entry (each extracted and
  passed through its item's type node), an `op` promoted via *Build Record* → *Extract as Op*,
  and a `Stream` placed beside it with that `op` already wired. Exactly one thing arrives
  unconnected — the `Stream`'s `source`.

### Added

- **A source can say what it knows.** A source node now surfaces its declared `@output`s beside
  its `source` socket — a HuggingFace source shows `class_names`, read from the dataset's own
  `ClassLabel` metadata (no records). Appended after `source`, so saved canvases keep their slots.
- **A value producer's `Any` params are WIRED, and it emits itself.** A param annotated bare `Any`
  renders as an optional socket accepting a source or another producer's object; every value node
  gains a trailing `object` output carrying the producer.

### Fixed

- **A wired `@output` no longer exports a reference nothing can load.** The exporter emitted
  `!ref:node.attr` for such a wire, and attribute references were removed from the config layer —
  so the file was written happily and then refused to open (reported as a broken graph after
  wiring a source's class names into the field that names the classes). A declared `@output` is
  now EVALUATED at export and written as a literal; the structural sockets that stand for the node
  (`source`, `op`, `object`) keep a whole-node reference. Which is which comes from the class, not
  a slot index — indexing got it wrong in both directions.

### Fixed

- **The HuggingFace pickers survive a graph LOAD, not only a hand-picked dataset.** The
  split / column / metadata-column pickers build their options from the node's dataset-id
  widget, and refreshed only in `onNodeCreated` — but a loaded graph fills widget values in
  `configure()`, AFTER that, by assigning `widget.value` directly, which fires no callback.
  So opening a saved workflow whose source already named a dataset left all three pickers
  empty, while choosing the same dataset by hand populated them. Each now stashes its
  refresh and re-runs it from `onConfigure`, chaining any previous handler.

- **An empty HuggingFace dataset picker now says WHY.** `HF_HOME` is read by
  `huggingface_hub` at IMPORT, so a running editor keeps the value it was started with —
  change it and the picker silently shows nothing, the reason buried in a `debug` line.
  `cached_hf_datasets()` returns `(ids, reason)`, the route sends both, and the picker shows
  the reason as its placeholder; a MISSING cache directory is a `warning` naming the path
  this process resolved and the `HF_HOME` it was started with.

- **A workspace graph opens as a NAMED workflow, so Save writes where it belongs.** Loaded
  straight onto the canvas it was an *Unsaved Workflow*: Ctrl-S demanded a name for a graph
  that already had a home, and cancelling meant the save mirror never fired. The graph is now
  persisted into ComfyUI's own store under `workflows/taidal/<workspace>.json` and opened from
  there, so Save is a plain overwrite — and that overwrite is exactly the POST the mirror
  copies back into the workspace's graph file. `openWorkflow` alone is not enough: it binds the
  identity but does not load the content (measured — the tab showed the right name over an
  empty canvas), so the graph is loaded with `loadGraphData(workflow, …, persisted)`.
- **Build Record declares no socket of its own.** The seeded `entry_0` was unioned by ComfyUI
  onto every generated builder, leaving a stray socket on a canvas that had already stated its
  entries — and it earned nothing: the compile clears it before naming its own, and growing
  sockets by hand was never wired up.
- *Extract from Record*'s native outputs keep their slot indices; the item outputs are
  appended. A saved canvas addresses outputs positionally, so inserting would have silently
  rewired every existing graph. Output names are `item_`-prefixed because the natives already
  own `image`/`mask`, and slot lookup by name resolves to the first match.

## [0.1.0a1] — unreleased

First public pre-release. StreamStudio is a ComfyUI custom-node extension, not a server of
its own: it generates the node palette from what is installed, so there are no
StreamStudio-specific ops to maintain.

### Nodes are generated, never hand-written
- Every node is built at import from the `@configurable` callables discovered through the
  `confluid.configurables` entry-point group. Install a package that declares them and its
  nodes appear; nothing needs registering here.
- Seven node shapes, one builder each: source, op, model, value, object, runnable, generic.
- A class's `@configurable` category decides its node shape where it names one: an
  `engine` (`Stream`, `FlowGraph`, `JointStream`) both takes and returns a
  `RECORDSTREAM_SOURCE`, so a source wires straight into it and engines chain.
- Widgets come from the callable's signature — closed `Literal`s become combos, PEP-593
  `Interval` marks become widget bounds, and each tooltip is the parameter's `Args:` entry.
- Object sockets are role-qualified (`RECORDSTREAM_OBJECT:model`) and claim their engine, so
  a cross-framework wire is refused as you drag it. The **Retag** node is the override.

### Running from the canvas
- **Taidal: compile to Op** turns a subgraph you drew into a `Stream` op — a `record` input,
  a *Build Record* gathering the promoted outputs (one socket per label), an *Extract as Op*
  terminator, and a promoted `op` — every arrow on the canvas. The compiler turns that shape
  into `RenameField`/`SelectFields` when it runs, so the graph you drew stays the graph you see. It runs only when you ask, reports what it built (or why it could not), and is
  re-runnable: the nodes it owns are rebuilt rather than duplicated. **Every** op in the rectangle is in the pipeline: loose ops are joined
  in canvas reading order, and anything you wired by hand keeps the order you gave it. A
  The **output labels are the record contract**: promoting an *Extract from Record* on `label`
  as `text` builds `RenameField(label → text)`, and `SelectFields` closes the set — real nodes
  placed inside the rectangle, so the contract is visible and editable. A rectangle that neither
  transforms records nor promises entries says so on the node instead of showing no socket.
- A runnable node runs on execute: it restores autograd for a training runnable, streams
  stdout live into a collapsible widget, drives ComfyUI's native progress bar, and is
  interruptible mid-run.
- Every declared `@output` is a live, typed, wireable socket.
- Workflow combinators nest: a runnable wired into a combinator slot is built rather than
  run, so only the selected branch executes.

### Plugins
- A domain node set is a plugin, never a requirement: install the package by any means and its
  nodes appear, because the palette is built from what the environment holds. Nothing here
  imports one unconditionally.
- A `streamstudio.plugins` entry point lets an installed package contribute editor surface
  without this package knowing it: the named module's `ROUTES` (an aiohttp `RouteTableDef`)
  merges onto the editor's route table, and its `WEB_DIRECTORY` of frontend JS is served at
  `/extensions/<dist>/…`. One broken plugin is isolated with a warning; the editor keeps
  running.
- A producer package tags a param's widget kind by stamping `__taidal_widget_params__` on
  the class — the bridge renders the tagged free-text STRING and the producer's own web
  extension supplies the picker. No widget special-cases live in the bridge.

### Import and export
- `streamstudio import --as subgraph` turns a `flow:` document into a **pipeline blueprint**:
  one rectangle with a declared `record` in, a `record` out, and an `op` output wired from an
  *Extract as Op* terminator placed for you. Drop it on a `Stream`'s op slot. The boundary
  comes from the document itself — the head step's unfed `record` and `outputs:` — and every
  slot renders before anything is connected to it.
- The canvas compiles to a Confluid `flow:` document — one per weakly-connected component —
  runnable head-less with `recordstream run`.
- A whole config round-trips both ways between YAML and a ComfyUI `workflow.json`.
- Export-skip and export-target marks scope what is emitted.

### The launcher
- The CLI is a **liquifai app**. Per-command help is generated from the command's own
  docstring and signature (`streamstudio restart -h`), and an option works on either side of
  the command name. Three spellings changed with it: a boolean turns off with `--install-`
  rather than `--no-install`, a list is `--custom-node=[a,b]` rather than a repeated flag,
  and the palette file is `--palette-config` (liquifai reserves `--config`). Short forms are
  declared per command: `-p` (`--port`), `-b` (`--background`), `-o` (`--output`); the old
  `--bg` is gone. A flag naming no parameter is now refused with a
  suggestion instead of being ignored. Completion moves to `liquifai-install-completions`;
  `argcomplete` is no longer a dependency.
- `--port` chooses where the studio serves (default 8188, ComfyUI's own). A port that is
  already in use is refused in a second, before any launch work, with the address of what is
  there printed as a clickable URL.
- `stop` and `restart` find the studio by the port it holds, so they work whatever started
  it. `restart` re-walks the whole launch flow, which is how you pick up newly installed
  nodes; stopping an idle port is not an error.
- `--background` (`-b`) starts the studio detached and hands the terminal back, logging to
  a per-port file under `~/.streamstudio/logs/`. Both `run` and `restart` honour it.
- `streamstudio` clones ComfyUI on first run, syncs its requirements on every run, seeds
  custom nodes from three tiers (package-declared, env, flags), symlinks this package in,
  and launches ComfyUI under the same interpreter.
- `--default-nodes-` and `--only-taidal-nodes` narrow the palette; the keep-list is a
  config file, not an environment variable.

### Quiet by default
- Importing the package emits nothing above debug. Node discovery walks one entry point per
  installed package, and those scan lines are diagnostics — visible with debug logging on,
  absent otherwise.

### In-canvas viewers
- Array/tensor histogram and confusion-matrix viewers draw on the litegraph node canvas.
  All the rendering math lives in the packages that own the data — the nodes are glue.

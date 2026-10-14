# StreamStudio Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md) (cited as *arch: "record title"*), the
rewrite's scope and decisions in [`docs/rewrite.md`](docs/rewrite.md) (§N), usage in the [README](README.md). Root
`AGENTS.md` rules are not repeated here.

## Current state

A graph editor for recordstream pipelines written as confluid YAML. Two editors behind one CLI, mid-rewrite:

- **The canvas editor** (bare `streamstudio <folder>`, port 8190) — the future. A Flask app (`app.py`) with a React
  Flow page (`frontend/`) over the YAML document (`document.py`, `_yamltext.py`), the class catalogue (`palette.py`),
  the layout sidecar (`layout.py`) and the page build (`page.py`). A host app can mount it (`editor_blueprint(host=…)`).
- **The ComfyUI editor** (`streamstudio --editor comfy`, port 8188) — `bridge.py`, `nodes.py`, `export.py`,
  `graphio.py`, `custom_nodes.py`, `config.default.yaml`, `web/*.js` and the ComfyUI half of `__init__.py`. It is
  deleted in phase 4 step 3, which the user has deferred. Until then its rules are live; they are all under the last
  heading so the deletion removes one section.

Landed: phases 0–3, 3b–3d, phase 4 steps 1 and 2 (a consuming workspace mounts the canvas editor and runs the YAML).
Next: phase 4 step 3, then phase 5 (agent tools; root `TASKS.md`). Version `0.1.0a1`, not yet on PyPI (root
`TASKS.md` "Publish streamstudio 0.1.0a1"); the repository is private.

Invariants:
- **The YAML document is the only truth** (§0); the canvas is a drawing of it.
- **The class-vocabulary slot is `classes`** (§6, decision A): a typed list, or a wired producer that declares its
  class names; the contract refuses a slot named after a parameter of any class the graph uses. (recordstream
  `tests/test_graph_contract.py::TestTheClassVocabulary`, `::TestSlotNamesAreNotParameters`;
  `tests/test_document.py::TestDerive::test_a_contract_root_has_an_input_per_output_slot_and_the_delivered_wires`)
- **Nothing new builds on the ComfyUI code**, and the canvas editor never reaches into it.

## The document

- **Never re-dump a document.** `Document` derives its model from `confluid.load(path, until="document",
  texts=…)`; every edit is a TEXT patch at the marker's location through `_yamltext.YamlText`. An edit that stops the
  document loading, or adds a problem to `check()`, is refused with the located message and changes nothing.
  (`tests/test_document.py::TestSetParam::test_a_setting_goes_to_the_settings_file_and_a_parameter_patches_one_line`)
  arch: "The graph document is the truth".
- `check()` also BUILDS the document once deriving found nothing; a document that derives but does not build is ONE
  located problem. (`tests/test_document.py::TestProblems::test_a_document_that_derives_but_does_not_build_is_one_located_problem`)
- A load problem is located where the error IS (`document._load_problem`), never by a pattern alone: a path may hold
  spaces. (`::TestAFolderWithASpaceKeepsTheLine`, `::TestAMissingIncludedFile`)
- **Node names are one scheme** — `recordstream.flow.Tracer`'s: a step by its key, a list member `stream.ops[1]`, a
  nested marker by its path, `contract`, `<host>`, inside a subgraph `prep/grey`. The sidecar, routes, engine and
  tools all use it; no ids go into the YAML. A rename re-keys the sidecar (positions and groups) in the same
  operation, and its undo/redo re-keys the CURRENT positions back. (`tests/test_document.py::TestHistory`)
- **Sockets come from recordstream and confluid only** (`algorithm_spec`, an op's
  `consumes`/`produces`/`flags`/`reports`, `confluid.input_specs`/`output_specs`/`to_pydantic`, `GraphContract`, the
  `recordstream.items` registry) — never from `bridge`'s socket vocabulary.
- **A value node's `object` output carries the scalar its class IS** (`bool`, `int`, `float`, `str`, in that order —
  a bool is an int), else `object` (`palette._built_socket`, `document._socket_of_built`: one rule, two tables, change
  both); so a number node (recordstream's `Quantity`) is wired into a number setting and saved `<param>: !ref:<P>`
  (user pick 2026-10-09: any node whose result is a number). The output keeps the name `object`. (`tests/test_number_wires.py`)
  arch: "A value node that IS a number offers a number".
- Parameter help comes from the class's docstring `Args:`, choices from a `Literal`, bounds from an `Interval` mark —
  never restated in the editor. (`tests/test_palette.py::TestAlgorithm::test_interval_and_literal_marks_reach_the_schema`)
- **A short class name resolves one entry point at a time** (`document._import_until`). Never call
  `load_configurables()` from a document path, and never `palette(scan=True)` in the server — only the catalogue's
  helper process (`python -P -m streamstudio.catalogue`) imports everything. Never skip an entry point because its
  module is in `sys.modules` (it may be half-imported by another thread). A test that counts imports resets
  `_pending_entry_points`. (`tests/test_short_name_lookup.py`) arch: "A short class name is looked up one entry point
  at a time".

## Save, New, recovery

- **Explicit Save:** every editing route changes the in-memory `Document`; only `POST …/save` writes. Save refuses,
  writing nothing, when a file changed on disk since the editor read it (`CHANGED_ON_DISK`;
  `CHANGED_ON_DISK_INCLUDED` offers Revert only). arch: "Changes stay in the editor until Save". Five rules, each from
  a measured break:
  1. The trace runs the in-memory document, never `confluid.load(path)`.
     (`tests/test_app.py::TestTrace::test_the_trace_runs_what_the_editor_shows_not_the_file_on_disk`)
  2. A read-only operation (Copy, Save as template) never holds an included file; when an edit first holds one,
     every older history entry gets the text it was read as.
  3. `PUT …/positions` MERGES over what the editor holds.
  4. **A load writes nothing:** it hands confluid the graph's path and every held text (`texts=`); the editor never
     parses the graph itself or rewrites an include for confluid. Every helper that reads an include of the graph's
     own text starts from `Document._folder`, never `self.path.parent`.
     (`tests/test_app_save.py::TestAHeldTextNeverTouchesTheDisk`, `tests/test_document.py::TestEveryLoadHandsConfluidTheGraphsPath`,
     `::TestAGraphFileThatIsASymbolicLink`)
  5. Every save, disk read and load runs under the process-wide `document._DISK_LOCK`.
- A Save writes all of its YAML files or none (`Document._refuse_unwritable`), the graph's own file LAST; a file it
  cannot read is refused (`CANNOT_READ`); any `OSError` a route meets is a `400 {error}` naming the file.
  (`tests/test_app_save.py::TestASaveTheDiskRefusesWritesNothing`) The sidecar is not in the check yet (`TASKS.md`).
- Only two gestures write without Save: File ▸ New… and Save as template….
- **No graph exists only in the editor:** New… writes its starter at once through `Document.create` (`open(path,
  "x")`, never over an existing file), and every refusal comes before anything is written. Do not reintroduce a
  graph held only in memory, a first-write case in Save, or a "clear workspace" command. The page keeps no copy of the
  file-name rule. (`tests/test_app_files.py::TestNew`, `tests/test_canvas.py::TestNewAndOpen`) arch: "Changes stay
  in the editor until Save", update 2026-09-29.
- **The recovery file** (`<started folder>/.streamstudio-workspace.json`) is written only as the process stops
  (SIGTERM, SIGHUP, Ctrl+C) and only when something is unsaved; it is merged into, never overwritten; an unreadable
  one is moved aside, never deleted; it is removed only by an answer (restore or discard). No graph opens before the
  question is answered; a tab of an earlier editor is refused `409` before anything changes; `GET /api/workspace`
  never raises. `stop`/`restart` ask the editor first (at most `cli.ASK_TIMEOUT`) and never claim work was kept
  without reading the recovery file back. There is no periodic
  autosave — adding one is a decision for the user. (`tests/test_recovery.py`, `tests/test_recovery_process.py`,
  `tests/test_cli_editor.py::TestWhatStopSays`, `tests/test_canvas.py::TestUnsavedWorkOfTheLastSession`,
  `::TestATabLeftOpenAcrossARestart`) arch: "Unsaved work is kept in a hidden file".
- Every file name the editor writes into a served folder matches `.streamstudio-workspace*.json`, which every project
  holding editor graphs ignores.
  (`tests/test_recovery.py::TestWhatAStopKeeps::test_the_file_names_are_the_ones_git_ignores`)
- **Agents and tests: never run `streamstudio stop` / `restart` without an explicit `--port`** — a bare one stops the
  person's own editor on 8190. Tests bind port 0 and stop only the pid they started; recovery tests use temporary
  folders. A harness that starts an editor as a child must reap it (psutil counts a zombie as alive; `TASKS.md`).

## Subgraphs, reuse, op lists

- A `Subgraph` marker is one node of kind `subgraph` whose only parameter is `result`; **`steps` is never a form
  field.** The deriver BUILDS a subgraph marker so the chain check stays on. A step name is scoped to its own steps
  mapping (a rename never follows `from:`/`bind:` into one), and `/` is reserved in step names. One defect, one
  message. An edit inside an included subgraph edits that file. Every refusal starts with its `file:line:col`. A
  name the editor gives a new step matches `document._STEP_NAME` and is never `include`. The palette, the sidebar
  and Add Node ▸ leave the `Subgraph` class out (`sidebar.SUBGRAPH_CLASS`).
  (`tests/test_subgraph_model.py`, `tests/test_subgraph_edits.py`, `tests/test_app_subgraph.py`) arch: "A subgraph
  is one node outside"; §6b.
- **Reuse is copy, template or include — nothing else** (user instruction 2026-09-29). Copy/paste carry the
  document's TEXT; a copied included subgraph pastes inline. The editor never writes a YAML anchor or a `!ref:` for
  reuse. A test that saves a template passes a temporary `templates_dir` — never `~/.streamstudio/templates`.
  (`tests/test_app_clipboard.py`, `tests/test_app_templates.py`,
  `tests/test_subgraph_model.py::TestUsingOneStepsFileTwice`) arch: "Reuse is a copy, a template or an include".
- **Op lists** (`document.OP_LISTS`, derived from `_NAMED_SLOTS`, never typed): members are added and removed only
  through `POST …/steps` / `…/steps/remove`, in the list's own style. A comment on its own line directly above a
  member belongs to that member. Every add, remove, undo and redo re-keys the LATER members' positions and groups,
  and undo/redo answer `shifts`; a new per-name map on the page must follow `shifts`. (`tests/test_op_lists.py`,
  `tests/test_canvas.py::TestOpListsAreEditable`, `::TestAnUndoInsideASubgraphMember`) arch: "An op-list member is
  named by its place".

## The app and its routes

- **No logic in the routes:** each `/api` route parses its request and calls ONE `Studio` method, so phase 5's tools
  cannot drift from the page. All state lives on `app.extensions["streamstudio"]` (`studio_of(app)`), never at module
  level — the catalogue (`catalogue.CATALOGUE`, see "The canvas page") is the one exception.
- The graph list leaves out hidden files, `*.layout.yaml`, `*.steps.yaml` and `*.settings.yaml` (File ▸ Open refuses
  a settings file); a file is read before it enters the registry. (`tests/test_app_files.py`,
  `tests/test_graph_settings.py::TestTheSettingsFile`)
- **The settings form:** `requires` (`document.GATE_PARAM`) is never a field, but stays in the MODEL (the gate wire
  and `card_height` read it). `GET …/app` problems are never added to `Document.check()` — a SETTINGS FILE's problem
  is (it stops a host loading the graph; next section). The page narrows to the
  `app:` fields — a graph without an `app:` block shows none and says so — until *all parameters* is ticked.
  (`tests/test_app_form.py`, `frontend/src/__tests__/settings.test.ts`)
- **Pickers are derived in `palette.picker_of`** (a stamp, then the HuggingFace names, then file/folder names) —
  never imported from `bridge`. The HuggingFace lookups are called through the package at call time so tests patch
  them; no test touches the network. (`tests/test_pickers.py`)
- **A plugin's LIST beside a stamped field** (`lookups.py`, 2026-10-08): a `streamstudio.plugins` module's `LOOKUPS =
  {tag: fn(values) -> (names, reason)}`; the palette marks a stamped field whose tag a plugin lists `lookup: true`
  beside its `picker` (a built-in picker kind never), and `GET /api/lookups/<tag>` answers `lookups.answer` — NEVER an
  error: no plugin, a raise and a list slower than `TIMEOUT_S` (a daemon thread) each answer `[]` with the reason. The
  table is read once per process (`_FOUND`) — like `catalogue.CATALOGUE`, it describes the installed packages, the same
  for every app a process mounts; tests replace `_FOUND`. A plugin that fails to import is a `warning`. The page sends
  the node's text/number/switch values and keys its cache on all of them (`pickers.pluginLookupKey`). The canvas
  modules still name no vendor. (`tests/test_plugin_lookups.py`, `frontend/src/__tests__/{pickers,api}.test.ts`)
  arch: "A plugin supplies the list beside a field".
- `POST …/slot` replaces one slot's value as one edit; a list slot or a typed parameter is refused.
  (`tests/test_app_slot.py`)
- The canvas modules import neither navigaitor, nor annotaide, nor ComfyUI. The canvas editor's tests use synthetic
  documents only — a graph SHAPED like a domain's (`test_canvas.py`'s `PANE_SHAPED_TEXT`), never the domain's file; a
  domain's shipped graphs are pinned in that domain's own suite. Only the ComfyUI editor's tests still read one, until
  phase 4 step 3 deletes them (user ruling 2026-10-08).

## A graph's settings file (`settings.py`, user design 2026-10-05)

arch: "A graph's settings live in a file of their own"; pins `tests/test_graph_settings.py`.

- **A SETTING (a field the `app:` block names) never goes into the graph:** `Document.set_param` writes it into
  `<graph>.settings.yaml` (`settings.set_setting`: its line replaced, else appended; comments kept); every other
  parameter is a graph edit. The model shows each setting on its node (`document._apply_settings` — confluid delivers
  a class line only at its `settled` stage, after the `document` stage the model is derived at); the text tab shows
  the graph.
- **Composition is confluid's `include:` rule and nothing else** — the held, never-written document
  `include: [<graph>, <settings>]` (`settings.composed`), loaded with `texts=`. Root ruling "A configuration utility,
  not a compiler": never a second precedence rule, a new spelling, or a walker into confluid for settings.
- **A setting names its node:** a flow step `flow.<step>.op.<param>`, a top-level object `<key>.<param>`, any other
  node `<Class>.<param>` only while that class is the node's alone (user pick); and a parameter ONE node of the whole
  drawing has may be written BARE (`floor_db: -60.0`, user question 2026-10-14) — confluid's own broadcast of a bare
  key reaches exactly that node (measured on two review graphs), so `Declared.bare` maps it to its field and the
  editor sets a bare line in place. A declared parameter two or more nodes have is refused bare, naming the full keys
  (`Declared.ambiguous`). Everything else is REFUSED at its `file:line:col` (`settings.read_settings`): a key that is
  none of the graph's settings, a class two unnamed nodes share, a `flow:` block (confluid moves the step to the end of
  the flow). Never fall back to confluid's warn-and-ignore. `settings.load_graph` is what a host runs: `SettingsError`
  for the settings file's problems only.
  (`tests/test_graph_settings.py::TestLoadingAGraphWithItsSettings::test_a_parameter_one_node_alone_has_may_be_written_bare`)
- **`app: {graph: settings-only}`** refuses every change but a setting: `@_structural` on each structural `Document`
  method refuses FIRST (before its own checks); `_commit` / `replace_text` refuse any other change of the graph
  (`_refuse_locked`). A new structural method gets `@_structural`. `GET …/app` answers `graph`.
- **Save As takes the held settings along** (`<new>.settings.yaml`, refused when one is there unasked); the old
  settings file stays. Removing or renaming a node a setting names is refused while the setting is there.

## Mounted in a host

arch: "A host mounts the editor through three methods"; pins `tests/test_host.py`.

- `editor_blueprint` takes exactly one of `graph_dir` and `host`. `EditorHost` is a Protocol (`name`, `folder()`,
  `record()`, `saved()`, `shipped()`), no base class.
- **`shipped()`** names graphs the host ships from OUTSIDE its folder: listed under their name while the folder holds no
  file of it (`Studio._scan`), opened with `Document.open_shipped` (the shipped text, nothing unsaved). A settings
  change writes only the settings file into the folder; the first Save of a graph change writes the copy, which wins
  from then on. The shipped file is NEVER written; Revert reads it again. Answers carry `shipped`.
  (`tests/test_graph_settings.py::TestAShippedGraph`)
- **`folder()` is asked at EACH request** — never cache it. A moved folder rebases every held document WITH its
  unsaved texts and history (`Document.moved`); never re-open documents on a move. Relative includes follow the move
  (`document.follow_includes`).
- `record()` runs only a graph with no source of its own, through the texts the editor holds; fills reach top-level
  ops only (`TASKS.md`). Any host failure is a `400` in words naming the host, never an HTML 500.
- `saved()` hears only document files actually written (never the sidecar), New… included. A host that raises does
  not undo the write. Every path handed to the host is spelled under `folder()` as the host spelled it
  (`Studio._spelled`).
- A document holding nothing unsaved is re-read when its file changed on disk; a dirty one never is.
- The host's stop calls `studio_of(app).stash()`.
- **One request at a time** (2026-10-13): every editor request holds `Studio.lock` (an `RLock`) — `_take_turn`, the
  blueprint's FIRST `before_request`, takes it and `_end_turn` (teardown) lets go, a refusal or a raise included;
  `stash()` takes it. A host that reads, changes and saves a document from its own route holds it across all three.
  Never take it in `page_blueprint`, and never hold it while waiting on the page. A route that waits on something
  OUTSIDE the editor and reads none of its graphs is in `app.TURN_FREE` (the catalogue, a class's settings, `types`,
  plugin lists, the HuggingFace routes); `trace`/`rerun` are too, and `Studio.trace` takes the lock itself only to read
  the document and to keep the run — never across the host's `record()` or the run. A new route that reads the graph
  list or a document never joins `TURN_FREE`. (`tests/test_studio_threads.py`)
  arch: "One editor request at a time".
- Importing `streamstudio.app` never changes the host's environment.
  (`tests/test_logflow_colorize_default.py::test_a_host_mounting_the_editor_keeps_its_environment_and_its_colors`)

## The canvas page (`frontend/`)

- `frontend/` is the page's source; its BUILD is committed inside the package, `streamstudio/canvas`
  (`page.OUT_DIR` = vite's `outDir`), with its record `.built-from`, and `package-data` ships it — every install
  serves the page, Node is for editing it. **A change to the page's source commits its rebuilt `streamstudio/canvas`
  in the same commit** (a start rebuilds it, or `cd frontend && npm run build`). The guard fails otherwise — never
  loosen it, never edit the record or the bundles by hand; a merge conflict in the folder is resolved by building
  again. ONE fingerprint (`page.fingerprint`): `npm run build` is `python3 ../streamstudio/page.py begin && npm run
  build:page && python3 ../streamstudio/page.py` — `begin` notes the fingerprint BEFORE vite reads a file, the last step
  records it and REFUSES when a file changed meanwhile (`SourceChanged`; stdlib only — it needs a `python3` command on
  PATH, 3.8+), never a second implementation in the page's tooling. **A server's start runs `npm run build:page`, never
  `build`: a start needs Node and nothing else** (a machine with Node and no `python3` command must still rebuild).
  **Every fingerprint input is a file a clone carries:** `*.local` (`.env.local`, `.env.production.local`) is
  git-ignored and never an input — add an input a clone would not have and CI fails the guard where the developer's
  machine passes it. No source map is built, committed or shipped. (`tests/test_committed_page.py`) arch: "The built
  page is committed inside the package".
- **The page keeps no document of its own.** ONE node component (`src/canvas/GenericNode.tsx`) renders every class
  from the model and `GET …/form`; never a per-class component or a widget table in the page. arch: "The canvas is a
  React Flow page over the document routes".
- The meet rule is the server's (`GET /api/types` = `document.supertypes()`), applied while dragging.
  (`tests/test_app.py::TestTypes::test_the_route_agrees_with_the_documents_own_meet_rule`)
- **A drawn wire is saved by the SERVER, in the document's own spelling, or refused in one plain line** — the page
  sends the two ends (`POST …/wires`, `…/wires/remove`) and draws the answer; it never draws an edge of its own and
  never chooses a spelling. `Document.add_wire` writes `delivered: {<slot>: !ref:<P>}` into a root's output slot,
  `<slot>: !ref:<P>` into any other card's single slot, `delivered: {<entry>: <key>}` from a record-entry output into a
  root's record entry; a record or op-list wire, a flow's, one inside a subgraph is refused (`document.WIRE_*`, the
  verbatim lines). `P` is a TOP-LEVEL key (a `!ref:` names nothing else): a producer inside a slot is moved out to
  `_moved_key` — its CLASS name in snake case (`_snake`: `hugging_face_source`), never the slot's, and never a name that
  is a top-level key, a node or a constructor parameter of ANY card, the slot's owner included (confluid hands a
  top-level key to every same-named parameter) — and its sidecar entries follow (`_rekey` + `_pin`). Every wire edit
  re-derives and must draw exactly the wire asked for (`_drawn`), and a removal may gain only its own open required
  input (`_commit(allow=…)`). A card's record-entry outputs are its built instance's `produces` (a source's, an op's —
  `ReadImage` follows its `output`); the root card draws no parameter rows. (`tests/test_wires.py`,
  `tests/test_app.py::TestWires`, `frontend/src/__tests__/wires.test.ts`, `tests/test_canvas.py::TestDrawingWires`,
  `::TestWiringTheSourceGraph`) arch: "A drawn wire is saved in the document's own spelling", update 2026-10-05.
- **A rooted graph's `inputs` are the HOST card's, never the root's** (user approval 2026-10-05): `_Builder.root_host`
  draws ONE `<host>` card (kind `host`) with an output per declared input — an item-typed one is an entry
  (`entry=<slot>`) a step's read is wired from — and the root has NO outputs; `_Derivation.provided` and
  `_contract_problems` read the declaration from the root's parameters (`_contract_slots`). A root without `inputs`
  draws no card. The card is never a document node: Rename and Delete are refused (`HOST_NOT_A_NODE`) — on the page
  they are GREYED while it is all that is selected, and Delete of a mixed selection leaves it
  (`CommandState.hostSelected`, `levels.withoutHost`) — and a wire from it is not saved (`WIRE_NOT_YET`). **A `host`
  wire** (`_Builder.host_wires`) runs from it into the parameter of that name on EXACTLY ONE card of the delivered
  stream's source chain (`_source_chain`) — the host's own fill-by-shape rule —
  and is never saved; its removal is refused verbatim (`WIRE_FROM_THE_HOST`); the page draws it dashed (`.wire-host`)
  and titles the card `host.hostCardTitle` (`from <host>`, `from the host`). A root's `optional` slots are inputs
  without the required mark, after its record entries, wired and removed like an output, never a problem unwired.
  (`tests/test_wires.py::TestTheFilesGraph`, `::TestTheHostWire`, `::TestAnOptionalSlot`,
  `tests/test_document.py::TestDerive`, `frontend/src/__tests__/{host,model,wires}.test.ts`,
  `tests/test_canvas.py::TestTheFilesGraphsHostCard`) arch: "What a host hands a rooted graph is one HOST card".
- **A class that is not an op is a CARD of its own** (user's design 2026-10-05): `Document.add_node` writes
  `<key>: !class:<path> {<params>}` as ONE edit — the key by the move-out rule (`_free_key`, shared with `_moved_key`;
  never a name a card takes as a parameter), the line above the root and its comment lines (`YamlText.top_key_line`),
  else at the end — and saves a dropped `wire` (one end given, the card the other) in the SAME edit via `add_wire`, or
  adds nothing. `add_step` hands every class whose DOCUMENT kind (`_kind_of`) is not in `_STEP_KINDS` to it: the
  catalogue's kind is never the decider (35 classes it lists as values are ops to the document — `FilterOp`); the page
  sends every add to `POST …/steps` and only places a card (`cards.addPlan`, at the gesture's point by `added`). The host
  card and the root are never an anchor (`add_step` reads such an `after` as none; the page's `steps.selectedAnchor`). A
  flow graph and a subgraph's inside refuse a card verbatim (`CARD_IN_A_FLOW`, `CARD_INSIDE_A_SUBGRAPH`; the user's
  line). `add_node` is `@_structural`. (`tests/test_add_node.py`, `tests/test_app.py::TestAddingACard`,
  `frontend/src/__tests__/{cards,steps}.test.ts`, `tests/test_canvas.py::TestACardOfItsOwn`) arch: "A class that is not
  an op is a card of its own".
- A refusal's or a warning's toast has a **copy** button (`Toasts.tsx`, `toast.copyText`) OUTSIDE the `[data-toast]`
  box: that box's text stays the message alone, which the canvas tests read. (`frontend/src/__tests__/toast.test.ts`)
- A test that derives a graph holding a HuggingFace source never asks the Hub: `tests/conftest.py::no_hub` answers its
  description with nothing; a test needing a dataset's columns patches `described_columns` itself.
- **Undo is two histories:** a document edit is undone by `POST …/undo` — the page never restores text of its own; a
  move, arrange or group is page state with before/after and never enters the document's history. arch: "Undo is two
  histories".
- **Groups live in the sidecar's `groups:` block only**, stored as the page spelled them; the server reads inside
  one only to follow a rename. arch: "A group lives in the layout sidecar only".
- A gesture reads the selection from React Flow AT the gesture (`App.selectedNow`): the selection listener arrives one
  effect late.
- ONE command table (`frontend/src/commands.ts`) drives the menu bar, the right-click menus and the keys.
- Shift+click ADDS a card to the selection (user answer "match", §6c), handled by a capture listener in
  `Canvas.tsx`. (`tests/test_canvas.py::TestShiftClickAdds`, `::TestShiftHeldWhileDragging`)
- The page sends only what the person changed: a rerun sends only values that differ from the run's recorded params
  (`run.changedParams`), Set source… only values changed from the constructor's defaults.
- **The first layout** stacks a column by `layout.card_height`, which must stay an UPPER bound of the drawn card;
  `ROW_DY` stays the minimum pitch and `CARD_GAP` is derived from the tallest shape that fits it. A change to the
  card's rows or CSS re-runs the calibration and updates the constants in the same change. `layout.py` imports no
  document code. (`tests/test_layout.py::TestCardHeight`, `::TestTallCards`, `tests/test_canvas.py::TestFirstLayout`)
  arch: "The graph document is the truth", update 2026-10-01.
- **The page is built at a server's START** (`streamstudio.page.ensure_page_built()`), by the CLI and by a host
  before it announces its server — **never from `create_app`, `editor_blueprint` or `page_blueprint`**. Tests replace
  it (`tests/conftest.py::page_checks`) and never run npm. npm, Node and the disk never stop a start.
  `page_blueprint` decides per request. (`tests/test_page_build.py`) arch: "The canvas page builds itself".
- **The catalogue is asked for on first need** (`frontend/src/catalogue.ts`); the page's mount effect must not call
  `getPalette`. (`tests/test_canvas.py::TestTheCatalogueIsAskedForOnFirstNeed`)
- **The catalogue is built by a helper process, never in the server** (`streamstudio/catalogue.py`): serving the page
  (`page_blueprint`'s `/`) starts it, and nothing else does but a catalogue request. Its list carries no settings
  (`palette.listed`); a class's settings are asked when it is SELECTED (`GET api/palette/<class>`, the page's
  `api.settingsOf`, once per class) — never put them back into the list. A list entry and the full spec agree on kind,
  sockets and problem (`palette._check_settings`). `catalogue.CATALOGUE` is the one piece of module-level state: it
  describes the installed packages, the same for every app a process mounts — one helper per process, stopped at exit.
  A test runs its own helper script (`tests/test_catalogue.py::Helper`) and stops every catalogue it makes.
  (`tests/test_catalogue.py`) arch: "The catalogue is built by a helper process".
- **In a host's panel the page decides nothing the server decides.** Run with no source of its own asks the server,
  which asks the host; pickers come from `schema.picker`; HuggingFace lists are asked only when the person reaches
  for one. `?embed=1` removes chrome, never a function. The `state`/`saved`/`open`/`unavailable` messages are a
  contract with the hosts — change them only together. A host switches graphs with `open`, never by setting the
  iframe's `src`. A gesture belongs to the graph it began on. An unknown `?graph=` opens nothing.
  **`commit`** (2026-10-08) is the host's second command: the page blurs the field being typed in (which writes it;
  on `?only=settings` saves it), waits for every write on its way (`writes`, filled by `commitParam`), and posts
  `committed` — a host READING what the page writes (a tool's run beside a settings panel) asks it first; never answer
  before the writes land. (`tests/test_canvas.py::TestTheHostHearsAndFollows::test_commit_writes_the_field_being_typed_in_and_answers_committed`)
  **`values`** (2026-10-08) is the third: `{graph, values}`, what the host RUNS that graph with over its settings (a
  pane's knobs, applied by name, never written) — the page keeps the last per graph and the settings form grays by it
  (`settings.offReason`); it never writes, saves or marks anything dirty. A field whose schema carries
  `applies_when: {param: [values]}` is grayed (a `fieldset disabled`, `data-param-off`, its title saying why) while
  `param` — the host's value, else the node's, compared as text — holds none of them; never hidden, never refused; a
  malformed declaration grays nothing. An `open` of the graph already shown re-presents it when its TEXT or its MODEL
  changed — a host saving a setting through the editor (`Studio.document(...).set_param` + `Studio.save(only=
  "settings")`) leaves the text as it was (`tests/test_canvas.py::TestTheHostHearsAndFollows::test_a_setting_the_host_saved_through_the_editor_is_shown_when_the_host_opens_it`).
  (`frontend/src/__tests__/{settings,host}.test.ts`,
  `tests/test_document.py::TestDerive::test_a_params_schema_keeps_which_values_of_another_param_read_it`)
  (`tests/test_canvas.py::TestMountedInAHost`, `::TestSetSourceInAHost`, `::TestTheHostHearsAndFollows`,
  `frontend/src/__tests__/{host,link,pickers,slots}.test.ts`) arch: "The page in a host's panel".
- **`?only=settings`** (a host's settings sheet, `link.onlyFromSearch`) renders the settings form and nothing else —
  no bar, canvas, Nodes sidebar, tabs or *all parameters* box (`SettingsForm fixed`) — and SAVES each change at once
  (`App.commitParam`; user pick 2026-10-05), the settings file ALONE (`Document.save_settings`, `POST …/save {only:
  "settings"}`): a change of the graph itself held in another view stays held (user pick 2026-10-05). Dialogs and toasts stay. (`tests/test_canvas.py::TestTheSettingsOnlyPage`,
  `frontend/src/__tests__/{link,settings}.test.ts`)
- Dialogs take the keyboard and give it back (`Modal`).

### Testing the page

- `tests/test_canvas.py` drives the BUILT page (`streamstudio/canvas`) and SKIPS only without it or headless
  chromium. After a page-source change, rebuild (`cd frontend && npm run build`) before claiming the change works, and
  report the passed line, not the skip.
- **Zero console errors is a gate.** The one allowed line is Chrome's `400` in a test that provokes a refusal
  (`REFUSED_400`); a new console error is a failing test, never a new allowed line.
- Tests wait on state, never on time (`data-saves`, `data-answers`, `wait_for_function`). A test that reads the
  catalogue waits on `data-palette-ready` after the gesture that shows it.

## The CLI

- One CLI, two editors: `--editor canvas|comfy` (`EditorName`, `EDITOR_PORTS`). An option of the other editor is
  refused by name, never ignored. Folders are resolved to absolute paths once, in the CLI; `restart` checks the folder
  before it stops anything. Every command a message prints must parse back. A consumer that needs ComfyUI names
  `--editor comfy`. (`tests/test_cli_editor.py`) arch: "One CLI serves two editors".
- `stop`/`restart` target the process LISTENING on the port (`_studio_process`), never the launcher and never a pid
  file; stop with `terminate()` plus a deterministic `wait`, never sleep-and-recheck. (`tests/test_cli_restart.py`)
  arch: "The studio is addressed by its PORT".
- A taken port is refused BEFORE any work: the probe binds with `SO_REUSEADDR`, the address is printed bare with
  `print()` (clickable), and the suggested `--port N+1` command parses back. `tests/test_cli.py` keeps its autouse
  `_port_always_free` fixture. (`tests/test_cli_port.py`)
- The CLI is a liquifai app (`streamstudio.cli:app`, `strict_flags=True` — every command takes plain values, so a
  flag that reaches nothing is an error). Do not restore argparse spellings: booleans are `--x-`, a repeatable option
  is a list literal, `-c`/`-s`/`-d`/`-h` are liquifai's, the palette file flag is `--palette-config`. Command bodies
  coerce the string values themselves (`_as_list` for a repeatable option), and a default that depends on the home
  directory is computed in the body (`default_comfy_dir()`), never in the signature. (`tests/test_cli.py`)
- `python -m streamstudio.cli` does not start from the workspace root (the `liquifai/` folder shadows the package);
  root `TASKS.md`.

## Packaging and discovery

- **Never hand-define a node, op or visualizer here.** A missing op or rendering helper goes to recordstream or
  waivefront; the editor shows what discovery finds.
- Discovery reads INSTALLED entry-point metadata: after changing a `confluid.configurables` (or `streamstudio.*`)
  entry point, reinstall that editable (`uv pip install -e ./<pkg> --no-deps`, or `aisland setup`) — never
  `--reinstall`/`--force-reinstall`.
- A module-scope `import X` (outside `try:`, `TYPE_CHECKING` or a function) means `X` is a declared dependency; a
  guarded import stays undeclared. (`tests/test_packaging.py::test_module_scope_imports_are_declared_dependencies`)
- No requirement, extras included, carries a URL — PyPI refuses it.
  (`tests/test_packaging.py::test_no_requirement_is_a_direct_url`)
- **No package that depends on streamstudio is one of its dependencies** — not even in an extra, not through another
  package (user ruling 2026-10-08: a domain depends on the editor, never the reverse). The walk reads INSTALLED
  metadata: reinstall a stale editable before trusting a pass. (`tests/test_dependencies.py`)
- Every file under `web/`, every file of the built page `canvas/` (never a `.map`), `config.default.yaml` and
  `py.typed` are in `package-data`; `MANIFEST.in` ships the CHANGELOG.
  (`tests/test_packaging.py::test_every_web_asset_is_shipped`,
  `::test_every_file_of_the_built_page_is_shipped_and_no_source_map`,
  `::test_the_wheel_built_from_a_checkout_serves_the_page`, `::test_the_declared_package_data_files_exist`,
  `::test_the_changelog_is_shipped_in_the_sdist`)
- The package speaks its own name only. (`tests/test_packaging.py::test_no_superseded_project_name_in_shipped_code`)

## ComfyUI editor (removed in phase 4 step 3)

Everything below goes with `bridge.py`, `nodes.py`, `export.py`, `graphio.py`, `web/` and `--editor comfy`. Nothing
new builds on it. *(unpinned)* marks a rule whose tests went with `tests/test_bridge.py`, `test_nodes.py`,
`test_export.py` and `test_graphio.py` (deleted 2026-07-23).

**Shape and launch**
- It is a ComfyUI custom-node extension, never a server, executor or frontend of its own: the CLI clones ComfyUI to
  `~/.streamstudio/ComfyUI`, symlinks the package into `custom_nodes/`, and runs `main.py` under the active
  interpreter.
- The package root is lazy about this half: `import streamstudio.app` imports no ComfyUI module and no torch; the
  mappings are made on first read (PEP 562) or at import inside ComfyUI. A test that patches a ComfyUI name reads
  `streamstudio.NODE_CLASS_MAPPINGS` first. (`tests/test_lazy_package.py`) arch: "The package root is lazy".
- `force_no_color()` runs only on the ComfyUI path — at the top of `__init__` when `server` is imported, at the start
  of `_load_comfy()`, and again after node discovery; never unconditionally. Keep the E402 ignore in BOTH
  `streamstudio/setup.cfg` and the root `.flake8`. (`tests/test_logflow_colorize_default.py`)
- The launcher seeds custom nodes from three tiers, deduped declared → `STREAMSTUDIO_CUSTOM_NODES` → `--custom-node`;
  it never updates an existing node; one node's clone or pip failure never blocks the launch; ComfyUI's own
  requirements are synced on every run (`--install-` skips). `restart` re-runs `_run` whole. (`tests/test_cli.py`)
- `--default-nodes-` and `--only-taidal-nodes` are read only through `hide_default_comfy_nodes()` /
  `only_taidal_nodes()` / `node_root()`; the keep-list lives in the palette config file, not an env var.
  (`tests/test_init_discovery.py::test_prune_default_comfy_nodes_spares_keep_list`,
  `tests/test_cli.py::test_main_only_taidal_nodes_exports_both_envs`)
- `web/empty_default_graph.js` swaps ComfyUI's Stable-Diffusion default graph for a blank one, by object identity.

**Discovery**
- Nodes come only from `confluid.configurables` entry points; a class becomes a node only if its category is in
  `_NODE_CATEGORIES`, or it is a runnable or a role/`lazy` member. A missing node is fixed by tagging the class
  upstream, never by special-casing the bridge.
- A second pass reads each module's `__all__` (re-exported classes and registered builder functions).
- Discovery is one-shot per process (the package is imported under two identities).
  (`tests/test_init_discovery.py::test_discover_is_one_shot_per_process`)
- A module that fails to scan is skipped at `debug`; a declared plugin that fails is a `warning`.
  (`tests/test_init_discovery.py::test_discover_skips_failing_module_at_debug_level`, `tests/test_plugin_discovery.py`)
- Plugins: a `streamstudio.plugins` entry point names a module with `ROUTES` and `WEB_DIRECTORY`, registered at import
  time. Vendor integrations are plugins, never bridge special cases. arch: "The plugin seam".

**Node shapes and keys**
- Where a class carries a category that names its shape (`model`, `engine`), the CATEGORY decides; structural probes
  only for untagged callables. The dispatch order in `create_comfy_node` is load-bearing.
  (`tests/test_engine_source_nodes.py`, `tests/test_model_node.py`) arch: "Node-shape classification".
- An op is the `@configurable(category="op")` tag with its `__call__` resolved through the MRO — never an annotation
  sniff. *(unpinned)*
- Node keys are `_node_key` (`Taidal_<package>_<category>[_<group>]_<callable>`), globally unique. Display names are
  verbatim (`__taidal_display_name__` may override), `_`-prefixed callables are hidden, and the palette path groups
  by role/task or category with a `[PKG]` label tag. *(unpinned)*

**Parameters and coercion** *(unpinned)*
- `_map_type` classifies the RESOLVED type structurally — never by substring. Stringized annotations are resolved
  first. Only a 2-element or variadic numeric tuple is a `__lo`/`__hi` range.
- A nested container rides a `RECORDSTREAM_VALUE` socket fed by a Value node; a document literal for it is imported
  as a Value node. (`tests/test_value_literal.py`)
- Every INT/FLOAT widget carries full-range `min`/`max` (the frontend clamps to `[0, 2048]` otherwise); an
  `Interval` mark narrows it. A `Literal` is a combo enumerated from the annotation.
- `Optional[X] = None` renders a blank "use the default" widget (a three-state combo for a bool, a blank first entry
  for a `Literal`) — never a forced value. A nullable numeric is a blank STRING whose converted socket accepts a
  numeric wire (`web/nullable_numeric_socket.js` plus the backend validator — both are required).
- `_coerce_kwargs`: a `Union` with a bare `str`/`int` arm is never comma-split; `"()"` decodes to `()` (a `("()",)`
  is truthy and silently overrode metadata); containers decode from JSON, Python literals, or comma strings.
- Tooltips come from the docstring `Args:`; widget kinds from the `__taidal_widget_params__` stamp.
  (`tests/test_model_node.py::test_stamped_widget_param_is_tagged`)
- HuggingFace pickers are gated on the `HuggingFace*` class name; `split` there is free text, distinct from
  `DatasetSplit.split`. A picker keyed off another widget refreshes in `onConfigure`; `hf_dataset_chooser.js` has no
  such hook. (`tests/test_hf_choosers.py`)

**Sockets**
- Object sockets are role-qualified and task-agnostic (`RECORDSTREAM_OBJECT:<role>`); the object-node output side and
  the runnable-slot input side agree on `_QUALIFIED_OBJECT_ROLES`. Retag is the visible escape hatch.
- An object socket also claims its engine inside a comma union with the bare role: exclusion requires two claims.
  The backend validator and `web/framework.js` apply the same predicate, and `_fallback_compatible` implements the
  comma-union intersection. (`tests/test_framework_sockets.py`)
- A wired-`Any` op parameter named after a strict slot role gets that role's socket. (`tests/test_value_node_wiring.py`)
- The record socket stays the generic `RECORDSTREAM_RECORD`. Item types get derived `RECORDSTREAM_ITEM_<NAME>`
  sockets and a generated `<Item> Type` family, built after discovery; outputs are APPENDED, never inserted (saved
  canvases address outputs by position). (`tests/test_contract_pipeline.py::TestItemSocketTypes`, `::TestTheItemTypeNodeFamily`)
- Optional sockets are marked by `web/optional_inputs.js`, cosmetically only.

**Runnable, object, value and model nodes**
- A runnable node runs on execute; it is dual-mode — a node whose output feeds a combinator's runnable slot is built,
  not run (`_runnable_consumed_as_instance`). (`tests/test_runnable_workflow.py`)
- Every declared `@output` is a static, wireable socket; a container output is a JSON `STRING`, a path-named output
  a plain `STRING` (never a picker). There is no results panel.
  (`tests/test_framework_sockets.py::test_a_container_output_is_still_a_json_string`,
  `tests/test_model_node.py::test_path_named_output_is_string_socket`)
- `run_runnable` restores autograd for a runnable declaring `__needs_autograd__` (`tests/test_run_runnable.py`); runs
  on a worker thread so the interrupt button works; streams output live, stripping ANSI on the widget copy only;
  drives ComfyUI's progress bar through `set_progress_callback`. All ComfyUI knowledge stays here. *(unpinned except
  autograd)*
- Object nodes have two outputs, `instance` and `class`; object-typed constructor params are input sockets, and a
  list-of-objects param is a `<name>_N` family. *(unpinned)*
- A no-arg `__call__` class is a value node; a bare-`Any` param on it is wired, and every value node APPENDS an
  `object` output. A wire into a parameter is folded by building the producer with its own wires resolved.
  (`tests/test_value_node_wiring.py`, `tests/test_contract_pipeline.py::TestWhatAWireBECOMESInTheDocument`)
- A `category="model"` class is dispatched before the op check; a `__taidal_model_params__` param is a wired model
  socket. (`tests/test_model_node.py`)
- Adapter and viewer nodes in `nodes.py` are glue: the rendering and analysis math lives in recordstream/waivefront.

**Export and import**
- One compiler per artifact — `export.py` (the `flow:` document) and `graphio.py` (the whole graph) — each shared
  verbatim by its route, its canvas menu and its CLI command; never fork one. The canvas compiles to `flow:` only;
  do not reintroduce a linearizing export or context cells. (`tests/test_export_flow.py`)
- An unregistered `Taidal_*` node refuses the export; a bare ComfyUI node still drops.
  (`tests/test_missing_node_refusal.py`) arch: "An unregistered `Taidal_*` node is a refusal".
- A decision is drawn as outcome sockets and `Join branches`, and folded by the one `export.fold_branches`; the
  canvas never shows `requires`. (`tests/test_branches.py`) arch: "A decision is DRAWN as outcome sockets".
- A flow document declares its own subgraph boundary — never infer one. (`tests/test_flow_subgraph.py`) arch: "A
  subgraph boundary is DECLARED".
- A template's App view is declared in its source `app:` block. arch: "The App view is declared in the source document".
- *Extract as Op* (`Taidal_SubgraphOp`) compiles its record-ancestor cone from the hidden `PROMPT`; its `record`
  input stays LAZY — pulling it would run a source-less op chain on `None`. *(lazy input unpinned; the compile:
  `tests/test_flow_subgraph.py`)*
- Compiling a subgraph to an op is a COMMAND (`taidal.compileToOp`), never a lifecycle hook; it reports, and is
  re-runnable. The canvas keeps the shape the user drew (*Build Record*); `export.expand_record_builders` translates
  it. (`tests/test_stream_op_extension.py`, `tests/test_record_builder.py`)
- `--as_ pipeline` turns a contract document into a starting canvas with only the `Stream`'s `source` left open.
  (`tests/test_contract_pipeline.py`)

**Launching a graph (`web/open_graph.js`)**
- One ComfyUI workflow document per graph (`workflows/taidal/<workspace>/<stem>.json`); the save mirror is
  path-keyed, `<stem>.app.json` included. A graph already on screen is identified by its document path, never by
  its node types. (`tests/test_open_graph_mirror.py`) arch: "A graph that is already open".
- The timing rules in `open_graph.js` (`setup()` never awaits the load; wait on observable state, then verify) are
  measured — do not simplify them. `taidal_view` sets the face on the workflow object and toggles only on a measured
  mismatch. (`tests/test_open_graph_mirror.py::TestTheRequestedView`)
- The preview nodes' record input is lazy and never pulled; a refusal is an image that says why.
  (`tests/test_preview_nodes.py`) arch: "The App view's Run for a pane graph".

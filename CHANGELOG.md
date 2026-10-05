# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Set source… keeps the keyboard while a picked class's settings load.** The dialog's box holds the focus from the
  pick until the first field can take it, so Escape cancels in between (before, the focus was on the page there).
- **A class that is not an op is a card of its own.** Add Node ▸, the double-click palette, a Nodes-sidebar entry and
  the palette of a wire dropped on empty canvas write a value, a source, an engine or a sink as a NEW top-level key
  named after its class (`class_names_scan: !class:recordstream.ops.contract.ClassNamesScan {}`, `_2` … when taken),
  directly above the graph's root and its comment lines (at the end of a graph without a root), as ONE edit, and draw
  the card where the menu, the drop or the wire's release was. A wire dropped on empty canvas is saved to the card in
  the same edit — from the root's `classes`, `classes: !ref:class_names_output` — or nothing is added. A flow graph
  refuses a card in one line (`The editor can't add a value card to a flow graph yet — pick an op.`). New route
  `POST /api/graphs/<name>/nodes {class_path, params, wire?}`; `POST …/steps` takes `wire` and hands a class that is
  not an op to the same edit. The host card and the root are never what an added class goes after.
- **The host card's Rename… and Delete are greyed**, and Delete of a selection holding it removes the other cards.
- **A refusal's or a warning's toast has a copy button** that copies the whole message.
- **A graph's settings live in a file of their own, `<graph>.settings.yaml`.** A value set in a field the graph's
  `app:` block names — on the canvas or in the settings tab — is written there, one line per setting
  (`flow.second.op.level: 0.9`), and never into the graph; every other edit goes to the graph as before. The editor
  shows each setting on its node; Save writes only what changed; Save As takes the settings along. A setting names
  its node: a flow step by its name, a top-level object by its key, a node with no name by its class while the class
  is that node's alone. A line that is none of the graph's settings is refused where it is written
  (`…/view_pane.settings.yaml:2:1: flow.second.op.levle is not a setting of view_pane — its settings are: …`), in the
  editor's Check and in `streamstudio.settings.load_graph`, which loads a graph with its settings over it.
  `GET …/app` answers `graph` (`editable` or `settings-only`), and an `app:` entry naming a class two nodes with no name
  share is a located problem with no field.
- **A host may ship graphs from outside its folder** — `EditorHost.shipped()` (a host now has four methods): such a
  graph is listed (`shipped: true`) and opened while the folder holds no file of its name; a settings change writes
  only its settings file into the folder, and the first Save of a change to the graph itself writes the folder's copy.
- **A graph may offer its settings only** — `app: {graph: settings-only}`: the editor refuses every other change
  (`locked offers its settings only — its graph is fixed where it is shipped`).
- **`?only=settings`** opens the page on the graph's settings and nothing else — no menus, canvas or tabs — and saves
  each change at once, the settings file alone (`POST …/save {only: "settings"}`): the form a host's settings sheet
  holds. A change of the graph itself held unsaved elsewhere stays unsaved.
- **The catalogue is built by a helper process, and a class's settings arrive when you select it.** Serving the
  editor's page starts `python -P -m streamstudio.catalogue`, which imports every installed package that declares
  classes and describes them; the editor itself no longer imports them (its memory after the catalogue: 0.13 GB,
  before 1.17 GB). `GET /api/palette` answers every class WITHOUT its settings (0.60 MB, before 11.7 MB) and `503`
  when the helper stopped before answering; `GET /api/palette/<class path>` answers one class with them. Set source…
  and the Nodes sidebar's card ask for a class's settings when it is selected and say `loading the settings…` until
  they arrive. Measured on a busy machine: the list 31 s after the editor opened (before: 55 s), at once when asked
  after the helper has answered.
- **A wire drawn on the canvas is saved** — in the document's own spelling, as ONE edit held until Save, one undo:
  `delivered: {classes: !ref:hugging_face_source}` into an output slot of the graph's root, `thing:
  !ref:hugging_face_source` into any other card's slot or setting, `delivered: {input: image}` from a source's record
  entry into a record entry of the root. A card written inside another card's slot (`stream.source`) moves out to a
  top-level key named after its class first (`hugging_face_source` for a `HuggingFaceSource`, or
  `hugging_face_source_2` when a top-level key, a card or a setting of any card — the slot's own card included — has
  the name), keeping its place and its groups. Click a wire
  and press Delete to take it out (several at once are one edit). A wire the editor cannot save yet is refused with one
  line and is not drawn: `This wire would change the order of the steps — the editor can't save that yet.` (a
  Pipeline's record wires, a wire into an `ops` member), `The editor can't save wires in this kind of graph yet.` (a
  flow). Routes `POST /api/graphs/<name>/wires` (answers `wired`, the card the wire comes from now) and
  `POST /api/graphs/<name>/wires/remove`; `Document.add_wire`, `Document.remove_wires`.
- **The graph's root card shows only what you connect** — one input per output slot, one per record entry
  (`input · Image`, `target · Label`) and one per optional slot (`optional: {classes: "List[str]"}`, without the `*`:
  never a problem while unwired), no rows for its declaration, no outputs, and the contract's name on its second line.
  A source card has one output per record entry it declares (`produces`: `image · Image`, `class · Label`), which
  follow its settings, and so does a step (`ReadImage`: `image · Image`).
- **What a host hands a rooted graph is one host card** — `from <host>` in a host's panel, `from the host`
  standalone — with an output per input the root declares (`files · List[str]`; a pane's `input · Signal` is an entry
  its steps' reads are wired from); it was drawn as outputs of the root. Where the host puts an input is a DASHED wire
  into the one card of the delivered stream's source chain that takes a setting of that name (the files graph:
  `files` → `file_source.files`); it is never saved, and deleting it is refused: `The host fills this when it runs the
  graph — it isn't a wire you can remove.` Rename and Delete of the host card are refused (`<host> is the host's side
  of the graph, not a node of the document`). Every rooted pane graph gains the card (the Bluetooth pane: 31 nodes).
- **The Nodes sidebar is a tree that folds at every level** — package ▸ kind ▸ the class's `@configurable` group, one
  folder per part of the group path (`group="fourier/numpy"`: `▸ Ops ▸ fourier ▸ numpy`). Every folder shows its
  count and starts folded; sub-folders come before the classes that sit in a folder directly, and a class without a
  group sits straight under its kind. The folders opened are remembered in the browser; a search opens every folder
  on the way to a match.
- **The canvas editor's page ships built, inside the package** — `streamstudio/canvas` (`index.html`, its js and css,
  and `.built-from`, the record of the source it was built from), committed and in the wheel. Every install serves the
  page: an install from GitHub, which carries no `frontend/`, answered `/` with `The canvas page is not built. Build it
  once — …`, and now serves the editor; a checkout without Node printed `streamstudio: the editor's page is not built
  and Node is not installed — run aisland setup`, and now starts silently. Node is needed only to edit the page: a
  checkout whose page source changed rebuilds it at its start, as before, into `streamstudio/canvas` — commit that
  folder with the source change. `tests/test_committed_page.py` fails when the committed page was not built from its
  source: ``the committed editor page is older than its source — start streamstudio once, or run `cd
  streamstudio/frontend && npm install && npm run build`, and commit streamstudio/streamstudio/canvas``. `npm run
  build` now writes that record too, so a page built by hand is current: it is `python3 ../streamstudio/page.py begin
  && npm run build:page && python3 ../streamstudio/page.py` — the source is noted before vite reads a file and the
  record is that note, refused (`the editor's page's source changed since its build began — build again`) when a file
  changed meanwhile. A server's start runs `npm run build:page` alone, so it needs Node and nothing else; the hand
  build also needs a `python3` command. The record covers only files a clone carries: `.env.local`,
  `.env.production.local` and every `*.local` are never inputs. No source map is built. `streamstudio.page` adds
  `OUT_DIR`, `built_folder()`, `begin_build()`, `record_build()` and `SourceChanged`; `web_dist()` answers
  `streamstudio/canvas`. The page's source is checked out with LF line endings everywhere (`frontend/.gitattributes`):
  the record is a fingerprint of its bytes.
- **The canvas page builds itself when a server starts and its source changed since its last build.**
  `streamstudio <folder>` — and an app that mounts the editor, through `streamstudio.app.ensure_page_built()` called
  at its start, before it announces its server — prints `streamstudio: the editor's page changed since its last build
  — building it` (or `… is not built yet …`), runs `npm ci` first when `frontend/node_modules` is missing or
  `package-lock.json` is newer than npm's last install, runs `npm run build:page`, and prints `streamstudio: built the
  editor's page in 2.0 s`. A page that is current costs a 2 ms check and prints nothing. Without Node, or when the
  build fails, the server still starts: the start prints `… is out of date and Node is not installed — run aisland
  setup` or `building the editor's page failed — npm run build:page exited with code 2; its last lines:` with npm's
  last 20 lines, and the page at `/` says the same instead of serving the old page. A page built by hand (no record of its
  source) is served as it is when there is no Node to rebuild it. npm may take 300 s for `npm ci` and 120 s for the
  build; past that it is stopped with everything it started (`… npm ci did not finish in 300 s`). Ctrl+C during the
  build stops npm and ends the start with `streamstudio: interrupted before the editor's page was built — the next
  start builds it` (exit code 130). What the page was built from is recorded in `streamstudio/canvas/.built-from`; two starts
  from one checkout never build at once on macOS and Linux — the second prints `another start is building the editor's
  page — waiting for it`. A start that could not build the page does not try again in the same process, and
  `streamstudio.app.page_check_environment()` hands its answer to a server process it starts. The editor's own start
  asks for its port again after the check. Making an app never builds anything, and an install from a wheel is never
  checked.
- **The page served instead of the editor tells a host's panel.** The placeholder, the no-Node page and the
  failed-build page post `{source: "streamstudio", type: "unavailable"}` to their parent when opened with `?embed`, so a
  host that covers its panel until the editor answers can show them. The placeholder's list of routes is escaped (a
  route ending `<template>` hid the rest of the page).
- **`page_blueprint` looks for the built page at every request**, not once when it is made: a page built after the app
  was made is served without a restart — unless this server's start could not build it, in which case `/` keeps
  saying why until the next start.
- **`import streamstudio.app` no longer runs the ComfyUI node discovery**: 0.2 s instead of 6.2–8.0 s, and no
  torch. The ComfyUI mappings are made when ComfyUI reads them (and at import inside ComfyUI); `streamstudio`
  commands start in 0.1 s. It also leaves the importing app's environment and logging alone: `NO_COLOR=1` (and
  loggair's colourless reconfiguration) is set only inside ComfyUI and when the ComfyUI half is made — a host that
  mounts the canvas editor, and every process it starts, keeps its console colours.
- **`page_blueprint()` without an argument serves the built page** (`web_dist()`), not the placeholder.
- **The first graph list no longer imports every installed package for one class name.** A document naming a
  class by its registry key (`!class:AnnotationUpdateSink`) now imports the installed entry points one at a time —
  those of packages already loaded first — and stops at the one that registers it. Measured in a host app over a
  sink graph: the first `GET /studio/api/graphs` 6.0 s → 1.1 s (15 of 127 entry points imported; 13.9 s on a cold
  disk before). A failing entry point is skipped with the warning line it always had, and none is imported twice. A
  package another request is still importing (the catalogue imports every package in its own request) is waited
  for, never reported as missing. A short name two installed classes share (`FourierOp`) is drawn with the first one
  the lookup imports until the catalogue is opened; its dotted path (`waivefront.fourier.FourierOp`) works everywhere.
- **The page asks for the catalogue (`GET api/palette`) the first time something lists it** — the Nodes sidebar,
  Add Node ▸, the palette, Set source… — not as it opens. Building it imports every installed package (6.5–8.4 s),
  and asked beside the first graph list it made that list wait too (7.2 s instead of 1.2 s). Until it arrives the
  four show `loading the catalogue…`; `data-palette-ready` still says when it has.
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

- **A graph opened for the first time no longer draws a card over another.** Within a column a card now starts
  below the card above it — 200 px below that card's top for a card up to 183 px tall (six rows; five on a subgraph
  card, whose bar of inner steps adds 26 px), further for a taller one (its height plus 17 px, on the canvas's 10-px
  grid). The Bluetooth pane, opened without a positions file:

  ```
                 known (373 px tall)   contract
  before         (40, 40)              (40, 240)   only the last 8 px of contract showed
  after          (40, 40)              (40, 430)   17 px below known
  ```

  Over 32 shipped graph levels, 8 pairs of cards overlapped on a first open before, none after; 25 of the 32 are
  laid out exactly as before, as is any graph whose cards are all six rows or shorter (five on a subgraph card). A
  node added later starts below the lowest card already placed, no longer 200 px below its top.
- **A problem in a folder whose name has a space keeps its line.** Check graph on a graph in `…/with space/demo.yaml`
  whose contract is written before its flow answered the location `…/with space/demo.yaml` — no line — while the
  message said `… Failed to construct GraphContract at …/with space/demo.yaml:1:11: …`; the same graph in a folder
  without a space was at `…/nospace/demo.yaml:1:11`. Now both are at line 1, column 11 of their `demo.yaml`. The
  same for an error confluid finds while loading (a key written twice: `duplicate key 'flow' at …/with
  space/demo.yaml:5:1`), in the graph or in an included steps file, saved or not.
- **A missing steps file is reported as missing from the graph that includes it, searched for in the graph's
  folder.** A graph `graphs/demo.yaml` whose subgraph says `steps: {include: prep.steps.yaml}`, with no such file,
  answered from an editor started in `~/work`:

  ```
  before  …/graphs/demo.yaml: does not load: ConfigFileNotFoundError: ~/work/string.yaml includes prep.steps.yaml:
          Not found: prep.steps.yaml (searched: ~/work/prep.steps.yaml, ~/work/prep.steps.yaml,
          ~/work/config/prep.steps.yaml, …)
          at …/graphs/demo.yaml
  after   …/graphs/demo.yaml: does not load: ConfigFileNotFoundError: …/graphs/demo.yaml includes prep.steps.yaml:
          Not found: prep.steps.yaml (searched: …/graphs/prep.steps.yaml, ~/work/prep.steps.yaml,
          ~/work/config/prep.steps.yaml, …)
          at …/graphs/demo.yaml:5:18 — the include line
  ```

  `string.yaml` never existed. The graph now reaches confluid as its own file, with every text the editor holds,
  so every include is looked for beside the file that names it, whatever folder the editor was started in. A graph
  opened through a symbolic link now names its real file in its nodes' locations (`real/demo.yaml:3:9`, where it
  said `link/demo.yaml:3:9`), as its steps files already did. A graph that is a symbolic link to a FILE
  (`served/demo.yaml` → `shared/demo.yaml`) reads its steps files from the real file's folder (`shared/`), no longer
  from the link's (`served/`) — the canvas, Check graph, a run, Copy and Save as template alike. Before, a
  `served/prep.steps.yaml` beside the link was the one the canvas showed and Copy took; with none there the graph
  did not load.

  The same location for a missing steps file spelled with `~`, and for an include that names a folder:

  ```
  include: ~/nope.steps.yaml   before  at …/graphs/demo.yaml       after  at …/graphs/demo.yaml:5:18
  include: adir.steps.yaml     before  at …/graphs/demo.yaml       after  at …/graphs/demo.yaml:5:18
  (a folder)
  ```
- **A YAML error in an included steps file is located in that file.** A graph `demo.yaml` that includes
  `prep.steps.yaml`, whose brace on line 2 never closes, was reported by Check graph at `…/demo.yaml:3:7` — the
  graph with the steps file's line — while the message named `prep.steps.yaml`. It is now at
  `…/prep.steps.yaml:3:7`, also for a steps file you have not saved yet; an error in the graph's own text still
  names the graph. The message is unchanged.
- **A steps file you have not saved never touches the disk.** After Move to its own file…, every check, edit and
  run wrote `prep.steps.yaml` into the graph folder for the length of the load and removed it after, and with the
  folder read-only every edit was refused (`does not load: PermissionError: [Errno 13] Permission denied:
  '…/prep.steps.yaml'`). The editor now hands the files it holds to confluid as text (`confluid.load(...,
  texts=)`): during 5 checks and 5 edits the folder lists `['demo.yaml']` only, a read-only folder checks, edits and
  runs, and Save writes `prep.steps.yaml`. The editor needs confluid 0.4.0, the release whose `load` takes `texts=`.
  On a disk that ignores case, an include respelling a steps file not saved yet (`PREP.steps.yaml` for
  `prep.steps.yaml`) is refused until Save.
- **A Save the disk refuses for a steps file no longer rewrites the graph.** After Move to its own file… into a
  read-only `parts/`, Save answered `…/parts/prep.steps.yaml cannot be written: Permission denied` — with `run.yaml`
  already rewritten to include that file, so the graph on disk no longer loaded (`ConfigFileNotFoundError`); Save As
  wrote the new graph the same way. Save and Save As now check the graph and every steps file first and write none of
  them when one cannot be written, and the graph's own file is written last.
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

- **The editor mounts in another app with a HOST** (`editor_blueprint(host=…)`): an object with a `name` (answered by
  `GET /api/workspace` as `host: {name}`, `null` standalone) and three methods —
  `folder()`, asked at each request, so a host that moves its folder is followed and the graphs held unsaved move
  with their edits and undo history; `record(graph, path)`, the record a **Run** of a graph with no source of its
  own runs (a `flow:` alone, a `Stream` with an empty `source`), with the window and band the host fills into the
  ops — the run answers `record: null` and `host: {label, filled}`; `saved(paths)`, told which graph files every
  Save, Save As and New… wrote. Every path the host is handed is spelled under `folder()` as `folder()` spells it.
  Whatever `record()` or `folder()` raises (but a `ValueError`, the host's own refusal, answered as written), and a
  `record()` answer that is no `HostRecord` of a mapping, is a 400 in words naming the host. A graph holding nothing
  unsaved whose file changed on disk is read again when it is next asked for; relative `include:` entries out of a
  folder the host moves are rewritten to name the same file. A graph whose own source yields no record says so in
  words (a source a host fills when it runs the graph is not handed to the editor). `studio_of(app).stash()` keeps
  the unsaved work across the host's own stop. Without a host such a Run is refused as before.
- **`GET /api/graphs/<name>/app`** — a graph's `app:` fields: `{declared, fields: [{node, param, loc}], form,
  problems}` (the Bluetooth pane: 30 fields instead of 247; `deviation_hz__lo` / `__hi` name one field). The gate
  a branch sets (`requires`) is never a field, in this form or in `GET …/form`.
- **`POST /api/graphs/<name>/slot {node, slot, class_path, params}`** — put a new object into one slot
  (`stream.source`), replacing what it held, comments kept, one undo; answers `set` (the node now in it).
- **Pickers.** A parameter's form schema carries `picker` — `directory`, `file`, `hf_dataset`, `hf_feature`,
  `hf_split`, `hf_metadata`, or a producer's stamped tag — by the rules the ComfyUI nodes' widgets followed;
  `GET /api/files?show=all` lists every file for a file picker (and a `$VAR` in `dir` is expanded; one this machine
  does not have is a 404 naming it: `DATA_ROOT is not set on the editor's machine ($DATA_ROOT/captures)`);
  `GET /api/hf/datasets`, `…/features`, `…/splits` answer the HuggingFace lookups.
- **The page in a host's panel.** It works under a host's prefix (`/studio/`); `?embed=1` fits it into a panel (no
  logo, a narrower side panel, the minimap off until asked; every menu and function kept); it posts `state` and
  `saved` messages to its parent window (`saved` after Save, Save As and New…) and takes an `open` command (same
  origin only). An `open` first writes a value typed and not left yet to the graph it was typed in, closes Set
  source…, reads the graph shown again when it is the one asked for, and asks the editor's folder afresh (New… does
  too), so a folder the host moved is the one named and written to. A graph's top level opens whole in the panel (a
  fit, never above zoom 1). Run on a graph without a source of its own runs the host's record (`record: <host>'s
  selection`) and names what ran and what the host filled in; *rerun this node* starts from, and keeps, the values
  the host filled in.
- **`?tab=settings|run`** opens a side tab from a link, and follows the tab shown. **`?graph=` naming a graph the
  editor does not list opens nothing** and says so, with the graphs there are — it used to open the first graph.
- **The settings tab lists a graph's `app:` fields**, with a tick box *all parameters* that stays for the graph while
  another tab is looked at; a `(min, max)` pair is one field of two numbers, and a box being typed into keeps what is
  typed while the other box's answer arrives. A graph without an `app:` block names no settings: the tab says so, and
  *all parameters* lists every one.
- **Set source…** — the `set…` button on a Stream's `source` row, the node menu, or a wire dragged out of the slot:
  a class (sources first) and its form, written by `POST …/slot`. The keyboard stays in it (the first field after a
  pick; the highlighted class in view), and a slot holding a reference says the referenced node stays, unwired.
- **Pickers on the page**: a `…` file browser beside folder and file fields, and lists of the cached HuggingFace
  datasets, a dataset's splits and columns beside a HuggingFace source's fields — asked only when you point at them;
  an empty list says why, and a failed one asks again the next time. Every dialog takes the keyboard when it opens and
  gives it back when it closes: Escape in the file browser over Set source… closes the browser alone.

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

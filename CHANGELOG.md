# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The file browser has a row of buttons that jump to a folder — the graph folder, then `HOME`, `PROJECT_ROOT`,
  `DATA_ROOT`, `MODEL_ROOT`, `RUN_DIR` when set — and, in Save As and the folder browsers, **New folder**
  (`POST /api/files/folder`).
- A host shows a line under a setting (`hints`: what it measured for it) and hands the settings form its own font
  (`style`) — two more host messages.
- A plugin's list ends in **↻ ask again**: it asks the plugin again, so what changed while the page was open (a model
  packaged since the list was first opened) shows without reloading the page.
- **View ▸ Arrange diagonally** and **View ▸ Arrange top to bottom** lay the graph out by the cards' sizes beside
  **Arrange left to right** (the former *Arrange*): diagonally, each column of steps starts below the tallest card of
  the column before, so a chain runs from the top left to the bottom right. All three are on the empty canvas's
  right-click menu too.
- On a level whose wires mostly run down (after *Arrange top to bottom*), each card's input sockets sit on its top edge
  and its output sockets on its bottom edge, so a wire runs straight from one card to the card below it.
- The page's `state` message to its host carries `asking`: true while the page asks about the last session's unsaved
  work, when no graph opens until the person answers — a host that covers its panel while a graph loads uncovers it.

### Changed
- A class names its parameters' picker tags in the class attribute `__streamstudio_widget_params__`
  (`{"dataset": "SHOP_DATASET"}`): the pickers and a plugin's lists read it.

### Fixed
- A graph's settings file another program wrote is read again even while the graph holds other unsaved work (a
  moved node): a setting changed in the settings sheet, or a reset, no longer starts from the old text, is no
  longer refused as changed on disk, and is no longer kept as unsaved work that a restore shows with the other
  settings at their defaults.
- A setting that takes a number or text (a number written with its unit, `2.5 MSa/s`) is a text box: a number box
  dropped the unit, so `200 kSa/s` was saved as `200`. A plain number typed there is still saved as a number.
- A graph a host ships that `include:`s a file shipped beside it opens with that file's nodes: its relative includes
  are rewritten to reach the shipped file from the host's folder. Before, it loaded no node and every `app:` entry
  was refused.

## [0.1.0a1] — 2026-10-15

First public pre-release: a graph editor for [RecordStream](https://github.com/Gearlux/recordstream) pipelines written
as [confluid](https://github.com/Gearlux/confluid) YAML documents. The YAML file is the graph — the editor draws it,
and every change is a patch of the line it is about. It runs on its own (`streamstudio <folder>`, port 8190) or
mounted in a host Flask app's page.

### The document is the graph
- The editor derives its model with `confluid.load` and checks it; every problem names its `file:line:col`. An edit
  that would stop the document loading, or add a problem, is refused in one line and changes nothing.
- **Explicit Save**, Save As and Revert: every edit stays in the editor until Save. Save writes all of a graph's files
  or none, and refuses to overwrite a file that changed on disk since the editor read it.
- **Undo and redo**: a document edit on the server's text history, a move, arrange or group on the page's.
- **New…** writes the starter file at once; **Open** and a file browser reach a graph anywhere.
- **Unsaved work survives a stop** — Ctrl+C, `streamstudio stop`/`restart` (SIGTERM) or a closed terminal (SIGHUP): it
  is kept in the folder's hidden `.streamstudio-workspace.json`, and the next start asks whether to restore it.
- One naming scheme for every node: a step by its key, a list member `stream.ops[1]`, inside a subgraph `prep/grey`.

### The canvas
- A React Flow page, shipped BUILT inside the package (`streamstudio/canvas`): every install serves it, and Node is
  needed only to change it. A checkout whose page source changed rebuilds it at the server's start.
- A menu bar, right-click menus and one keyboard map; a **Nodes sidebar** folding by package, kind and group, with
  search. The catalogue of installed classes is built by a helper process; a class's settings arrive when it is
  selected.
- **A drawn wire is saved** in the document's own spelling (`thing: !ref:hugging_face_source`, `delivered: {classes:
  !ref:…}`), or refused in one plain line; a nested producer moves out to a top-level key first.
- **A class that is not an op is a card of its own**, written as a new top-level key above the graph's root.
- **Op lists** (`ops:`): members added and removed in the list's own style, comments kept with their member.
- **Subgraphs**: one node outside, a nested level inside; Move to its own file…, includes, **templates**, and copy and
  paste that carry the document's text.
- **Groups** kept in the layout sidecar beside the graph (`<graph>.layout.yaml`), with the positions; a first layout
  that stacks a column by the cards' heights.
- **Set source…** puts a source into a `Stream`; **pickers** fill a field: a file or folder, a HuggingFace dataset,
  its columns and splits, or the list a plugin supplies for a tag a class stamps on a parameter.
- **Run**: run, step through, inspect and re-run a graph from the run panel, with the values held unsaved.
- A value node that IS a number (`2.5 MSa/s`) wires into a number setting.

### Settings
- A graph's `app:` block names its settings; the settings tab shows those fields, *all parameters* the rest, and
  `?only=settings` the settings alone, each change saved at once.
- **Settings live in `<graph>.settings.yaml`**, one line per setting naming its node (`flow.second.op.level: 0.9`); a
  line naming nothing is refused at its `file:line:col`. `app: {graph: settings-only}` offers a graph's settings and
  refuses every other change.
- A field whose schema says `applies_when: {<parameter>: [values]}` is grayed while that parameter holds another value.
- A setting may be written by its bare parameter name (`floor_db: -60.0`) when exactly one node of the graph has that
  parameter; a parameter two or more nodes have is refused bare, naming the full keys to write (`level is a
  parameter of 2 nodes (first, third) — a bare line would set them all; write flow.first.op.level or
  flow.third.op.level`).

### Mounted in a host app
- `editor_blueprint(host=…)` with a host's `name` and `folder()`, `record()`, `saved()`, `shipped()`;
  `page_blueprint()` serves the page; `studio_of(app).stash()` keeps the unsaved work across the host's stop.
- A rooted graph's inputs are drawn as the host's card, with a dashed wire to where the host fills each one.
- In a host's panel: `?embed=1`, `?tab=`, the `state`/`saved`/`open`/`unavailable` messages, and the host's `commit`
  and `values` commands. One editor request runs at a time.
- `import streamstudio.app` takes 0.2 s and imports no torch.

### Plugins
- A module named in the `streamstudio.plugins` entry-point group may declare `LOOKUPS = {"<TAG>": fn}`: the list
  beside a field its class stamps with that tag, asked with the node's current values (`GET /api/lookups/<tag>`),
  never failing a request.

### The command line
- `streamstudio [folder] [--port 8190] [--templates-dir DIR]`, `streamstudio stop`, `streamstudio restart` (which
  serves the folder the running editor served). A taken port is refused before any work, with a clickable URL and a
  command one port up that the CLI accepts; an option a command does not have is refused by name.
- Shell completion through `liquifai-install-completions`; `streamstudio.__version__` is read from the installed
  distribution.

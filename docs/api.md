# The document model and the HTTP API

How the editor reads a graph and names its nodes, every route it serves, and which route each gesture on the page
calls — for a script, a test or a tool that edits graphs without the page.

## The document model and node names

`streamstudio.document.Document` opens a graph file and derives a model from it — nodes with typed sockets,
wires, located problems — by reading declarations that already exist (`recordstream.algorithm_spec`, an op's
`consumes`/`produces`, `confluid.input_specs` / `output_specs` / `to_pydantic`, `GraphContract.inputs`/`outputs`)
and the `recordstream.items` registry for socket types. Nothing is written per class. An edit patches the line the
marker came from; the file is never re-dumped. A re-dump would drop every comment and replace `root: $DATA_ROOT/pets`
with the path the variable resolved to; a patch changes one line and keeps both.

An edit that makes the document stop loading, or gain a problem (a wire whose types do not meet), is refused with
a message naming `file:line:col`. A problem that was already there is not "gained" when an edit only moves it: a
step inserted above a broken `bind:` moves that problem from `flow.yaml:6:9` to `flow.yaml:9:9`, and the edit goes
through — while a second broken `bind:` is still refused. A problem is at the line it is about in any folder, one
whose name has a space included. A missing steps file is at the `include:` entry that names it, and is looked for
beside the file holding that entry, whatever folder you started the editor in:

```
…/graphs/demo.yaml: does not load: ConfigFileNotFoundError: …/graphs/demo.yaml includes prep.steps.yaml:
Not found: prep.steps.yaml (searched: …/graphs/prep.steps.yaml, <working directory>/prep.steps.yaml, …)
at …/graphs/demo.yaml:5:18
```

The same holds for `include: ~/nope.steps.yaml` (the message names it with your home folder written out) and for an
include that names a folder (`… is a directory, not a config file`). A graph file that is a symbolic link
(`served/demo.yaml` → `shared/demo.yaml`) reads its steps files beside the file it points to, in `shared/` — the
canvas, Check graph, a run, Copy and Save as template alike; a `prep.steps.yaml` beside the link is not read.

**A class spelled by its registry key** (`!class:DirectorySink`, not the dotted path) is found once the
package defining it is imported. On the first document naming such a class, the editor imports the installed
packages' `confluid.configurables` entry points one at a time — those of packages already loaded first, then the
rest in their listed order — and stops at the first that registers it; the rest are left for a later miss, and none
is imported twice. A package another request is still importing (two first lookups at once) is waited for, so a
class half a second from being registered is never reported as missing. So the first graph list waits only for the
packages up to the one that defines the class. A name nothing defines imports them all, once; a later miss then
costs nothing.

**Node names** are one scheme shared by the model, the engine, the sidecar and
the routes: a `flow:` step by its key (`floor`), a list member by `path[i]` (`stream.ops[1]`), a nested marker by
its document path (`processor.stream`), a contract root by its key (`contract`), the host's side of a graph as one
node, `<host>` — for a graph without a contract what its steps read that nothing produces, for a rooted one the
`inputs` its contract declares — and a node inside a subgraph as `<subgraph>/<step>` (`prep/grey`), with
the two boundary nodes `prep/<in>` and `prep/<out>`. An inner node carries the `file:line:col` of its own line —
in the steps file when the steps are included.

## The routes

One `Studio` method each — JSON in, JSON out. A refusal is `400 {error}` with the located message; a name that
does not exist is `404 {error}` listing what there is. Every route that names a node takes a qualified name
(`prep/scaled`); in a URL it may carry the slash (`/steps/prep/grey`). Every edit is held in memory until
`…/save`.

The page sends the header `X-Streamstudio-Session` with the session `GET /api/workspace` answered. A request whose
session is not the running editor's (a tab left open across a restart) is refused with `409` and changes nothing;
`GET /api/workspace` itself is always answered, so the page can join again. A request without the header — a
script, a tool — is served as always.

| verb | path | what it does |
| --- | --- | --- |
| GET | `/api/workspace` | `{folder, session, recovery, host}` — `host` is `{name}` for an editor mounted with a host ([Mounting the editor in a host app](hosting.md)), `null` standalone; `recovery` is `null`, or the unsaved work a stop kept: `{saved_at, graphs: [{name, path, disk: unchanged\|changed\|gone\|unreadable, reason, changed_on_disk, saved_at}], error, aside}`; for a file that cannot be used, `graphs` is empty, `error` says why and `aside` is where discard moves it |
| POST | `/api/workspace/recovery/restore` | open the kept graphs (unsaved, no undo history) and remove the file; answers the graph list, each entry with `restored`; `404` when there is nothing kept |
| POST | `/api/workspace/recovery/discard` | remove the file (move an unreadable one aside); answers the graph list; `404` when there is nothing kept |
| GET | `/api/graphs` | `[{name, path, kind, dirty, shipped}]` — `kind` is `flow`, `graph` or `contract`; `shipped` is a graph the host ships whose folder holds no copy yet ([A graph the host ships](settings.md#a-graph-the-host-ships)) |
| POST | `/api/graphs` | `{name, kind: flow\|pipeline\|source, dir?}` — WRITE the starter to `<dir or the graph folder>/<name>.yaml` at once and add it to the list; answers the graph with `dirty: false` (its `name` may be `fresh~2`). Refused, writing nothing, when the file exists, the name breaks the file-name rule, or the folder is not a writable folder (`404` when it does not exist) |
| POST | `/api/graphs/open` | `{path}` — add any `.yaml` on this machine to the list |
| GET | `/api/files?dir=&show=` | `{dir, parent, entries: [{name, path, kind: dir\|yaml\|file}]}` — folders first; `show=graphs` (the default: File ▸ Open, a folder picker) adds the `*.yaml` files but the layout sidecars, `show=all` (a file picker) every file as `file`; hidden entries never; no `dir` = the graph folder; `$VAR` in `dir` is expanded |
| GET | `/api/graphs/<name>` | `{name, shipped, path, kind, text, model, positions, groups, history_depth, dirty}`; `model.inside` holds each subgraph's inside, recursively; `model` shows each setting of the graph's settings file on its node, `text` is the graph's own |
| PUT | `/api/graphs/<name>` | `{text}` replaces the whole document in the editor; the history starts over |
| POST | `/api/graphs/<name>/save` | write every changed file — the graph, its settings file, a steps file — and the sidecar; refused when a file changed on disk. `{only: "settings"}` writes the graph's settings file alone (a settings sheet's save): a change of the graph itself stays held |
| POST | `/api/graphs/<name>/save-as` | `{path, overwrite?}` — write the graph to another file; the entry follows it |
| POST | `/api/graphs/<name>/revert` | read the file again, dropping unsaved changes |
| GET | `/api/graphs/<name>/check` | `{problems: [{message, loc}]}` |
| POST | `/api/graphs/<name>/param` | `{node, param, value}` — a SETTING (a field the `app:` block names) goes to the graph's settings file, one line each ([A graph's settings file](settings.md#a-graphs-settings-file--graphsettingsyaml)); any other parameter: patch one value on its own line, or insert it when the file left the default |
| POST | `/api/graphs/<name>/slot` | `{node, slot, class_path, params}` — put a new object into a single slot (`stream.source`), replacing what it held; one edit. Answers the graph plus `set` (the node now in the slot, `stream.source`) |
| POST | `/api/graphs/<name>/wires` | `{source, source_socket, target, target_socket}` — save a drawn wire in the document's own spelling ([Wiring](editor.md#wiring--a-drawn-wire-is-saved)); one edit. Answers the graph plus `wired` (the card the wire comes from now — `source` for a `stream.source` moved out of its slot); a wire it cannot save yet is a `400` with one plain line |
| POST | `/api/graphs/<name>/wires/remove` | `{wires: [{source, source_socket, target, target_socket}, …]}` — remove several wires as ONE edit, all or nothing |
| POST | `/api/graphs/<name>/steps` | `{name?, after?, list?, class_path, params}` — `after` a flow step: a new step on its level named `name`, and what read `after` now reads it; `after` an op-list member (`stream.ops[1]`): a new member after it; no `after` and `list: "stream.ops"`: a new member at the end. `after` naming the host card or the root counts as none. A class that is not an op is a card of its own, as `…/nodes` makes it (`after`, `list` and `name` place ops only), and `wire` is the end a dropped wire was dragged out of. Answers the graph plus `added` (the new node's name) |
| POST | `/api/graphs/<name>/nodes` | `{class_path, params, wire?}` — a class that is not an op as a card of its own: a new top-level key named after its class (`class_names_scan`, `_2` …), above the root and its comment lines, else at the end; one edit. `wire` — `{target, target_socket}` (the card feeds that input, from its first output that fits) or `{source, source_socket}` (that output goes into the card's first input that takes it) — is saved in the same edit, or nothing is added. Refused in one line for an op, a flow graph, a file that is one object. Answers the graph plus `added` |
| DELETE | `/api/graphs/<name>/steps/<node>` | remove one step (what read it now reads what it read from) or one list member |
| POST | `/api/graphs/<name>/steps/remove` | `{nodes}` — remove several steps or members as ONE edit, all or nothing; removing every member leaves `ops: []` |
| POST | `/api/graphs/<name>/rename` | `{node, new}` — the key, every reference on its level, its position and group entries (for a subgraph: every `prep/…` name) |
| POST | `/api/graphs/<name>/undo`, `…/redo` | one step of the text history; `changed` says whether there was one, `shifts` how list members were renumbered (`[{list: "stream.ops", first: 2, by: 1}]`) |
| POST | `/api/graphs/<name>/copy` | `{nodes}` → `{text, kind: steps\|ops}` — the steps' own YAML text |
| POST | `/api/graphs/<name>/paste` | `{text, after}` → the graph plus `pasted: [names]` — one undo entry |
| POST | `/api/graphs/<name>/subgraph` | `{nodes, name}` → the graph plus `subgraph: <new node>` — Convert selection to subgraph |
| POST | `/api/graphs/<name>/subgraph/<node>/extract` | `{path?}` — Move to its own file |
| POST | `/api/graphs/<name>/include` | `{path, after, name?}` — Insert steps file |
| POST | `/api/graphs/<name>/templates/<template>/insert` | `{after}` — Insert template |
| GET | `/api/templates` | `[{name, path, steps, doc}]` |
| POST | `/api/templates` | `{name, graph, nodes, overwrite?}` — Save as template (written at once) |
| DELETE | `/api/templates/<name>` | delete one; answers the remaining list |
| PUT | `/api/graphs/<name>/positions` | `{positions: {node: [x, y]}}` — merged over what the editor holds |
| POST | `/api/graphs/<name>/layout` | lay every level out afresh (longest-path layering) |
| PUT | `/api/graphs/<name>/groups` | `{groups: [{id, title, color, nodes, bounds?, parent?}, …]}` — stored as sent |
| GET | `/api/types` | `{socket type: [the types it may be wired into]}` — the meet rule the canvas applies while you drag |
| GET | `/api/graphs/<name>/form` | `{node: JSON-schema properties}` from `confluid.to_pydantic` — the settings form, every level; a parameter with a picker carries `picker` ([Pickers](settings.md#pickers)); `requires` (the gate a branch sets) is never a field |
| GET | `/api/graphs/<name>/app` | `{declared, graph, fields: [{node, param, loc}], form, problems: [{message, loc}]}` — the graph's `app:` fields ([Settings: a graph's `app:` fields](settings.md#settings-a-graphs-app-fields)); `graph` is `editable` or `settings-only` ([A graph that offers its settings only](settings.md#a-graph-that-offers-its-settings-only)); `declared: false` and no field without an `app:` block |
| GET | `/api/hf/datasets` | `{datasets, reason}` — the HuggingFace datasets this machine has cached, and why there are none when there are none |
| GET | `/api/hf/features?path=&name=&split=` | `{features, all_columns}` — the columns of that dataset (`[]` without one, or when it cannot be read) and the `metadata_features` word for "every other column" (`*`) |
| GET | `/api/hf/splits?path=&name=` | `{splits}` — the split names of that dataset |
| GET | `/api/palette` | every configurable class as a typed node spec, without its settings (`params_schema`, `params_defs`); waits for the catalogue's helper process, which serving the page starts; `?refresh=1` runs a new one (it sees packages installed or edited since). `503 {error}` when the helper stopped before it answered — the next request runs a new one |
| GET | `/api/palette/<class path>` | one class's whole node spec, settings included — at once when its package is loaded in the editor, else when the helper has described it; `404 {error}` naming the close class paths for an unknown one |
| POST | `/api/graphs/<name>/trace` | `{record, until}` — run record `#record` of the graph's own source through what the editor holds, pausing before `until` (`prep/scaled` pauses inside); a graph with no source runs the record its host supplies (`record` does not apply). Answers the run: `{run, graph, record, host, trace, statuses, generations, paused_at}` — `record: null` and `host: {label, filled: {node: {attribute: value}}}` for a host's record |
| GET | `/api/runs/<run>` | the trace: per node its params, inputs, outputs, status and milliseconds; inner rows carry `parent` |
| POST | `/api/runs/<run>/rerun` | `{node, params}` — rebuild one node through its constructor and rerun from it |


## What the canvas does, gesture by gesture

The page writes only through the routes below, and draws what the server answers; a refusal is a toast carrying
the server's located message.

| you do | the page calls | what the document says afterwards (in the editor, until Save) |
| --- | --- | --- |
| change a widget on a node, or in the **settings** tab | `POST …/param` | the value on its own line (a refused value reverts) |
| click a Nodes-sidebar entry, or pick one in the palette / Add Node ▸ | `POST …/steps` — on a flow `{after}` the selected node (else the flow's output; inside a subgraph, its last step); on a Pipeline `{after}` the selected member, else `{list: "stream.ops"}`; the host card and the root are never the selected node | on a flow a new step, `from:` the anchor; on a Pipeline a new member after the selected one, or at the end of the list; a class that is not an op: a card of its own, `class_names_scan: !class:…ClassNamesScan {}` above the root ([A class that is not an op](editor.md#a-class-that-is-not-an-op--a-card-of-its-own)), drawn where the menu or the palette was opened |
| drag a sidebar entry onto the canvas | `POST …/steps`, then its position | the same, placed at the drop point |
| drag a wire out of a socket and drop it on nothing | the palette, narrowed to the classes that fit that type; a pick sends `POST …/steps` with `wire` — the end dragged out of | the step (or member) inserted after that node; out of a Pipeline's empty `transforms[0]` input, at the end of its `transforms`; a card: written and wired as ONE edit (out of the root's `classes`, `classes: !ref:class_names_output`) |
| drag a wire between two sockets whose types meet ([Wiring](editor.md#wiring--a-drawn-wire-is-saved)) | `POST …/wires {source, source_socket, target, target_socket}` | the wire in the document's own spelling — `classes: !ref:hugging_face_source` under the root's `delivered:`, `thing: !ref:hugging_face_source` on a card, `input: image` for a record entry; a card inside another card's slot moved out to a top-level key named after its class first. A wire it cannot save yet: one line in a toast, nothing drawn (a mismatch is refused while dragging) |
| click a wire (`Cmd`/`Ctrl`+click adds more), `Delete` | ONE `POST …/wires/remove` | the wires gone — a `delivered:` or `!ref:` pair dropped; a card written inside the slot moved out, both cards kept — one undo brings all back. The dashed wire from the host card is refused in one line |
| `F2`, or double-click a node's name | `POST …/rename` | the key and every reference to it, on its own level |
| select nodes, `Delete` | ONE `POST …/steps/remove` | the steps (or list members) gone and what read them rewired — one undo brings all back |
| click a card, then `Shift`+click more cards | nothing — the selection | each Shift+clicked card is ADDED to the selection; Shift+click never takes one out |
| `Cmd`+click (Mac) / `Ctrl`+click (elsewhere) a card | nothing — the selection | the card is added, or taken out when it was selected — the one way to drop a single card |
| `Shift`+drag on EMPTY canvas | nothing — the selection | every card inside the box is selected |
| drag a selected card (Shift held or not) | `PUT …/positions` | unchanged — the whole selection moves together |
| `Ctrl+X` / `Ctrl+C`, then `Ctrl+V` | `POST …/copy`, then `POST …/paste` | the copied YAML text inserted after the selection (a Stream member after a member); a paste pressed before the copy answered waits for it |
| `G`, Add group, resize or drag into / out of a box | `PUT …/groups` | unchanged — groups live in the sidecar |
| drag a node or a box | `PUT …/positions` | unchanged — positions live in the sidecar |
| *Arrange* (left to right by the cards' sizes) / *Arrange in columns* (the server's layers), then the view fits the whole level (never above zoom 1) | `PUT …/positions` / `POST …/layout` | unchanged |
| `Ctrl+Z` / `Ctrl+Shift+Z` | a document edit: `POST …/undo` / `…/redo`; a move or group: the page writes the old layout back | the text history is the server's — an edit made through the API is undone the same way |
| **run** tab: *run* (record #N, pausing before the selected node), *step*, click a node | `POST …/trace`, `GET/POST /api/runs/<run>…` | unchanged — each node shows its status, time and generation |
| **run** tab in an editor mounted in a host, on a graph with no source of its own: *run* | `POST …/trace {}` — no record number; the host's record runs | unchanged — the panel says `record: the workspace's selection` before (the host's `name`), and after names what ran (`run_… · rec_7 · 0.25–0.75 s · 1–2 MHz`) and what the host filled in (`window: start_s = 0.25, stop_s = 0.75`) |
| *set…* on a Stream's `source` row, **Set source…** in its right-click menu, or a wire dragged out of the `source` slot onto nothing; pick a class (sources first), fill its form, **Set source** | `POST …/slot {node, slot, class_path, params}` with the values changed from the defaults | `  source: !class:recordstream.sources.files.FilesSource {root: /data/captures}` — what was in the slot is replaced; one undo takes it out. A slot holding a REFERENCE (`source: !ref:files`) gets the new class in the reference's place, and the dialog says the referenced node stays, unwired: `FilesSource goes into stream's source slot in place of its reference to files; files (FilesSource) stays in the document, unwired.` After a pick the keyboard is in the form's first field (Escape cancels, Tab moves on) — and in the dialog itself while the class's settings are still loading, so Escape cancels then too; the arrows keep the highlighted class in view |
| the `…` beside a folder or file field ([Pickers](settings.md#pickers)) | `GET /api/files` (a folder) or `GET /api/files?show=all` (any file) — the editor's machine | the path chosen, as if typed |
| point at the list beside a HuggingFace source's `path`, `split`, `input_feature` / `target_feature`, or *add column…* beside `metadata_features` | `GET /api/hf/datasets`, `…/splits?path=`, `…/features?path=&split=` — asked once per question, only when you reach for the list | the name picked, as if typed (`*` replaces the column list, a column is appended) |
| **settings** tab: tick *all parameters* | `GET …/form` (already read) | unchanged — every parameter of the level shown instead of the `app:` fields; the tick stays for that graph while you look at another tab |
| a `(min, max)` pair: type the min, Tab, type the max | `POST …/param` twice, in that order: `[min, old max]`, then `[min, max]` | the pair as typed — the max box keeps what you type while the first answer arrives |
| in a node's entry: change a value, *rerun this node* | `POST /api/runs/<run>/rerun` with the values that differ | unchanged — the value is a trial; the node and what follows are recomputed. On a host's record the form starts from what the host FILLED (the window, the band — what ran), and those are sent only when you type new ones: a pane spelling `start_s: 0` reruns on the host's 0.001–0.003 s, not on the whole capture |
| **Graph ▸ Check graph** | `GET …/check` | unchanged — every problem, with its `file:line:col` |

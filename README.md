# StreamStudio

A graph editor for [RecordStream](https://github.com/Gearlux/recordstream) pipelines written as
[confluid](https://github.com/Gearlux/confluid) YAML documents.

```bash
pip install streamstudio
streamstudio path/to/graphs      # the editor over that folder:  http://127.0.0.1:8190
```

It runs on its own, or inside a host app's page ([Mounting the editor in a host app](#mounting-the-editor-in-a-host-app)).

## The canvas editor

The YAML file is the graph. The editor draws it, and every change you make is a patch of the line it is about —
comments, `$VAR` spellings and key order survive. No change is written to disk until you **Save**. A graph made
with **New…** is written at once, so every graph the editor shows is a file on disk.

### Starting and stopping it

```bash
streamstudio path/to/graphs                                  # port 8190; the folder's *.yaml files are the graphs
streamstudio                                                 # the same, over the current directory
streamstudio path/to/graphs --port 8191 --templates-dir ~/my-templates
streamstudio stop                                            # stop what listens on 8190
streamstudio stop --port 8191                                # stop what listens on 8191
streamstudio restart                                         # stop it, then serve the SAME folder again
streamstudio restart path/to/graphs                          # stop it, then serve that folder
```

`stop` and `restart` never throw away unsaved work: the editor keeps it in a hidden file of its folder, and the
next start offers it back ([Unsaved work survives a stop](#unsaved-work-survives-a-stop)). `restart` without a folder asks
the running editor which folder it serves and serves that one again, so the kept work is offered at once.

The folder is resolved once, so every path the editor shows is absolute. A folder that is not there is refused
before anything is stopped or started (`restart` included):

```
$ streamstudio restart grpahs
Graph directory not found: /home/me/grpahs
```

A taken port is refused before anything starts. The message gives the address to open, and the same command one
port up (the folder printed absolute):

```
$ streamstudio graphs
Port 8190 is already in use — StreamStudio may already be running there.

    Open it:  http://127.0.0.1:8190

If that is a different program, run StreamStudio elsewhere:  streamstudio /home/me/graphs --port 8191
```

An option the command does not have is refused by name, not ignored:

```
$ streamstudio restart --prt 9000
Error: streamstudio: unknown option 'prt' — it names no parameter of the 'restart' command and no key in the
configuration. Did you mean 'port'? Declared parameters: graph_dir, port, templates_dir.
```

### Unsaved work survives a stop

When the editor stops while it holds unsaved work — `streamstudio stop`, `streamstudio restart`, Ctrl+C in its
terminal, or closing that terminal window — it writes that work to a hidden file in the folder it serves,
`.streamstudio-workspace.json`. The graph
files themselves are not touched. `stop` says where the work went:

```
$ streamstudio stop
Stopped StreamStudio on port 8190 (pid 57474). Its unsaved work (demo.yaml) is kept in /home/me/graphs/.streamstudio-workspace.json — the next start asks what to do.
```

The editor's own terminal prints the same as it exits (`Unsaved work (demo.yaml) is kept in … — the next start
asks what to do.`). With nothing unsaved, `stop` prints only `Stopped StreamStudio on port 8190 (pid 57503).` and no
file is written. A graph made with New… and not changed since is not unsaved work: New… already wrote its file.

**The next start on that folder** says so before anything else:

```
$ streamstudio graphs
Unsaved work from the last session (29 Sep 20:06): demo.yaml — the page asks whether to restore it.
```

and the page asks before it opens any graph:

```
Unsaved work from the last session (29 Sep 13:25)
  demo.yaml           changed
  radio_view.yaml     changed — the file on disk changed after the editor stopped
[Restore unsaved work]   [Open the saved files]
```

| you choose | what happens |
| --- | --- |
| **Restore unsaved work** | Those graphs open as they were when the editor stopped — the text, the node positions, the groups — marked ●. Nothing is written until you Save. |
| **Open the saved files** | One more question: `Discard the unsaved work of the last session? The unsaved changes of demo.yaml and radio_view.yaml are lost. This cannot be undone.` **Discard** drops the kept work and shows the files as they are on disk; **Cancel** goes back to the first question. |

Either answer removes the hidden file; a later stop writes a new one if something is unsaved again. The question
must be answered — Escape does not close it, and no graph opens until it is. What a row can say after the file
name:

| the row says | meaning |
| --- | --- |
| `changed` | the kept text differs from the file; the file on disk is as the editor last saw it |
| `changed — the file on disk changed after the editor stopped` | someone wrote the file since; after Restore, Save refuses as usual (`… changed on disk since the editor read it — Revert to load it, or Save As to keep both`) |
| `changed — the file was deleted or moved away after the editor stopped; Save writes it again` | the file is gone; Save puts it back |
| `changed — /home/me/other/away.yaml cannot be read: Permission denied` | the file is there but cannot be read |
| `changed (kept 29 Sep 18:09)` | the same file was kept twice, by two stops (two editors on one folder, or a stop before anyone answered); both rows are restored, the second as `flow~2` |

Files are named by their way from the folder, so a file opened from elsewhere reads `../other/flow.yaml`; it
rejoins the graph list on Restore. A graph the editor already holds unsaved changes of is not overwritten: the kept
version opens beside it as `demo~2`.

**What a stop cannot keep:**

- A `kill -9`, a crash or a power cut. The file is written as the editor stops; there is no periodic autosave.
- The undo history. A restored graph starts with an empty history.

**Two more cases:**

- A kept file the editor cannot read (hand-edited, cut short) never stops the start. The start line says
  `Unsaved work from the last session cannot be read — <the file and why>. The page offers to move it aside.`, and
  the page offers only **Open the saved files**, which moves the file to `.streamstudio-workspace.broken.json`
  (then `…broken-2.json`) — nothing is deleted.
- A browser tab left open across a `restart` cannot write into the new editor. Its next edit changes nothing and
  shows `the editor was restarted after this page was loaded — nothing was changed; the page asks the new editor
  what it holds, and the unsaved work the stop kept is offered again`, and the tab then asks the question above.

The hidden file's name matches `.streamstudio-workspace*.json`. If the graph folder is inside a git checkout, add
that line to its `.gitignore` (this repository's own `.gitignore` carries it).

### The window

A menu bar across the top, the canvas in the middle, a side panel on the right with three tabs — **text** (the
YAML as the editor holds it), **settings** (a form of every node on the level the canvas shows — of the graph's
`app:` fields when it declares them, [below](#settings-a-graphs-app-fields)) and **run**.

| menu | items (shortcut; on a Mac ⌘ for Ctrl) |
| --- | --- |
| **New** | Flow graph… · Pipeline… · Source graph (with its contract)… |
| **File** | Open… `Ctrl+O` · Save `Ctrl+S` · Save As… `Ctrl+Shift+S` · Revert |
| **Edit** | Undo `Ctrl+Z` · Redo `Ctrl+Shift+Z` (or `Ctrl+Y`) · Cut `Ctrl+X` · Copy `Ctrl+C` · Paste `Ctrl+V` · Delete `Delete`/`Backspace` · Select all `Ctrl+A` · Rename… `F2` |
| **View** | Nodes sidebar `N` · Fit to view · Arrange · Arrange in columns · Minimap |
| **Graph** | Add group · Group selection `G` · Convert selection to subgraph · Insert template ▸ · Save selection as template… · Delete template ▸ · Check graph |
| **Run** | Run `Ctrl+Enter` · Step · Rerun selected node |

An item that cannot act is greyed — Save while nothing changed, Copy with nothing selected. Keys are ignored
while you type in a field. `Escape` closes an open menu; with none open, it goes up one subgraph level. The bar
also has a **Nodes** button (the sidebar) and a green **▶ Run**.

**A link opens a view.** The address follows what the page shows, so a reload or a shared link opens the same
graph, subgraph level and side tab:

```
/?graph=view_bte                     the graph view_bte, the text tab
/?graph=view_bte&tab=settings        the same graph, the settings tab open
/?graph=demo&level=prep              the inside of the subgraph prep
/?graph=nosuch                       nothing opens; the canvas says
                                     "There is no graph 'nosuch' in /w/state — the graphs there are: source, view_bte and view_fft."
                                     with one button per graph
```

**The settings tab** of a graph with an `app:` block lists only the fields it names (the Bluetooth pane: 30 of its
247 parameters), one section per node in the block's order, and a tick box **all parameters** lists every one. A
`(min, max)` pair (`deviation_hz`) is ONE field of two numbers. An `app:` entry that names nothing is listed under the
fields in the server's words.

**The graph list** (the dropdown in the bar) holds every `*.yaml` of the folder, every file opened with
File ▸ Open…, and every file New… wrote in another folder. It leaves out `*.layout.yaml` (the positions sidecars)
and `*.steps.yaml` (subgraph steps files, below). A file that appears in the folder later shows up on the next
look. A file from elsewhere (opened, or made there with New…) whose name the list already holds gets `demo~2`. A
graph with unsaved changes reads `● demo · flow`.

### Save, Save As, Revert — changes stay in the editor until you save

Every edit — a value, a new step, a rename, undo, a move, a group — changes the editor's copy only. The title, the
file name in the bar and the dropdown show **●** while a graph holds unsaved changes; closing or reloading the
page asks first. Measured on the document of [Subgraphs](#subgraphs--a-flow-used-as-one-step):

| you do | the file on disk | the editor |
| --- | --- | --- |
| set `target_max` of `prep/scaled` to `2.0` | `target_max: 1.0` | `● demo.yaml` |
| **File ▸ Save** | `target_max: 2.0` | `demo.yaml` |
| undo back to the saved text | unchanged | the ● goes away |

**Run** traces what the editor holds, not the file: an unsaved value is the value the run uses.

**Save refuses to overwrite someone else's change.** When the file changed on disk since the editor read or wrote
it, nothing is written:

```
/home/me/graphs/demo.yaml changed on disk since the editor read it — Revert to load it, or Save As to keep both
```

For a steps file the graph includes, Save As cannot keep both (the new graph would include the same file), so the
message offers Revert only. A graph file that was deleted meanwhile is simply written again. A file Save cannot
read is refused the same way, since the editor cannot compare it with what it read:

```
/home/me/graphs/demo.yaml cannot be read: Permission denied — the editor never writes over a file it cannot compare with what it read; make it readable, or Save As to write the graph elsewhere
```

Before it writes anything, Save checks that the disk will take every file it is about to write — the graph and each
steps file it changed. When one cannot be written — a steps file Move to its own file… put in a folder you may not
write — nothing is written, the graph stays ● and the message names the file:

```
/home/me/graphs/parts/prep.steps.yaml cannot be written: Permission denied
```

The graph on disk keeps its old text; Save again once the folder can be written. The graph's own file is written
last, so even a write the disk cuts short (a full disk) never leaves a graph that includes a steps file that is not
there. Save As refuses the same way and writes no new graph.

**File ▸ Save As…** writes the graph to another `.yaml` and the list entry follows it: its name becomes the new
file's (`copy`), the old file is left as it was, the positions sidecar comes along, and a relative `include:` is
rewritten so it still names the same steps file. An existing file makes the dialog's button read **Replace**. A
file another list entry holds is refused:

```
/home/me/graphs/demo.yaml is open as 'demo' — save that one, or choose another file
```

**File ▸ Revert** drops the unsaved changes (the undo history, moves and groups too) and reads the file again —
for a graph made with New…, the starter New… wrote. A graph whose file was deleted while the editor held it has
nothing to read:

```
/home/me/graphs/fresh.yaml is not on disk any more — Save writes it again
```

### New, Open and the file browser

**New ▸ Flow graph… / Pipeline… / Source graph (with its contract)…** asks where the new file goes, and
**Create writes it at once**. There is no graph that lives only in the editor, so a New… made by mistake is a file
you can see and delete, never hidden state:

```
New flow graph
  named steps, each reading the record of the one before (flow:)
  Name     [ fresh                ]
  Folder   [ /home/me/graphs      ]  [Browse…]
  → /home/me/graphs/fresh.yaml
                                          [Cancel]  [Create]
```

The title names the kind you picked. **Folder** starts as the folder the editor serves; type another one, or
**Browse…** for the file browser (below) in its folder mode. The line under it is the file Create will write, and
it follows what you type (a `.yaml` you add yourself is not doubled). After Create the graph opens with no ●:
`fresh.yaml` holds the starter, and nothing is unsaved until you change it. A file made in another folder joins
the list the way File ▸ Open… adds one — if the list already holds its name, the entry is `clip~2`.

Each kind starts from one step that reads and writes nothing, so it has no problem to fix before you begin:

```yaml
# fresh — a flow graph: each step reads the record the step above it returns
flow:
  probe:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: probe}
outputs: probe
```

**Pipeline…** starts a `stream: !class:recordstream.core.stream.Stream` with an `ops:` list; **Source graph (with
its contract)…** starts the flow above plus a `contract: !class:recordstream.ops.contract.GraphContract` root
written AFTER it (written before `flow:`, the contract would receive the file's `outputs:` line and fail to
build).

**What Create refuses.** Nothing is written, the dialog stays open with what you typed, and it shows the reason in
red; changing the name or the folder clears it. Measured:

| you type | the dialog says |
| --- | --- |
| Name `demo`, and `demo.yaml` is already there | `/home/me/graphs/demo.yaml already exists — pick another name, or open it with File ▸ Open` — the file is left byte for byte |
| Folder `/home/me/nowhere` | `/home/me/nowhere does not exist — pick another folder` |
| Folder `/home/me/graphs/demo.yaml` | `/home/me/graphs/demo.yaml is not a folder — pick another folder` |
| Name `x`, Folder `/home/me/locked` (one you may not write into) | `/home/me/locked/x.yaml cannot be written: Permission denied — pick another folder` — the folder stays empty |
| Name `x.layout` or `my/graph` | `'x.layout' cannot name a graph file — use letters, digits, '_', '-', '.' or spaces, not starting with '.' and not ending in '.layout' or '.steps'` |
| Name `fresh`, while the list holds a `fresh` whose file was deleted | `/home/me/graphs/fresh.yaml is open as 'fresh' — its file is gone from disk; Save that graph, or pick another name` |

**File ▸ Open…** and **Save As…** use a file browser of the machine the editor runs on, starting in the graph
folder: folders first, then `.yaml` files; hidden entries and `*.layout.yaml` are left out. An opened file joins
the list. An unreadable file is refused with its name, and the list is untouched.

### The Nodes sidebar

`N` (or the **Nodes** button) opens every class the editor can describe as a tree of folders: the **package** (the
first part of the class path), inside it the **kind** — Sources, Ops, Algorithms, Engines, Sinks, Runnables, Values,
Contracts — and inside a kind one folder per part of the class's `@configurable` **group**:

```python
@configurable(category="op", group="numpy")          # recordstream ▸ Ops ▸ numpy ▸ Threshold
class Threshold: ...

@configurable(category="op", group="fourier/numpy")  # <package> ▸ Ops ▸ fourier ▸ numpy ▸ FourierOp
class FourierOp: ...
```

Every folder shows how many classes it holds and starts folded; a folder lists its sub-folders first, then the
classes that sit in it directly. A class without a group sits straight under its kind. The folders you open are
remembered in this browser. The search box narrows by every word you type (in the name, the class path or the
description) and opens every folder on the way to a match; clearing it folds them back as they were. Each entry shows its name, one line of description and coloured chips
for the record entries it reads and writes. Hovering shows the whole description and the parameters; a long list
ends with `+37 more parameters` rather than running off the window. Docstring markup such as
``:class:`DirectorySink` `` and ``` ``fields.json`` ``` shows as inline code.

**Click** an entry to add it after the selected node; **drag** it onto the canvas to place it where you drop it.
A class that is not an op — a value, a source, an engine, a sink — is a card of its own instead
([below](#a-class-that-is-not-an-op--a-card-of-its-own)): a click lets the editor lay it out, a drag places it.
The footer says where an op goes: `Click adds after grey · drag places it`. On a Pipeline (a `Stream` with an `ops:` list) with
nothing selected it reads `Click adds at the end of stream.ops · drag places it` ([Pipelines](#pipelines--adding-to-and-removing-from-an-ops-list)).
The `Subgraph` class is not listed — an empty one
is refused — and the footer says how one is made instead:
`A subgraph is made from steps: Graph ▸ Convert selection to subgraph, Insert template ▸ or Insert steps file…`.
Double-clicking empty canvas opens the same catalogue as a search palette.

**When the catalogue is read.** Building it means importing every installed package that declares classes (torch,
TensorFlow, JAX, the training and detection backends) and describing each class. The editor does that in a separate
helper process, started when the editor's page opens; the editor itself never imports those packages for it, so it
stays small and its other requests do not wait. The page asks for the catalogue the first time you open something
that lists it — the Nodes sidebar, Add Node ▸, the palette or Set source… — and never as it opens. Until it arrives
those four show `loading the catalogue…`; after that it is kept for as long as the page is open. Measured 2026-10-05
with every backend installed, on a busy machine (load ~120):

```
you open the editor, then at once Set source…   list after 31 s      (before: 55 s, built inside the editor)
you look at a graph first, Set source… later    list at once (3 ms)  once the helper has answered
the editor's memory after the catalogue         0.13 GB              (before: 1.17 GB)
```

A class's **settings** are not in the list: they are asked for when you select the class — pick it in Set source…, or
rest the pointer on it in the Nodes sidebar — and kept for the page's life. A class whose package the editor has
already loaded answers at once; any other once the helper has described every class's settings (about 13 s after the
list, at that load); until then the dialog and the card say `loading the settings…`. A class only the editor's own
process registered (one a host app defined) is listed and described too.

### Pipelines — adding to and removing from an `ops:` list

On a Pipeline the ops are a list, not named steps, so the canvas names each one by its place: `stream.ops[0]`,
`stream.ops[1]`, … Adding and removing work on that list. Starting from this file:

```yaml
# demo — a stream over one image
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.files.FilesSource
    files: [demo.png]
  ops:
    - !class:recordstream.ops.image.ReadImage {}
    - !class:recordstream.ops.image.ConvertMode {mode: L}   # one channel
    # then the mask
    - !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
```

| you do | what changes in the text (in the editor, until Save) |
| --- | --- |
| select `stream.ops[1]` (ConvertMode), click `PrintRecordOp` in the Nodes sidebar | one line is added right after ConvertMode, above `# then the mask`: `- !class:recordstream.ops.debug.PrintRecordOp {}` — it is `stream.ops[2]`, and Threshold becomes `stream.ops[3]` |
| nothing selected, click `PrintRecordOp` | one line at the end of the list: `- !class:recordstream.ops.debug.PrintRecordOp {}` |
| select two members, press `Delete` | both lines go, as ONE edit — one Ctrl+Z brings both back |
| delete every member | `ops: []` — a stream that passes records through unchanged |

The palette, right-click ▸ Add Node ▸ and a wire dropped on empty canvas add an op the same way (any other class is
a card of its own, [next](#a-class-that-is-not-an-op--a-card-of-its-own)). Every other line —
comments included — stays byte for byte. A later member keeps its place on the canvas and its group when its
number changes (`stream.ops[3]` → `stream.ops[2]`), and an undo moves the numbers back.

- **A comment on its own line above a member belongs to that member:** it goes when the member is deleted, moves
  with it on Cut and Paste, and a member added before it lands above it. The comments above the FIRST member stay
  with the list.
- **The list keeps its own style.** A block list gets a `- ` line; a one-line list stays one line
  (`ops: [!class:…PrintRecordOp {label: a}, !class:…PrintRecordOp {label: new}]`); an empty `ops: []   # nothing yet`
  becomes a block list and keeps its comment on the key line (`ops:   # nothing yet`); a Stream that spells no `ops:`
  gets the key.
- **A `Pipeline` holds a list too** (`transforms:`). Select the Pipeline and click an entry to add at the end of its
  `transforms`; select one of its members to add after that member.
- **Where it cannot go:** there is no place BEFORE the first member — select nothing to append, or a member to add
  after it. A list written in brackets over several lines is refused
  (`…/brackets.yaml:2:8: stream.ops is written in brackets over several lines — spell it as a block list (one '- ' item per
  line) to add to it or remove from it`), and so is a list that lives in an included steps file
  (`stream.ops comes from …/parts.yaml; edit that list there`). A graph holding two op lists, with nothing selected
  that says which, answers `this graph holds 2 op lists (stream.ops, other.ops) — select a member of one, or the node
  holding it, to add to it`.

### A class that is not an op — a card of its own

An op goes after a step or into an op list. Any other class — a value like `ClassNamesScan`, a source, an engine, a
sink — is a CARD of its own: right-click ▸ Add Node ▸, the double-click palette, a Nodes-sidebar entry dropped on the
canvas or the palette of a wire dropped on empty canvas write it as a NEW top-level key, ONE edit (one Ctrl+Z), and
draw the card where you asked — the right-click, the drop, the wire's release. On the files graph a host workspace
seeds (its template spells a comment line for the root), right-click ▸ Add Node ▸ ClassNamesScan — nothing selected,
or the host card selected — changes the text to:

```yaml
stream: !class:recordstream.core.stream.Stream
  source: !ref:file_source
  ops:
    - !class:recordstream.ops.image.ReadImage {field: file, output: image}
class_names_scan: !class:recordstream.ops.contract.ClassNamesScan {}
# The graph's ROOT — what this graph must deliver; the host applies a drop through its check.
contract: !class:recordstream.ops.contract.GraphContract
  …
```

and **Check graph** finds nothing. Before, the same gesture was refused:

```
nothing selected        add recordstream.ops.contract.ClassNamesScan to stream.ops at [1] refused at …/files.yaml:12:7:
                        cannot wire stream.ops[1].object (object) into stream.ops[1] (Op) at …/files.yaml:9:9: the types do not meet
the host card selected  a new flow step needs a name — after '<host>', it has none
```

- **The key is named after the class**, as a card moved out of a slot is ([Wiring](#wiring--a-drawn-wire-is-saved)):
  `ClassNamesScan` → `class_names_scan`, a second one `class_names_scan_2`, and `_2`, `_3` … whenever the name is a
  top-level key, a card, or a setting of any card (a top-level key is handed to every card with a setting of that name).
- **The line goes directly above the graph's root** and the comment lines written over it, so the root stays last; a
  graph without a root gets it at the end of the file. Its values are spelled on its line (`{names: [cat, dog]}`).
- **A wire dropped on empty canvas, then a card picked:** the wire is saved to the new card in the same edit. Drag out
  of the root's `classes` socket, drop it on empty canvas, pick `ClassNamesOutput`, and the text gains
  `class_names_output: !class:recordstream.ops.contract.ClassNamesOutput {}` above the root and
  `classes: !ref:class_names_output` under its `delivered:` — one Ctrl+Z takes both. Out of an output, the wire goes
  into the card's first input that takes it (`file_source.stream` → `class_names_scan.source`). When the wire cannot be
  saved nothing is added, and its one line says why (`class_names_scan has no input that takes stream.ops[0].image
  (Image).`).
- **The host card and the root are never what something is added after:** with only them selected, an op goes where it
  goes with nothing selected.
- **Which classes are ops is the document's word**, not the catalogue's: a class the Nodes sidebar files under Values
  but whose `__call__` takes the record (`FilterOp`) still goes into the list.
- **Where a card cannot go yet**, in one line, nothing written: a flow graph (`The editor can't add a value card to a
  flow graph yet — pick an op.` — `an engine card` for a `Stream`), the inside of a subgraph (`The editor can't add a
  value card inside a subgraph yet — pick an op.`), a file that is one object.

### Wiring — a drawn wire is saved

Drag from an output to an input whose type meets it. The editor writes the wire into the document as ONE edit — held
until Save like every edit, one Ctrl+Z takes it out — and the canvas draws what the document now says: reopen the graph
and the wire is still there. An input that held something is replaced. A wire the editor cannot save yet is refused at
once with one line in a toast, and nothing is drawn or written.

On the source graph a host workspace seeds — its source written inside the Stream's slot, the graph's root after it:

```yaml
stream: !class:recordstream.core.stream.Stream
  source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
  ops: []
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
```

drawing `stream.source.class_names → contract.classes`, then `hugging_face_source.image → contract.input` and
`hugging_face_source.class → contract.target`, and pressing Save writes
(`tests/test_wires.py::TestTheUsersSourceGraph`):

```yaml
hugging_face_source: !class:recordstream.sources.huggingface.HuggingFaceSource {path: ylecun/mnist}
stream: !class:recordstream.core.stream.Stream
  source: !ref:hugging_face_source
  ops: []
contract: !class:recordstream.ops.contract.GraphContract
  name: "classification source"
  outputs: {"stream": "Stream", "classes": "List[str]"}
  records: {"input": "Image", "target": "Label"}
  delivered:
    stream: !ref:stream
    classes: !ref:hugging_face_source
    input: image
    target: class
```

| the wire goes into | it is written as |
| --- | --- |
| an output slot of the graph's root (`classes`), or an optional one | `classes: !ref:hugging_face_source` under the root's `delivered:` — `delivered:` is added when the root has none |
| a slot or a setting of any other card (`thing` of `holder`) | `thing: !ref:hugging_face_source` on that card (`{size: 1, thing: !ref:hugging_face_source }` in a one-line card — the space is YAML's) |
| a record entry of the root (`input`), from a card's record entry (`image`) | `input: image` under `delivered:` — the key the root reads `input` from |

- **A `!ref:` names a top-level key only**, so a card written inside another card's slot (`stream.source`) is MOVED
  OUT first: its lines become a top-level key named after its CLASS, in snake case — `HuggingFaceSource` →
  `hugging_face_source:`, `FilesSource` → `files_source:`, `HTTPSource` → `http_source:` — placed above the key that
  held it, and the slot reads `!ref:hugging_face_source`. When that name is taken — by a top-level key, a card, or a
  setting name of ANY card, the one whose slot it sat in included (a top-level key is handed to every card with a
  setting of that name: a key `source` would be given to a second Stream, and back to the first once its wire is
  removed) — it is `hugging_face_source_2`, `_3` …. The card keeps its place on the canvas and its groups; Ctrl+Z puts
  it back in the slot.
- **A card hands itself over, not one output**: `!ref:hugging_face_source` is drawn from the card's FIRST output that
  fits the input, so drawing another one is refused — `hugging_face_source has more than one output that fits
  holder.thing; only stream can be wired into it.`
- **The record entries.** The root card has one input per output slot (`stream`, `classes`, each marked `*`), then one
  per record entry (`input · Image`, `target · Label`), then one per OPTIONAL slot — `optional: {classes: "List[str]"}`
  — without the `*`: the host has its own answer when it is left unwired, so it is never a problem; wired, it is
  written under `delivered:` and removed like an output slot. A source card has one output per record entry it declares
  — its class's `produces` (`image · Image`, `class · Label`) — next to `stream`, and a step card one per entry it
  writes (`ReadImage`: `image · Image`, following its `output`); the outputs follow the card's settings: a HuggingFace
  source with `metadata_features: [coarse_label]` gets a `coarse_label · Label` output, and with every column kept
  (`*`, the default) it lists the columns its dataset's description names (CIFAR-100: `coarse_label`). A record entry
  is read from the card that writes the key LAST into the stream the root delivers: wire from the last step that
  writes `image`, or the source when none does. A key no card declares draws no wire; the root's row shows it
  (`input · Image = pixels`).
- **The root card shows only what you connect** — its sockets, no rows for `name`, `inputs`, `outputs`, `optional`,
  `records`, `delivered` (the text tab shows the declaration) — and its second header line is the contract's name
  (`classification source`). It has no outputs: what its `inputs` declare is the host card's.
- **The host card.** A graph whose root declares `inputs` — what a host hands it when it runs it — draws them on one
  card of their own, titled after the host: `from <host>` in a host's panel, `from the host` standalone. One output
  per input: `files · List[str]`; an item-typed input (a pane's `input · Signal`) is an entry of the record the host
  hands in, and the wire to a step that reads it by name leaves this card. It is no node of the document — it has no
  settings, **Rename… and Delete are greyed** while it is all that is selected (with other cards selected, Delete
  removes those and leaves it), and the routes refuse both (`<host> is the host's side of the graph, not a node of the
  document`); nothing added goes after it. A graph without a root gets the same card for what its steps read that
  nothing produces.
- **The dashed wire — where the host puts an input.** When exactly one card of the stream the root delivers, or of
  its source chain (the card in its `source` slot, that card's own `source`, …), takes a setting named like an
  input, a DASHED wire runs from the host card into that setting: the host fills it there when it runs the graph.
  It is derived from the document, never written; none or several such cards, no wire. Deleting it is refused —
  `The host fills this when it runs the graph — it isn't a wire you can remove.`

The files graph a host workspace seeds (`tests/test_wires.py::TestTheFilesGraph`):

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

is drawn as these wires, and **Check graph** finds nothing:

```
<host>.files         ╌╌>  file_source.files      dashed: where the host puts the files it is handed
file_source.stream   ──>  stream.source
stream.ops[0].op     ──>  stream.ops[0]          the read-image step, a member of ops
stream.stream        ──>  contract.stream *
stream.ops[0].image  ──>  contract.input         input: image
                          contract.classes       optional, unwired
```

**Removing.** Click a wire (`Cmd`+click on a Mac, `Ctrl`+click elsewhere, adds more) and press Delete or Backspace: one edit, one undo for all of them. A
`delivered:` pair goes (and `delivered:` with its last one); a `!ref:` goes and the setting returns to its default; a
card written inside the slot is moved out to a top-level key named after its class and both cards stay. Removing the
wire into a required input leaves it open — **Check graph** then lists it (`contract.classes (List[str]) is required
and nothing is wired into it`); a removal that would break anything else is refused, and so is the dashed wire from
the host card.

What cannot be saved yet — one line each, nothing drawn or written:

```
a record wire, or a wire into a member of ops      This wire would change the order of the steps — the editor can't save that yet.
a wire of a flow (from:, bind:, a gate)              The editor can't save wires in this kind of graph yet.
a wire inside a subgraph                             The editor can't save wires inside a subgraph yet.
from a card inside a list member                     The editor can't save a wire from stream.ops[1].op yet: it sits inside a list (stream.ops[1]).
into a record entry, from what is not one            Only a record entry — like a source's image — can be wired into input.
any other                                            The editor can't save this wire yet.
```

### Set source… — putting a source into a `Stream`

A `Stream`'s `source` is a SLOT that one object fills, not a list. A wire drawn into it brings a card that is already
on the canvas ([Wiring](#wiring--a-drawn-wire-is-saved)); to put a NEW object into it the canvas asks for a class and
its values instead. Three ways in, one dialog: the **set…** button on
the `source` row of the card, **Set source…** in the node's right-click menu, or a wire dragged out of the `source`
slot and dropped on empty canvas. The dialog lists the classes the slot takes (the sources first, then the engines —
a `Stream` is a source too), then the chosen class's form, with the [pickers](#pickers); **Set source** sends only the
values you changed from the class's defaults. Starting from this file:

```yaml
# read_files — a stream over the files of one folder
stream: !class:recordstream.core.stream.Stream
  source:   # nothing yet — Set source… fills it
  ops:
    - !class:recordstream.ops.debug.PrintRecordOp {}
```

choosing `FilesSource` and typing `/data/captures` into its `root` changes ONE line (measured through
`POST …/slot`):

```yaml
  source: !class:recordstream.sources.files.FilesSource {root: /data/captures}   # nothing yet — Set source… fills it
```

What the slot held is replaced — a marker, a `!ref:` or nothing — and the comments around it stay; one Ctrl+Z
restores the text byte for byte. A slot holding a REFERENCE (`source: !ref:files`) gets the new class in the
reference's place, and the dialog says so before you press Set: `FilesSource goes into stream's source slot in place
of its reference to files; files (FilesSource) stays in the document, unwired.` What `POST …/slot` refuses (the page
offers neither), changing nothing:

```
the ops list                     stream.ops is a list — add a member to it, or remove one; it is never set whole
an op into the source slot       stream.source = !class:recordstream.ops.numpy.Threshold {} refused at …/read_files.yaml:2:9:
                                 cannot wire stream.source.op (Op) into stream.source (Source) at …/read_files.yaml:2:9: the types do not meet
```

### Right-click menus

| on | items |
| --- | --- |
| empty canvas | Add Node ▸ (by kind, with a search) · Add Group · Paste · Insert template ▸ · Insert steps file… · Arrange |
| a node | Rename… · Delete · Copy · Group · Convert to subgraph · Save as template… · Run to here — on a `Stream` also Set source… ([above](#set-source--putting-a-source-into-a-stream)), and on a subgraph node also Open · Move to its own file… |
| a group's title bar | Rename… · Colour ▸ · Ungroup · Convert group to subgraph |

A right-click on a node that is not selected selects it first, so the node menu acts on what you see highlighted.
Run to here runs one record and pauses before the node. On the host card Rename… and Delete are greyed — it is the
host's side of the graph, not a node of the document.

### Where a node is drawn — the first layout

A graph opened without a positions file beside it (`<graph>.layout.yaml`) is laid out by the editor: one column per
step of the chain, left to right along the wires, the nodes of a column stacked top to bottom. A card starts below
the card above it — 200 px below that card's top when it is up to 183 px tall, further when it is taller: its
height plus 17 px, on the canvas's 10-px grid. 183 px is six rows under the title (one row per input, parameter and
output), or five on a subgraph card, whose bar of inner steps adds 26 px. The Bluetooth pane, opened without a
positions file:

```
               known (373 px tall)   contract
before         (40, 40)              (40, 240)   only the last 8 px of contract showed
after          (40, 40)              (40, 430)   17 px below known
```

A node the positions file does not name yet — a step an agent added — is laid out the same way, starting below the
lowest card already placed. **View ▸ Arrange in columns** lays the whole graph out this way again; **View ▸ Arrange**
spaces the cards by the sizes the page drew, left to right. Delete a line of the positions file to lay that node
out again, the file to lay out everything.

### Groups

Select nodes and press `G` for a titled, coloured box that moves its members with it. **Graph ▸ Add group** drops
an empty box on free canvas near the middle of the view; **right-click ▸ Add Group** drops it where you clicked.
Drag a node into a box to add it, out of the box to remove it. Resize a box by its corner grip (or its edges when it
is selected): afterwards it holds exactly the nodes whose centre it covers. Groups live in the positions sidecar
beside the graph, never in the YAML — a box that was sized by hand keeps its `bounds`, and a box drawn inside a
subgraph names that level as its `parent`:

```yaml
# demo.layout.yaml
positions:
  prep: [40.0, 40.0]
  prep/grey: [100.0, 60.0]
groups:
- id: g1
  title: front end
  color: '#3b82f6'
  nodes: [prep/grey]
  bounds: [60.0, 20.0, 420.0, 260.0]
  parent: prep
```

### Subgraphs — a flow used as one step

A subgraph is a `flow:` mapping written inside the step that uses it. It is an ordinary op,
`recordstream.flow.subgraph.Subgraph` — no new YAML syntax:

```yaml
flow:
  prep:
    op: !class:recordstream.flow.subgraph.Subgraph
      steps:
        grey:
          op: !class:recordstream.ops.image.ConvertMode {mode: L}
        scaled:
          op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
      result: scaled
  mask:
    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
outputs: mask
```

`result:` names the inner step whose record leaves the subgraph (left out: the last inner step).

**What you see.** The canvas shows TWO nodes, `prep` and `mask`. `prep` is drawn as a stacked blue card — a
`record` in, a `record` out, a bar listing its inner steps (`grey → scaled`) and an `open ▸` button. Its one
setting is `result`; the steps are not a form field. **Double-click** it (or `open ▸`, or right-click ▸ Open) and
the canvas shows its inside:

```
demo › prep                                         ← the breadcrumb; click "demo" or press Escape to go up
<in> ──▶ grey ──▶ scaled ──▶ <out>                  ← <in> / <out> are where the record enters and leaves
```

The inside opens whole when it fits at a readable zoom (0.8 to 1), else at 0.8 with `<in>` in view, and the address follows it — `?graph=demo&level=prep` — so a
reload or a shared link opens the same level (a level the graph no longer holds opens the top). The settings tab
lists the inner steps. Editing `target_max` on `prep/scaled` rewrites that one line; everything else in the file
stays byte for byte.

**Names.** An inner node is named `<subgraph>/<step>`: `prep/grey`, and a subgraph that is the second member of a
Stream's `ops:` list gives `stream.ops[1]/grey`. So a step name may not contain `/`. Inner names are scoped to
their subgraph — an outer `grey` and an inner `grey` are two steps, and renaming the outer one never touches the
inside. A rename stays on its level:

```
rename prep/grey to other/mono
→ …/prep.steps.yaml:2:7: prep/grey can only be renamed on its own level — 'prep/<name>' or a bare name, not 'other/mono'
```

**Making one.** Select consecutive steps and use **Graph ▸ Convert selection to subgraph** (or the node menu's
Convert to subgraph). The steps move inside with their own lines and comments; the last one is the `result`, and
every outer reference to it (`from:`, `merge_from:`, `bind:`, `outputs:`) now names the subgraph. Converting
`grey` and `scaled` of a three-step chain gives exactly the document above. Refused, with the reason:

| you select | the editor says |
| --- | --- |
| `grey` and `mask`, `scaled` between them | `grey and mask are not consecutive steps — select the steps between them too (scaled)` |
| `grey` and `scaled`, while `boxes` reads `grey` | `boxes reads grey (from: grey), which would move inside the subgraph — select boxes too, or leave grey out` |
| steps on two levels | `the nodes to convert must be steps of ONE level — 'prep/grey' and 'tail' are on different levels` |

**Convert group to subgraph** (a group's title-bar menu) does the same for a box's members, names the subgraph
after the box (`front end` → `front_end`) and places it at the box's corner. Consecutive members of a Stream's
`ops:` list become ONE member whose inner steps are named after their classes (`convert_mode`, `scale`). One
Ctrl/Cmd+Z undoes a conversion, box included.

**Running it.** The **run** tab lists the inner rows indented under their subgraph (`└ grey`, `└ scaled`), and a
subgraph row carries a small `subgraph` tag. Select `prep/scaled` and Run: the run pauses INSIDE the subgraph, before
`scaled`; **Step** carries on through the inside, then the outer steps. **Rerun selected node** on `prep/scaled`
recomputes `scaled`, then the steps after `prep` — nothing before it runs again.

### Using one subgraph twice — copy, templates, or include

Three ways, and only these. YAML anchors (`&`/`*`/`<<`) and `!ref:` are not reuse spellings — nothing in the
editor writes them (why: [docs/architecture.md](docs/architecture.md)).

**1. Copy.** Copy a subgraph node and paste it: the clipboard holds the step's own YAML text, comments included,
and the paste is an independent copy (a taken name gets `_2`). Copying a subgraph whose steps are in a steps file
puts those steps inline in the copy, so it never shares the other's file. The clipboard is text, so a copy
pastes into another graph as well:

```yaml
prep:
  op: !class:recordstream.flow.subgraph.Subgraph
    steps:
      grey:
        op: !class:recordstream.ops.image.ConvertMode {mode: L}   # to one channel
      scaled:
        op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
    result: scaled
```

**2. Templates.** **Save as template…** on a subgraph node (or on a selection of steps) writes its
steps to `<templates folder>/<name>.steps.yaml` at once — no Save needed. The templates folder is
`~/.streamstudio/templates` (made on the first save), or the one `--templates-dir` names. **Insert template ▸**
adds a COPY as a new subgraph step after the selected node, named after the template (`prepare`, then
`prepare_2`), with `result:` its last step. Nothing links the copy back: editing it never changes the template.
**Delete template ▸** removes the file. A saved template starts with a line saying where it came from:

```yaml
# prepare: grey, scaled — saved from /home/me/graphs/chain.yaml
grey:
  op: !class:recordstream.ops.image.ConvertMode {mode: L}   # to one channel
scaled:
  op: !class:recordstream.ops.numpy.Scale {source_min: 0.0, source_max: 255.0, target_min: 0.0, target_max: 1.0}
```

**3. Include a steps file.** **Move to its own file…** (a subgraph node's menu) moves the steps to
`<graph folder>/prep.steps.yaml` — a bare steps mapping, the same grammar as `flow:` — and writes `steps: {include:
prep.steps.yaml}` in their place; the new file is written with the graph on Save. Until then it exists only in the
editor: a check, an edit or a run reads it from there and writes nothing into the folder, so in a folder you
cannot write they all keep working — only Save needs the folder. **Insert steps file…** (the empty-canvas menu) adds a subgraph step that
includes an existing one:

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

Each use runs its own op objects. An edit inside either use lands in `prep.steps.yaml`, so it changes **every**
use — that is what include is for. Measured: `target_max: 0.5` set on `prep/scaled` reads `0.5` on `prep2/scaled`
too, and both nodes are located at `prep.steps.yaml:4:7`. Use Copy or a template when the uses must differ.

### What the canvas does, gesture by gesture

The page writes only through the routes below, and draws what the server answers; a refusal is a toast carrying
the server's located message.

| you do | the page calls | what the document says afterwards (in the editor, until Save) |
| --- | --- | --- |
| change a widget on a node, or in the **settings** tab | `POST …/param` | the value on its own line (a refused value reverts) |
| click a Nodes-sidebar entry, or pick one in the palette / Add Node ▸ | `POST …/steps` — on a flow `{after}` the selected node (else the flow's output; inside a subgraph, its last step); on a Pipeline `{after}` the selected member, else `{list: "stream.ops"}`; the host card and the root are never the selected node | on a flow a new step, `from:` the anchor; on a Pipeline a new member after the selected one, or at the end of the list; a class that is not an op: a card of its own, `class_names_scan: !class:…ClassNamesScan {}` above the root ([A class that is not an op](#a-class-that-is-not-an-op--a-card-of-its-own)), drawn where the menu or the palette was opened |
| drag a sidebar entry onto the canvas | `POST …/steps`, then its position | the same, placed at the drop point |
| drag a wire out of a socket and drop it on nothing | the palette, narrowed to the classes that fit that type; a pick sends `POST …/steps` with `wire` — the end dragged out of | the step (or member) inserted after that node; out of a Pipeline's empty `transforms[0]` input, at the end of its `transforms`; a card: written and wired as ONE edit (out of the root's `classes`, `classes: !ref:class_names_output`) |
| drag a wire between two sockets whose types meet ([Wiring](#wiring--a-drawn-wire-is-saved)) | `POST …/wires {source, source_socket, target, target_socket}` | the wire in the document's own spelling — `classes: !ref:hugging_face_source` under the root's `delivered:`, `thing: !ref:hugging_face_source` on a card, `input: image` for a record entry; a card inside another card's slot moved out to a top-level key named after its class first. A wire it cannot save yet: one line in a toast, nothing drawn (a mismatch is refused while dragging) |
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
| `Ctrl+Z` / `Ctrl+Shift+Z` | a document edit: `POST …/undo` / `…/redo`; a move or group: the page writes the old layout back | the text history is the server's — an agent's edit is undone the same way |
| **run** tab: *run* (record #N, pausing before the selected node), *step*, click a node | `POST …/trace`, `GET/POST /api/runs/<run>…` | unchanged — each node shows its status, time and generation |
| **run** tab in an editor mounted in a host, on a graph with no source of its own: *run* | `POST …/trace {}` — no record number; the host's record runs | unchanged — the panel says `record: the workspace's selection` before (the host's `name`), and after names what ran (`run_… · rec_7 · 0.25–0.75 s · 1–2 MHz`) and what the host filled in (`window: start_s = 0.25, stop_s = 0.75`) |
| *set…* on a Stream's `source` row, **Set source…** in its right-click menu, or a wire dragged out of the `source` slot onto nothing; pick a class (sources first), fill its form, **Set source** | `POST …/slot {node, slot, class_path, params}` with the values changed from the defaults | `  source: !class:recordstream.sources.files.FilesSource {root: /data/captures}` — what was in the slot is replaced; one undo takes it out. A slot holding a REFERENCE (`source: !ref:files`) gets the new class in the reference's place, and the dialog says the referenced node stays, unwired: `FilesSource goes into stream's source slot in place of its reference to files; files (FilesSource) stays in the document, unwired.` After a pick the keyboard is in the form's first field (Escape cancels, Tab moves on) — and in the dialog itself while the class's settings are still loading, so Escape cancels then too; the arrows keep the highlighted class in view |
| the `…` beside a folder or file field ([Pickers](#pickers)) | `GET /api/files` (a folder) or `GET /api/files?show=all` (any file) — the editor's machine | the path chosen, as if typed |
| point at the list beside a HuggingFace source's `path`, `split`, `input_feature` / `target_feature`, or *add column…* beside `metadata_features` | `GET /api/hf/datasets`, `…/splits?path=`, `…/features?path=&split=` — asked once per question, only when you reach for the list | the name picked, as if typed (`*` replaces the column list, a column is appended) |
| **settings** tab: tick *all parameters* | `GET …/form` (already read) | unchanged — every parameter of the level shown instead of the `app:` fields; the tick stays for that graph while you look at another tab |
| a `(min, max)` pair: type the min, Tab, type the max | `POST …/param` twice, in that order: `[min, old max]`, then `[min, max]` | the pair as typed — the max box keeps what you type while the first answer arrives |
| in a node's entry: change a value, *rerun this node* | `POST /api/runs/<run>/rerun` with the values that differ | unchanged — the value is a trial; the node and what follows are recomputed. On a host's record the form starts from what the host FILLED (the window, the band — what ran), and those are sent only when you type new ones: a pane spelling `start_s: 0` reruns on the host's 0.001–0.003 s, not on the whole capture |
| **Graph ▸ Check graph** | `GET …/check` | unchanged — every problem, with its `file:line:col` |

### What it refuses, and how it says so

A refused edit changes nothing. The message is the server's, verbatim (paths shortened to `…`). A refusal's or a
warning's toast has a small **copy** button on its corner: it copies the whole message — to report it — and then
says `copied` (`copy failed` when the browser refuses the clipboard); a click on the message itself closes the toast.

| you try | what you see |
| --- | --- |
| drag an `Image` output onto a `Label` input | the wire does not attach — no edge, no message |
| drag a wire between two steps of a `flow:` (`decode.image` → `boost.image`) | `The editor can't save wires in this kind of graph yet.` — no edge |
| drag a record wire on a Pipeline (`stream.ops[0]` → `stream.ops[2]`) | `This wire would change the order of the steps — the editor can't save that yet.` — no edge |
| drag a second output of a card into an input another of its outputs fits first (`source.class_names` → an untyped setting) | `source has more than one output that fits holder.thing; only stream can be wired into it.` |
| type a value the class refuses (`limit` → `-1`) | `clip.limit = -1 refused at …/clip.yaml:5:9: clip: Failed to construct Clip at …/clip.yaml:5:9: limit must be positive, got -1` — the widget shows the old value again |
| delete every step of a subgraph from inside it | `'prep/grey' is the only step of the subgraph 'prep' — nothing would be left for it to return; remove 'prep' instead` — nothing is removed |
| add a step named `a/b` inside `prep` | `'a/b' is not a name on the level of 'prep' — name it 'b'` |
| add a step to a subgraph whose steps are written on one line | `…/demo.yaml:6:14: the steps of the subgraph 'prep' are written on one line ({…}) — spell them as a block mapping, one step per line, to add a step` |
| add a class to a list written in brackets over several lines | `…/brackets.yaml:2:8: stream.ops is written in brackets over several lines — spell it as a block list (one '- ' item per line) to add to it or remove from it` |
| add a class that is not an op (`ClassNamesScan`) to a flow graph | `The editor can't add a value card to a flow graph yet — pick an op.` (`an engine card` for a `Stream`; inside a subgraph `… inside a subgraph yet — pick an op.`) |
| pick a card from a wire whose end the card cannot take (`stream.ops[0].image` → `ClassNamesScan`) | `class_names_scan has no input that takes stream.ops[0].image (Image).` — nothing is added |
| add a class on a graph holding two op lists, nothing selected | `this graph holds 2 op lists (stream.ops, other.ops) — select a member of one, or the node holding it, to add to it` (the page's words; nothing is sent) |
| drag a wire out of the FIRST member's `record` input and drop it on nothing | `nothing feeds stream.ops[0] — a step can only be inserted after another one` (the page's words) |
| rename a node to a name in use | `'gain' is already a node (the nodes are: ['gain', 'clip'])` — the title goes back |
| *run* a graph with no source of its own, in the standalone editor (mounted in a host, the host's record runs — [Mounting the editor in a host app](#mounting-the-editor-in-a-host-app)) | `clip.yaml: this graph has no source to draw a record from — a host supplies the record (mount the editor in one) or wire a source` |
| *run* such a graph while the host has no record to give | the host's own words, as it raised them: `no record is selected in the page — open one there, then press Run` |
| put a class into a list slot (`stream.ops`) with *set source…* | `stream.ops is a list — add a member to it, or remove one; it is never set whole` |
| put a class whose output does not meet the slot into it (an op into `stream.source`) | `stream.source = !class:recordstream.ops.numpy.Threshold {} refused at …/source.yaml:15:3: cannot wire stream.source.op (Op) into stream.source (Source) at …/source.yaml:15:3: the types do not meet` — nothing changes |
| undo with nothing to undo | `wires.yaml: nothing to undo — the editor holds no earlier edit of this document` |

### What is not there yet

- **A host's fills inside a subgraph.** A host's window and band reach the ops on the graph's own level — a step of
  a `flow:`, a member of the `Stream`'s `ops` — not an op inside a subgraph step.
- **A source the host fills.** A graph whose source is wired but empty until the HOST fills it when it runs the graph
  (a workspace's files graph: a `FilesSource` with no files) is refused by Run in the editor, in words — the host
  hands the editor a record, never the files for a source.
- **Folding the side panel in a host's panel.** It stays 320 px wide; a large graph opens fitted, every card shown,
  and you zoom in to read it.
- **Wires that change the order of steps, and a flow's wires.** A Pipeline's record wires ARE its `ops:` order, and a
  `flow:`'s wires are its steps' `from:` / `bind:` / gates: drawing one is refused in one line
  ([Wiring](#wiring--a-drawn-wire-is-saved)). Reorder a Pipeline by removing and adding members.
- **A member at the START of a list.** Adding goes after a member or at the end, so a new first member is made by
  adding it and deleting the ones before it. Paste, Insert template and Insert steps file place by the older rule:
  after the selected node — into an empty `ops: []` with nothing selected they are refused.
- **Adding to, or removing from, a list written in an included steps file.** Edit that file's list there.
- **An include that spells a steps file you have not saved in another case.** On a disk that ignores case (macOS,
  Windows), `include: PREP.steps.yaml` finds `prep.steps.yaml` once it is saved; before Save the edit that writes it
  is refused: `…/run.yaml: does not load: ConfigFileNotFoundError: … includes PREP.steps.yaml: Not found:
  PREP.steps.yaml`. Spell it as Move to its own file… wrote it, or Save first. A steps file already on disk is found
  whatever the case.
- **Unsaved work that ends without a stop.** A crash, a `kill -9` or closing the editor's terminal loses it
  ([what a stop cannot keep](#unsaved-work-survives-a-stop)).
- **A subgraph held in another op's slot** (`RandomApply(op=Subgraph(…))`) is drawn and edited as a subgraph node
  (`maybe.op`), but the run tab shows no rows for its inside.
- **A quick first open for an editor started on its own over graphs naming a class nothing has imported yet.** Over
  graphs spelling `_target_: WindowSignal`, `streamstudio <folder>`'s first graph list still takes about 6 s: nothing
  has imported that class's package, so the walk above goes through every backend listed before it (85 entry
  points). An editor mounted in a host app that has already imported the package is not affected.
- **One short class name for two installed classes.** When two installed modules each register a class of one name
  (a NumPy `FourierOp` in `<package>.fourier` and a torch one in `<package>.fourier_torch`, say), a graph spelling
  `_target_: FourierOp` is drawn with the first of the two the lookup imports, while a run refuses the name
  (`AmbiguousClassError 'FourierOp' is registered by 2 classes, so a bare lookup cannot choose between them`). Spell
  such a class by its dotted path: `_target_: <package>.fourier.FourierOp`.

### The document model and node names

`streamstudio.document.Document` opens a graph file and derives a model from it — nodes with typed sockets,
wires, located problems — by reading declarations that already exist (`recordstream.algorithm_spec`, an op's
`consumes`/`produces`, `confluid.input_specs` / `output_specs` / `to_pydantic`, `GraphContract.inputs`/`outputs`)
and the `recordstream.items` registry for socket types. Nothing is written per class. An edit patches the line the
marker came from; the file is never re-dumped:

```
re-dump of a shipped 24-step pane:   275 lines / 148 comment lines  ->  179 lines / 0 comments
                                     root: $DATA_ROOT/pets           ->  the resolved path, the variable gone
set_param(size, max_band_hz, 30e6):  1 line differs; 148 comment lines kept; $DATA_ROOT kept
```

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

**A class spelled by its registry key** (`!class:AnnotationUpdateSink`, not the dotted path) is found once the
package defining it is imported. On the first document naming such a class, the editor imports the installed
packages' `confluid.configurables` entry points one at a time — those of packages already loaded first, then the
rest in their listed order — and stops at the first that registers it; the rest are left for a later miss, and none
is imported twice. A package another request is still importing (two first lookups at once) is waited for, so a
class half a second from being registered is never reported as missing.
Measured 2026-10-02, the editor mounted in a host app over a sink graph naming one class:

```
before:  first graph list  6.0 s   every entry point imported (127)
after:   first graph list  1.1 s   15 imported, the walk stopping at the one that registers AnnotationUpdateSink
```

A name nothing defines imports them all, once; a later miss then costs nothing.

**Node names** are one scheme shared by the model, the engine, the sidecar and
the routes: a `flow:` step by its key (`floor`), a list member by `path[i]` (`stream.ops[1]`), a nested marker by
its document path (`processor.stream`), a contract root by its key (`contract`), the host's side of a graph as one
node, `<host>` — for a graph without a contract what its steps read that nothing produces, for a rooted one the
`inputs` its contract declares — and a node inside a subgraph as `<subgraph>/<step>` (`prep/grey`), with
the two boundary nodes `prep/<in>` and `prep/<out>`. An inner node carries the `file:line:col` of its own line —
in the steps file when the steps are included.

### The routes

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
| GET | `/api/workspace` | `{folder, session, recovery, host}` — `host` is `{name}` for an editor mounted with a host ([Mounting the editor in a host app](#mounting-the-editor-in-a-host-app)), `null` standalone; `recovery` is `null`, or the unsaved work a stop kept: `{saved_at, graphs: [{name, path, disk: unchanged\|changed\|gone\|unreadable, reason, changed_on_disk, saved_at}], error, aside}`; for a file that cannot be used, `graphs` is empty, `error` says why and `aside` is where discard moves it |
| POST | `/api/workspace/recovery/restore` | open the kept graphs (unsaved, no undo history) and remove the file; answers the graph list, each entry with `restored`; `404` when there is nothing kept |
| POST | `/api/workspace/recovery/discard` | remove the file (move an unreadable one aside); answers the graph list; `404` when there is nothing kept |
| GET | `/api/graphs` | `[{name, path, kind, dirty, shipped}]` — `kind` is `flow`, `graph` or `contract`; `shipped` is a graph the host ships whose folder holds no copy yet ([A graph the host ships](#a-graph-the-host-ships)) |
| POST | `/api/graphs` | `{name, kind: flow\|pipeline\|source, dir?}` — WRITE the starter to `<dir or the graph folder>/<name>.yaml` at once and add it to the list; answers the graph with `dirty: false` (its `name` may be `fresh~2`). Refused, writing nothing, when the file exists, the name breaks the file-name rule, or the folder is not a writable folder (`404` when it does not exist) |
| POST | `/api/graphs/open` | `{path}` — add any `.yaml` on this machine to the list |
| GET | `/api/files?dir=&show=` | `{dir, parent, entries: [{name, path, kind: dir\|yaml\|file}]}` — folders first; `show=graphs` (the default: File ▸ Open, a folder picker) adds the `*.yaml` files but the layout sidecars, `show=all` (a file picker) every file as `file`; hidden entries never; no `dir` = the graph folder; `$VAR` in `dir` is expanded |
| GET | `/api/graphs/<name>` | `{name, shipped, path, kind, text, model, positions, groups, history_depth, dirty}`; `model.inside` holds each subgraph's inside, recursively; `model` shows each setting of the graph's settings file on its node, `text` is the graph's own |
| PUT | `/api/graphs/<name>` | `{text}` replaces the whole document in the editor; the history starts over |
| POST | `/api/graphs/<name>/save` | write every changed file — the graph, its settings file, a steps file — and the sidecar; refused when a file changed on disk. `{only: "settings"}` writes the graph's settings file alone (a settings sheet's save): a change of the graph itself stays held |
| POST | `/api/graphs/<name>/save-as` | `{path, overwrite?}` — write the graph to another file; the entry follows it |
| POST | `/api/graphs/<name>/revert` | read the file again, dropping unsaved changes |
| GET | `/api/graphs/<name>/check` | `{problems: [{message, loc}]}` |
| POST | `/api/graphs/<name>/param` | `{node, param, value}` — a SETTING (a field the `app:` block names) goes to the graph's settings file, one line each ([A graph's settings file](#a-graphs-settings-file--graphsettingsyaml)); any other parameter: patch one value on its own line, or insert it when the file left the default |
| POST | `/api/graphs/<name>/slot` | `{node, slot, class_path, params}` — put a new object into a single slot (`stream.source`), replacing what it held; one edit. Answers the graph plus `set` (the node now in the slot, `stream.source`) |
| POST | `/api/graphs/<name>/wires` | `{source, source_socket, target, target_socket}` — save a drawn wire in the document's own spelling ([Wiring](#wiring--a-drawn-wire-is-saved)); one edit. Answers the graph plus `wired` (the card the wire comes from now — `source` for a `stream.source` moved out of its slot); a wire it cannot save yet is a `400` with one plain line |
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
| GET | `/api/graphs/<name>/form` | `{node: JSON-schema properties}` from `confluid.to_pydantic` — the settings form, every level; a parameter with a picker carries `picker` ([Pickers](#pickers)); `requires` (the gate a branch sets) is never a field |
| GET | `/api/graphs/<name>/app` | `{declared, graph, fields: [{node, param, loc}], form, problems: [{message, loc}]}` — the graph's `app:` fields ([Settings: a graph's `app:` fields](#settings-a-graphs-app-fields)); `graph` is `editable` or `settings-only` ([A graph that offers its settings only](#a-graph-that-offers-its-settings-only)); `declared: false` and no field without an `app:` block |
| GET | `/api/hf/datasets` | `{datasets, reason}` — the HuggingFace datasets this machine has cached, and why there are none when there are none |
| GET | `/api/hf/features?path=&name=&split=` | `{features, all_columns}` — the columns of that dataset (`[]` without one, or when it cannot be read) and the `metadata_features` word for "every other column" (`*`) |
| GET | `/api/hf/splits?path=&name=` | `{splits}` — the split names of that dataset |
| GET | `/api/palette` | every configurable class as a typed node spec, without its settings (`params_schema`, `params_defs`); waits for the catalogue's helper process, which serving the page starts; `?refresh=1` runs a new one (it sees packages installed or edited since). `503 {error}` when the helper stopped before it answered — the next request runs a new one |
| GET | `/api/palette/<class path>` | one class's whole node spec, settings included — at once when its package is loaded in the editor, else when the helper has described it; `404 {error}` naming the close class paths for an unknown one |
| POST | `/api/graphs/<name>/trace` | `{record, until}` — run record `#record` of the graph's own source through what the editor holds, pausing before `until` (`prep/scaled` pauses inside); a graph with no source runs the record its host supplies (`record` does not apply). Answers the run: `{run, graph, record, host, trace, statuses, generations, paused_at}` — `record: null` and `host: {label, filled: {node: {attribute: value}}}` for a host's record |
| GET | `/api/runs/<run>` | the trace: per node its params, inputs, outputs, status and milliseconds; inner rows carry `parent` |
| POST | `/api/runs/<run>/rerun` | `{node, params}` — rebuild one node through its constructor and rerun from it |

### Mounting the editor in a host app

A consuming Flask app mounts the API and the page under ONE prefix of its own — the page calls `api/…` relative to
its own address, so the two must sit side by side — and gives the editor a HOST: any object with a `name` and four
methods.

```python
from pathlib import Path
from typing import Mapping, Sequence

from streamstudio.app import HostRecord, editor_blueprint, page_blueprint, studio_of


class WorkspaceHost:
    name = "the workspace"                                     # how the page names it: "record: the workspace's selection"

    def folder(self) -> Path:                                  # asked at EACH request
        return workspace.state_dir()

    def record(self, graph: str, path: Path) -> HostRecord:    # a Run of a graph with no source of its own
        record, (t0, t1), (f0, f1), name = workspace.selection()   # raise ValueError("…") when there is none
        return HostRecord(
            record=record,
            fills=({"start_s": t0, "stop_s": t1}, {"f0_hz": f0, "f1_hz": f1}),
            label=f"{name} · {t0:.3f}–{t1:.3f} s",
        )

    def saved(self, paths: Sequence[Path]) -> None:            # after every Save, Save As and New… that wrote one
        workspace.rerun_panes_reading(paths)                   # spelled under folder() as folder() spells it

    def shipped(self) -> Mapping[str, Path]:                   # the graphs it ships from outside folder() — or {}
        return {"view_bte": package_dir / "bte_view.yaml"}


app.register_blueprint(editor_blueprint(host=WorkspaceHost()), url_prefix="/studio")   # /studio/api/…
app.register_blueprint(page_blueprint(), url_prefix="/studio")                          # the page at /studio/
...
studio_of(app).stash()   # on the host's own stop: the unsaved work is offered at the next start
```

```
GET /studio            -> 308 /studio/            GET /studio/api/graphs    -> the host folder's graphs
GET /studio/           -> the built page          GET /api/workspace        -> the HOST's own route, untouched
GET /studio/assets/…   -> the page's assets       GET /studio/api/workspace -> {folder: <the host's folder>, …,
                                                                                 host: {name: "the workspace"}}
```

The smallest host, whole — a folder holding one graph with no source of its own, and a record of one image. It runs
as written (Flask's test client, no server); the three answers below are what it printed:

```yaml
# state/pane.yaml
flow:
  mask:
    op: !class:recordstream.ops.numpy.Threshold {low_level: 0.5}
```

```python
import sys
from pathlib import Path

import numpy as np
from flask import Flask
from recordstream.items import Image

from streamstudio.app import HostRecord, editor_blueprint, page_blueprint


class Host:
    name = "demo"

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def folder(self) -> Path:
        return self._folder

    def record(self, graph: str, path: Path) -> HostRecord:
        image = Image(np.linspace(0, 1, 16, dtype=np.float32).reshape(4, 4))
        return HostRecord({"image": image}, fills=({"low_level": 0.25},), label="the demo image")

    def saved(self, paths) -> None:
        print("saved:", [p.name for p in paths])

    def shipped(self):
        return {}


app = Flask(__name__)
app.register_blueprint(editor_blueprint(host=Host(Path(sys.argv[1]))), url_prefix="/studio")
app.register_blueprint(page_blueprint(), url_prefix="/studio")

client = app.test_client()
print(client.get("/studio/api/workspace").get_json()["host"])
run = client.post("/studio/api/graphs/pane/trace", json={}).get_json()
print(run["record"], run["host"], run["statuses"])
client.post("/studio/api/graphs/pane/param", json={"node": "mask", "param": "low_level", "value": 0.75})
client.post("/studio/api/graphs/pane/save")
```

```
$ python smallest_host.py state
{'name': 'demo'}
None {'filled': {'mask': {'low_level': 0.25}}, 'label': 'the demo image'} {'mask': 'ok'}
saved: ['pane.yaml']
```

The Run asked the host for its record and set its fill on `mask` (`Threshold` has a `low_level`); the Save wrote
`low_level: 0.75` into `state/pane.yaml` and told the host.

- **`name`** is how the page names the host (`record: the workspace's selection`); `GET /api/workspace` answers it
  as `host: {name}` — `null` for a standalone editor — which is also how the page knows Run asks the host.
- **`folder()`** is asked at each request. When it names another folder than before — a workspace's first save
  MOVES its state folder out of a temporary one, a save-as COPIES it — every graph of the old folder is the same
  file in the new one: the list follows, and a graph held unsaved keeps its edits and its undo history and saves
  into the new folder. A graph opened from elsewhere (File ▸ Open) stays where it is.
- **`record(graph, path)`** answers what a **Run** of a graph WITHOUT a source of its own runs — a `flow:` alone, a
  `Stream` whose `source` is empty. The run takes it through the text the editor holds, unsaved values included,
  on the same tracer as a graph's own record (step, pause before a node, rerun a node all work). Each `fills` group
  is set on every op holding ALL its attributes, on the graph's own level, before the run: the window above fills
  `WindowSignal` (it has `start_s` and `stop_s`), the band fills a tuner (`f0_hz`, `f1_hz`), and an op with
  `start_s` alone gets nothing. A rerun keeps them — it rebuilds a node from its current values. The run answers
  `record: null` and `host: {label, filled: {node: {attribute: value}}}`. A host with no record raises
  `ValueError("no record is selected in the page — open one there, then press Run")`, and that sentence is the
  run's refusal. Anything else going wrong is a refusal in words naming the host, never a bare server error:

  ```
  record() raises IndexError  ->  400 …/view_x.yaml: the workspace could not give a record: IndexError: list index out of range
  record() answers a dict     ->  400 …/view_x.yaml: the workspace answered a dict, not a HostRecord
  HostRecord(record=None)     ->  400 …/view_x.yaml: the record the workspace gave is None, not a mapping of entries
  folder() raises             ->  400 the workspace could not name its folder: LookupError: …   (every request)
  ```

  A graph WITH a source draws its own record; the host is not asked. When that source yields NOTHING — a source the
  host fills itself when IT runs the graph (the files a workspace hands its files graph) — the run says so:
  `files.yaml: stream.source (FilesSource) yields no records, so there is no record #0 to run. The editor runs a source
  as the document spells it — what the workspace fills into it when the workspace runs this graph is not handed to the
  editor; spell what the source reads (a folder, files) to run it here`.
- **`saved(paths)`** is told, after every Save, Save As and New… (New writes its starter file at once), which graph
  files it wrote — the graph's own last: `[…/prep.steps.yaml, …/sub.yaml]`. A Save of positions alone writes only the
  layout sidecar and tells nothing; Save as template… writes no graph and tells nothing. A Save of a changed SETTING
  tells the settings file (`[…/view_bte.settings.yaml]`). If `saved` raises, the write stands: the answer is the graph
  plus `host_error: "RuntimeError: …"`, for the page to show.
- **`shipped()`** names the graphs the host ships from OUTSIDE its folder (a package's templates), by name: each is
  listed and opened while the folder holds no file of its name ([A graph the host ships](#a-graph-the-host-ships)).
  A host that ships nothing answers `{}`. If it raises, the request is a refusal in words naming the host, like
  `folder()`'s.
- **Every path the host is handed is spelled ONE way** — under `folder()` exactly as `folder()` spells it, for a file
  in that folder (else resolved): `record(graph, path)` and `saved(paths)` name `/var/…/T/workspace-x/view_x.yaml`
  for a folder given as `/var/…/T/workspace-x`, never its resolved `/private/var/…` twin. A host can match a path with
  `path.parent == folder()` or `path.relative_to(folder())`.
- **A graph file the host writes itself** (a conversion, an edit of its own source graph) is read again the next
  time the editor is asked for it — when the editor holds NOTHING unsaved of it: the list, the panel and Run then
  show what the file says. A graph holding unsaved edits keeps them, and its Save refuses ("changed on disk since
  the editor read it — Revert to load it, or Save As to keep both"). Post `open` for the graph shown (below) to make
  the panel show the new text at once.
- **Relative `include:` entries follow a moved folder**: when `folder()` names another folder, a graph the editor holds
  that includes a file OUT of the old folder (`../../home/gert/steps/prep.steps.yaml`, as Insert steps file… writes
  it) is rewritten to name the same file from the new place (`../../steps/prep.steps.yaml`), in its undo history
  too, as Save As does; the graph is then unsaved, and Save writes the corrected include. An include between two
  files of the moved folder is left as written.
- **`studio_of(app).stash()`** on the host's stop keeps the unsaved work in the host folder's recovery file
  ([Unsaved work survives a stop](#unsaved-work-survives-a-stop)); the next mount over that folder offers it, and
  the page asks the same question as the standalone editor.
- **One request at a time:** the panel's requests and the host's own reach the editor from the threads of one server,
  so every editor request holds `studio_of(app).lock` from start to answer, and `stash()` takes it too. A request that
  waits on something outside the editor takes no turn — the class catalogue and a class's settings (a helper process
  that imports every installed package), a plugin's list, the HuggingFace lookups — and a Run holds it only while it
  reads the graph: a catalogue that hangs leaves the panel, its saves and the host's own requests working. A route of the
  host's own that reads, changes and saves a document holds it across all three — without it, such a route racing the
  panel's load died with `RuntimeError: dictionary changed size during iteration`:

  ```python
  studio = studio_of(app)
  with studio.lock:                     # waits while the panel's request is served
      document = studio.document("view_fft")
      document.set_param("fft", "size", 4096)
      studio.save("view_fft", only="settings")
  ```

#### The page in the host's panel

The host shows the page in an `<iframe>` of its own page, `?embed=1` added to the link: the menus and every function
stay; the streamstudio logo goes (the host's page says what app this is), the side panel is narrower and the minimap
is off until View ▸ Minimap. The page and the host then talk through `postMessage`, same origin only, and the page
listens to its own parent window only:

```html
<iframe id="studio" src="/studio/?graph=view_bte&tab=settings&embed=1"></iframe>
<script>
  // what the page posts — on every change of what it shows or holds, and after a Save wrote a file
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.source !== "streamstudio") return;
    // {source: "streamstudio", type: "state", graph: "view_bte", dirty: true, unsaved: ["view_bte"], tab: "settings"}
    // {source: "streamstudio", type: "saved", graph: "view_bte", path: "/w/state/view_bte.yaml"}
    // {source: "streamstudio", type: "unavailable"}   — from the page served INSTEAD of the editor
    // {source: "streamstudio", type: "committed", graph: "view_bte"}   — the answer to a `commit`
  });
  // what the host asks: show another graph (and tab) WITHOUT reloading — a reload asks "Leave site?" when unsaved
  studio.contentWindow.postMessage({target: "streamstudio", type: "open", graph: "view_fft", tab: "run"}, location.origin);
  // …or, about to READ what the page writes (a run button beside a settings panel): write the field being typed in
  studio.contentWindow.postMessage({target: "streamstudio", type: "commit"}, location.origin);
  // …or say what the host RUNS the graph with over its settings (values it applies by name and never writes)
  studio.contentWindow.postMessage({target: "streamstudio", type: "values", graph: "view_scan",
                                    values: {rule: "fixed"}}, location.origin);
</script>
```

A host that saves a SETTING itself does it through the editor — `studio_of(app).document(name).set_param(node, param,
value)`, then `studio_of(app).save(name, only="settings")`, the editor staying the settings file's one writer — and
posts `open` for the graph shown: the page shows the new value (an `open` of the graph shown re-presents it when its
text or its model changed).

`values` changes nothing written: the settings form grays a field by the host's value of the parameter it depends on
(below, [A field another parameter does not ask for is grayed](#a-field-another-parameter-does-not-ask-for-is-grayed))
instead of the graph's — the values a host sent last stand until it sends others for that graph.

A field is written when it is LEFT, so a host button pressed while a field still holds its typed value would read the
file before that write lands. `commit` leaves the field (writing it — and, on a `?only=settings` page, saving it) and
the page answers `committed` once every write on its way is done: the host reads the files then.

`unsaved` lists every graph holding unsaved edits — what closing the panel would leave unsaved (the edits stay in the
editor; `studio_of(app).stash()` keeps them over the host's stop). `unavailable` is posted by the page `/` answers
when there is no editor to show — the package carries no built page, the page's source changed and Node was missing at
the server's start, or the start's build failed — and that page says which and what to do. No editor answers in that frame: a host that covers its panel
until the first `state` uncovers it on `unavailable`, and loads the frame again (rather than posting `open`) to show
another graph. `saved` is posted after Save, Save As and New….
An `open` naming a graph the editor does not list shows the missing-graph message, as a link does, naming the folder
the editor serves NOW (the page asks again at every `open` and before New…, so a folder the host moved is the one
shown and written to).

What an `open` does to what the person was doing in the panel:

- a value typed into a field and not left yet is written to the graph it was typed in — the field is left first —
  and the next graph draws its own values (a node of the same name included);
- Set source… closes: it was about the graph it was opened on (the palette and the context menu close too);
- an `open` of the graph already shown reads it again: a file the host rewrote is shown as the file says.

In the panel a graph's top level opens WHOLE (a fit, never above zoom 1): measured at 900x700, the shipped Bluetooth
pane's 30 cards all show at zoom 0.057, where zoom 1 from the top-left showed 1 of them. The standalone page keeps
its zoom-1 first view.

`editor_blueprint(graph_dir)` (the standalone editor's) and `editor_blueprint(host=…)` are the two ways; both, or
neither, is refused before anything is registered. `page_blueprint()` serves the page the package carries
(`web_dist()`, `streamstudio/canvas`), the placeholder when there is none. The editor's state lives on `app.extensions["streamstudio"]` (`studio_of`), so
two apps in one process are two editors.

`import streamstudio.app` is cheap for the host's start: measured 2026-10-01, 0.2 s, and no torch.

### Settings: a graph's `app:` fields

A graph may say which of its parameters are its SETTINGS — an `app:` block listing `[Class, param]` pairs.
`GET …/app` answers them, for the settings tab to open on that short form:

```yaml
app:
  inputs:
    - [RefuseLargeWindow, max_band_hz]
    - [ClassifyBluetooth, deviation_hz__lo]   # the two halves of one range field …
    - [ClassifyBluetooth, deviation_hz__hi]   # … are ONE field, deviation_hz
```

```
bte_view: GET …/form -> 247 fields on 30 nodes      GET …/app -> 30 fields (32 entries; two ranges as __lo/__hi)
```

A class may be named short (`ClassifyBluetooth`) or dotted; a class used by two nodes gives a field on each. An
entry that names no node, no parameter of its class, or `requires` (the gate a branch sets — never a field, here or
in `GET …/form`) is a problem in words, at the entry's `file:line:col`:

```
…/pane.yaml:19:7: the app input [Missing, level] names no node of this graph (its classes are: FormKnobs, FormOther)
```

The settings tab lists those fields until **all parameters** is ticked, which lists every parameter of the level the
canvas shows. A value set in a field the block names is a SETTING: it goes to the graph's settings file, never into
the graph ([A graph's settings file](#a-graphs-settings-file--graphsettingsyaml)). A graph WITHOUT an `app:` block names no settings, and the tab says so rather than listing its Stream's
plumbing, the values a host fills in at Run and its root contract as though they were settings:

```
☐ all parameters   the graph has no app: block
This graph names no settings — it has no app: block. Its parameters are on the canvas; tick all parameters to list every one here.
```

#### A field another parameter does not ask for is grayed

A parameter only some values of ANOTHER parameter read says so in its schema — `applies_when: {<parameter>: [values]}`,
a pydantic `Field` mark on the constructor parameter, which `confluid.to_pydantic` carries into `GET …/form` and
`GET …/app` — and the settings form GRAYS it while that parameter holds any other value: disabled, its value kept, its
title saying why. Nothing is hidden and nothing is refused.

```python
class Detector:
    def __init__(
        self,
        rule: Literal["fixed", "median"] = "median",
        level: Annotated[float, Field(json_schema_extra={"applies_when": {"rule": ["fixed"]}})] = -150.0,
    ) -> None: ...
```

```
rule    median
level   -150        (grayed — title: "rule is median — read only while rule is fixed")
```

The value compared is the host's when it sent one (`values`, above), else the node's; compared as text. A declaration
that is no map of lists, or names a parameter the node does not hold, grays nothing. Several entries must all hold.

### A graph's settings file — `<graph>.settings.yaml`

The values a person changes of the fields the `app:` block names never go into the graph: they go into a file of
their own beside it, one line each. The graph keeps its nodes and wiring; the settings file keeps values.

```yaml
# view_pane.yaml — the graph (here a host ships it, see below)
flow:
  first:
    op: !class:knobs_demo.Knobs {level: 0.25}
  second:
    op: !class:knobs_demo.Knobs {}
    from: first
outputs: second
app:
  inputs:
    - [Knobs, level]
```

```
POST …/view_pane/param {node: second, param: level, value: 0.9}   then   POST …/view_pane/save
saved: ['view_pane.settings.yaml']                 # what the host is told — the graph itself is not written
view_pane.settings.yaml: 'flow.second.op.level: 0.9\n'
```

A setting names its node:

| the node | its line |
| --- | --- |
| a `flow:` step — by its NAME, so two nodes of one class are set apart | `flow.second.op.level: 0.9` |
| a top-level object — by its key | `sink.output_dir: /data/out` |
| a node with no name (a member of an `ops:` list) — by its CLASS, while the class is used by that node alone | `AcfSlice.max_points: 512` (or the block `AcfSlice: {max_points: 512}`) |
| a parameter that ONE node of the graph alone has — bare, confluid's own spelling (its broadcast reaches that one node) | `floor_db: -60.0` |

A bare name two nodes share is refused naming the full keys to write (`level is a parameter of 2 nodes (first,
third) — a bare line would set them all; write flow.first.op.level or flow.third.op.level`); a name that is none of
the graph's settings is refused as before, the message adding which parameters may be written bare.

The editor shows each setting on its node (the canvas and the settings tab read the same model); the text tab shows the
graph itself. Undo takes a setting back like any edit. Save writes only the files that changed — the settings file
alone after a setting, the graph after a change to the graph. Save As takes the settings along
(`<new>.settings.yaml`, refused like the graph when a file is already there) and leaves the old settings file as it
was. A field the `app:` block does NOT name (ticked in with *all parameters*) is a change to the graph, as before.

**Loading them together.** The two are ONE confluid document by confluid's own `include:` rule: the graph first, the
settings below it, so a setting wins over what the graph spells. A host runs it with `load_graph` — the settings file
may sit anywhere (a host keeps a shipped graph's in its own folder); nothing is written:

```python
from streamstudio.settings import load_graph, settings_path

built = load_graph(package / "pane_view.yaml", state / "view_pane.settings.yaml")   # None, or a missing file: the graph alone
built["flow"]["first"]["op"].level, built["flow"]["second"]["op"].level              # (0.25, 0.9)
settings_path(Path("state/view_bte.yaml"))                                            # state/view_bte.settings.yaml
```

**A line that is none of the graph's settings is refused where it is written** — by `load_graph` (a `SettingsError`)
and by the editor's Check. Confluid by itself warns and IGNORES an unknown key, so a misspelt setting would run the
graph's value:

```
state/view_pane.settings.yaml:2:1: flow.second.op.levle is not a setting of view_pane — its settings are: flow.first.op.level, flow.second.op.level
```

So are a class line that two nodes with no name share (`SetKnobs is the class of 2 nodes with no name
(stream.ops[1], stream.ops[2]) — a setting cannot tell them apart; give them names (flow steps) to set each`), the
`app:` entry naming such a class (a located problem in `GET …/app`, and no field), and a `flow:` block — confluid merges
one by moving its step to the END of the flow, so the refusal names the one-line spelling instead
(`… write each setting on a line of its own: flow.first.op.level: 0.9`).

Two edits that would leave a setting naming nothing are refused while the setting is there: removing or renaming the
node a setting names (`… flow.first.op.level is not a setting of pane …`) — change or remove the line first.

#### A graph the host ships

A host may SHIP graphs from outside its folder (`shipped()`, [Mounting the editor in a host app](#mounting-the-editor-in-a-host-app)).
Such a graph is listed and opened while the folder holds no file of its name — `shipped: true` in the list and in the
graph — and it holds nothing unsaved. A change of a setting writes only its settings file into the folder; the FIRST
Save of a change to the graph itself writes the graph there, and that copy wins from then on (`shipped: false`). The
shipped file is never written. Revert of a graph not copied yet reads the shipped file again.

```
GET /studio/api/graphs              -> [('view_pane', shipped: True)]     state/: []
a setting changed, Save             -> state/: ['view_pane.settings.yaml']
a step added, Save                  -> state/: ['view_pane.settings.yaml', 'view_pane.yaml']   (saved: ['view_pane.yaml'])
```

The settings stay in force on the copy: the canvas of the copy shows them, and a Run runs them.

#### A graph that offers its settings only

A graph may say that a person changes its settings and nothing else — `graph: settings-only` in its `app:` block. The
editor then refuses every other change (a node, a wire, a step, a rename, the text, a parameter the block does not name)
before anything is held, and `GET …/app` answers `graph: settings-only` so a host can leave out its button to the
canvas:

```yaml
app:
  graph: settings-only
  inputs:
    - [Knobs, level]
```

```
POST …/locked/steps {…}   ->  400 {"error": "locked offers its settings only — its graph is fixed where it is shipped"}
```

Without the line a graph is `editable`; another word is a located problem (`…:6:10: graph: frozen is not what a graph
lets a person change — one of: editable, settings-only`) and the graph stays editable.

The editor reads `inputs` and `graph` out of the block and nothing else: any other key there is the host's own (how a
host lists the graph, say), and the editor's edits and saves leave its line as it is.

#### The settings and nothing else — `?only=settings`

A host that shows a graph's settings in a sheet of its own opens the page with `?only=settings` (beside `?embed=1`):
no menus, no canvas, no tabs and no *all parameters* box — the `app:` fields alone, at the panel's full width — and
every change is SAVED at once, so the host hears `saved` and re-runs what reads the graph. The save writes the
settings file ALONE (`POST …/save {only: "settings"}`): a change of the graph itself that the editor holds unsaved in
another view of the same graph stays unsaved there. Toasts and the restore question work as on the whole page.

```html
<iframe src="/studio/?graph=view_bte&embed=1&only=settings"></iframe>
```

A graph without an `app:` block says `This graph names no settings — it has no app: block.`

### Pickers

`GET …/form` (and the palette) marks a parameter that gets a picker with `picker`, read off the class:

| the parameter | `picker` |
| --- | --- |
| a name holding `file` / `checkpoint` / `ckpt` and no folder word (`checkpoint_path`, `log_file`) | `file` — browse `GET /api/files?show=all` |
| a name holding `path` / `dir` / `root` / `folder`, or annotated `pathlib.Path` (`output_dir`, `checkpoint_dir`) | `directory` — browse `GET /api/files` |
| a bool, int or float (`checkpoint_strict`, `root_count`), or a `Literal` (its choices are already a list) | none |
| on a `HuggingFace*` source: `path` / `repo_id` | `hf_dataset` — `GET /api/hf/datasets` (`path` is a hub id there, not a folder) |
| … `input_feature` / `target_feature` | `hf_feature` — `GET /api/hf/features?path=<its dataset>` |
| … `split` | `hf_split` — `GET /api/hf/splits?path=<its dataset>` |
| … `metadata_features` | `hf_metadata` — the same columns, plus `*` (every other column) |
| a parameter its class stamps with a tag (the class's widget stamp, `{"output_dir": "PLATFORM_DATASET_ID"}` — the attribute `streamstudio.palette.picker_of` reads) | the stamp, whatever the rules above say |
| … a stamp an installed [plugin lists](#a-plugins-lists-beside-a-field) | the stamp and `lookup: true` — the plugin's list, `GET /api/lookups/<tag>?<the node's values>` |

A picker fills the field; the field stays editable. The HuggingFace lookups never fail a request: no dataset, or
one that cannot be read, answers `[]` (the datasets list says why).

On the page a `directory` or `file` field gets a `…` button that opens the editor's file browser — on the editor's
machine, starting at the folder the field names — and the HuggingFace fields a list beside them. A field naming
`$DATA_ROOT/captures` starts where the variable points; when the editor's machine does not have the variable, the
browser says so and shows the editor's folder (`DATA_ROOT is not set on the editor's machine ($DATA_ROOT/captures) —
showing the editor's folder instead`). The browser takes the keyboard while it is open: Escape closes it alone (over
Set source…, Set source… stays with what was typed). A list is asked for only when you point at it (a column lookup
can open a dataset on the editor's machine, which nobody asked for by opening a graph), each question once for the
page's life; an empty list says why in its tooltip, in the server's words (`no datasets cached at
/home/me/.cache/huggingface/hub`), and a list whose lookup FAILED asks again the next time you point at it.

### A plugin's lists beside a field

A field its class stamps with a tag of its own (the widget stamp `streamstudio.palette.picker_of` reads, mapping a
parameter to a tag: `{"dataset": "SHOP_DATASET"}`) is drawn as a plain text field — unless an installed plugin supplies
a LIST for that tag. A plugin is a module an installed package names in the `streamstudio.plugins` entry-point group:

```toml
# the plugin package's pyproject.toml
[project.entry-points."streamstudio.plugins"]
my-plugin = "my_plugin.studio"
```

and that module declares `LOOKUPS`, one function per tag:

```python
# my_plugin/studio/__init__.py
from typing import List, Mapping, Tuple

def shop_datasets(values: Mapping[str, str]) -> Tuple[List[str], str]:
    """The node's current values in (`{"project": "store", …}`), the names and why there are none out."""
    names = ask_the_shop(project=values.get("project", ""))
    return names, "" if names else "the shop has no datasets"

LOOKUPS = {"SHOP_DATASET": shop_datasets}
```

The form marks the field `lookup: true` beside its `picker` tag, and the page puts a `list…` beside it, asked
(`GET /api/lookups/SHOP_DATASET?project=store&dataset=…`) when the person points at it — with the node's current text,
number and switch values, so a list can follow the field beside it (the versions of the dataset named there): a changed
field asks again. A list never fails a request: a tag no plugin lists, a function that raises and one that takes longer
than 20 s (`streamstudio.lookups.TIMEOUT_S`) answer no names and say why (`{"names": [], "reason": "ConnectionError:
…"}`). The plugins' lists are read once per process; a plugin that fails to import is skipped with a warning naming
it. The group is read from installed metadata — reinstall an editable plugin after adding the entry point.

### Building the canvas page

The page's source is `frontend/` (vite + React Flow + TypeScript). Its BUILD is committed too, inside the package:
`streamstudio/canvas` — `index.html`, `assets/index-*.js`, `assets/index-*.css` and `.built-from`, the record of the
source it was built from. The editor serves that folder at `/`, so every install has the page: a checkout without
Node, an install from GitHub (`pip install git+https://github.com/Gearlux/streamstudio.git@main`, which carries no
`frontend/` at all), a wheel. **Node and npm are needed only to EDIT the page** — `pyproject.toml` declares them as a
system dependency, so `aisland setup` installs them.

**Editing the page: commit the rebuilt `streamstudio/canvas` with the source change.** Say one label of the
palette changed in `frontend/src/canvas/Palette.tsx`. Until the page is rebuilt, a test fails — in CI too, which has
no Node — and says what to do (`tests/test_committed_page.py`, measured; the folder shortened to `…`):

```
AssertionError: the committed editor page is older than its source — start streamstudio once, or run `cd
streamstudio/frontend && npm install && npm run build`, and commit streamstudio/streamstudio/canvas (the page in
…/streamstudio/canvas records source '005e8579…'; the source now is b3a4b936…)
```

A server builds the page as it STARTS when the page's source changed since its build — `streamstudio <folder>` does
it, and so does an app that mounts the editor when it starts (it calls `streamstudio.app.ensure_page_built()` before it
serves):

```
streamstudio: the editor's page changed since its last build — building it
streamstudio: built the editor's page in 5.5 s
```

and `git status` then shows what to commit beside the source — the test passes again:

```
 M frontend/src/canvas/Palette.tsx
 M streamstudio/canvas/.built-from
 D streamstudio/canvas/assets/index-D8HFRaK-.js
 M streamstudio/canvas/index.html
?? streamstudio/canvas/assets/index-CMOyHA-n.js
```

A fresh checkout, and every `git pull`, start silently: the committed page matches its committed source. Two
branches that both changed the page merge as a conflict in `streamstudio/canvas` (git treats the folder as binary —
`.gitattributes`): resolve it by building the page again and committing what the build writes.

What each line a start can print means (each begins `streamstudio: `):

| line | what happened |
| --- | --- |
| *(nothing)* | the page is current — the check took about 2 ms |
| `the editor's page is not built yet — building it` | `streamstudio/canvas` holds no page (it was deleted) |
| `the editor's page changed since its last build — building it` | a file the build reads changed |
| `the editor's page has no record of the source it was built from — building it` | the page holds no `.built-from` (built before the record existed); it is built once more to record its source |
| `installing the page's libraries first (npm ci) — the first time takes about a minute` | `frontend/node_modules` is missing, or `package-lock.json` is newer than npm's own record of its last install |
| `another start is building the editor's page — waiting for it` | a second server started from the same checkout while the first builds; it waits, then finds the page current |
| `built the editor's page in 2.0 s` | done; the time covers the install too |
| `the editor's page is out of date and Node is not installed — run aisland setup` | the page's source changed and there is no Node to build it: the server starts anyway, and `/` says the same instead of serving the old page (`… is not built and Node is not installed …` when there was no page) |
| `the editor's page has no record of the source it was built from and Node is not installed — the page built earlier is served` | a page without a record may well be current: without Node it is served as it is |
| `building the editor's page failed — npm run build:page exited with code 2; its last lines:` and npm's last 20 lines | the server starts anyway, and `/` says it failed and names the command that shows why |
| `building the editor's page failed — npm ci did not finish in 300 s` | npm was stopped, with everything it started (`npm ci` may take 300 s, `npm run build:page` 120 s — an unreachable registry otherwise kept a start waiting for good) |
| `interrupted before the editor's page was built — the next start builds it` | Ctrl+C during the check: npm is stopped and the start ends (exit code 130) |

"Node is not installed" means `node` and `npm` are not on the server's `PATH`. On Linux `aisland setup` puts them in
`aisland/bin`, which `source project.bashrc` adds to `PATH`.

What counts as the page's source: everything under `frontend/src/` and `frontend/public/` except the page's tests
(`__tests__/`, `*.test.*`, `*.spec.*`) and hidden files, plus `index.html`, `package.json`, `package-lock.json`,
`vite.config.*`, `tsconfig*.json` and the `.env` files `vite build` reads that every clone has (`.env`,
`.env.production` — not `.env.test` or `.env.development*`). A change to a test file builds nothing, and neither does a
file `frontend/.gitignore` ignores — `*.local`, `.env.local` and `.env.production.local` among them. The record
`streamstudio/canvas/.built-from` is a SHA-256 over those files' relative paths and bytes — never where the checkout is
or when its files were written — so it is the same on every machine; `frontend/.gitattributes` checks the source out
with LF line endings everywhere (a Windows checkout that wrote CRLF would otherwise find the page out of date). Every
input is a file a clone carries, so the record a developer commits is the one CI and every other checkout compute. The
page reads no `import.meta.env`: a `.env.local` of your own changes nothing the browser runs, and if the page ever
reads one, its values come from `.env` / `.env.production`, which are committed.

Whether `npm ci` runs first is decided by file TIMES, because npm keeps no record of the lock it installed from: a
`package-lock.json` rewritten with the same bytes (a branch switched away and back) installs once more at the next
start that builds — about a minute the first time, seconds from npm's cache. A start whose page source did not change
installs nothing.

Two servers started at once from the same checkout never build together on macOS and Linux (there is no lock on
Windows): the second says it waits, waits, then finds the page current. Only a START checks — making an app
(`create_app`, `page_blueprint`) never builds anything, and an install, which has no `frontend/` source, is never
checked: it serves the page its package carries. A start that could not build the page (no Node, a failed build) does
not try again in the same process: an app that checks before it announces its server and again as it serves prints
its lines once, and a server process it starts in the background takes its answer over through
`page_check_environment()` — passed in the child's environment — instead of running npm again where its output is
discarded.

Building by hand works the same, and is what the page's own tools need:

```bash
cd frontend
npm install      # once, and again after package.json changes
npm run build    # begin, then tsc --noEmit && vite build -> streamstudio/canvas, then record (python3 ../streamstudio/page.py)
npm test         # the page's unit tests (vitest)
npm run dev      # optional: a hot-reloading copy on vite's own port, proxying api/ to an editor on port 8190
```

`npm run build` is three steps — `python3 ../streamstudio/page.py begin && npm run build:page && python3
../streamstudio/page.py` — and the two around vite write `.built-from` with the same fingerprint a start uses
(`streamstudio: recorded the source of the editor's page in …/streamstudio/canvas/.built-from`), so a page built by hand
is current. The first notes the fingerprint BEFORE vite reads a file, the last records that note: a file saved while
vite runs (about 3 s) is not vouched for — the build ends with `streamstudio: the editor's page's source changed since
its build began — build again (npm run build)` and no record, and the next start builds the page again. `npm run
build:page` is the build alone (`tsc --noEmit && vite build`), which is what a server's start runs: a start needs Node
and nothing else, while the hand build also needs a `python3` (3.8 or later) command on `PATH` — python.org's Windows
installer has `python` and `py` but no `python3`, which is why the start does not depend on it. vite writes no source
map (`*.map` is gitignored and not in the wheel); `npm run dev` serves the sources with maps for debugging.

Only a package that carries no build at all — `streamstudio/canvas` deleted — answers `/` with a placeholder that says
what to do:

```
streamstudio editor
The canvas page is not built. Build it once — cd streamstudio/frontend && npm install && npm run build — and
reload; the page lands in streamstudio/canvas inside the package. The API is up:
  GET /api/graphs
  GET /api/graphs/<name>
  …
```

The page served at `/` is looked up at every request, so a page built after the server's app was made is served
without a restart — unless this server's start could not build it: then `/` keeps saying why (no Node, or the
build failed) until the next start, which checks again.

The browser tests of the page (`tests/test_canvas.py`, headless chromium) run against the committed build; they skip
only when `streamstudio/canvas/index.html` is absent, and the skip reason is that same build command.

## Installation

```bash
pip install streamstudio
```

The editor's page comes built inside the package ([Building the canvas page](#building-the-canvas-page)); Node is needed
only to change the page. The editor keeps every file you edit — the graph, a steps file it includes, each one Move to
its own file… makes — in memory, and hands them to confluid as text with the graph's own path, instead of writing them
to disk; confluid then looks for every included file beside the file that names it, whatever folder you started the
editor in.

### The palette is what is installed

The Nodes sidebar lists every `@configurable` class the installed packages declare through the `confluid.configurables`
entry-point group. A domain package — your own ops, a signal library, a trainer — needs no registration here: install
it however you normally would (from PyPI, a git checkout, editable) and restart the editor (`streamstudio restart`);
its classes appear. There is no extra to enable and nothing to configure.

### Shell completion

The CLI is a [liquifai](https://github.com/Gearlux/liquifai) app, so completion comes from the shared installer
rather than a per-CLI hook:

```bash
liquifai-install-completions            # bash / zsh / fish
streamstudio re<TAB>                    # -> restart
```

### Options

Every option is derived from the command function's signature, so `--help` on a command is generated rather than
hand-maintained and cannot go stale:

```bash
streamstudio restart --help             # or -h
```

Options work on either side of the command name (`streamstudio --port 9000 restart` ≡
`streamstudio restart --port 9000`), and a key may be spelled with hyphens or underscores. `-p` is the short form of
`--port`:

```bash
streamstudio stop -p 8191               # stop whatever StreamStudio listens on 8191
```

A flag that names no parameter is **refused**, not ignored
([Starting and stopping it](#starting-and-stopping-it)).

## Dependencies

| Package | Purpose |
|---------|---------|
| `recordstream` | The record engine a Run steps through, and the op-chain contract a graph is checked against. |
| `confluid` | Loads the YAML documents; `to_pydantic` gives each class's settings form; the `confluid.configurables` entry-point group the palette is built from. |
| `flask` | The editor's server — a blueprint a host app can mount. |
| `pyyaml` | Locating, and patching, the line an edit is about. |
| `liquifai` | The CLI. |
| `loggair` | Logging. |
| `psutil` | `stop` / `restart` find the server by the port it holds. |
| `datasets` | The HuggingFace pickers: a dataset's columns and splits. |
| any package declaring `confluid.configurables` *(a plugin, never a dependency — install it and its classes appear)* | A domain's classes in the palette. |

## Development

```bash
git clone git@github.com:Gearlux/streamstudio.git
cd streamstudio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

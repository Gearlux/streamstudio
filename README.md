# StreamStudio

A graph editor for [RecordStream](https://github.com/Gearlux/recordstream) pipelines written as confluid YAML
documents — and, behind `--editor comfy`, a [ComfyUI](https://github.com/comfyanonymous/ComfyUI) extension that
auto-generates ComfyUI nodes from RecordStream / Waivefront operations.

```bash
streamstudio path/to/graphs      # the canvas editor over that folder:  http://127.0.0.1:8190
streamstudio --editor comfy      # the ComfyUI editor:                  http://127.0.0.1:8188
```

The two are separate servers and can run side by side. The canvas editor is the rewrite
([`docs/rewrite.md`](docs/rewrite.md)) and is described first. From [Installation](#installation) down, this page
describes the ComfyUI editor, which a consuming annotation workspace still opens until the rewrite's phase 4
([what is not there yet](#what-is-not-there-yet)).

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

An option that belongs to the ComfyUI editor is refused by name, not ignored:

```
$ streamstudio --comfy-dir /path/to/ComfyUI
--comfy-dir only applies to --editor comfy
$ streamstudio restart -b
--background only applies to --editor comfy
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
YAML as the editor holds it), **settings** (a form of every node on the level the canvas shows) and **run**.

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

`N` (or the **Nodes** button) opens every class the editor can describe, grouped by **package** (the first part of
the class path) and then by **kind** — Sources, Ops, Algorithms, Engines, Sinks, Runnables, Values, Contracts. The
search box narrows by every word you type. Each entry shows its name, one line of description and coloured chips
for the record entries it reads and writes. Hovering shows the whole description and the parameters; a long list
ends with `+37 more parameters` rather than running off the window. Docstring markup such as
``:class:`DirectorySink` `` and ``` ``fields.json`` ``` shows as inline code.

**Click** an entry to add it after the selected node; **drag** it onto the canvas to place it where you drop it.
The footer says which: `Click adds after grey · drag places it`. On a Pipeline (a `Stream` with an `ops:` list) with
nothing selected it reads `Click adds at the end of stream.ops · drag places it` ([Pipelines](#pipelines--adding-to-and-removing-from-an-ops-list)).
The `Subgraph` class is not listed — an empty one
is refused — and the footer says how one is made instead:
`A subgraph is made from steps: Graph ▸ Convert selection to subgraph, Insert template ▸ or Insert steps file…`.
Double-clicking empty canvas opens the same catalogue as a search palette.

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

The palette, right-click ▸ Add Node ▸ and a wire dropped on empty canvas add the same way. Every other line —
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

### Right-click menus

| on | items |
| --- | --- |
| empty canvas | Add Node ▸ (by kind, with a search) · Add Group · Paste · Insert template ▸ · Insert steps file… · Arrange |
| a node | Rename… · Delete · Copy · Group · Convert to subgraph · Save as template… · Run to here — and on a subgraph node also Open · Move to its own file… |
| a group's title bar | Rename… · Colour ▸ · Ungroup · Convert group to subgraph |

A right-click on a node that is not selected selects it first, so the node menu acts on what you see highlighted.
Run to here runs one record and pauses before the node.

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

**2. Templates, like ComfyUI.** **Save as template…** on a subgraph node (or on a selection of steps) writes its
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
prep.steps.yaml}` in their place; the new file is written with the graph on Save. **Insert steps file…** (the
empty-canvas menu) adds a subgraph step that includes an existing one:

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
| click a Nodes-sidebar entry, or pick one in the palette / Add Node ▸ | `POST …/steps` — on a flow `{after}` the selected node (else the flow's output; inside a subgraph, its last step); on a Pipeline `{after}` the selected member, else `{list: "stream.ops"}` | on a flow a new step, `from:` the anchor; on a Pipeline a new member after the selected one, or at the end of the list |
| drag a sidebar entry onto the canvas | `POST …/steps`, then its position | the same, placed at the drop point |
| drag a wire out of a socket and drop it on nothing | the palette, narrowed to the classes that fit that type | the step (or member) inserted after that node; out of a Pipeline's empty `transforms[0]` input, at the end of its `transforms` |
| drag a wire between two sockets | nothing — a dashed proposal | unchanged: no route writes a wire yet; a mismatch is refused while dragging |
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
| in a node's entry: change a value, *rerun this node* | `POST /api/runs/<run>/rerun` with the values that differ | unchanged — the value is a trial; the node and what follows are recomputed |
| **Graph ▸ Check graph** | `GET …/check` | unchanged — every problem, with its `file:line:col` |

### What it refuses, and how it says so

A refused edit changes nothing. The message is the server's, verbatim (paths shortened to `…`):

| you try | what you see |
| --- | --- |
| drag an `Image` output onto a `Label` input | the wire does not attach — no edge, no message |
| drag between two sockets whose types meet, where the document has no such wire | a dashed wire and `wires.yaml: the wire decode.image → boost.image is drawn as a proposal, not written — on boost it would be spelled 'from: decode' (a record wire) or 'bind: {image: decode.image}' (a value), and no route writes those yet. …` |
| type a value the class refuses (`limit` → `-1`) | `clip.limit = -1 refused at …/clip.yaml:5:9: clip: Failed to construct Clip at …/clip.yaml:5:9: limit must be positive, got -1` — the widget shows the old value again |
| delete every step of a subgraph from inside it | `'prep/grey' is the only step of the subgraph 'prep' — nothing would be left for it to return; remove 'prep' instead` — nothing is removed |
| add a step named `a/b` inside `prep` | `'a/b' is not a name on the level of 'prep' — name it 'b'` |
| add a step to a subgraph whose steps are written on one line | `…/demo.yaml:6:14: the steps of the subgraph 'prep' are written on one line ({…}) — spell them as a block mapping, one step per line, to add a step` |
| add a class to a list written in brackets over several lines | `…/brackets.yaml:2:8: stream.ops is written in brackets over several lines — spell it as a block list (one '- ' item per line) to add to it or remove from it` |
| add a class on a graph holding two op lists, nothing selected | `this graph holds 2 op lists (stream.ops, other.ops) — select a member of one, or the node holding it, to add to it` (the page's words; nothing is sent) |
| drag a wire out of the FIRST member's `record` input and drop it on nothing | `nothing feeds stream.ops[0] — a step can only be inserted after another one` (the page's words) |
| rename a node to a name in use | `'gain' is already a node (the nodes are: ['gain', 'clip'])` — the title goes back |
| *run* a graph with no source of its own | `clip.yaml: this graph has no source to draw a record from — a host supplies the record (mount the editor in one) or wire a source` |
| undo with nothing to undo | `wires.yaml: nothing to undo — the editor holds no earlier edit of this document` |

### What is not there yet

- **The flip.** A consuming annotation workspace still opens the ComfyUI canvases (`streamstudio run --editor
  comfy`), not this editor. Mounting this editor in their place is phase 4, and the ComfyUI extension described
  from [Installation](#installation) down is deleted in the same phase.
- **Writing a wire.** A drawn wire stays a dashed proposal until a route can write `from:`, `bind:` or `!ref:`.
- **A member at the START of a list.** Adding goes after a member or at the end, so a new first member is made by
  adding it and deleting the ones before it. Paste, Insert template and Insert steps file place by the older rule:
  after the selected node — into an empty `ops: []` with nothing selected they are refused.
- **Adding to, or removing from, a list written in an included steps file.** Edit that file's list there.
- **Unsaved work that ends without a stop.** A crash, a `kill -9` or closing the editor's terminal loses it
  ([what a stop cannot keep](#unsaved-work-survives-a-stop)).
- **A steps file made by Move to its own file… and not saved yet** is written into the graph folder for the length
  of each load (a trace, a check) and removed afterwards; the list and Save never see it.
- **A subgraph held in another op's slot** (`RandomApply(op=Subgraph(…))`) is drawn and edited as a subgraph node
  (`maybe.op`), but the run tab shows no rows for its inside.
- **The HuggingFace pickers and the directory chooser** of the ComfyUI nodes. The settings tab lists every
  parameter; it does not yet narrow to a pane's `app:` field list.
- **The page inside the package.** An install from GitHub serves a placeholder until a release builds the page
  in ([Building the canvas page](#building-the-canvas-page)).

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
a message naming `file:line:col`. **Node names** are one scheme shared by the model, the engine, the sidecar and
the routes: a `flow:` step by its key (`floor`), a list member by `path[i]` (`stream.ops[1]`), a nested marker by
its document path (`processor.stream`), a contract root by its key (`contract`), the host's side of a graph
without a contract as one node, `<host>`, and a node inside a subgraph as `<subgraph>/<step>` (`prep/grey`), with
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
| GET | `/api/workspace` | `{folder, session, recovery}` — `recovery` is `null`, or the unsaved work a stop kept: `{saved_at, graphs: [{name, path, disk: unchanged\|changed\|gone\|unreadable, reason, changed_on_disk, saved_at}], error, aside}`; for a file that cannot be used, `graphs` is empty, `error` says why and `aside` is where discard moves it |
| POST | `/api/workspace/recovery/restore` | open the kept graphs (unsaved, no undo history) and remove the file; answers the graph list, each entry with `restored`; `404` when there is nothing kept |
| POST | `/api/workspace/recovery/discard` | remove the file (move an unreadable one aside); answers the graph list; `404` when there is nothing kept |
| GET | `/api/graphs` | `[{name, path, kind, dirty}]` — `kind` is `flow`, `graph` or `contract` |
| POST | `/api/graphs` | `{name, kind: flow\|pipeline\|source, dir?}` — WRITE the starter to `<dir or the graph folder>/<name>.yaml` at once and add it to the list; answers the graph with `dirty: false` (its `name` may be `fresh~2`). Refused, writing nothing, when the file exists, the name breaks the file-name rule, or the folder is not a writable folder (`404` when it does not exist) |
| POST | `/api/graphs/open` | `{path}` — add any `.yaml` on this machine to the list |
| GET | `/api/files?dir=` | `{dir, parent, entries: [{name, path, kind: dir\|yaml}]}` — the file browser; no `dir` = the graph folder |
| GET | `/api/graphs/<name>` | `{name, path, kind, text, model, positions, groups, history_depth, dirty}`; `model.inside` holds each subgraph's inside, recursively |
| PUT | `/api/graphs/<name>` | `{text}` replaces the whole document in the editor; the history starts over |
| POST | `/api/graphs/<name>/save` | write every changed file and the sidecar — refused when a file changed on disk |
| POST | `/api/graphs/<name>/save-as` | `{path, overwrite?}` — write the graph to another file; the entry follows it |
| POST | `/api/graphs/<name>/revert` | read the file again, dropping unsaved changes |
| GET | `/api/graphs/<name>/check` | `{problems: [{message, loc}]}` |
| POST | `/api/graphs/<name>/param` | `{node, param, value}` — patch one value on its own line, or insert it when the file left the default |
| POST | `/api/graphs/<name>/steps` | `{name?, after?, list?, class_path, params}` — `after` a flow step: a new step on its level named `name`, and what read `after` now reads it; `after` an op-list member (`stream.ops[1]`): a new member after it; no `after` and `list: "stream.ops"`: a new member at the end. Answers the graph plus `added` (the new node's name) |
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
| GET | `/api/graphs/<name>/form` | `{node: JSON-schema properties}` from `confluid.to_pydantic` — the settings form, every level |
| GET | `/api/palette` | every configurable class as a typed node spec; `?refresh=1` rescans the registry |
| POST | `/api/graphs/<name>/trace` | `{record, until}` — run record `#record` of the graph's own source through what the editor holds, pausing before `until` (`prep/scaled` pauses inside) |
| GET | `/api/runs/<run>` | the trace: per node its params, inputs, outputs, status and milliseconds; inner rows carry `parent` |
| POST | `/api/runs/<run>/rerun` | `{node, params}` — rebuild one node through its constructor and rerun from it |

A consuming Flask app mounts the API and the page under ONE prefix — the page calls `api/…` relative to its own
address, so the two must sit side by side:

```python
from streamstudio.app import editor_blueprint, page_blueprint, web_dist

app.register_blueprint(editor_blueprint(graph_dir, templates_dir=templates), url_prefix="/editor")   # /editor/api/…
app.register_blueprint(page_blueprint(web_dist()), url_prefix="/editor")                              # the page at /editor/
```

```
API at /editor, page_blueprint(web_dist()):   GET /editor/ -> the built page     GET /editor/api/graphs -> 200
API at the root, page_blueprint():            GET /editor/ -> the placeholder    GET /editor/api/graphs -> 404
```

The editor's state lives on `app.extensions["streamstudio"]`, so two apps in one process are two editors.

### Building the canvas page

The page's source is `frontend/` (vite + React Flow + TypeScript, committed). Building it needs node and npm, which
`pyproject.toml` declares as a system dependency. Build it once:

```bash
cd frontend
npm install      # once, and again after package.json changes
npm run build    # tsc --noEmit && vite build  ->  frontend/dist
npm test         # the page's unit tests (vitest)
npm run dev      # optional: a hot-reloading copy on vite's own port, proxying api/ to an editor on port 8190
```

`frontend/dist` is a build product: gitignored, not inside the wheel, not built by CI. `streamstudio` serves it at
`/` when it is there. Until then — and after a `pip install` from GitHub, which carries no page — `/` answers
a placeholder that says what to do:

```
streamstudio editor
The canvas page is not built. Build it once — cd streamstudio/frontend && npm install && npm run build — and
reload; the page lands in frontend/dist beside the package. The API is up:
  GET /api/graphs
  GET /api/graphs/<name>
  …
```

The browser tests of the page (`tests/test_canvas.py`, headless chromium) skip when `frontend/dist/index.html` is
absent, and the skip reason is that same build command.

## Installation

StreamStudio is not on PyPI yet; install it from GitHub:

```bash
pip install git+https://github.com/Gearlux/streamstudio.git@main
```

The canvas editor needs nothing more (plus a built page — [Building the canvas page](#building-the-canvas-page)).
ComfyUI itself is **not** a Python dependency — `streamstudio --editor comfy` clones it on first run (see below).

### Domain node sets are plugins, not requirements

StreamStudio generates its palette from whatever `@configurable` callables are installed. So a
domain package — a signal/RF library, a task-specific trainer, your own ops — needs no
registration here: install it, and its nodes appear. Nothing in StreamStudio imports one
unconditionally, and the two places that use one (the colormap list and the array→image
render) fall back cleanly when it is absent.

Install the domain package however you normally would — from PyPI, from a git checkout,
editable — and restart the editor (`streamstudio restart`, or `streamstudio restart --editor comfy`). There is no extra to enable and nothing to configure
here: discovery reads what the environment holds.

### Shell completion

The CLI is a [liquifai](https://github.com/Gearlux/liquifai) app, so completion comes from
the shared installer rather than a per-CLI hook:

```bash
liquifai-install-completions            # bash / zsh / fish
streamstudio exp<TAB>                   # -> export  export-workflow
```

### Options

Every option is derived from the command function's signature, so `--help` on a command is
generated rather than hand-maintained and cannot go stale:

```bash
streamstudio restart --help             # or -h
```

Options work on either side of the command name (`streamstudio --port 9000 restart` ≡
`streamstudio restart --port 9000`), and a key may be spelled with hyphens or underscores.
The launcher commands carry short forms — **`-p`** for `--port`, **`-b`** for `--background`
(the ComfyUI editor only), and `-o` for `--output` on the export/import commands:

```bash
streamstudio restart --editor comfy -b  # the ComfyUI editor, detached
streamstudio stop -p 8189               # stop whatever StreamStudio listens on 8189
```

`--editor canvas|comfy` picks the editor for `run` (the default command), `stop` and `restart`;
the default port follows it — 8190 for `canvas` (the default), 8188 for `comfy`.

Three spellings differ from a typical argparse CLI:

| | |
|---|---|
| turn a boolean **off** | `--install-` (and `--install+` for on) — not `--no-install` |
| pass a list | `--custom-node=[urlA,urlB]` — not a repeated flag |
| the palette file | `--palette-config` — `--config` is liquifai's own config-file flag |

A flag that names no parameter is **refused**, not ignored:

```
$ streamstudio restart --prt 9000
Error: streamstudio: unknown option 'prt' — it names no parameter of the 'restart'
command and no key in the configuration. Did you mean 'port'? …
```

## Quick start — the ComfyUI editor

Everything from here down is the ComfyUI editor. Start it with `--editor comfy`:

```bash
streamstudio --editor comfy
```

On first run this:

1. Clones ComfyUI into `~/.streamstudio/ComfyUI` when it is missing, and on EVERY launch syncs its `requirements.txt` into the active environment (pip is a no-op when satisfied — a checkout that ComfyUI-Manager updated in place otherwise imports a module the environment never got; `--install-` skips the sync).
2. Installs the [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) custom node, plus any custom nodes declared by installed packages or requested by you — see [Custom nodes](#custom-nodes-comfyui-manager--extras) below.
3. Symlinks the installed `streamstudio` package into `ComfyUI/custom_nodes/streamstudio`.
4. Launches ComfyUI (`python main.py --port 8188`) under the **same interpreter**, so ComfyUI's node discovery reads the entry-point metadata from your active `.venv`.

Reuse an existing ComfyUI checkout with `--comfy-dir`:

```bash
streamstudio --editor comfy --comfy-dir /path/to/ComfyUI
```

Every ComfyUI option (`--comfy-dir`, `-b`/`--background`, `--default-nodes-`, `--only-taidal-nodes`,
`--palette-config`, `--install-`, `--manager-`, `--declared-nodes-`, `--custom-node`) needs `--editor comfy`;
without it the canvas editor refuses it by name (`--comfy-dir only applies to --editor comfy`).

### The port

The ComfyUI editor serves on `http://127.0.0.1:8188` (ComfyUI's own default). The launcher checks that
address **before** it does any work, so a studio that is already up is reported in a second rather
than after a full boot:

```
Port 8188 is already in use — StreamStudio may already be running there.

    Open it:  http://127.0.0.1:8188

If that is a different program, run StreamStudio elsewhere:  streamstudio --editor comfy --port 8189
```

The command exits non-zero and starts nothing. Use `--port` to serve somewhere else — to run a
second studio beside one that is already open, or when another program holds 8188:

```bash
streamstudio --editor comfy --port 8189
```

### Stopping and restarting

`stop` and `restart` find the studio by the port it holds, so they work whatever started it — this
launcher, an editor's launch button, or a `python main.py` you ran by hand:

```bash
streamstudio restart --editor comfy               # stop what is on 8188, then launch again
streamstudio stop --editor comfy                  # just stop it
streamstudio restart --editor comfy --port 8189
```

Without `--editor comfy`, `stop` and `restart` act on the canvas editor's port, 8190.

`restart` re-walks the whole launch flow (requirements sync, custom nodes, symlink), so it is also
how you pick up newly installed nodes or a changed entry point. Stopping a port nothing is listening
on is not an error — `restart` simply starts.

### Running detached

Both `run` and `restart` hold the terminal by default, the way a server should. `--background` (or
`-b`) hands it back:

```bash
streamstudio restart --editor comfy -b
```

```
Stopped StreamStudio on port 8188 (pid 7819).
StreamStudio is starting in the background — log: ~/.streamstudio/logs/studio-8188.log

    Open it:  http://127.0.0.1:8188
```

The studio runs in its own session, so it survives the shell that started it, and its output goes to
a per-port log under `~/.streamstudio/logs/` — never the directory you launched from. The command
returns as soon as the process is spawned rather than waiting for the bind, so a boot that fails
shows up in that log; `streamstudio stop --editor comfy` is how you end it.

Taidal nodes then appear in ComfyUI's *Add Node* menu grouped by what they ARE, not by which package ships them. A role-bearing class nests under `Taidal/<Role>[/<Task>]` (role-first) — e.g. `Taidal/Model/Classification`, `Taidal/Trainer/Detection`, `Taidal/Logger` (loggers are task-agnostic, so no task level). An op / source / engine nests under `Taidal/<Category>[/<group>]` — `Taidal/Op/numpy`, `Taidal/Source`. The **producing package rides the node label** as an uppercased tag — `[RECORDSTREAM] Threshold`, `[YOURPACKAGE] YourModel` — so you can tell where a node comes from without burying it in a per-package folder.

Object sockets (model / loss / metric) are **role-qualified and task-agnostic**: a classification model and a segmentation model both carry `RECORDSTREAM_OBJECT:model`, so a model docks into *any* trainer's `model` slot — the role still stops you wiring a loss into a model slot. The **Retag** node re-stamps an object's role to force a connection when you need it.

They are also **engine-aware**. A class that declares which framework's API it belongs to gets that engine on its socket, and a cross-engine wire is refused as you drag it — a `torch.nn` loss will not dock into a Keras trainer, because it cannot run there. The guard is deliberately one-sided: it fires only when *both* ends declare an engine, so an object from a library that says nothing about frameworks still connects anywhere it did before. Retag remains the override.

A model node can come from a **builder function**, not just a class. Many libraries ship their models as factory functions — torchvision's detectors are `fasterrcnn_resnet50_fpn(...)`, `ssdlite320_mobilenet_v3_large(...)`, etc. A package registers them with `role="model"`, so each appears under `Taidal/Model/Detection` as an object node whose widgets (`num_classes`, `weights_backbone`, …) are read straight from the function's signature. Wire its **`class`** output into a detection **Trainer**'s `model` slot — because the node is lazy, the trainer injects the dataset-derived `num_classes` at run time, so you can leave that widget blank.

### Custom nodes: ComfyUI-Manager + extras

Every launch (not just the first) seeds ComfyUI's `custom_nodes/` directory from **three tiers** (deduped, installed in this order):

1. **Package-declared — the `streamstudio.custom_nodes` entry-point group.** Nodes declared here are *generally available*: declared once in a package's `pyproject.toml`, they install for every user of that package on every launch, with no flags or env vars. streamstudio itself bundles **[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)** this way — it adds a *Manager* button to the ComfyUI toolbar from which you install, update, and remove further custom nodes without leaving the GUI. Any installed package can contribute:

   ```toml
   [project.entry-points."streamstudio.custom_nodes"]
   my_package = "my_package.custom_nodes:CUSTOM_NODE_URLS"   # a git-URL str, or an iterable of them
   ```

   To bundle more nodes with **streamstudio itself**, append to `CUSTOM_NODE_URLS` in [`streamstudio/custom_nodes.py`](streamstudio/custom_nodes.py) — that tuple is streamstudio's own declaration. (Editing the tuple takes effect on the next launch; *adding or renaming an entry point* changes installed metadata, so an editable install must be reinstalled — `uv pip install -e ./streamstudio`.) Skip just the manager with `--manager-`; skip the entire declared tier with `--declared-nodes-`.

2. **`STREAMSTUDIO_CUSTOM_NODES` env var** — comma-separated git URLs:

   ```bash
   STREAMSTUDIO_CUSTOM_NODES="https://github.com/rgthree/rgthree-comfy,https://github.com/crystian/ComfyUI-Crystools" streamstudio --editor comfy
   ```

3. **The `--custom-node` list** — ad-hoc, per launch:

   ```bash
   streamstudio --editor comfy --custom-node=[https://github.com/rgthree/rgthree-comfy,https://github.com/crystian/ComfyUI-Crystools]
   ```

Each repo is cloned into `custom_nodes/<repo-name>` and, if it ships a `requirements.txt`, its dependencies are pip-installed into the **active environment** (the same one ComfyUI runs in). The install is **idempotent**: a repo whose directory already exists is skipped, so updating or removing a node is ComfyUI-Manager's job, not the launcher's. A clone or dependency failure of one node — or a broken entry-point declaration — logs a warning and the launch continues; a broken custom node never blocks the studio. (`--manager-` filters the manager from the *declared* tier only: explicitly requesting its URL via env/flag still installs it.)

### Hiding ComfyUI's built-in nodes

By default the palette shows ComfyUI's built-in nodes (KSampler, Preview Image, …) **alongside** the Taidal nodes. To launch a Taidal-only palette, pass `--default-nodes-`:

```bash
streamstudio --editor comfy --default-nodes-
```

This (a) prunes ComfyUI's built-in / `comfy_extras` / API nodes from the *Add Node* menu, leaving the auto-discovered Taidal nodes (and any other third-party custom nodes), and (b) surfaces the Taidal nodes at the **palette root** — `RecordStream/Op/numpy`, `Waivefront/Op/targets`, `StreamStudio` — instead of nested under a `Taidal/` folder (there's nothing left to disambiguate them from). The default `--default-nodes` keeps everything visible and the `Taidal/` grouping.

**Keeping specific default nodes (a config file).** A few native nodes are useful even in a Taidal-only palette, so a configurable **keep-list** survives the prune. **Preview Image** and **Preview as text** (`PreviewAny`) are *always* kept; list any others in a **config file** under `keep_nodes:` (use the node's `class_type`/registration key):

```yaml
# ~/.streamstudio/config.yaml
keep_nodes:
  - MaskToImage
  - LoadImage
```

A **default config ships with the package** (`streamstudio/config.default.yaml`, source-controlled) and is used as the fallback when you haven't supplied your own — so sensible defaults (`MaskToImage`, …) apply out of the box. Resolution order: `--palette-config <file>` / `$STREAMSTUDIO_CONFIG` → your `~/.streamstudio/config.yaml` → the shipped `config.default.yaml`. To customize, copy the shipped file to `~/.streamstudio/config.yaml` (or any path) and edit:

```bash
cp "$(python -c 'import streamstudio,os;print(os.path.join(os.path.dirname(streamstudio.__file__),"config.default.yaml"))')" ~/.streamstudio/config.yaml
streamstudio --editor comfy --default-nodes- --palette-config ./my-palette.yaml   # or point at any file
```

**Only Taidal nodes** (`--only-taidal-nodes`) goes further: it hides **every** non-Taidal node — ComfyUI's built-ins **and** other third-party custom packs (mtb, WWAA, …) — leaving only Taidal nodes + the keep-list. It implies `--default-nodes-`.

```bash
streamstudio --editor comfy --only-taidal-nodes                                  # only Taidal + Preview Image/as-text
streamstudio --editor comfy --only-taidal-nodes --palette-config ./my-palette.yaml
```

> **Note:** hiding the defaults removes most of ComfyUI's native viewers, but the keep-list retains the ones in use, and Taidal's own **custom viewer nodes are self-contained** — the **Array / Tensor Histogram** and **Display Any** nodes render on their own canvas/widget (no native viewer needed) — so the Taidal-only palette stays fully usable for inspecting data.

Under the hood: the keep-list lives in the config **file** (read inside the launched ComfyUI process); the CLI passes only the file's *location* and the launch-mode toggles via env vars — `STREAMSTUDIO_HIDE_DEFAULT_NODES=1` (hide built-ins), `STREAMSTUDIO_ONLY_TAIDAL_NODES=1` (only-Taidal filter), `STREAMSTUDIO_CONFIG=<path>` (config-file location; empty ⇒ the default). The only-Taidal filter runs in the browser after all nodes load (so it catches custom packs that load after streamstudio), via the `/taidal/palette_config` route + `web/palette_filter.js`.

### Empty starting canvas

StreamStudio starts on an **empty canvas** rather than ComfyUI's built-in Stable-Diffusion record workflow (Checkpoint → CLIP → KSampler → VAE → Save Image), which has nothing to do with Taidal pipelines. The same empty canvas is loaded when you close the last workflow tab. This is a bundled ComfyUI web extension (`web/empty_default_graph.js`) — no flag needed; your own saved/restored workflows open normally.

## Building a pipeline

Discovered callables are classified by their RecordStream role so they wire together by **type**, not opaque strings:

- **Source** nodes (e.g. `HuggingFaceSource`, under `Taidal/RecordStream/Source`) construct a dataset and output a `RECORDSTREAM_SOURCE`.
- **Engine** nodes (`Stream`, `FlowGraph`, `JointStream` — every `@configurable(category="engine")` class, under `Taidal/RecordStream/Engine`) wrap a source and apply ops to it, so they both **take** and **output** a `RECORDSTREAM_SOURCE`: the `source` param is a wired input socket (not a text widget), `ops` becomes the dynamic `op_0`, `op_1`, … `RECORDSTREAM_OP` family, and the result is a `source` output you can chain into the next engine. Wire `HuggingFaceSource → Stream` directly.
- **Op** nodes (e.g. `Threshold`, `ToTensor`, `ConvertToImage`) take a `RECORDSTREAM_RECORD` input **plus** their constructor config as widgets, apply the op, and output a `RECORDSTREAM_RECORD` — so ops chain. A class becomes an op node by carrying the `@configurable(category="op")` tag (its `__call__` is resolved through the MRO, so an op that inherits `__call__` from a base `Transform` — a kernel-only transform — classifies correctly).
- **Generic** nodes (plain functions) keep scalar widgets with a `STRING` fallback.

A record on the `RECORDSTREAM_RECORD` wire is a **record**: a plain `dict` of typed values (an `Image` carrying its layout, a `Label` its classes), with no role tags. Node field selectors are therefore record **keys** (`image`, `class`, `mask`, …); the legacy `input` / `target` selections still work through a conventional-key fallback scan.

Node labels are the **verbatim** callable name (underscores become spaces; capitals are preserved — `HuggingFaceSource` stays `HuggingFaceSource`, not `Huggingfacesource`). Internal helpers (names starting with `_`) are not surfaced. Hovering any widget shows a **tooltip** sourced from the wrapped callable's docstring `Args:` block (see *Constructor-parameter tooltips* below).

**Numeric widget ranges.** Every INT/FLOAT widget carries explicit full-range bounds (without them the ComfyUI frontend clamps input to `[0, 2048]` — negative dB values would be unenterable). An op can declare a *real* range on the parameter itself with a PEP-593 mark — `Annotated[float, Interval(ge=-200.0, le=50.0)]` (`annotated_types`) — which the bridge renders as the widget's `min`/`max` (a marked `Tuple[float, float]` range bounds both its `__lo`/`__hi` widgets), and the **same mark** flows into a generated JSON schema via `confluid.to_pydantic`. Declare the mark on the op's own signature; a shared alias per physical quantity (a dB power, a probability, a watt range) reads better than an inline `Interval(...)` at every parameter.

**Container / structured widgets.** A constructor param whose type is a container that has no scalar widget renders as a `STRING` you type into, and the bridge decodes it back to the real Python value at run time:

| Param annotation | Type into the widget | Decodes to |
|---|---|---|
| `List[str]` | `a, b, c` (comma-separated) | `["a", "b", "c"]` |
| `Tuple[str, ...]` | `A, B` or `("A", "B")` | `("A", "B")` |
| `Dict[int, str]` | `{0: "BG", 1: "5GNR"}` (Python/JSON literal) | `{0: "BG", 1: "5GNR"}` |
| `list[str \| tuple[str, str]]` (a rename map — a bare key keeps its name, a pair renames it) | `snr_db, noise_power_db:nf` *or* `[("snr_db","snr"), "noise_power_db"]` | `["snr_db", ("noise_power_db", "nf")]` |
| `list[int] \| str \| int` (a Union with a bare scalar arm, e.g. `pytorch_lightning.Trainer.devices`) | `auto` *or* `1,2,3` *or* `2` | `"auto"` / `"1,2,3"` / `"2"` (passed through verbatim) |

For the list-of-pairs case you can use either a **friendly comma list** — items split on `,`, and each item split on `:` into a `(src, dst)` rename pair (a bare item stays a plain key, i.e. `src == dst`) — or a full **Python/JSON literal** for any nesting. This is generic for any list-of-pairs/compound param; it's handled in the bridge's `_coerce_kwargs`, so it works for every such node and the value round-trips through the YAML exporters too.

The **last row is the exception to the splitting rule:** a `Union` that has both a *list* arm **and** a bare `str`/`int` arm (the canonical case is Lightning's `devices`, defaulting to `"auto"`) is **not** comma-split — the string is passed through verbatim so the library parses it itself (`"auto"`, the comma-string `"1,2,3"`, or a single `"2"`). Splitting it would yield `["auto"]`, which Lightning rejects with *"Device IDs … you passed a sequence of str"* — so the default Trainer node simply works as-is.

Three adapter nodes (under `Taidal/StreamStudio`) let you step through a dataset and view records with **native** ComfyUI viewers (plus one interactive value inspector):

- **Walk Dataset (by index)** — takes a `RECORDSTREAM_SOURCE` and an `index`, and outputs the record at that index (`source[index]`). The `index` widget uses ComfyUI's `control_after_generate`, **defaulting to *increment*** so each **Queue Prompt** advances to the next record in order — set the control to *fixed* (or `index` `0`) to reset, or *randomize* to record at random. `wrap` cycles modulo the dataset length.
- **Extract from Record** — takes a `RECORDSTREAM_RECORD` and a free-text `field` **record key** and unpacks the selected value onto **typed** sockets — `IMAGE`, `MASK`, `STRING`, `INT`, `FLOAT`, `array` — so you wire the one your viewer expects. Type the key you want (`image`, `mask`, `class`, …); the legacy selections `input` / `target` (and any key not present in the record) fall back to a conventional-key scan — `input` → the first of `input`/`image`/`signal`/`iq` else the record's FIRST key, `target` → `target`/`label`/`mask`/`class` else its SECOND — so canvases saved before the record model keep working. The chosen entry is payload-unwrapped (a `Label` yields its value, any other typed item its underlying data). ComfyUI types sockets statically, so instead of retyping one socket per run it surfaces every plausible type; *which* one is meaningful is derived from the resolved entry itself. `IMAGE` is rendered via `waivefront.visualizers.value_to_image` (PIL / tensor / 2-D map → image; a non-visual value renders as a small text image); the `colormap` widget is a **dropdown** of the supported colormaps (the closed `waivefront.visualizers.Colormap` set — `viridis`, `magma`, `gray`, …) applied to 2-D maps, and `max_size` caps the longest side of the rendered image in pixels (default `512`, larger renders are downscaled, aspect ratio preserved). `MASK` carries a 2-D field (segmentation targets); `STRING` is a label/scalar (with `label_name` when the target has it) or a compact type summary; `INT` / `FLOAT` carry a scalar (e.g. a class id). The **`array`** output (`RECORDSTREAM_ARRAY`) carries the **raw field value on demand** — a tensor stays a tensor, an ndarray stays an ndarray (NOT normalized/colormapped like `IMAGE`) — to feed the **Array / Tensor Histogram** viewer or any array-consuming node. Drop two of these next to each other (one set to `image`, one to `class`) to view both at once.
- **Array / Tensor Histogram** — wire the `array` output above (or a native `IMAGE`) into this node and **Queue Prompt** to inspect the raw values *on the node itself*: a **histogram** of the value distribution (with a `log scale` toggle), the array **rendered as a colormapped image**, and a **hover readout** — move the cursor over the image to read the value (and source coordinate) under the pointer. The `channel` widget selects which channel to view (`-1` = all → mean view / natural RGB; `0..N` picks one) and `colormap` / `bins` / `max_size` control the render. All computation lives in recordstream (`array_histogram` / `select_channel` / `value_to_image`); the node just ships the result to its in-canvas widget — so unlike *Extract → Preview Image* it works **regardless of `--default-nodes-`** (no native viewer needed). It passes the array straight through so it can sit inline in a pipeline.
- **Confusion Matrix** — wire the **`all_metrics`** output of a **classification evaluator** (a JSON `STRING` of every metric) — or a raw `N×N` matrix on the `matrix` input plus optional `class_names` — into this node and **Queue Prompt**. It **extracts every confusion matrix** from the metrics (the square `N×N` ones — there can be several) and shows a **class-labeled grid** *on the node itself*: rows = the true class, columns = the predicted class, cells coloured by value with the diagonal (correct predictions) outlined, and the class names down the left and across the top. A `view:` button cycles between **count → row % → col % → all %** (the normalized views render as **percentages**, **without re-running** — the variants are precomputed); a `cm:` button cycles between matrices when several are present; and a **hover readout** reports `true=<class>, pred=<class>` and the count (and percentage when a % view is active) under the pointer. To get a confusion matrix, wire a `MulticlassConfusionMatrix` metric node's **`class`** output into the evaluator's `metrics` slot (the deferred `class` output sizes itself to the model's `num_classes` at run time). The evaluator output stays generic (all metrics); the extraction + normalization math lives in recordstream (`confusion_matrices_payload`), so the node is glue and works under `--default-nodes-`.

- **Display Any** — wire **any** value (a scalar, list, a whole record, tensor, op/source) into `value` and its text representation appears on the node after **Queue Prompt**. `mode` picks `raw value` (`repr`, truncated for huge values) or `tensor shape` (a recursive summary of shapes/dtypes through lists/dicts — a record IS a dict, so it unpacks entry by entry — "what shape is this actually?"). The text is also emitted on a `text` output. A Taidal-native, `--only-taidal-nodes`-friendly stand-in for WAA's *Display Any*.
- **Apply Text Template** — fill a `template` string with `{a}`…`{e}` placeholders from the matching `a`…`e` inputs (any type; an unwired input is empty; standard format specs like `{a:.2f}` work; an unknown `{x}` renders empty rather than erroring). Emits the filled string on `text` — wire it into *Display Any* or a sink. Inspired by mtb's *Apply Text Template*.
- **Draw Text to Image** — draw `text` (e.g. from *Apply Text Template*) onto an `image`, or onto a fresh `width`×`height` `background` canvas when none is wired, and output an `IMAGE`. Word-wraps to the image width and anchors by a 9-grid `position` with a `margin` inset; `color`/`background` accept any PIL color name or hex. Feed the `IMAGE` into *Preview Image*, the *Array / Tensor Histogram*, or a save node. The rendering lives in `recordstream.ops.image.draw_text`; the node is glue.

This replaces the old all-in-one "Show Record" output node: extraction lives here, **previewing lives in ComfyUI's own nodes** — wire `IMAGE` into *Preview Image*, `STRING` into a text node, the `array` socket into **Array / Tensor Histogram**, any value into **Display Any**, etc.

A minimal "walk an image dataset" graph:

```
HuggingFaceSource  (path=ylecun/mnist, split=train, input_feature=image, target_feature=label)
        │  RECORDSTREAM_SOURCE          # `path` is a free-text dataset-id field (+ cached-dataset picker), not a folder
        ▼
Walk Dataset (by index)          # Queue Prompt repeatedly to step through records
        │  RECORDSTREAM_RECORD
        ▼
ToTensor                          # optional op chain — stack any Taidal op node here
        │  RECORDSTREAM_RECORD
        ▼
Extract from Record (field=image)
        │  IMAGE                    # also: MASK / STRING / INT / FLOAT / array
        ├──────────────► Preview Image            # ← native ComfyUI viewer (IMAGE)
        │  array
        ▼
Array / Tensor Histogram         # histogram + colormapped image + hover-to-read-value
```

`Extract from Record` renders the `IMAGE` socket via `value_to_image` (in recordstream, re-exported from waivefront); the `STRING` / `INT` / `FLOAT` / `array` outputs work without any domain package installed. The **Array / Tensor Histogram** viewer likewise runs on the core install (its math is in recordstream), so it needs no extra and no native viewer.

### Running a trainer / evaluator / processor

A trainer / evaluator / processor node **runs itself when you Queue the graph** — it IS a terminal output node, so there is no separate "Run" node to wire. **Queue Prompt** executes the (potentially long) `.run()`. The node honours ComfyUI's **interrupt button**: it runs the work on a background thread and, when you press interrupt, injects a `KeyboardInterrupt` so the run stops cleanly at the next batch / record boundary (PyTorch Lightning shuts down gracefully). A single long-running C kernel can't be interrupted mid-call, so cancellation lands between steps, not instantly. (To build/inspect/export a graph without training, just don't Queue it — use the **Export Graph → Taidal YAML** menu command.)

**Progress shows two ways.** The node fills ComfyUI's **native green progress bar** (the overlay on the node) as the run proceeds — for training it advances smoothly over the *whole run* (all epochs), for evaluation / data processing it tracks the test / record count. AND it shows the runner's full output **live** in a collapsible widget on itself: training metrics, logs, and the in-place-updating **tqdm / Lightning text bar** stream in (and still mirror to the console). Use the `▾ output` toggle to fold the log away; the captured text is runtime output and is never saved into the workflow.

> How it works: the runnable reports progress through the comfy-free `recordstream.runnable.ProgressReporting` protocol (a plain `(value, total)` callback), and StreamStudio drives `comfy.utils.ProgressBar` from it — so the bar fills regardless of whether the captured text bar is visible. A runnable that doesn't support the protocol simply shows no native bar (the text widget still streams).

**Outputs are wireable sockets.** Every confluid `@output` a runnable declares is a typed output socket on the node, filled with the run's LIVE value. For a trainer / evaluator runnable that's `computed_metrics` (the run's final scalar metrics, e.g. `test/accuracy`, `val/loss`) and `class_names` (the resolved class names) — both `STRING` sockets carrying JSON, so wire them into ComfyUI's native **Show Text** node to see them — plus `trained_model` (a `RECORDSTREAM_OBJECT:model` you wire straight into an evaluator node's `model` slot to chain train → eval in one graph). Add a new value `@output` upstream and it becomes a socket automatically — no StreamStudio change needed.

**Attaching loggers / callbacks.** An object node's constructor params that accept an *object* (rather than a typed/string value) render as wired `RECORDSTREAM_OBJECT` input sockets — so to log a run, drop a logger node (e.g. `TensorBoardLogger`, `CSVLogger`) and wire its **`instance`** output into the **Lightning Trainer** node. A param that accepts a *list* of objects (the Trainer's `logger` / `callbacks`) grows **dynamic `logger_0`, `logger_1`, …** sockets as you wire — so you can attach several loggers (or callbacks) at once, just like the `op_N` / `source_N` list inputs. String-settable knobs like `accelerator` (`"gpu"`) and `precision` stay editable widgets; only genuinely object-typed params become sockets.

### Composing runnables — workflows (run conditionally / in sequence)

To run several runnables together — *train then evaluate*, or *skip training if the checkpoint already exists* — use the **workflow combinator** nodes from `recordstream.workflow`: **Sequence** (run a list in order), **Conditional** (run `if_true` / `if_false` by a condition), **Switch** (key a selector into cases). They are themselves runnable nodes, so they expose `RECORDSTREAM_RUNNABLE` input sockets: wire a trainer/evaluator node's **`runnable`** output into a **Conditional**'s `if_true` / `if_false`, or into a **Sequence**'s `steps_0` (a fresh `steps_1` appears as you wire, like the `op_N` lists).

A runnable wired into a combinator slot is **dual-mode**: instead of running itself when you Queue, it is built as an **instance** and the *combinator* runs the selected branch — so only the chosen runnable trains, and nothing runs twice. (A combinator nested inside another combinator behaves the same.) Autograd is restored for an inner trainer and the native progress bar follows whichever step is running. Queue the combinator node (it's the terminal output) to run the whole workflow — the same document also runs head-less with `recordstream run workflow.yaml` and round-trips through the YAML export.

### Running a model (inference)

A **model node** (any class tagged `@configurable(category="model")`) RUNS inference — distinct from a
deterministic op. Wire a record into its optional `record` input and it outputs the applied `record`
(with predictions stamped) PLUS the loaded model on a `RECORDSTREAM_OBJECT:model` output (which you can
also dock into an evaluator's `model` slot). The model loads lazily on the first run.

A model node may also TAKE a model on a wired `RECORDSTREAM_OBJECT:model` **input** — the loader→runner
decoupling that many inference libraries use. A *runner* class holds no configuration of its own; a
*loader* class holds all of it (checkpoint URI, window length, …) and the runner just executes it. Wire
the loader node's `model` output into the runner node's `model` input, then feed it a record. It is the
canvas spelling of the nested YAML `model: !class:ModelInference(model=!class:ModelLoader(...))`.

A typical detection graph, entirely from auto-discovered nodes:

1. A **dataset value node** — pick a dataset identifier, set its version; on Queue it downloads into
   `$DATA_ROOT` and outputs the local `directory` (a `STRING`).
2. Wire `directory` into a **file source**'s `directory` widget (right-click → *Convert directory to
   input*, then drag the wire), then **Walk Dataset** to step through records.
3. A **model node** runs detection, renders the spectrogram and stamps the detections; a second model
   node can refine the labels.
4. An **overlay op** draws the boxes and labels onto the image, then **Extract from Record**
   (`input` → `IMAGE`) → native **Preview Image**.

A node that downloads from a credentialed platform reads its credentials from the environment; each
such package documents its own variables.

## Composing ops (higher-order ops)

Every op node is **dual-mode**. Besides the `record` input (`RECORDSTREAM_RECORD`, now *optional*) and the applied `record` output, it has a second **`op`** output (`RECORDSTREAM_OP`) carrying the *constructed op itself*. So you can either chain ops by `record` (the linear pipeline above) **or** take the `op` output and feed a **higher-order op** — one whose constructor takes a list of ops (`Parallel`, `Pipeline`, `Enable`, …).

Those higher-order nodes don't show a text widget for their op-list; they grow dynamic `op_0`, `op_1`, … `RECORDSTREAM_OP` inputs (a fresh empty slot appears whenever the last fills, via `web/op_list.js`). Wire each op node's `op` output into one:

```
ToTensor    ──op──┐
Threshold   ──op──┤  (op_0, op_1)
                  ▼
               Parallel  ─record→ Extract → Preview
Walk ───────record┘
```

`Parallel(ops=[ToTensor, Threshold])` applies both to the incoming record. Leave a higher-order op's `record` unconnected and take *its* `op` output to nest it inside another higher-order op.

**Single-op params** work the same way with ONE socket instead of a list: a constructor param named `op` or `target` (`RandomApply.op`, `ConfigureOp.target`) renders as one optional `RECORDSTREAM_OP` input. A node may have **several**: a composite whose op slots carry names of its own can advertise them through the bridge's `__taidal_op_params__` stamp, and each becomes its own socket. The canonical use is **`ConfigureOp`** (recordstream) — a per-record parameter injector: wire the value-computing ops into its `op_N` inputs, the op to configure into `target`, and name the attribute in `param`:

```
FormulaOp    ──op──→ (op_0)   ConfigureOp(param="low_level", source="image")  ─record→ …
Threshold    ──op──→ (target)      ▲
Walk ────────────────────record────┘
```

Per record, the compute chain runs on a **side branch** (its transformations are discarded — the original record continues), the `source`-keyed entry of that branch's final record becomes the value, `target.low_level` is set to it, and `target` is applied to the original record — replacing the *Extract → Math → widget-wire* pattern with something the flow export can serialize.

> **Known gap.** The exporters currently emit `ConfigureOp(ops=…, target=…, param=…)` without the `source=` record key the op now requires, so an exported value chain raises `ConfigureOp: 'source' … is required` when the YAML is run. Set `source` by hand on the exported document until the emitters are ported (tracked in the workspace `TASKS.md`); building and running the node **on the canvas** is unaffected.

### Wiring a value into an op parameter

Any op's scalar parameter (a number you'd normally type, e.g. `noise_power_db`) can instead be **driven by a wire**: right-click the node → *"Convert `<param>` to input"*, then connect a numeric output into the new socket. Nullable dB-style params (`Optional[float] = None`) are handled specially — they show as a blank field (blank = use the stamped-metadata fallback / constructor default), and StreamStudio makes the wire work end-to-end: a bundled extension retypes the converted socket to `FLOAT`/`INT` so the link draws, and the node's `VALIDATE_INPUTS` tells ComfyUI to accept that numeric wire at **Queue** time (without it the link draws but Queue fails with *"Return type mismatch between linked nodes"* — which is why a wired param can look fine until you run an output node, like *Preview as text*, that includes it).

What you can wire in, and how each compiles, is the [DAG → sequential](#dag--sequential) table below — a fixed value from a **Primitive** node inlines as a literal, a same-record **Extract → Math** derivation becomes a `ConfigureOp`, and **another op's `@output`** (e.g. a `NoiseFloor`'s `applied_snr_db` into a later op's `noise_power_db`) becomes a context `Capture` + `Apply` pair as long as the producing op is upstream on the same record path.

> **Scope.** Works for any op whose constructor takes an `ops`/`transforms` op-list or an `op`/`target` single-op param — and, for a composite using slot names of its own, whichever slots it stamps as op-valued (a stamped op-list slot grows its own `<param>_N` socket family). — multi-op branches mean pre-composing a branch into one op (future). `FilterOp`/`WrappedOp` wrap a *raw Python callable*, so they aren't canvas nodes at all (bare `@configurable`, i.e. UNcategorised — neither `op` nor `engine`).

## Subgraph as an Op

**Draw a subgraph, then compile it.** Chain some ops record → record, select them, convert to a
subgraph — then right-click the rectangle and choose **Taidal: compile to Op** (or use
**Taidal: make a Stream op from selection**, which converts and compiles in one step). It reports
what it built:

```
extract → op — record contract: {text, image}. Drag the rectangle's 'op' output into a Stream.
```

```
┌─ my ops ─────────────┐
│ (record)         op ●│────────► Stream.op_0
└──────────────────────┘
```

Compiling is a **decision, never automatic** — nothing is added to your canvas until you ask — and it
is re-runnable: the nodes it owns are marked, so changing the boundary and compiling again rebuilds
them rather than duplicating them.

### What the compile builds

**Every op in the rectangle**, joined into one chain in canvas reading order (left to right, then top
to bottom) — so the pipeline is the whole graph, not whichever branch happened to reach the end.
Anything you wired by hand keeps the order you gave it; only the gaps between wired runs are filled:

```
laid out C  B  A   and wired  C → A     ⟹   compiled: C → A → B
```

**A *Build Record* gathering your promoted OUTPUTS**, one socket per label. The labels you promote
*are* the record keys, so the wiring states the contract with every arrow present:

```
Extract(field='label') ──text──┐
                                ├──► Build Record ──record──► Extract as Op ──op──► (op)
Extract(field='input') ──image──┘
```

**An *Extract as Op* terminator** on the end, with its `op` promoted to the boundary.

> **Where the record ops come from.** *Extract from Record* and *Build Record* are canvas glue — they
> have no recordstream counterpart, so neither can appear inside a compiled `Pipeline`. The
> **compiler** translates the shape: each builder entry becomes `RenameField(field → label)`, closed
> by a `SelectFields` over the labels. So the canvas keeps the graph you drew, and the translation
> happens where it belongs. Your `{text, image}` rectangle runs as:
>
> ```
> in : {input: 'IMG', label: 3, path: '/x.png'}
> out: {text: 3, image: 'IMG'}
> ```
>
> `SelectFields` closes it — afterwards the record holds the promised entries and nothing else — so
> the rectangle is a promise that can be checked. That is exactly the shape an annotation workspace
> declares as its interface, drawn instead of written.

A promoted output carrying a whole **record** combines — its entries ride through, and the close is
skipped because its keys are not knowable from the boundary. **Two** record outputs is a fan-in, and
a `Pipeline` is a straight op list, so the compile refuses and says so (use the flow export for a
branchy graph). A rectangle that neither transforms records nor promises entries is refused with the
reason, rather than silently doing nothing.

The terminator is still a normal palette node (`Taidal/StreamStudio` → **Extract as Op**) if you want
to place it yourself — on the flat canvas, or to pick a different tail. It compiles the whole upstream
sub-pipeline into a single `recordstream.transform.Pipeline` and emits it on an **`op`** output
(`RECORDSTREAM_OP`), which is what makes an arbitrary record → record DAG (fan-out, `Mix` fan-in) into
one reusable op:

```
(subgraph input: RECORDSTREAM_RECORD)
   │
   ▼
 ToTensor ─┬─→ Threshold ────────┐
           └─→ ConnectedComponents ┤ (fan-out → Mix)
                            Mix Records
                               │ record
                               ▼
                        Extract as Op ──op──► (promote to subgraph boundary)
```

Placed by hand or automatically, the subgraph instance exposes a **`RECORDSTREAM_OP` output** and behaves as:

- **an Op** — wire that output into any op slot (`Parallel`/`Pipeline`/`Enable` `op_N`, `ConfigureOp.target`/`RandomApply.op`, or another op-list); the subgraph *is* the transformation, reusable across the canvas;
- **a graph** — run a record through the subgraph's record boundary inline (the normal native-subgraph use).

The DAG→sequential translation (fan-out → auto `Save`/`Use` context cells, `Mix` → fan-in) is the same compiler used by the flow export below, so a subgraph-Op exports faithfully: a consumer fed by it embeds a nested `!class:…Pipeline` whose `transforms:` is the flattened inner pipeline. **Extract as Op** also works on the flat canvas (no subgraph required) — it always compiles its own upstream cone.

> **How it works (no extra wiring).** The node is self-contained: at run time it reads ComfyUI's hidden `PROMPT` (the flat graph — native subgraphs are already expanded by the frontend, so the inner op DAG is present) plus its own `UNIQUE_ID`, and compiles its record-ancestor cone via `streamstudio.export`. No snapshot widget, no extra route. An unwired terminator is the identity (`Pipeline([])`).
>
> **Why the `record` input is lazy.** Because the node compiles from the static `PROMPT` and never reads the runtime record, its `record` input is a **lazy** ComfyUI input — running the graph (e.g. Queueing a `DatasetProcessor` fed by the Stream) does **not** execute the op chain feeding it. That matters: when you use a subgraph purely as an Op, the op chain has **no source on the canvas** (the source feeds the `Stream` directly, and the ops arrive via the compiled `Pipeline`), so forcing it to run would push a `None` record through the head op and crash a downstream viewer or value-chain `Extract from Record`. The lazy input avoids that entirely. Running a record *through* the subgraph's boundary (using it as a graph, not an Op) is unaffected.
>
> **Both exporters compile it.** The *flow export* (next section) embeds a subgraph-Op as a nested `Pipeline` in its consumer. The **whole-graph** export (*Export Graph → Taidal YAML*) now compiles it too: when the terminator feeds a `Stream`'s op list, its cone is compiled (same DAG→sequential compiler, same constant-hoisting) and **spliced inline** into the Stream's `ops:` — a sequential op-list makes a nested chain equivalent to its ops spliced, and inline ops survive `recordstream run`'s deep-flow where a nested deferred chain marker would not. An **unwired** terminator (used as a graph, or floating) has nothing to compile and is skipped-with-comment as before.

## Class names — wired, not typed

A contract declaring an item type that carries a class vocabulary (`Label`, `MultiLabel`) makes
the pipeline template place a **Class Names Output** node, coloured and titled *"class_names —
graph OUTPUT, fill this in"*. Fill it either way:

```
HuggingFaceSource.class_names ──→ ClassNamesOutput.names      # one wire, nothing else needed
```

A HuggingFace source knows its own classes — `class_names` reads `features[target_feature].names`
from the dataset metadata (measured 0.00008 s once the schema is known, no records read) and the
source node surfaces it as an output socket beside `source`. For a source that declares none — a
folder reader, a CSV — place **Class Names Scan**, wire the source into it, and wire its
`class_names` onward; it walks the label column through `recordstream.projection`.

**A wire becomes a literal.** A config document references a NODE, never one of its attributes
(attribute references were removed from the config layer), so the exporter EVALUATES a wired
`@output` and writes its value down:

```yaml
class_names_output:
  _target_: ClassNamesOutput
  names: ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']
```

Which output is an attribute and which is the node is decided by asking the class, not by slot
index: a declared `@output` folds, while the structural sockets that stand for the node itself
(`source`, `op`, `object`) keep a whole-node `!ref:`.

## Pipeline blueprints (a `flow:` document as a subgraph)

`Subgraph as an Op` above is the by-hand route: you draw the chain, drop **Extract as Op**, promote
its output. A `flow:` document can be imported as that whole rectangle in one step, boundary and all:

```bash
streamstudio import pipeline.yaml -o blueprint.json --as subgraph
```

A flow document already **declares** its own boundary — the head step's `record` input is fed by
nothing (that is where a source connects) and `outputs:` names the step whose record leaves — so
nothing has to be inferred. From this document:

```yaml
flow:
  image: {op: !class:recordstream.ops.contract.RecordContract {name: image, fields: {image: Image}}}
  class: {from: image, op: !class:recordstream.ops.contract.RecordContract {name: class, fields: {class: Label}}}
outputs: class
```

you get one node on the canvas — a subgraph instance whose sockets are all visible **before anything
is wired to them**:

```
  instance in : [('record', 'RECORDSTREAM_RECORD', None)]
  instance out: [('record', 'RECORDSTREAM_RECORD', []), ('op', 'RECORDSTREAM_OP', [])]

  subgraph 'pipeline'
    nodes: [RecordContract, RecordContract, Extract as Op]
    links: -10 -> 1, 1 -> 2, 2 -> -20 (record), 2 -> 3, 3 -> -20 (op)
```

The **Extract as Op** terminator is placed and promoted for you, so the `op` output is there without
your having to know that node exists. Wire it into a `Stream`'s `op_0` and export, and the chain
lands in the Stream's ops:

```yaml
stream:
  _target_: Stream
  source: {_ref_: source}
  ops:
  - {_target_: RecordContract, name: image, fields: {image: Image}}
  - {_target_: RecordContract, name: class, fields: {class: Label}}
```

**One shape, pipeline *and* viewer.** Open the rectangle and hang taps (*Extract from Record*, a
viewer) off any step, then promote them as extra boundary outputs. The terminator compiles its record
**ancestor** cone, and a tap is a *descendant* of a step rather than an ancestor of the terminator —
so taps never enter the `Pipeline`. Only a fork that **re-merges** inside the cone is refused, naming
the step:

```
The op cone feeding this 'Extract as Op' terminator is branchy (at step 'node_13'),
and a Pipeline holds a straight op LIST — it cannot express a fork or a Mix
```

**Limit:** taps have to be drawn *in the editor*, not carried in the document. A document whose steps
fork is branchy, and importing one is not supported yet — it refuses, naming the forking steps (see
`TASKS.md`).

## Exporting the canvas to a `flow:` document

The **op** half of a canvas graph (everything between the source and the viewer) exports to a
Confluid **`flow:` document** and reattaches to *any* source in code — the inverse of how the
canvas builds a pipeline. The source and the Walk/Extract adapter nodes are dropped; the op
**DAG** (including fan-out and the *Mix Records* fan-in adapter) compiles into **one flow
document per connected component**, where the wires you drew ARE the step grammar.

> **`flow:` is the only interchange format (2026-08-01).** The flat serial `ops:` export was
> removed: it could not express a fork, and the per-record context ops that used to manufacture
> a flat spelling for a branchy graph were deleted from recordstream with the lowering pass. The
> canvas IS a graph, so it now compiles AS one.

Two equivalent entry points (both call the same compiler, `streamstudio.export.export_flow_yamls`):

- **On the canvas:** the **Taidal → "Export Taidal Ops → Flow YAML"** menu command
  (`web/export_flow.js`) POSTs the current graph to the `/taidal/export_flow` route and downloads
  `flow.yaml` — or, when the canvas holds several disconnected op components, **one file per
  component** (`flow_1.yaml` … `flow_N.yaml`; the browser may ask once to allow multiple downloads).
- **Offline CLI:** export a saved **API-format** graph (ComfyUI dev-mode *Save (API Format)*):

  ```bash
  streamstudio export graph.json -o flow.yaml     # omit -o to write YAML to stdout
  ```

  This works without a running ComfyUI — importing `streamstudio` populates the node registry via
  entry-point discovery. With several disconnected components, `-o flow.yaml` writes
  `flow_1.yaml` … `flow_N.yaml`; without `-o` they stream to stdout as `---`-separated documents.

The result is a `{flow: {...}, outputs: …}` document of named steps, e.g.:

```yaml
flow:
  node_1: !class:recordstream.ops.torch.ToTensor()
    normalize: true
  node_2: !class:recordstream.ops.numpy.Threshold()
    low_level: 0.5
    low_op: '>='
    output: mask
outputs: node_2
```

Step names are the canvas node ids, so a step in the document points at the node that produced
it. A fork becomes `from:` (the step naming an earlier step as its input), a *Mix Records* fan-in
becomes `merge_from:`, and an op-`@output` wired into a later op's param becomes `bind:`.

Reattach it to your own source:

```python
from recordstream import FlowGraph
from recordstream.sources import HuggingFaceSource

graph = FlowGraph.from_yaml("flow.yaml")
graph.source = HuggingFaceSource(path="ylecun/mnist")
for record in graph:
    ...
```

A **linear** flow document also converts to a `Stream` (`graph.to_stream()`) if you want the
dataset surface; a branchy one raises, because a `Stream`'s op list cannot express a fork.

### DAG → sequential

The compiler converts the whole **op DAG** of every Taidal/StreamStudio node into one flat op sequence (a `Stream` runs a list, not a graph), stopping only at non-trivial native ComfyUI nodes (viewers, third-party nodes):

- **Fan-out** (one op output wired to ≥2 op nodes): branches are scheduled one after another; the fork-point record is snapshotted into a per-record Context cell with an auto-inserted `recordstream.ops.context.Save` and each later branch restarts from it with `Use` (the last branch's `Use` carries `drop=True`, so the cell is freed). Branches are **isolated** — graph wiring routes through per-record Context cells and never touches the record's own fields or their item metadata, so nothing compiler-inserted can leak into a sink.
- **Fan-in via *Mix Records*** (`input` from A, `target` from B, `metadata` = union of all three): the branches run in Mix-argument order and the merge compiles to ONE `recordstream.ops.context.MergeFields` reading the three branch cells (metadata merged last-write-wins) — the canvas Mix node itself disappears from the YAML. The same construction you would hand-write with `Save`/`Use`/`MergeFields` context ops, generated for you.
- The **deepest sink is scheduled last**, so the document's final record is the longest path's output; viewer-only side branches run earlier in the sequence (delete them from the YAML if you don't want their compute).
- A graph holding **several disconnected components** exports each individually — one complete, individually-loadable document per component, in deterministic smallest-node-id order.

**Wired widget values.** A widget **driven by a link** is folded where possible instead of dropped:

| Upstream node | Exported as |
|---|---|
| Native ComfyUI primitive (`PrimitiveInt`/`PrimitiveFloat`/`PrimitiveString`/`PrimitiveStringMultiline`/`PrimitiveBoolean`) | the literal `value` widget, inlined |
| A Taidal **value node** marked `@configurable(constant=True)` (see the worked example below) | **hoisted** as a top-level `!class:` entry carrying its widget settings verbatim; the consumer kwarg becomes a dotted `!ref:<name>.<output>` |
| A value node **without** the `constant` mark | omitted + a note nudging you to tag the class `constant=True` |
| An op node's `op` value output wired into a **single-op param** (`RandomApply.op`, `ConfigureOp.target`) or an `op_N` list | embedded as a nested `!class:` instance |
| An ***op-chain → Extract(input) → Math → widget*** value chain rooted at the consumer's own record feed | **auto-compiled to a `ConfigureOp`**: the chain ops + a `FormulaOp` (the Math formula) re-run per record inside the wrap and inject the result as the param (e.g. `Threshold.low_level = max * 0.2`); a PURE chain leaves the op schedule entirely |
| An op's **`@output`** socket (e.g. `NoiseFloor.applied_snr_db`) wired into a LATER op's param, where the producer is on the consumer's **record-ancestor** path | **auto-compiled to the context ops `Capture` + `Apply`**: the producer's ACTUAL `@output` value is recorded into a Context cell after it runs (stochastic-safe — captured, not recomputed) and injected into the param per record. A cross-branch producer (not an ancestor) instead gets the omit note below |
| Anything else (a runtime value the compiler can't relate to the consumer's record) | omitted (the op keeps its constructor default) + a note pointing at the sanctioned mechanisms — wire it through `ConfigureOp` yourself, or, for `str`-capable params, set the widget to a `'{meta_key}'` **metadata expression** an upstream op writes |

A hoisted constant exports like this — the *settings* survive, not just their effect. The
class names below are **illustrative placeholders**, not shipped classes (see the note after
the block):

```yaml
# `myproject.config.TuningConfig` is @configurable(constant=True) and declares
# @output properties `disabled` / `probability_overrides`.
tuning_config: !class:myproject.config.TuningConfig
  quantize: false
  iq_imbalance: false
ops:
- !class:myproject.ops.TunedOp()
  probability: 0.5
  tx_disabled: !ref 'tuning_config.disabled'
  tx_probability_overrides: !ref 'tuning_config.probability_overrides'
```

> **No class in this workspace currently carries `@configurable(constant=True)`.** The mark's
> original users were a set of per-transform config utilities that have since been removed, so
> the example above is written against placeholder names rather than a real import path — a
> copy-pasted class path would not resolve. The mechanism itself is live and regression-tested
> (`tests/test_impairments_export.py`, driven by a committed workflow fixture); tag your own
> value class `constant=True` and it hoists exactly as shown.

`op_N` links (an op consumed as a `RECORDSTREAM_OP` value by a higher-order op like `Pipeline`/`Enable`/`Parallel`) compile too: each wired producer embeds as a nested `!class:` instance in the consumer's op-list kwarg.

### Excluding nodes from an export

Three ways to keep canvas plumbing out of the YAML:

- **The export-skip mark — "runs on canvas, excluded from export".** Right-click any node → **"Taidal: exclude from export"** (`web/export_skip.js`). The node dims AND gets a red **NOT EXPORTED** badge on its title bar (drawn from the property every frame, so it survives reloads and color edits); the mark is `properties["taidal_export"] = "skip"` — persisted in the saved workflow. On export, the marked node and **everything data-downstream of it** (record wires, Mix arguments) are left out, with a note in the document's comment block. The closure is **forward-only**: it prunes the nodes *behind* the mark, never the ones before it — marking the node right after a Mix exports the full pipeline up to and including the Mix. To drop an entire side branch (a preview spectrogram and the ops feeding it), mark the branch's **head**. Config value nodes feeding *only* excluded nodes through widget wires don't linger: the flow export folds constants consumer-driven (no surviving consumer → no hoist), and the whole-graph export prunes such non-record feeders explicitly. This is the tool for visualization helpers (`RenderSignalPlot`, preview branches, `RenderOverlays` tails): they keep executing on the canvas, but the exported pipeline stops where the marks start. Marking a **subgraph instance** excludes the whole instance, and a mark placed on a node **inside** a subgraph is honoured too — the marks are read in Python from the posted workflow (`export.collect_export_marks`), which sees nodes inside subgraph definitions (the browser collector used to miss them). Toggle again ("Taidal: include in export") to restore.
- **The export-target mark — "export exactly what computes THIS".** Right-click → **"Taidal: set as export target"** (teal **EXPORT TARGET** badge; mutually exclusive with the skip mark per node). When any target is set, the selection inverts from opt-out to opt-in: the flow export emits **one document per target**, each containing exactly the ops in that target's data **ancestor cone** (the target is the final op) — side branches and viewer helpers simply aren't scheduled, no per-helper skip marks needed. On the impairments graph, targeting the *Mix Records* node exports the production pipeline and drops all three preview branches in one move. The whole-graph export restricts its op surface to the union of the target cones (`outside the export-target cone` in the comment block). CLI twin: `streamstudio export graph.json --target 60` (repeatable). Skip and target compose naturally: the forward-only skip closure trims nodes *after* its mark, the cone selects the nodes computing the target — marking the node after a target as skip still exports the full cone. A skip mark placed directly **on** a node inside a target's cone contradicts the target (it's needed to compute it) and errors — clear one of the two marks. A target placed on a node **inside a subgraph** works the same: the mark is read from the posted workflow and resolved to the expanded prompt node (`371` → `378:368:371`), so you can target a node within a *Spectral Analysis* / *Spectrogram* subgraph and export exactly its ancestor cone.
- **ComfyUI mute (Ctrl+M) / bypass (Ctrl+B)** — the native modes already work: the frontend drops muted nodes and reroutes bypassed ones *before* the API prompt reaches the exporter, so a muted branch never exports. Unlike the mark, these also stop the node from running on the canvas.
- **Offline CLI**: an API-format file carries no node properties, so name the ids explicitly — `streamstudio export graph.json --skip 87 --skip 61` / `--target 60` (both repeatable). Each id is matched against the expanded prompt path by **segment**, so a subgraph-instance id excludes its whole subtree and a subgraph-**inner** id like `--target 371` resolves to its expanded node (`378:368:371`). (The canvas button collects marks automatically — Python reads them from the workflow.)

The whole-graph export (**Export Graph → Taidal YAML**) honors the same mark: marked nodes (and their record-descendants) land in the skipped-nodes comment block instead of the config. Nodes marked *inside* a subgraph definition are honored there too; marking a subgraph *instance* is currently flow-export only.

**Still unsupported** (`UnsupportedTopologyError`, HTTP 400 from the route): cycles, an unresolvable op node, the *Compose Record* adapter (its "metadata from C only" semantics needs a metadata stash/restore — follow-up), and a component merging **distinct record streams** (two different sources fanning into one Mix — one document carries exactly one stream; use `JointStream` by hand).

**Approximations** (documented, deliberate): a metadata-only Mix (input==target==metadata) is discarded on export and not recovered on import.

### Opening with a given canvas (URL params)

A tool that launches the editor can name the graph FILE it works from:

```
http://127.0.0.1:8188/?taidal_graph=/abs/path/ws.d/source.json   # the launcher's graph file
http://127.0.0.1:8188/?taidal_graph=/abs/path/pipeline.yaml      # a Confluid config, converted
```

A launcher may also name the **view** to open the graph on: `?taidal_graph=<file>&taidal_view=app`
lands on ComfyUI's App view (the form of the widgets the document's `extra.linearData` names),
`&taidal_view=graph` on the node canvas. The opener sets the face on the editor's workflow
object, the way the frontend itself does, then checks after the load that the canvas holds the
graph and shows that face, switching with the editor's own toggle only when it does not. The
file is never rewritten for this: absent the parameter, the file's own flag decides. A graph
declaring no `linearData` has nothing to show as a form, so `app` falls back to the canvas and
says so in the console.
A third parameter, `&taidal_workspace=<origin>`, names where the launching workspace serves; the
canvas's workspace-preview nodes (below) get that address in their `workspace_url` widget.


A `.json` path is a native workflow file: the `Taidal.OpenGraph` extension puts it on the
canvas via `/taidal/read_graph` (a missing file is fine — the first save creates it) and
**mirrors every native workflow save** (Ctrl-S / Save / Save As) into it via
`/taidal/write_graph` — the file, not a network push, is how the canvas reaches the
launcher, which stats it whenever it likes. A `.yaml` path is a Confluid config,
converted by `/taidal/import_yaml` as before. Either way the canvas is loaded only when
it does not already show that graph (re-opening never stacks duplicate workflow tabs),
without the param the editor keeps its own last session, and the param is applied once
and stripped, so reloading afterwards keeps what you build.

### Previewing a pane graph from the App view

A view graph a workspace runs per pane has no record source on the canvas: the workspace feeds the
record and fills the window. Its App view is a form of the thresholds, but the Run button would
show nothing — until the source document says `preview: true` in its `app:` block:

```yaml
app:
  inputs:
    - [ClassifyBluetooth, ble_index]
  outputs: [BluetoothVerdict]
  preview: true
```

The importer then places two output nodes around the chain and makes them the App's outputs
(the first name under `outputs` is the op the preview hangs on):

| node | what Run shows |
|---|---|
| **Selection** | the record window the workspace page currently selects, rendered by the workspace |
| **Preview on Selection** | the chain feeding it, converted from the live canvas with the form's unsaved values, run by the workspace over that selection: its curves, note and step report as one image |

Run saves nothing; Ctrl-S persists the values as before. Both nodes carry a `workspace_url` widget
that the launcher fills through `taidal_workspace`, so a file drawn against one address follows the
workspace wherever it serves. The protocol a workspace serves is three routes: `GET /api/state`
(the selected record and window), `POST /api/preview {document}` (the converted document over that
selection, answering `{record, window, payload}`) and
`GET /api/recordings/<id>/thumbnail.png?w&h&x0&x1&y0&y1`. The preview node never executes the ops
upstream inside the editor: its `record` input is lazy and the chain is read off the prompt, so a
canvas without a record source runs nowhere but in the workspace. A refusal (no record selected,
no workspace answering) comes back as an image that says so.

## Importing a `flow.yaml` back to the canvas

A flow document round-trips back to a connected graph — the **inverse** of the canvas compiler.
Use **Taidal → "Import Taidal YAML → Graph"** on the canvas, or the offline CLI:

```bash
streamstudio import flow.yaml -o workflow.json    # then drag workflow.json onto the canvas
```

`streamstudio import` auto-detects a flow-export document (a top-level `flow:` step mapping) and
rebuilds the op graph: each step becomes a node wired from the step before it, a hoisted
`constant=True` value node is placed and its dotted `!ref:` consumers re-wired, and dead-end taps
are tinted.

The source is intentionally left unattached (the export contract — wire your own `Source → Walk`
into the head op). **A BRANCHY flow document does not import yet**: the replay walks a sequence,
while a forking/merging step graph needs a node-per-step walk (tracked in `TASKS.md`) — a linear
document imports normally. `--as group` wraps the result in one box; `--as subgraph` degrades to
`graph` (a flow doc has no natural boundary inputs). Backed by
`streamstudio.graphio.flow_yaml_to_workflow`.

## Importing & exporting whole pipelines (Taidal YAML ↔ canvas)

Beyond the ops-only export above, StreamStudio round-trips a **whole** Confluid config — sources, ops, object members (model / loss / optimizer / loaders / logger / Lightning Trainer), and the runnable that ties them together — between a Confluid YAML and a full ComfyUI `workflow.json`. So you can load an existing training config onto the canvas, edit it visually, and save it back to YAML that runs with `recordstream run -c config.yaml`.

The converter lives in `streamstudio.graphio` (`yaml_to_workflow` / `workflow_to_yaml`); the **Fluid graph is the shared intermediate representation** (confluid owns YAML↔Fluid via `resolve`/`dump`, StreamStudio owns Fluid↔`workflow.json`). It works **without a running ComfyUI**.

### On the canvas — the **Taidal** menu

| Command | What it does |
|---|---|
| **Import Taidal YAML → Current Graph** | Pick a `.yaml`; its nodes + wiring are **merged into the active graph** (added to what you're building). |
| **Import Taidal YAML → New Graph** | Pick a `.yaml`; load it as its **own workflow** (a fresh tab) instead of merging. |
| **Import Graph JSON → Current Graph** | Pick a ComfyUI `workflow.json` (e.g. an "Export Ops → Graph" file) and **merge it into the active graph** — the merge variant ComfyUI's native open lacks. |
| **Export Graph → Taidal YAML** | Serialize the whole active graph and download the hoisted-`!ref:` YAML. |
| **Export Taidal Ops → Flow YAML** | Compile the op DAG to a Confluid `flow:` document (one file per disconnected component). |
| **Export Taidal Ops → Graph (subgraphs intact)** | Extract the op DAG as a raw ComfyUI `workflow.json` **as drawn**, keeping your subgraph instances as black boxes (so the pipeline stays connected). Downloads `ops_graph.json`. |
| **Export Taidal Ops → Graph (flattened)** | Same, but **inline every subgraph** so the inner ops surface, and reconnect the pipeline through the dropped adapters. Downloads `ops_graph_flat.json`. |

**Where the import lands:** **Current Graph** **paste-merges into the active graph** — the imported nodes are added to what you are building (at the canvas mouse position, selected, in one undo step, leaving your own clipboard untouched) rather than replacing it / opening a separate tab, using the same litegraph clipboard/paste path as ComfyUI's node-template insertion. So you can import several configs side by side, and it also works while you are editing inside a subgraph. **New Graph** loads the config standalone (its own workflow / a fresh tab — Current Graph also falls back to this on a ComfyUI build without `pasteFromClipboard`). (The `subgraph` / `group` packaging modes are still available from the `streamstudio import --as subgraph|group` CLI; after a Current-Graph import you can also use ComfyUI's native grouping / *Convert to Subgraph*.) Backed by the `/taidal/import_yaml` and `/taidal/export_workflow` routes (`web/import_yaml.js`, `web/export_workflow.js`).

**Exporting the ops — YAML vs. graph.** All three operate on the op-kind nodes + the *Mix Records* fan-in (dropping the source + Walk/Extract/viewer adapters) and **honor the same `export-skip` / `export-target` marks** you set for the YAML export (right-click a node → *Set as export target* / *Skip export*). **→ Flow YAML** *compiles* the DAG into a `flow:` document (named steps, `from:`/`merge_from:`/`bind:` wiring, constant hoisting) you reattach to any source. The two **→ Graph** commands *extract* the raw DAG into a `workflow.json` (positions, widgets, wiring preserved, nothing compiled). Because a real pipeline routes the record *through* subgraph instances and past viewer/analysis side-taps, the graph export: (1) **honors a target mark** — restricting the output to that target's data ancestor cone, so the dead-end analysis branches you didn't target are dropped (mark the node you care about, exactly as for the YAML export); (2) **keeps the config/value nodes** (the `constant=True` producers) that feed the kept ops; and (3) treats **subgraphs** two ways — **subgraphs intact** keeps the instances as black boxes (with their nested definitions), while **flattened** inlines every subgraph so the inner ops surface and reconnects through the dropped pass-throughs (Reroute/Extract/boundaries). Both produce a connected graph. Backed by the `/taidal/export_flow` and `/taidal/export_ops_graph` (`flatten` flag) routes (`web/export_flow.js`). *Note:* flattened mode keeps the inlined nodes' own positions, which can overlap — auto-arrange on load.

### Offline CLI

```bash
streamstudio import config.yaml -o workflow.json          # YAML → canvas workflow
streamstudio import config.yaml --as subgraph -o sub.json # … as a reusable subgraph blueprint
streamstudio export-workflow workflow.json -o config.yaml # whole canvas → hoisted-!ref: YAML
streamstudio convert-server                               # resident converter: JSON lines on stdin
```

`convert-server` answers `{"ready": true}` once its node registry is imported, then serves
`{"json": "<workflow>", "yaml": "<out>"}` requests until stdin closes — for a consuming
workspace that converts several graphs per user action, the ~10 s import is paid once
instead of per call.

### The exported shape (hoisted `!ref:`)

Every node becomes a **top-level key** named by the socket it feeds (path-prefixed on collision); a wire becomes a `!ref:`; shared scalar dimensions (`num_classes` / `image_size`) are hoisted. A deferred member (optimizer / loaders / Lightning Trainer) emits `!lazy:`, an eager one `!class:`. It matches the shape of a hand-written reference config:

```yaml
num_classes: 37
model: !class:myproject.models.ImageClassifier
  model_name: efficientvit_b0
  num_classes: !ref:num_classes
optimizer: !lazy:torch.optim.AdamW
  lr: 0.0001
runnable: !class:myproject.training.ClassificationTrainer
  task: fit
  model: !ref:model
  optimizer: !ref:optimizer
```

### How import works

`yaml_to_workflow` broadcast-resolves the config to a Fluid **marker** graph with `confluid.load(until="settled")` (no instantiation — fast, side-effect-free) and walks it. Pass `--instantiate` (CLI) / `instantiate=True` (API) to instead walk *live-but-inert* objects from `confluid.load(..., solidify=False)` — same result, with the expensive `solidify()` (e.g. building a model backbone) suppressed.

### v1 scope & limitations

- **Export skips what YAML can't express — with one exception, it never fails on it.** A canvas usually contains nodes with no Confluid form: native ComfyUI nodes (*Preview Image*, *Show Text*), the canvas-only Taidal adapters (*Walk Dataset*, *Extract from Record*, *Retag*, …), and occasionally a node whose config can't be serialized. **Export Graph → Taidal YAML** leaves those out and lists each one (with the reason) in a leading `#` comment block of the downloaded YAML — the document below stays a normal, loadable config of the remaining Taidal nodes. A wire from a skipped node into an exported config member is dropped (the member falls back to its default) and noted in the same block — **except** a wire from a native **primitive** node (`PrimitiveInt`/`PrimitiveFloat`/…), which inlines as its literal `value` widget. The canvas **wiring is preserved**: each run of record-chained ops folds into a `pipeline: !class:recordstream.core.stream.Stream` entry — `source: !ref:<source>` (traced through the *Walk Dataset* adapter back to the source node) plus the ordered `ops:` list, with fan-out compiled to `Save`/`Use` context cells — so the exported YAML reconstructs the same data flow (`confluid.load("config.yaml")["pipeline"]` is an iterable `Stream`). A chain broken by a skipped node continues as its own `pipeline_N` entry.
- **The one exception: a Taidal node this StreamStudio does not have REFUSES the export.** Every discovered node is keyed `Taidal_<package>_<category>_<Class>`, so a `Taidal_*` class type the running server has no class for is not a ComfyUI node — it is a canvas the server is out of step with, almost always because the op was added *after* the server started (the node registry is read once, at startup). Dropping such a node would hand you a silently shortened chain, so the export stops and names them:

  ```
  this StreamStudio does not register 11 node(s) the canvas names: node 2
  (Taidal_waivefront_op_measure_TuneToBand), node 3 (…SelectBurst), … A Taidal node the
  canvas names but this installation has no class for is normally an op added AFTER the
  editor started — the node registry is read once at startup. Restart it
  (`streamstudio restart --editor comfy`) and convert again; if the op really is gone from this
  installation, the canvas is older than the code and needs redrawing.
  ```

  The canvas button surfaces it as the error; `streamstudio export-workflow` exits with it and writes nothing (it used to write the partial YAML). Native ComfyUI nodes and the registered canvas-only adapters (*Extract as Op*, *Mix Records*, *Value*, *Math*) are unaffected — they still drop into the comment block as before.
- **Value chains compile here too.** The ``op-chain → Extract(input) → Math → widget`` pattern wraps the consumer in a `ConfigureOp` (chain ops + `FormulaOp`) exactly like the flow export — inside pipeline folds and on standalone op-value entries; the consumed Extract/Math adapters are no longer listed as "left out".
- **Value nodes export by their class name with DOTTED output refs.** A wire from a value-producer node's output slot (e.g. a config node's `disabled` output feeding an op's `tx_disabled` param) emits `!ref:<entry>.<output>` — the `@output` *property value*, not the config instance a plain `!ref:` would inject. On **import**, confluid's resolver instantiates such dotted refs (a documented `resolve()` caveat), so the consumer widget receives the *evaluated* value and the config lands as a standalone value node carrying its settings — re-wire the two on the canvas to re-establish the live link.
- A config member with **no StreamStudio node** (a class not tagged op/source/engine/member — e.g. a torchmetrics `MetricCollection`) is **dropped from the canvas with a warning** on import; the runnable falls back to its default. To surface such a class, tag/register it (a package registers its loggers and lazy torch / Lightning object members the same way).
- Object **nodes** grow dynamic `logger_N` / `callbacks_N` sockets for list-valued object params (see *Attaching loggers / callbacks* above), and the whole-graph graphio round-trip respects them: **import** wires a `List`-valued member's items into `logger_0`, `logger_1`, … and **export** collapses those sockets back into one `logger:` list (the importer/exporter read the node's own `<name>_0` seed to decide).
- **Subgraphs** are self-contained in the `workflow.json` (`definitions.subgraphs`); on export they're **inlined** back into the flat YAML — including workflows saved by current ComfyUI (which stores a definition's inner links as dicts), several instances of the same definition, nested definitions, and the wires crossing the subgraph boundary (an outer wire into a subgraph input re-attaches to the inner node(s) it feeds — fan-out included — and a subgraph output re-originates at its inner producer). Standalone reusable `include:` files are a follow-up.
- Round-trip is **semantic** (topology + kwargs + kinds), not byte-exact: node positions are re-derived by a deterministic layered layout (sources left → runnable right), and only non-default widget values are emitted.

## Making your class show up here

StreamStudio consumes the [confluid](https://github.com/Gearlux/confluid) `@configurable` registry — it never
hand-defines a node, and you never edit StreamStudio to add one. The full contract a class must satisfy
(entry-point registration, the `task` × `role` taxonomy, zero-arg/lazy construction, docstring `Args:`,
closed `Literal`s, PEP-593 `Interval` range marks) lives in
**[confluid `docs/extending-discovery.md`](https://github.com/Gearlux/confluid/blob/main/docs/extending-discovery.md)**.

What is specific to StreamStudio:

- **Admission** is the positive allowlist `_NODE_CATEGORIES` in `bridge.py` — an untagged class is
  never a node, and there is no negative special-casing (see *How nodes are generated* below).
- **Palette grouping** reads `role`/`task` first, else `category` + the presentation-only `group`
  tag; the producing package moves into the `[PKG]` label (see *Palette path* below).
- **Socket types** come from the annotations: `_map_type` for widgets/wires (see *Type mapping*),
  and role-qualified `RECORDSTREAM_OBJECT:<role>` for object members (see *Connection typing*).

A missing or wrong tag makes the node vanish from the palette with no error — fix it upstream in the
class's home package, never here.

## Plugins

A package can contribute more than nodes: custom **routes** on the editor's backend and its own
**frontend JS** (a picker, a viewer) — without StreamStudio knowing the package exists. Declare a
`streamstudio.plugins` entry point naming a module:

```toml
# the plugin package's pyproject.toml
[project.entry-points."streamstudio.plugins"]
my-plugin = "my_plugin.studio"
```

At editor start-up StreamStudio imports that module and reads two optional module-level attributes:

```python
# my_plugin/studio/__init__.py
WEB_DIRECTORY = "web"                  # a directory of frontend *.js, relative to this module

try:
    from aiohttp import web           # the editor's own dependency; guard it so the module
except ImportError:                   # still imports in a venv without it
    ROUTES = None
else:
    ROUTES = web.RouteTableDef()

    @ROUTES.get("/taidal/my_datasets")
    async def my_datasets(request):
        return web.json_response({"datasets": []})
```

- **`ROUTES`** — an aiohttp `RouteTableDef`; each route is merged onto the editor's route table
  (first registration wins per method + path). Keep handlers non-blocking: run anything slow in an
  executor with a timeout, and degrade to an empty result rather than erroring the editor.
- **`WEB_DIRECTORY`** — served at `/extensions/<your-dist-name>/…`, the same URL depth as any
  ComfyUI extension, so relative imports of `../../scripts/app.js` keep working. Ship the JS files
  via `[tool.setuptools.package-data]` — the directory has no `__init__.py`, so nothing else puts
  them in your wheel.

To attach a **custom picker to one of your node's fields**, stamp the class instead of patching the
bridge: `__taidal_widget_params__ = {"<param>": "<TAG>"}` renders that constructor param as a
free-text STRING carrying `taidal_widget="<TAG>"`, and your own `WEB_DIRECTORY` JS matches the tag
(`config[1].taidal_widget === "<TAG>"`) and adds the picker. The tag string is opaque to
StreamStudio — its meaning, its route, and its JS all live in your package.

A broken plugin is skipped with a warning naming it; the editor and the other plugins keep working.
Like node discovery, the group is read from installed metadata — reinstall your editable after
adding the entry point. (This is separate from `streamstudio.custom_nodes`, which seeds *git
repositories* of third-party ComfyUI node packs at launch.)

## How nodes are generated

StreamStudio never hand-defines a node. At ComfyUI startup, `_discover_and_register_nodes()` (`streamstudio/__init__.py`):

1. Enumerates the `confluid.configurables` entry-point group via `importlib.metadata`.
2. For each module, `recordstream.discovery.scan_module` finds the compatible callables.
3. `register_module_nodes` / `create_comfy_node` (`streamstudio/bridge.py`) classify each callable (Source / Op / Generic — see *Building a pipeline*) and wrap it in a ComfyUI node class, keyed by `_node_key` as `Taidal_<package>_<category>[_<group>]_<callable>` (globally unique, so a same-named class in two modules — e.g. numpy's vs torch's `Threshold` — doesn't clobber its twin) and filed under a `Taidal/<Package>/<Category>[/<group>]` palette path (built by `_node_category`: top-level package via `_PACKAGE_DISPLAY_NAMES` for canonical casing, then the `@configurable` category, then the optional `group` tag). The displayed name stays the verbatim class name.

Registration is a **positive allowlist**: a class becomes a node only if its `@configurable` category is one of `_NODE_CATEGORIES` (currently `{op, model, source, engine, sink, value}`). So the palette is exactly the deliberately-tagged ops, sources, engines, and sinks. The recordstream **engines** (`Stream` / `FlowGraph` / `JointStream`, `category="engine"`) and the source-view `DatasetSplit` (`category="source"`) **are** buildable on the canvas: their source-typed constructor params render as wired sockets — a single `source` → a `RECORDSTREAM_SOURCE` input, a source-list (`streams`) → dynamic `source_N` inputs, an op-list (`ops`) → dynamic `op_N` `RECORDSTREAM_OP` inputs — so a Source/Op node (which already outputs *itself* as `RECORDSTREAM_SOURCE` / `RECORDSTREAM_OP`) wires straight into them. The RecordStream storage **sinks** (`category="sink"` — `HDF5Sink` / `ZarrGroupSink` / `ZarrBatchSink` / `DirectorySink` / `JsonSink` / `JsonPerWindowSink` / `YoloSink`) become **object-member producer** nodes (two outputs, `instance` + `class`, on a role-qualified `RECORDSTREAM_OBJECT:sink` wire) that dock into a `DatasetProcessor` runnable node's `sink` slot — wire `Source → Stream → DatasetProcessor(stream, sink)` and queue it to write the dataset, or export the graph to a `recordstream run` YAML. Excluded *by construction*: the **raw-callable op wrappers** (`FilterOp` / `WrappedOp`) — UNcategorised (bare `@configurable`), since they take a raw Python callable nothing a widget or socket supplies (so they stay out even with `engine` allowed); **task datasets** (`*_dataset`, e.g. `classification_dataset`) — a trainer-config concept a config generator cares about, but a canvas only needs a `source` to yield records; uncategorised module-level helper functions; and an **UNtagged sink** (a raw `Storage` subclass without `category="sink"`, including the `write(prediction, metadata)` **prediction** sinks `DetectionPredictionsSink` / `ClassificationPredictionsSink`, which aren't record sinks). You can build a dataset by wiring `Source → Op → …` *or* by feeding a `Source`/`Op` into a `Stream` / `DatasetSplit` engine node. (To surface another role, add its category to `_NODE_CATEGORIES` in `bridge.py` and tag the classes. Note: the flow export compiles the op DAG — fan-out, *Mix Records* fan-in, `op_N` op-values — but engine-node graphs (`Stream`/`JointStream` built on the canvas) and sink/`DatasetProcessor` nodes aren't exportable through it; use the whole-graph export for those.)

A module that fails to import or scan is skipped at `debug` level, so one package's missing optional dependency never empties the palette.

> **Entry points live in installed metadata.** Discovery reads `importlib.metadata`, so editable installs *freeze* the entry-point table at install time. After adding or changing a `[project.entry-points."confluid.configurables"]` declaration in any workspace package, reinstall that editable or its nodes silently vanish from the palette. Do **not** use `--reinstall` / `--force-reinstall` — that re-resolves your dependencies from the index instead of your checkouts, so an editable install is silently swapped for a release wheel and nothing errors.

## Type mapping

Node sockets and widgets are derived from each callable's signature by `_map_type` (`streamstudio/bridge.py`):

| Python type | ComfyUI mapping |
|-------------|-----------------|
| `int` / `float` / `str` / `bool` (incl. `Optional[...]` / `Union[...]` of these) | scalar widgets (`INT` / `FLOAT` / `STRING` / `BOOLEAN`) |
| `recordstream.Record` (== `Dict[str, Any]`; incl. `Optional[Record]` / `Iterator[Record]`) | `RECORDSTREAM_RECORD` socket |
| a record-yielding source class | `RECORDSTREAM_SOURCE` output socket |
| `datasets.Dataset` | `HF_DATASET` socket |
| the dataset-id param of a `HuggingFace*` source (`repo_id` / `path`) | free-text `STRING` + optional cached-dataset picker (`web/hf_dataset_chooser.js` → `/taidal/hf_datasets`), tagged `taidal_widget="HF_DATASET_ID"` |
| a feature-selector param of a `HuggingFace*` source (`input_feature` / `target_feature`) | free-text `STRING` + optional column picker (`web/hf_feature_chooser.js` → `/taidal/hf_dataset_features`), tagged `taidal_widget="HF_FEATURE"` |
| the `split` param of a `HuggingFace*` source | free-text `STRING` + optional split picker (`web/hf_split_chooser.js` → `/taidal/hf_dataset_splits`), tagged `taidal_widget="HF_SPLIT"` |
| the `metadata_features` list param of a `HuggingFace*` source | comma-separated `STRING` + multi-select "add column" picker incl. `*` (`web/hf_metadata_chooser.js` → `/taidal/hf_dataset_features`), tagged `taidal_widget="HF_METADATA"` |
| `pathlib.Path`, or a param named `*path*` / `*dir*` / `*root*` / `*folder*` | directory-picker `STRING` (`web/directory_chooser.js` → `/taidal/choose_directory`) |
| a param named `*file*` / `*checkpoint*` / `*ckpt*` (without a `*dir*` / `*root*` / `*folder*` segment — `checkpoint_dir` stays a directory) | file-picker `STRING` (`web/directory_chooser.js` → `/taidal/choose_file`) |
| anything else | `STRING` fallback |

`RECORDSTREAM_SOURCE` and `RECORDSTREAM_RECORD` are StreamStudio socket types that carry the live `Stream` / record (a plain `dict`) objects between nodes (ComfyUI connects two sockets only when their type strings match), which is what lets ops chain into a pipeline.

The pickers call the `/taidal/choose_directory` / `/taidal/choose_file` routes registered in `streamstudio/__init__.py`, currently native **macOS** dialogs (`osascript`) — a folder dialog cannot select a file, which is why the two kinds exist. Extend `TYPE_MAP` / `_map_type` to support new types — never special-case an individual node.

### HuggingFace dataset-id field

A HuggingFace dataset source's identifier is a **name**, not a folder — `recordstream.sources.huggingface.HuggingFaceSource` happens to call it `path` (mirroring `datasets.load_dataset`), while another package's dataset class may call it `repo_id`. Either way StreamStudio renders it as a free-text `STRING` (so you can type any Hub repo id like `kitofrank/RFUAV`, or a local imagefolder path), **not** the directory picker. `web/hf_dataset_chooser.js` adds an optional “📚 cached dataset” combo populated from the `/taidal/hf_datasets` route (`huggingface_hub.scan_cache_dir()`, dataset repos only) — selecting one fills the text field, which stays editable. The field is detected by the `HuggingFace*` class-name convention, so any future `HuggingFace*` source inherits it automatically.

### HuggingFace feature-selector fields

`HuggingFaceSource`'s `input_feature` / `target_feature` name which dataset *column* feeds the record's `"image"` / `"class"` entries — an `Image` / `Label` typed item (the column defaults are `image` / `label`). They render as free-text `STRING`s — but typing column names blind is error-prone, so `web/hf_feature_chooser.js` adds an optional “🏷️ columns” combo next to each, populated from the **selected** dataset's actual columns. The columns come from the `/taidal/hf_dataset_features` route (`datasets.load_dataset_builder().info.features`, falling back to streaming one example), keyed off whatever is currently in the node's dataset-id field — so it's the cached-dataset picker one level down: there you pick *which dataset*, here *which of its columns*. The combo refreshes whenever the dataset id / `name` / `split` changes (including when you pick a cached dataset), and selecting a column fills the editable text field. Detected by the same `HuggingFace*` class-name convention, so any future `HuggingFace*` source inherits it.

### HuggingFace split field

`HuggingFaceSource`'s `split` stays a free-text `STRING` — HF accepts slice syntax like `train[:90%]` or `train+test`, which a closed dropdown couldn't express — but `web/hf_split_chooser.js` adds an optional “🪓 splits” combo populated from the **selected** dataset's actual splits via the `/taidal/hf_dataset_splits` route (`datasets.get_dataset_split_names`, metadata-only). Like the column picker it keys off the current dataset-id field and refreshes when the dataset id / `name` changes; selecting a split fills the editable field. This is distinct from `DatasetSplit.split`, which is a closed `Literal["train","val","test"]` fraction-view selector and renders as a plain combo (via `_literal_options`), not this picker.

### HuggingFace metadata-columns field (multi-select)

`HuggingFaceSource.metadata_features` is a **list** — which columns ride along in the record as their own per-column `Label` entries, keyed by the column name. It renders as a comma-separated `STRING` (which `_coerce_kwargs` splits back into a list), and `web/hf_metadata_chooser.js` adds a “🗂️ add column” combo. ComfyUI has no native multi-select, so the combo **appends** the picked column each time (pick repeatedly to build the list); the field stays editable. The options are the **selected** dataset's columns (reusing the `/taidal/hf_dataset_features` route) **minus** whatever is currently mapped as `input_feature` / `target_feature`, plus a special **`*`** entry meaning *every column except input/target* (the `recordstream.sources.huggingface.METADATA_ALL_FEATURES` sentinel — resolved against the real dataset at load; left empty ⇒ no extra metadata). Picking `*` is exclusive (it already means "the rest"). The list refreshes when the dataset id / `name` / `split` **or** the input/target feature changes.

### Constructor-parameter tooltips

Each widget's hover tooltip is sourced from the wrapped callable's docstring `Args:` block (parsed by `confluid.parse_param_docs`) — the **same** per-parameter help a schema generator surfaces as JSON-Schema `description`s. Document a constructor parameter once in the upstream class's `Args:` and it shows up in both GUIs. A parameter with no `Args:` entry simply has no tooltip; the fix is to document it in the package that owns the class, never to hand-write a tooltip in StreamStudio.

## Declaring a canvas's App view (`app:`)

ComfyUI can open a workflow as a **form** instead of a node graph — its App view — showing only the
widgets a document names. A source document declares that view in an `app:` block, and
`streamstudio import` emits it, so regenerating the JSON keeps it:

```yaml
flow:
  # …the graph…
outputs: verdict

app:
  inputs:
    - [ClassifyBluetooth, envelope_spread_db]   # [class name, widget name]
    - [ClassifyBluetooth, ble_index]
  outputs: [BluetoothVerdict]                   # what the Run button drives
```

Nodes are addressed by **class name**, not by node id: an id is assigned by the importer, so
regenerating would renumber it, and a class name reads the same in both document shapes (a `flow:`
step graph and a whole-graph config). A `(min, max)` parameter arrives as two widgets, so expose the
halves (`deviation_hz__lo` / `deviation_hz__hi`).

A declaration that could not render is **refused at import**, naming what the canvas does carry —
because the frontend fails silently otherwise (an entry whose node or widget does not resolve is
dropped, leaving an empty form with no error):

```
AppViewError: app: MeasureSymbolClock.windows_hz is a wired socket, not a widget — an App view
shows fields, so expose one of: max_points, inst_freq, output, findings
```

`app:` is refused for `--as_ subgraph` too: subgraph packaging moves every node inside a definition,
and the App view renders a subgraph-internal widget only once it has been promoted onto the instance
node. Import a template that wants an App view **flat**.

## Drawing a decision — outcome sockets and `Join branches`

An op that declares its outcomes — a class-level `flags` tuple, the record engine's per-node
contract — is a **test node**: the canvas gives it one **record output per outcome**, named by
it, after its `record` / `op` outputs. Wire a node from an outcome and it runs only for the
records that took that outcome; the branches come back together in a **`Join branches`** node
(one output, up to six record inputs — "whichever branch ran"). The picture, for the Bluetooth
chain:

```
… → MeasureDeviation ─▶ ClassifyBluetooth ├ ble      ─▶ DecodeBleAdvertising ────┐
                                          ├ bredr    ─▶ DecodeClassicAccessCode ─┼─▶ Join branches ─▶ BluetoothVerdict
                                          ├ try_weak ─▶ CorrelateAccessAddress ──┘
                                          └ fsk        (unwired)
```

There is nothing to type: the wire is the condition. A decision may nest — a branch node that
is itself a test node hangs its own branches off its own outcomes (the radio chain's
`MeasureLinearEye` decides `linear` inside the `try_linear` branch), and every outermost tail
goes into the one join.

**What is exported is the chain that runs.** Both exporters (the whole-graph YAML the
annotation workspace runs, and the `flow:` export) fold the drawing into a linear, gated
chain: each branch node carries `requires: <outcome>` — the document's spelling, which the
canvas never shows — the branches follow the test node in its own outcome order, and the join
disappears. A closed gate passes the record through at the cost of one flag check, so no
branch is computed "just in case". Importing such a document draws the branches back: a step
gated on `ble` hangs off the outcome socket of the earlier step that decides `ble`, and the
first ungated step after the branches is fed through a join.

| you draw | the editor answers |
|---|---|
| a `Join branches` fed by nodes that are not outcomes of one test node | `Join branches joins the outcomes of ONE decision node — its inputs come from MeasureEnvelope, MeasureInstantaneousFrequency, which decide nothing` |
| tails of two separate decisions into one join | `… its inputs come from branches of ClassifyBluetooth and of ClassifyRadio; join each decision separately` |
| a node with no gate of its own on a branch | `MeasureEnvelope sits on the 'ble' branch of ClassifyBluetooth but runs on every record — it cannot be gated …` |
| a branch that forks | `the 'ble' branch of ClassifyBluetooth forks at DecodeBleAdvertising — a branch is a straight run into Join branches` |
| a plain fan-out (two consumers of one record output) | unchanged: the exporters' own branchy refusal |

**A nested-container literal lands on the canvas as a `Value` node.** A param typed as a nested
sequence (`MeasureSymbolClock.windows_hz: [[0.9e6, 1.1e6], [1.8e6, 2.2e6]]`) has no widget — the
bridge gives it a `RECORDSTREAM_VALUE` socket — so the importer places a `Value` node holding the
literal (numbers, even where the YAML parser read `0.9e6` as a string) and wires it in; both
exporters inline it back into the kwarg. The canvas is the truth for that table too: edit the
Value node and the exported chain follows.

**The contract is checked where the chain is drawn.** A straight chain's ops are constructed
from their widget values and run through the record engine's `check_chain`: a node that reads
an entry nothing before it writes is refused at export with the engine's located message —
`MeasureSymbolClock needs the record entry 'inst_freq', which nothing before it produces …` —
instead of surfacing later as an empty pane. A chain whose ops embed other nodes is checked
where it runs.

## Connection typing

Every record wire keeps the single `RECORDSTREAM_RECORD` socket string, so any op *can* be wired and the pipeline still chains. The record is self-describing — a plain `dict` whose typed item values each carry their own metadata — so an op that receives an incompatible record raises naturally when it is applied at execution; there is no separate connection-time type filter.

## Dependencies

| Package | Purpose |
|---------|---------|
| `recordstream` | Op discovery (`scan_module`) + pipeline engine. |
| `confluid` | `confluid.configurables` entry-point group that drives discovery. |
| `datasets` | HuggingFace `Dataset` ↔ `HF_DATASET` socket. |
| `liquifai` | App / CLI framework. |
| `loggair` | Logging. |
| `waivefront` *(optional plugin — install it and its nodes appear)* | RF-domain ops + visualizers (spectrogram, IQ time/freq). |

## Development

```bash
git clone git@github.com:Gearlux/streamstudio.git
cd streamstudio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

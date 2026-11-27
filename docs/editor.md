# The editor

The YAML file is the graph. The editor draws it, and every change you make is a patch of the line it is about —
comments, `$VAR` spellings and key order survive. No change is written to disk until you **Save**; a graph made with
**New…** is written at once, so every graph the editor shows is a file on disk. This page goes through the editor
gesture by gesture: what each does to the document, and what it refuses.

## Starting and stopping it

```bash
streamstudio path/to/graphs                                  # port 8190; the folder's *.yaml files are the graphs
streamstudio                                                 # the same, over the current directory
streamstudio path/to/graphs/demo.yaml                        # that graph's folder; the browser opens on demo
streamstudio path/to/graphs --port 8191 --templates-dir ~/my-templates
streamstudio path/to/graphs --background                     # serve detached; the prompt comes back once it answers
streamstudio list                                            # every running editor: port, folder, address
streamstudio stop                                            # stop the running editor
streamstudio stop 8191                                       # the one on port 8191
streamstudio stop path/to/graphs                             # the one serving that folder
streamstudio stop --all                                      # every running editor
streamstudio restart                                         # stop it, then serve the SAME folder again
streamstudio restart 8191                                    # the one on port 8191, on that port again
streamstudio restart path/to/graphs                          # stop the one serving that folder, serve it again
```

Naming a graph's file serves its folder — every graph of the folder is in the list — and opens your browser on that
graph once the editor answers (`http://127.0.0.1:8190/?graph=demo`, also printed on a line of its own). A folder
already being served is not started twice: the tab opens on the graph in the running editor. `--browser never` keeps
the browser closed.

`-p` is the short form of `--port`, `-b` of `--background`. `--browser always|new|never` opens the editor's address in
your browser once it answers (default: `never` for a folder, `always` for a graph's file); `--auto-port` takes the next free port without asking.

`stop` and `restart` never throw away unsaved work: the editor keeps it in a hidden file of its folder, and the
next start offers it back ([Unsaved work survives a stop](#unsaved-work-survives-a-stop)). A bare `restart` serves the
folder served last; `restart <port>` serves the folder the editor on that port serves, with the templates folder it
was started with, so the kept work is offered at once.

**From the page:** the bar ends in two buttons. **↻** restarts the server with the code on disk now — what
`streamstudio restart <port>` does — and reloads the tab once the new server answers. **⏻** stops it — what
`streamstudio stop <port>` does — and closes the tab. Both keep unsaved work the same way. Every other open tab of that
editor is told its server went. A page shown in another app's panel has neither button: that server is the app's.

The folder is resolved once, so every path the editor shows is absolute. A folder that is not there is refused
before anything is stopped or started (`restart` included):

```
$ streamstudio restart grpahs
Error: 'grpahs' is not a workspace folder
```

A folder that is already being served is not started twice — the command prints where it is served and returns:

```
$ streamstudio graphs
streamstudio is already running — serving /home/me/graphs
  http://127.0.0.1:8190
  'streamstudio stop' stops it; 'streamstudio restart' reloads it.
```

When ANOTHER folder is served on the port, the command asks whether to take the next free port (`--auto-port` takes
it without asking). A port a program that is not StreamStudio holds is refused, and `stop` and `restart` never signal
such a program:

```
$ streamstudio graphs --port 5432
Error: port 5432 on 127.0.0.1 is in use (pid 801 postgres) — name another with --port (or stop that process)
$ streamstudio stop 5432
Error: port 5432 is held by something that is not streamstudio (pid 801 postgres) — not stopping it
```

With several editors running, a bare `stop` lists them and asks which one (`--all` stops every one).

An option the command does not have is refused by name, not ignored:

```
$ streamstudio restart --prt 9000
Error: streamstudio: unknown option 'prt' — it names no parameter of the 'restart' command and no key in the
configuration. Did you mean 'port'? Declared parameters: target, port, templates_dir, background, browser.
```

## Unsaved work survives a stop

When the editor stops while it holds unsaved work — `streamstudio stop`, `streamstudio restart`, Ctrl+C in its
terminal, or closing that terminal window — it writes that work to a hidden file in the folder it serves,
`.streamstudio-workspace.json`. The graph
files themselves are not touched. `stop` says where the work went:

```
$ streamstudio stop
streamstudio (/home/me/graphs): stopped pid 57474 (port 8190)
Its unsaved work (demo.yaml) is kept in /home/me/graphs/.streamstudio-workspace.json — the next start asks what to do.
```

The editor's own terminal prints the same as it exits (`Unsaved work (demo.yaml) is kept in … — the next start
asks what to do.`). With nothing unsaved, `stop` prints only its first line and no
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
  scan.yaml           changed — the file on disk changed after the editor stopped
[Restore unsaved work]   [Open the saved files]
```

| you choose | what happens |
| --- | --- |
| **Restore unsaved work** | Those graphs open as they were when the editor stopped — the text, the node positions, the groups — marked ●. Nothing is written until you Save. |
| **Open the saved files** | One more question: `Discard the unsaved work of the last session? The unsaved changes of demo.yaml and scan.yaml are lost. This cannot be undone.` **Discard** drops the kept work and shows the files as they are on disk; **Cancel** goes back to the first question. |

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
that line to its `.gitignore`.

## The window

A menu bar across the top, the canvas in the middle, a side panel on the right with three tabs — **text** (the
YAML as the editor holds it), **settings** (a form of every node on the level the canvas shows — of the graph's
`app:` fields when it declares them, [below](settings.md#settings-a-graphs-app-fields)) and **run**.

**A setting is typed where it is shown** — on its card, and in the settings tab. Enter or leaving the field saves the
value into the editor; a refused value puts the old one back. What the field is follows the setting's type:

| the setting is | its field |
| --- | --- |
| a choice | a list to pick from |
| yes / no | a tick |
| a number | a number box |
| text | a text box |
| a list of text (`names: List[str]`) | ONE text box, the names separated by commas: `cat, dog` writes `names: [cat, dog]`. Spaces around a name go; an emptied box writes an empty list. |
| a list of numbers (`rates_hz: List[float]`) | ONE text box, the numbers separated by commas: `1200, 2400` writes `rates_hz: [1200, 2400]`. Text that is not all numbers goes back to the value; an emptied box writes an empty list. |
| a fixed count of numbers (`shape: Tuple[int, int, int]`) | that many number boxes, written together as one list when a box is left. Two of them are a `(min, max)` pair. |
| names with a number or a text each (`mapping: Dict[str, int]`) | in the settings tab, one row per name: a name box, a value box and ✕ to remove it; **+ add a name** adds a row. The rows are written together as one map, the names in the order of the rows. A row with a name and no value yet (or the reverse) waits, marked, and nothing is written. A card shows the map and does not edit it. |
| anything else (a list of lists, a map of lists, a list whose names hold a comma) | shown, not typed: edit it in the graph's YAML file |
| read from a key at the top of the graph (`stop: !ref:limit` under `limit: 3`) | the key and its value, `limit = 3`, shown and not typed: the key may feed several settings, so change it in the text tab. Setting it through the editor is refused: `source.stop is the value of the top-level key limit (!ref:limit) — change limit in the text tab`. A key that is not there is a problem at the node's line: `source.stop: !ref:nowhere names no node and no top-level value`. |

| menu | items (shortcut; on a Mac ⌘ for Ctrl) |
| --- | --- |
| **New** | Flow graph… · Pipeline… · Source graph (with its contract)… |
| **File** | Open… `Ctrl+O` · Save `Ctrl+S` · Save As… `Ctrl+Shift+S` · Revert |
| **Edit** | Undo `Ctrl+Z` · Redo `Ctrl+Shift+Z` (or `Ctrl+Y`) · Cut `Ctrl+X` · Copy `Ctrl+C` · Paste `Ctrl+V` · Delete `Delete`/`Backspace` · Select all `Ctrl+A` · Rename… `F2` |
| **View** | Nodes sidebar `N` · Fit to view · Arrange left to right · Arrange diagonally · Arrange top to bottom · Arrange in columns · Minimap |
| **Graph** | Add group · Group selection `G` · Convert selection to subgraph · Insert template ▸ · Save selection as template… · Delete template ▸ · Check graph |
| **Run** | Run `Ctrl+Enter` · Step · Rerun selected node |

An item that cannot act is greyed — Save while nothing changed, Copy with nothing selected. Keys are ignored
while you type in a field. `Escape` closes an open menu; with none open, it goes up one subgraph level. The bar
also has a **Nodes** button (the sidebar) and a green **▶ Run**.

**The sidebars are sizable.** Drag the edge of the Nodes sidebar or of the side panel that faces the canvas to
change its width — the pointer turns into a resize arrow over it. A sidebar is never narrower than 200 px and the
canvas always keeps 240 px. Double-click the edge to put the width back. The widths are remembered in your browser;
nothing of them is written into a graph's files.

**A link opens a view.** The address follows what the page shows, so a reload or a shared link opens the same
graph, subgraph level and side tab:

```
/?graph=scan                         the graph scan, the text tab
/?graph=scan&tab=settings            the same graph, the settings tab open
/?graph=demo&level=prep              the inside of the subgraph prep
/?graph=nosuch                       nothing opens; the canvas says
                                     "There is no graph 'nosuch' in /home/me/graphs — the graphs there are: demo, scan and source."
                                     with one button per graph
```

**The settings tab** of a graph with an `app:` block lists only the fields it names (say 6 of its 40 parameters),
one section per node in the block's order, and a tick box **all parameters** lists every one. A
`(min, max)` pair (`deviation_hz`) is ONE field of two numbers. An `app:` entry that names nothing is listed under the
fields in the server's words.

**The graph list** (the dropdown in the bar) holds every `*.yaml` of the folder, every file opened with
File ▸ Open…, and every file New… wrote in another folder. It leaves out `*.layout.yaml` (the positions sidecars)
and `*.steps.yaml` (subgraph steps files, below). A file that appears in the folder later shows up on the next
look. A file from elsewhere (opened, or made there with New…) whose name the list already holds gets `demo~2`. A
graph with unsaved changes reads `● demo · flow`.

## Save, Save As, Revert — changes stay in the editor until you save

Every edit — a value, a new step, a rename, undo, a move, a group — changes the editor's copy only. The title, the
file name in the bar and the dropdown show **●** while a graph holds unsaved changes; closing or reloading the
page asks first. On the document of [Subgraphs](#subgraphs--a-flow-used-as-one-step):

| you do | the file on disk | the editor |
| --- | --- | --- |
| set `target_max` of `prep/scaled` to `2.0` | `target_max: 1.0` | `● demo.yaml` |
| **File ▸ Save** | `target_max: 2.0` | `demo.yaml` |
| undo back to the saved text | unchanged | the ● goes away |

**Run** traces what the editor holds, not the file: an unsaved value is the value the run uses. In a graph of several
streams it runs the one no other node draws its records from — a stream that only makes a source indexable for a
split or a range is a helper, not the run. Two streams that are both an end of the graph are refused by name:
`graph.yaml: several streams draw records (['first', 'second']) and no node draws from either — tracing one of
several is not supported`.

**Inspecting a step.** After a run, select a card or click the step's row in the run table: the run tab shows what the step was handed (**input**) and what it
handed on (**output**), an entry a line — its type, shape and range. An entry that is a picture (an image, a mask, a
2-D array of numbers such as a spectrogram) has **show** / **hide** at the end of its line. What the step made — an
output entry it changed or added — is shown at once, fitted to the panel; what it only passed on waits behind
**show**. An array of numbers is drawn grey from its lowest to its highest value, and the boxes the step's record
holds for a picture of that size are outlined on it. Click a picture for every pixel of it, in a dialog that scrolls
both ways; Escape closes it. After **rerun this node** the pictures of that step are asked again. A row opens any step the run traced, also one
the canvas draws no card for (a step inside a subgraph whose steps are a `!ref:`); such a step shows its entries and
pictures and has no rerun form. A row never changes the selection, which is where the next run pauses.

**Running the whole graph.** Run and Step send ONE record through the graph. A graph that holds a `runnable:` — a
conversion, a dataset build — is also run whole: **run all…** in the run tab, or **Run all…** in the bar, which opens
the run tab on the same plan.

1. The plan is shown first, and nothing runs yet: the checks a run makes, how many records it builds, the sink —
   what `recordstream run <graph>.yaml --dry_run` prints. A graph that declares scopes (`smoke: !scope:smoke`) has a
   tick box for each; ticking one shows the plan under it.
2. **Start run** runs every record as one job of the editor. Its count (`312 of 807 records`) and **Stop** show in
   the run tab and in a strip under the bar, in every tab.
3. At the end the run tab says how it ended and how long it took: the records run, the error in words, or where it
   was stopped.

**Stop** ends the run after the record in hand. The sink is closed as at the end of a run, so what was written is
kept. A full run reads the SAVED graph: with unsaved changes it is refused, and says to save first. One run of a
graph goes at a time. The run lives in the editor: stopping or restarting the editor ends it. In a host's panel a
full run is not offered. A source that cannot say how many records it holds runs without a count.

**What a run deletes is said before it starts.** Every `delete …` line of the plan — a sink that replaces its
folder says which, with the files and size it holds — is shown again in amber directly above **Start run**. The
lines are the runnable's own; a sink that does not report what it replaces shows none.

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

## New, Open and the file browser

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
red; changing the name or the folder clears it:

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

Above the path, a row of buttons jumps to a folder: **Graphs** (the folder the editor was started on), then `HOME`,
`PROJECT_ROOT`, `DATA_ROOT`, `MODEL_ROOT` and `RUN_DIR` — each only when the editor's environment sets it to a
folder; the button of the folder shown is highlighted, and hovering one shows its path. Every browser of the editor
has them: Open, Save As, New…'s **Browse…**, and the `…` of a folder or file field.

**New folder** (in Save As and the folder browsers) makes a folder inside the one shown and enters it — a place for a
run's outputs. It is made at once, without Save. Escape closes its name field and keeps the browser open.

| You type in New folder | The browser says |
| --- | --- |
| `outputs`, inside `/data` | enters `/data/outputs` |
| `outputs`, where `/data/outputs` exists | `/data/outputs already exists — pick another name` |
| `a/b` | `'a/b' is not a folder name — a name holds no '/'` |
| `.cache` | `'.cache' starts with a dot — the file browser hides it; pick another name` |
| nothing | `a new folder needs a name` |

## The Nodes sidebar

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
Double-clicking empty canvas opens the same catalogue as a search palette. It, too, looks for every word you type, in
any order, in the class's name and path: `class names` finds `ClassNamesOutput`, as `ClassNames` does.

**When the catalogue is read.** Building it means importing every installed package that declares classes (torch,
TensorFlow, JAX, the training and detection backends) and describing each class. The editor does that in a separate
helper process, started when the editor's page opens; the editor itself never imports those packages for it, so it
stays small and its other requests do not wait. The page asks for the catalogue the first time you open something
that lists it — the Nodes sidebar, Add Node ▸, the palette or Set source… — and never as it opens. Until it arrives
those four show `loading the catalogue…`; after that it is kept for as long as the page is open. With many large
packages installed the first list can take tens of seconds; a list asked for after the helper has answered comes at
once.

A class's **settings** are not in the list: they are asked for when you select the class — pick it in Set source…, or
rest the pointer on it in the Nodes sidebar — and kept for the page's life. A class whose package the editor has
already loaded answers at once; any other once the helper has described every class's settings; until then the dialog and the card say `loading the settings…`. A class only the editor's own
process registered (one a host app defined) is listed and described too.

## Pipelines — adding to and removing from an `ops:` list

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

## A class that is not an op — a card of its own

An op goes after a step or into an op list. Any other class — a value like `ClassNamesScan`, a source, an engine, a
sink — is a CARD of its own: right-click ▸ Add Node ▸, the double-click palette, a Nodes-sidebar entry dropped on the
canvas or the palette of a wire dropped on empty canvas write it as a NEW top-level key, ONE edit (one Ctrl+Z), and
draw the card where you asked — the right-click, the drop, the wire's release. On a files graph whose root has a
comment line above it, right-click ▸ Add Node ▸ ClassNamesScan — nothing selected, or the host card selected —
changes the text to:

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

and **Check graph** finds nothing.

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

## Wiring — a drawn wire is saved

Drag from an output to an input whose type meets it. The editor writes the wire into the document as ONE edit — held
until Save like every edit, one Ctrl+Z takes it out — and the canvas draws what the document now says: reopen the graph
and the wire is still there. An input that held something is replaced. A wire the editor cannot save yet is refused at
once with one line in a toast, and nothing is drawn or written.

Take a source graph whose source is written inside the Stream's slot, with the graph's root after it:

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
`hugging_face_source.class → contract.target`, and pressing Save writes:

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
  per input: `files · List[str]`; an item-typed input (`input · Image`) is an entry of the record the host
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

A files graph — the files a host hands it go into its source:

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

## Set source… — putting a source into a `Stream`

A `Stream`'s `source` is a SLOT that one object fills, not a list. A wire drawn into it brings a card that is already
on the canvas ([Wiring](#wiring--a-drawn-wire-is-saved)); to put a NEW object into it the canvas asks for a class and
its values instead. Three ways in, one dialog: the **set…** button on
the `source` row of the card, **Set source…** in the node's right-click menu, or a wire dragged out of the `source`
slot and dropped on empty canvas. The dialog lists the classes the slot takes (the sources first, then the engines —
a `Stream` is a source too), then the chosen class's form, with the [pickers](settings.md#pickers); **Set source** sends only the
values you changed from the class's defaults. Starting from this file:

```yaml
# read_files — a stream over the files of one folder
stream: !class:recordstream.core.stream.Stream
  source:   # nothing yet — Set source… fills it
  ops:
    - !class:recordstream.ops.debug.PrintRecordOp {}
```

choosing `FilesSource` and typing `/data/captures` into its `root` changes ONE line:

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

## Right-click menus

| on | items |
| --- | --- |
| empty canvas | Add Node ▸ (by kind, with a search) · Add Group · Paste · Insert template ▸ · Insert steps file… · Arrange left to right · Arrange diagonally · Arrange top to bottom |
| a node | Rename… · Delete · Copy · Group · Convert to subgraph · Save as template… · Run to here — on a `Stream` also Set source… ([above](#set-source--putting-a-source-into-a-stream)), and on a subgraph node also Open · Move to its own file… |
| a group's title bar | Rename… · Colour ▸ · Ungroup · Convert group to subgraph |

A right-click on a node that is not selected selects it first, so the node menu acts on what you see highlighted.
Run to here runs one record and pauses before the node. On the host card Rename… and Delete are greyed — it is the
host's side of the graph, not a node of the document.

## Where a node is drawn — the first layout

A graph opened without a positions file beside it (`<graph>.layout.yaml`) is laid out by the editor: one column per
step of the chain, left to right along the wires, the nodes of a column stacked top to bottom. A card starts below
the card above it — 200 px below that card's top when it is up to 183 px tall, further when it is taller: its
height plus 17 px, on the canvas's 10-px grid. 183 px is six rows under the title (one row per input, parameter and
output), or five on a subgraph card, whose bar of inner steps adds 26 px. A tall card never hides the card below it.

A node the positions file does not name yet — a step added through the API — is laid out the same way, starting below the
lowest card already placed. **View ▸ Arrange in columns** lays the whole graph out this way again. The other three
arranges space the cards by the sizes the page drew, along the wires:

- **Arrange left to right** — a column per step, the first on the left.
- **Arrange diagonally** — the same columns, each one starting 20 px below the tallest card of the column before it,
  so a chain runs from the top left to the bottom right.
- **Arrange top to bottom** — a row per step, the first at the top.

The sockets follow the drawing. When most of a level's wires run down — as *Arrange top to bottom* leaves them — every
card on that level draws its inputs along its top edge and its outputs along its bottom edge, in the order its rows
list them; on any other level they sit on the card's left and right. A parameter's small socket stays on its row. The
sockets turn when a drag is let go, with an undo, and when the graph is opened again, since they are read from where
the cards are.

Each is saved like a drag and undone with `Ctrl+Z`. Delete a line of the positions file to lay that node out again,
the file to lay out everything.

## Groups

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

## Subgraphs — a flow used as one step

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

## Using one subgraph twice — copy, templates, or include

Three ways, and only these. YAML anchors (`&`/`*`/`<<`) and `!ref:` are not reuse spellings — nothing in the
editor writes them (why: [architecture.md](architecture.md)).

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
use — that is what include is for: `target_max: 0.5` set on `prep/scaled` reads `0.5` on `prep2/scaled`
too, and both nodes are located at `prep.steps.yaml:4:7`. Use Copy or a template when the uses must differ.

## What it refuses, and how it says so

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
| *run* a graph with no source of its own, in the standalone editor (mounted in a host, the host's record runs — [Mounting the editor in a host app](hosting.md)) | `clip.yaml: this graph has no source to draw a record from — a host supplies the record (mount the editor in one) or wire a source` |
| *run* such a graph while the host has no record to give | the host's own words, as it raised them: `no record is selected in the page — open one there, then press Run` |
| put a class into a list slot (`stream.ops`) with *set source…* | `stream.ops is a list — add a member to it, or remove one; it is never set whole` |
| put a class whose output does not meet the slot into it (an op into `stream.source`) | `stream.source = !class:recordstream.ops.numpy.Threshold {} refused at …/source.yaml:15:3: cannot wire stream.source.op (Op) into stream.source (Source) at …/source.yaml:15:3: the types do not meet` — nothing changes |
| undo with nothing to undo | `wires.yaml: nothing to undo — the editor holds no earlier edit of this document` |

## What is not there yet

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
- **Unsaved work that ends without a stop.** A crash, a `kill -9` or a power cut loses it
  ([what a stop cannot keep](#unsaved-work-survives-a-stop)).
- **A subgraph held in another op's slot** (`RandomApply(op=Subgraph(…))`) is drawn and edited as a subgraph node
  (`maybe.op`), but the run tab shows no rows for its inside.
- **A quick first open of graphs that spell a class by its short name.** Over graphs spelling `_target_: Threshold`,
  the first graph list of an editor started on its own waits until the installed packages are imported one by one up
  to the one that defines the class — seconds when many large packages are installed
  ([how a short name is found](api.md#the-document-model-and-node-names)). An editor mounted in a host app that has
  already imported that package is not affected.
- **One short class name for two installed classes.** When two installed modules each register a class of one name
  (a NumPy `FourierOp` in `<package>.fourier` and a torch one in `<package>.fourier_torch`, say), a graph spelling
  `_target_: FourierOp` is drawn with the first of the two the lookup imports, while a run refuses the name
  (`AmbiguousClassError 'FourierOp' is registered by 2 classes, so a bare lookup cannot choose between them`). Spell
  such a class by its dotted path: `_target_: <package>.fourier.FourierOp`.

# Settings, pickers and plugin lists

Which parameters of a graph are its settings, where their values are kept, and what fills a field for you.

## Settings: a graph's `app:` fields

A graph may say which of its parameters are its SETTINGS — an `app:` block listing `[Class, param]` pairs.
`GET …/app` answers them, for the settings tab to open on that short form:

```yaml
app:
  inputs:
    - [Detector, level]
    - [Bandpass, band_hz__lo]   # the two halves of one range field …
    - [Bandpass, band_hz__hi]   # … are ONE field, band_hz
```

`GET …/form` lists every parameter of every node; `GET …/app` lists the two fields this block names (three entries,
the range spelled as its two halves).

A class may be named short (`Bandpass`) or dotted; a class used by two nodes gives a field on each. An
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

### A field another parameter does not ask for is grayed

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

## A graph's settings file — `<graph>.settings.yaml`

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
| a node with no name (a member of an `ops:` list) — by its CLASS, while the class is used by that node alone | `Scale.target_max: 2.0` (or the block `Scale: {target_max: 2.0}`) |
| a parameter that ONE node of the graph alone has — bare, confluid's own spelling (its broadcast reaches that one node) | `floor_db: -60.0` |

A bare name two nodes share is refused naming the full keys to write (`level is a parameter of 2 nodes (first,
third) — a bare line would set them all; write flow.first.op.level or flow.third.op.level`); a name that is none of
the graph's settings is refused, the message naming the parameters that may be written bare.

The editor shows each setting on its node (the canvas and the settings tab read the same model); the text tab shows the
graph itself. Undo takes a setting back like any edit. Save writes only the files that changed — the settings file
alone after a setting, the graph after a change to the graph. Save As takes the settings along
(`<new>.settings.yaml`, refused like the graph when a file is already there) and leaves the old settings file as it
was. A field the `app:` block does NOT name (ticked in with *all parameters*) is a change to the graph itself.

A host can put every setting back to the graph's own value at once with `Document.reset_settings()`: the settings
file is emptied as one edit (Undo brings the values back), and the Save that follows removes the file.

**Written by someone else.** While the editor holds nothing unsaved of a settings file, it reads the file again
whenever another program wrote it — even when the graph itself holds unsaved work, a moved node say — so the form,
a setting set and a reset all start from what the file holds. An unsaved change of the settings is never replaced:
its Save is refused instead, like any file changed on disk.

**Loading them together.** The two are ONE confluid document by confluid's own `include:` rule: the graph first, the
settings below it, so a setting wins over what the graph spells. A host runs it with `load_graph` — the settings file
may sit anywhere (a host keeps a shipped graph's in its own folder); nothing is written:

```python
from streamstudio.settings import load_graph, settings_path

built = load_graph(package / "view_pane.yaml", state / "view_pane.settings.yaml")   # None, or a missing file: the graph alone
built["flow"]["first"]["op"].level, built["flow"]["second"]["op"].level              # (0.25, 0.9)
settings_path(Path("state/view_scan.yaml"))                                           # state/view_scan.settings.yaml
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

### A graph the host ships

A host may SHIP graphs from outside its folder (`shipped()`, [Mounting the editor in a host app](hosting.md)).
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

### A graph that offers its settings only

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

### The settings and nothing else — `?only=settings`

A host that shows a graph's settings in a sheet of its own opens the page with `?only=settings` (beside `?embed=1`):
no menus, no canvas, no tabs and no *all parameters* box — the `app:` fields alone, at the panel's full width — and
every change is SAVED at once, so the host hears `saved` and re-runs what reads the graph. The save writes the
settings file ALONE (`POST …/save {only: "settings"}`): a change of the graph itself that the editor holds unsaved in
another view of the same graph stays unsaved there. Toasts and the restore question work as on the whole page.

```html
<iframe src="/studio/?graph=view_scan&embed=1&only=settings"></iframe>
```

A graph without an `app:` block says `This graph names no settings — it has no app: block.`

## Pickers

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
| a parameter its class stamps with a tag (`__streamstudio_widget_params__ = {"output_dir": "PLATFORM_DATASET_ID"}` on the class) | the stamp, whatever the rules above say |
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

## A plugin's lists beside a field

A class names a picker tag of its own for a parameter in the class attribute `__streamstudio_widget_params__`:

```python
class ShopSource:
    __streamstudio_widget_params__ = {"dataset": "SHOP_DATASET"}   # parameter -> tag

    def __init__(self, project: str = "", dataset: str = "") -> None: ...
```

A field stamped with a tag the editor does not know is drawn as a plain text field — unless an installed plugin supplies
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
field asks again. The page keeps a list's answer while it is open; the list's last entry, **↻ ask again**, asks the
plugin again — for what changed since (a model packaged after the list was first opened, a dataset added). Picking it
picks nothing, and the page never asks again unbidden. A list never fails a request: a tag no plugin lists, a function that raises and one that takes longer
than 20 s (`streamstudio.lookups.TIMEOUT_S`) answer no names and say why (`{"names": [], "reason": "ConnectionError:
…"}`). The plugins' lists are read once per process; a plugin that fails to import is skipped with a warning naming
it. The group is read from installed metadata — reinstall an editable plugin after adding the entry point.

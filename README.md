# StreamStudio

A graph editor for [RecordStream](https://github.com/Gearlux/recordstream) pipelines written as
[confluid](https://github.com/Gearlux/confluid) YAML documents.

The YAML file is the graph. StreamStudio draws it as a canvas of cards and wires, and every change you make is a patch
of the line it is about — comments, `$VAR` spellings and key order survive. Nothing is written until you **Save**. Run
a graph from the editor, step through it record by record, and re-run a node with other values. The editor runs on its
own over a folder of graphs, or inside a page of your own Flask app.

## Installation

```bash
pip install streamstudio
```

Python 3.12 or later. The editor's page comes built inside the package: no Node.js is needed to use it.

## Quick start

Put a graph in a folder, as `graphs/demo.yaml`:

```yaml
# One step that prints each record — a comment the edit leaves alone.
flow:
  probe:
    op: !class:recordstream.ops.debug.PrintRecordOp {label: before}
outputs: probe
```

and serve the folder:

```bash
streamstudio graphs          # then open http://127.0.0.1:8190
```

Pick `demo` in the graph list, type `after` into the `label` field of the `probe` card, and press `Ctrl+S`. The file on
disk differs in that one line. The same edit through the editor's HTTP API, without a browser — this is
[`examples/01_edit_a_graph.py`](examples/01_edit_a_graph.py):

```python
from streamstudio.app import create_app

client = create_app(folder).test_client()
client.post("/api/graphs/demo/param", json={"node": "probe", "param": "label", "value": "after"})
client.post("/api/graphs/demo/save", json={})
```

```diff
 # One step that prints each record — a comment the edit leaves alone.
 flow:
   probe:
-    op: !class:recordstream.ops.debug.PrintRecordOp {label: before}
+    op: !class:recordstream.ops.debug.PrintRecordOp {label: after}
 outputs: probe
```

Stop the editor with `Ctrl+C` or `streamstudio stop`. Work you have not saved is kept in a hidden file of the folder,
and the next start offers it back.

## What it does

- **Edits the document, never a copy of it.** An edit that would stop the document loading, or add a problem (a wire
  whose types do not meet), is refused with a message naming its `file:line:col`, and changes nothing.
- **Draws every installed class.** The Nodes sidebar lists every `@configurable` class the installed packages
  declare through the `confluid.configurables` entry-point group — install a package and its sources, ops and sinks
  appear. Nothing is registered by hand.
- **Saves what you draw.** A wire drawn between two cards is written into the YAML in the document's own spelling
  (`source: !ref:hugging_face_source`), or refused in one plain line.
- **Subgraphs and reuse.** A run of steps becomes one subgraph step; it can be copied, saved as a template, or moved to
  a steps file that several graphs include.
- **Settings apart from the graph.** A graph's `app:` block names its settings; their values go into
  `<graph>.settings.yaml` beside it, so the graph itself stays as it was shipped.
- **Runs what it shows.** The run panel traces the graph as the editor holds it, unsaved values included: pause before
  a node, step, inspect each node's inputs and outputs, re-run one node with other values.
- **Embeds in a host app.** A Flask app mounts the editor under a prefix of its own and hands it the record to run;
  the page talks to the host page through `postMessage`.

## The editor at a glance

| menu | items (on a Mac ⌘ for Ctrl) |
| --- | --- |
| **New** | Flow graph… · Pipeline… · Source graph (with its contract)… — each written to disk at once |
| **File** | Open… `Ctrl+O` · Save `Ctrl+S` · Save As… `Ctrl+Shift+S` · Revert |
| **Edit** | Undo `Ctrl+Z` · Redo `Ctrl+Shift+Z` · Cut · Copy · Paste · Delete · Select all · Rename… `F2` |
| **View** | Nodes sidebar `N` · Fit to view · Arrange · Arrange in columns · Minimap |
| **Graph** | Add group · Group selection `G` · Convert selection to subgraph · Insert template ▸ · Save selection as template… · Check graph |
| **Run** | Run `Ctrl+Enter` · Step · Rerun selected node |

A side panel holds three tabs: **text** (the YAML as the editor holds it), **settings** (a form of the nodes' parameters)
and **run**. The address follows what you look at, so a link opens the same graph, subgraph level and tab:
`/?graph=demo&level=prep&tab=settings`.

Three kinds of graph:

| kind | what it is |
| --- | --- |
| flow graph | `flow:` — named steps, each reading the record of the one before (or the one its `from:` names) |
| pipeline | `stream: !class:recordstream.core.stream.Stream` with an `ops:` list |
| source graph | a pipeline plus a `contract:` root — a `GraphContract` saying what the graph delivers |

## Mounting the editor in your own app

```python
from flask import Flask
from streamstudio.app import editor_blueprint, page_blueprint, studio_of

app = Flask(__name__)
app.register_blueprint(editor_blueprint(graph_dir), url_prefix="/studio")   # the API at /studio/api/…
app.register_blueprint(page_blueprint(), url_prefix="/studio")             # the page at /studio/
...
studio_of(app).stash()   # on your app's stop: unsaved work is offered at the next start
```

Instead of a folder, an app may give the editor a HOST — an object with a `name` and the methods `folder()`,
`record()`, `saved()` and `shipped()` — so that a Run of a graph without a source of its own runs the record the app
has selected, and the app hears every file a Save wrote. Show the page in an `<iframe>` with `?embed=1`, and switch
graphs with the page's `open` message. The whole protocol, with a host that runs as written:
[docs/hosting.md](docs/hosting.md).

## Plugins

A class can name a picker of its own for a parameter, and an installed package can supply the list behind it. The
class stamps the parameter with a tag:

```python
class ShopSource:
    __streamstudio_widget_params__ = {"dataset": "SHOP_DATASET"}   # parameter -> tag
```

and the package names a module in the `streamstudio.plugins` entry-point group that declares one function per tag:

```python
# my_plugin/studio/__init__.py
def shop_datasets(values):            # the node's current values in
    names = ask_the_shop(project=values.get("project", ""))
    return names, "" if names else "the shop has no datasets"   # the names, and why there are none

LOOKUPS = {"SHOP_DATASET": shop_datasets}
```

See [docs/settings.md](docs/settings.md#a-plugins-lists-beside-a-field).

## Command line

```bash
streamstudio path/to/graphs                    # serve that folder on port 8190
streamstudio                                   # the current directory
streamstudio path/to/graphs --port 8191 --templates-dir ~/my-templates
streamstudio stop                              # stop what listens on 8190 (its unsaved work is kept)
streamstudio restart                           # stop it, then serve the same folder again
streamstudio restart --help                    # every command's options, generated from its signature
```

`-p` is the short form of `--port`, and an option works on either side of the command name. A taken port is refused
before anything starts, with the address of what is there and the same command one port up. An option a command does
not have is refused by name, not ignored:

```
$ streamstudio restart --prt 9000
Error: streamstudio: unknown option 'prt' — it names no parameter of the 'restart' command and no key in the
configuration. Did you mean 'port'? Declared parameters: graph_dir, port, templates_dir.
```

Shell completion comes from [liquifai](https://github.com/Gearlux/liquifai)'s installer: `liquifai-install-completions`.

## Documentation

| page | what it covers |
| --- | --- |
| [docs/editor.md](docs/editor.md) | the editor, gesture by gesture: saving and recovery, the Nodes sidebar, op lists, cards, wiring, subgraphs, reuse, what it refuses and what is not there yet |
| [docs/settings.md](docs/settings.md) | a graph's `app:` fields, the settings file, graphs a host ships, pickers and plugin lists |
| [docs/hosting.md](docs/hosting.md) | mounting the editor in a host app: the host's methods, the page in an `<iframe>`, its messages |
| [docs/api.md](docs/api.md) | the document model, node names, every HTTP route, and which route each gesture calls |
| [docs/development.md](docs/development.md) | building the editor's page, and the tests |
| [CHANGELOG.md](CHANGELOG.md) | what each release changed |

## Dependencies

| package | used for |
| --- | --- |
| `recordstream` | the record engine a Run steps through, and the op-chain contract a graph is checked against |
| `confluid` | loading the YAML documents; each class's settings form (`to_pydantic`); the entry points the palette is built from |
| `flask` | the editor's server — a blueprint an app can mount |
| `pyyaml` | locating, and patching, the line an edit is about |
| `liquifai` | the command line |
| `loggair` | logging |
| `psutil` | `stop` and `restart` find the server by the port it holds |
| `datasets` | the HuggingFace pickers: a dataset's columns and splits |

## Development

```bash
git clone git@github.com:Gearlux/streamstudio.git
cd streamstudio
pip install -e ".[dev]"
pytest
```

Changing the editor's page needs Node.js; the page is rebuilt and committed with its source
([docs/development.md](docs/development.md)).

## License

MIT — see [LICENSE](LICENSE).

# Mounting the editor in a host app

A consuming Flask app mounts the API and the page under ONE prefix of its own — the page calls `api/…` relative to
its own address, so the two must sit side by side — and gives the editor a HOST: any object with a `name` and four
methods.

```python
from pathlib import Path
from typing import Mapping, Sequence

from streamstudio.app import HostRecord, editor_blueprint, page_blueprint, studio_of


class ViewerHost:
    name = "the viewer"                                        # how the page names it: "record: the viewer's selection"

    def __init__(self, viewer) -> None:                        # your app's own object
        self.viewer = viewer

    def folder(self) -> Path:                                  # asked at EACH request
        return self.viewer.graph_folder()

    def record(self, graph: str, path: Path) -> HostRecord:    # a Run of a graph with no source of its own
        record, (t0, t1), name = self.viewer.selection()       # raise ValueError("…") when there is none
        return HostRecord(record=record, fills=({"start_s": t0, "stop_s": t1},), label=f"{name} · {t0:.3f}–{t1:.3f} s")

    def saved(self, paths: Sequence[Path]) -> None:            # after every Save, Save As and New… that wrote one
        self.viewer.reload(paths)                              # spelled under folder() as folder() spells it

    def shipped(self) -> Mapping[str, Path]:                   # the graphs it ships from outside folder() — or {}
        return {"view_scan": Path(my_package.__file__).parent / "graphs" / "scan.yaml"}


app.register_blueprint(editor_blueprint(host=ViewerHost(viewer)), url_prefix="/studio")   # /studio/api/…
app.register_blueprint(page_blueprint(), url_prefix="/studio")                          # the page at /studio/
...
studio_of(app).stash()   # on the host's own stop: the unsaved work is offered at the next start
```

```
GET /studio            -> 308 /studio/            GET /studio/api/graphs    -> the host folder's graphs
GET /studio/           -> the built page          GET /api/workspace        -> the HOST's own route, untouched
GET /studio/assets/…   -> the page's assets       GET /studio/api/workspace -> {folder: <the host's folder>, …,
                                                                                 host: {name: "the viewer"}}
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

- **`name`** is how the page names the host (`record: the viewer's selection`); `GET /api/workspace` answers it
  as `host: {name}` — `null` for a standalone editor — which is also how the page knows Run asks the host.
- **`folder()`** is asked at each request. When it names another folder than before — an app whose first save
  MOVES its folder out of a temporary one, or whose save-as COPIES it — every graph of the old folder is the same
  file in the new one: the list follows, and a graph held unsaved keeps its edits and its undo history and saves
  into the new folder. A graph opened from elsewhere (File ▸ Open) stays where it is.
- **`record(graph, path)`** answers what a **Run** of a graph WITHOUT a source of its own runs — a `flow:` alone, a
  `Stream` whose `source` is empty. The run takes it through the text the editor holds, unsaved values included,
  on the same tracer as a graph's own record (step, pause before a node, rerun a node all work). Each `fills` group
  is set on every op holding ALL its attributes, on the graph's own level, before the run: the window above fills
  an op that has both `start_s` and `stop_s`, and an op with `start_s` alone gets nothing. A rerun keeps them — it rebuilds a node from its current values. The run answers
  `record: null` and `host: {label, filled: {node: {attribute: value}}}`. A host with no record raises
  `ValueError("no record is selected in the page — open one there, then press Run")`, and that sentence is the
  run's refusal. Anything else going wrong is a refusal in words naming the host, never a bare server error:

  ```
  record() raises IndexError  ->  400 …/view_x.yaml: the viewer could not give a record: IndexError: list index out of range
  record() answers a dict     ->  400 …/view_x.yaml: the viewer answered a dict, not a HostRecord
  HostRecord(record=None)     ->  400 …/view_x.yaml: the record the viewer gave is None, not a mapping of entries
  folder() raises             ->  400 the viewer could not name its folder: LookupError: …   (every request)
  ```

  A graph WITH a source draws its own record; the host is not asked. When that source yields NOTHING — a source the
  host fills itself when IT runs the graph (the files an app hands its files graph) — the run says so:
  `files.yaml: stream.source (FilesSource) yields no records, so there is no record #0 to run. The editor runs a source
  as the document spells it — what the viewer fills into it when the viewer runs this graph is not handed to the
  editor; spell what the source reads (a folder, files) to run it here`.
- **`saved(paths)`** is told, after every Save, Save As and New… (New writes its starter file at once), which graph
  files it wrote — the graph's own last: `[…/prep.steps.yaml, …/sub.yaml]`. A Save of positions alone writes only the
  layout sidecar and tells nothing; Save as template… writes no graph and tells nothing. A Save of a changed SETTING
  tells the settings file (`[…/view_scan.settings.yaml]`). If `saved` raises, the write stands: the answer is the graph
  plus `host_error: "RuntimeError: …"`, for the page to show.
- **`shipped()`** names the graphs the host ships from OUTSIDE its folder (a package's templates), by name: each is
  listed and opened while the folder holds no file of its name ([A graph the host ships](settings.md#a-graph-the-host-ships)).
  A relative `include:` in it names the file beside where it ships (another graph the host ships with it): the editor
  rewrites the entry to reach that file from the folder, so the graph loads there and a copy saved later keeps it.
  A host that ships nothing answers `{}`. If it raises, the request is a refusal in words naming the host, like
  `folder()`'s.
- **Every path the host is handed is spelled ONE way** — under `folder()` exactly as `folder()` spells it, for a file
  in that folder (else resolved): `record(graph, path)` and `saved(paths)` name `/var/…/T/viewer-x/view_x.yaml`
  for a folder given as `/var/…/T/viewer-x`, never its resolved `/private/var/…` twin. A host can match a path with
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
  ([Unsaved work survives a stop](editor.md#unsaved-work-survives-a-stop)); the next mount over that folder offers it, and
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

### The page in the host's panel

The host shows the page in an `<iframe>` of its own page, `?embed=1` added to the link: the menus and every function
stay; the streamstudio logo goes (the host's page says what app this is), the side panel is narrower and the minimap
is off until View ▸ Minimap. The page and the host then talk through `postMessage`, same origin only, and the page
listens to its own parent window only:

```html
<iframe id="studio" src="/studio/?graph=view_scan&tab=settings&embed=1"></iframe>
<script>
  // what the page posts — on every change of what it shows or holds, and after a Save wrote a file
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.source !== "streamstudio") return;
    // {source: "streamstudio", type: "state", graph: "view_scan", dirty: true, unsaved: ["view_scan"], tab: "settings",
    //  asking: false}   — `asking`: the page asks about the last session's unsaved work; no graph opens until answered
    // {source: "streamstudio", type: "saved", graph: "view_scan", path: "/data/app/graphs/view_scan.yaml"}
    // {source: "streamstudio", type: "unavailable"}   — from the page served INSTEAD of the editor
    // {source: "streamstudio", type: "committed", graph: "view_scan"}   — the answer to a `commit`
  });
  // what the host asks: show another graph (and tab) WITHOUT reloading — a reload asks "Leave site?" when unsaved
  studio.contentWindow.postMessage({target: "streamstudio", type: "open", graph: "view_fft", tab: "run"}, location.origin);
  // …or, about to READ what the page writes (a run button beside a settings panel): write the field being typed in
  studio.contentWindow.postMessage({target: "streamstudio", type: "commit"}, location.origin);
  // …or say what the host RUNS the graph with over its settings (values it applies by name and never writes)
  studio.contentWindow.postMessage({target: "streamstudio", type: "values", graph: "view_scan",
                                    values: {rule: "fixed"}}, location.origin);
  // …or show a line under a setting (what the host measured for it), never written
  studio.contentWindow.postMessage({target: "streamstudio", type: "hints", graph: "view_scan",
                                    hints: {level_db: "measured 14.6 dB"}}, location.origin);
  // …or hand the settings form the host page's font
  studio.contentWindow.postMessage({target: "streamstudio", type: "style", font_size_px: 13,
                                    font_family: "-apple-system, sans-serif"}, location.origin);
</script>
```

When the host stopped with unsaved work in the editor (`studio_of(app).stash()`), the page's next load asks the
person whether to restore it, and opens no graph — the one the link or an `open` names included — until that is
answered. Meanwhile every `state` says `asking: true`, so a host that covers its panel until the graph it asked for
shows must uncover it then, or the question is never seen.

A host that saves a SETTING itself does it through the editor — `studio_of(app).document(name).set_param(node, param,
value)`, then `studio_of(app).save(name, only="settings")`, the editor staying the settings file's one writer — and
posts `open` for the graph shown: the page shows the new value (an `open` of the graph shown re-presents it when its
text or its model changed).

`values` changes nothing written: the settings form grays a field by the host's value of the parameter it depends on
(below, [A field another parameter does not ask for is grayed](settings.md#a-field-another-parameter-does-not-ask-for-is-grayed))
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

In the panel a graph's top level opens WHOLE (a fit, never above zoom 1): a graph of 30 cards in a 900×700 panel shows
every card, where zoom 1 from the top-left would show one of them. The standalone page keeps its zoom-1 first view.

`editor_blueprint(graph_dir)` (the standalone editor's) and `editor_blueprint(host=…)` are the two ways; both, or
neither, is refused before anything is registered. `page_blueprint()` serves the page the package carries
(`web_dist()`, `streamstudio/canvas`), the placeholder when there is none. The editor's state lives on `app.extensions["streamstudio"]` (`studio_of`), so
two apps in one process are two editors.

`import streamstudio.app` is cheap for the host's start: about 0.2 s, and it imports no torch.

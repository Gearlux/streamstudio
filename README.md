# StreamStudio

Auto-generating [ComfyUI](https://github.com/comfyanonymous/ComfyUI) nodes from [RecordStream](https://github.com/Gearlux/recordstream) / Waivefront operations.

StreamStudio is the GUI layer of [Taidal](https://github.com/Gearlux). Rather than ship its own editor, it runs as a ComfyUI **custom node** package: every callable already exposed by RecordStream, Waivefront, or HuggingFace `datasets` is auto-wrapped into a ComfyUI node, so you compose and run pipelines visually on ComfyUI's canvas with no StreamStudio-specific ops to maintain.

## Installation

StreamStudio is on PyPI as a pre-release, so `pip` needs `--pre` to see it:

```bash
pip install --pre streamstudio
```

ComfyUI itself is **not** a Python dependency — the CLI clones it on first run (see below).

### Domain node sets are plugins, not requirements

StreamStudio generates its palette from whatever `@configurable` callables are installed. So a
domain package — a signal/RF library, a task-specific trainer, your own ops — needs no
registration here: install it, and its nodes appear. Nothing in StreamStudio imports one
unconditionally, and the two places that use one (the colormap list and the array→image
render) fall back cleanly when it is absent.

Install the domain package however you normally would — from PyPI, from a git checkout,
editable — and restart the editor. There is no extra to enable and nothing to configure
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
(and `-o` for `--output` on the export/import commands):

```bash
streamstudio restart -b                 # detached
streamstudio stop -p 8189
```

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

## Quick start

```bash
streamstudio
```

On first run this:

1. Clones ComfyUI into `~/.streamstudio/ComfyUI` when it is missing, and on EVERY launch syncs its `requirements.txt` into the active environment (pip is a no-op when satisfied — a checkout that ComfyUI-Manager updated in place otherwise imports a module the environment never got; `--install-` skips the sync).
2. Installs the [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) custom node, plus any custom nodes declared by installed packages or requested by you — see [Custom nodes](#custom-nodes-comfyui-manager--extras) below.
3. Symlinks the installed `streamstudio` package into `ComfyUI/custom_nodes/streamstudio`.
4. Launches ComfyUI (`python main.py --port 8188`) under the **same interpreter**, so ComfyUI's node discovery reads the entry-point metadata from your active `.venv`.

Reuse an existing ComfyUI checkout with `--comfy-dir`:

```bash
streamstudio --comfy-dir /path/to/ComfyUI
```

### The port

StreamStudio serves on `http://127.0.0.1:8188` (ComfyUI's own default). The launcher checks that
address **before** it does any work, so a studio that is already up is reported in a second rather
than after a full boot:

```
Port 8188 is already in use — StreamStudio may already be running there.

    Open it:  http://127.0.0.1:8188

If that is a different program, run StreamStudio elsewhere:  streamstudio --port 8189
```

The command exits non-zero and starts nothing. Use `--port` to serve somewhere else — to run a
second studio beside one that is already open, or when another program holds 8188:

```bash
streamstudio --port 8189
```

### Stopping and restarting

`stop` and `restart` find the studio by the port it holds, so they work whatever started it — this
launcher, an editor's launch button, or a `python main.py` you ran by hand:

```bash
streamstudio restart          # stop what is on 8188, then launch again
streamstudio stop             # just stop it
streamstudio restart --port 8189
```

`restart` re-walks the whole launch flow (requirements sync, custom nodes, symlink), so it is also
how you pick up newly installed nodes or a changed entry point. Stopping a port nothing is listening
on is not an error — `restart` simply starts.

### Running detached

Both `run` and `restart` hold the terminal by default, the way a server should. `--background` (or
`--bg`) hands it back:

```bash
streamstudio restart --bg
```

```
Stopped StreamStudio on port 8188 (pid 7819).
StreamStudio is starting in the background — log: ~/.streamstudio/logs/studio-8188.log

    Open it:  http://127.0.0.1:8188
```

The studio runs in its own session, so it survives the shell that started it, and its output goes to
a per-port log under `~/.streamstudio/logs/` — never the directory you launched from. The command
returns as soon as the process is spawned rather than waiting for the bind, so a boot that fails
shows up in that log; `streamstudio stop` is how you end it.

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
   STREAMSTUDIO_CUSTOM_NODES="https://github.com/rgthree/rgthree-comfy,https://github.com/crystian/ComfyUI-Crystools" streamstudio
   ```

3. **Repeatable `--custom-node` flag** — ad-hoc, per launch:

   ```bash
   streamstudio --custom-node https://github.com/rgthree/rgthree-comfy --custom-node https://github.com/crystian/ComfyUI-Crystools
   ```

Each repo is cloned into `custom_nodes/<repo-name>` and, if it ships a `requirements.txt`, its dependencies are pip-installed into the **active environment** (the same one ComfyUI runs in). The install is **idempotent**: a repo whose directory already exists is skipped, so updating or removing a node is ComfyUI-Manager's job, not the launcher's. A clone or dependency failure of one node — or a broken entry-point declaration — logs a warning and the launch continues; a broken custom node never blocks the studio. (`--manager-` filters the manager from the *declared* tier only: explicitly requesting its URL via env/flag still installs it.)

### Hiding ComfyUI's built-in nodes

By default the palette shows ComfyUI's built-in nodes (KSampler, Preview Image, …) **alongside** the Taidal nodes. To launch a Taidal-only palette, pass `--default-nodes-`:

```bash
streamstudio --default-nodes-
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
streamstudio --default-nodes- --config ./my-palette.yaml   # or point at any file
```

**Only Taidal nodes** (`--only-taidal-nodes`) goes further: it hides **every** non-Taidal node — ComfyUI's built-ins **and** other third-party custom packs (mtb, WWAA, …) — leaving only Taidal nodes + the keep-list. It implies `--default-nodes-`.

```bash
streamstudio --only-taidal-nodes                          # only Taidal + Preview Image/as-text
streamstudio --only-taidal-nodes --config ./my-palette.yaml
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
ToTensor  (normalize=true)         # optional op chain — stack any Taidal op node here
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

A `.json` path is a native workflow file: the `Taidal.OpenGraph` extension puts it on the
canvas via `/taidal/read_graph` (a missing file is fine — the first save creates it) and
**mirrors every native workflow save** (Ctrl-S / Save / Save As) into it via
`/taidal/write_graph` — the file, not a network push, is how the canvas reaches the
launcher, which stats it whenever it likes. A `.yaml` path is a Confluid config,
converted by `/taidal/import_yaml` as before. Either way the canvas is loaded only when
it does not already show that graph (re-opening never stacks duplicate workflow tabs),
without the param the editor keeps its own last session, and the param is applied once
and stripped, so reloading afterwards keeps what you build.

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

- **Export skips what YAML can't express — it never fails on it.** A canvas usually contains nodes with no Confluid form: native ComfyUI nodes (*Preview Image*, *Show Text*), the canvas-only Taidal adapters (*Walk Dataset*, *Extract from Record*, *Retag*, …), and occasionally a node whose config can't be serialized. **Export Graph → Taidal YAML** leaves those out and lists each one (with the reason) in a leading `#` comment block of the downloaded YAML — the document below stays a normal, loadable config of the remaining Taidal nodes. A wire from a skipped node into an exported config member is dropped (the member falls back to its default) and noted in the same block — **except** a wire from a native **primitive** node (`PrimitiveInt`/`PrimitiveFloat`/…), which inlines as its literal `value` widget. The canvas **wiring is preserved**: each run of record-chained ops folds into a `pipeline: !class:recordstream.core.stream.Stream` entry — `source: !ref:<source>` (traced through the *Walk Dataset* adapter back to the source node) plus the ordered `ops:` list, with fan-out compiled to `Save`/`Use` context cells — so the exported YAML reconstructs the same data flow (`confluid.load("config.yaml")["pipeline"]` is an iterable `Stream`). A chain broken by a skipped node continues as its own `pipeline_N` entry.
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
| anything else | `STRING` fallback |

`RECORDSTREAM_SOURCE` and `RECORDSTREAM_RECORD` are StreamStudio socket types that carry the live `Stream` / record (a plain `dict`) objects between nodes (ComfyUI connects two sockets only when their type strings match), which is what lets ops chain into a pipeline.

The directory picker calls the `/taidal/choose_directory` route registered in `streamstudio/__init__.py`, currently a native **macOS** folder dialog (`osascript`). Extend `TYPE_MAP` / `_map_type` to support new types — never special-case an individual node.

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

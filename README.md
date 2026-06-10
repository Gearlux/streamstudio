# FluxStudio

Auto-generating [ComfyUI](https://github.com/comfyanonymous/ComfyUI) nodes from [DataFlux](https://github.com/Gearlux/data-flux) / Waivefront operations.

FluxStudio is the GUI layer of [Taidal](https://github.com/Gearlux). Rather than ship its own editor, it runs as a ComfyUI **custom node** package: every callable already exposed by DataFlux, Waivefront, or HuggingFace `datasets` is auto-wrapped into a ComfyUI node, so you compose and run pipelines visually on ComfyUI's canvas with no FluxStudio-specific ops to maintain.

> **v0.2.0 pivot.** This release replaces the previous bespoke FastAPI server + React/`@xyflow` frontend with the ComfyUI-extension design below. The old `/api/*` server, `SteppingExecutor`, Confluid-YAML graph round-trip, and type→visualizer registry have been removed.

## Installation

```bash
pip install git+https://github.com/Gearlux/fluxstudio.git@main
```

Add the `waivefront` extra to expose RF-domain ops and visualizers (spectrograms, IQ time-domain plots):

```bash
pip install "fluxstudio[waivefront] @ git+https://github.com/Gearlux/fluxstudio.git@main"
```

ComfyUI itself is **not** a Python dependency — the CLI clones it on first run (see below).

## Quick start

```bash
fluxstudio
```

On first run this:

1. Clones ComfyUI into `~/.fluxstudio/ComfyUI` and installs its `requirements.txt` into the active environment.
2. Symlinks the installed `fluxstudio` package into `ComfyUI/custom_nodes/fluxstudio`.
3. Launches ComfyUI (`python main.py`) under the **same interpreter**, so ComfyUI's node discovery reads the entry-point metadata from your active `.venv`.

Reuse an existing ComfyUI checkout with `--comfy-dir`:

```bash
fluxstudio --comfy-dir /path/to/ComfyUI
```

Taidal nodes then appear in ComfyUI's *Add Node* menu grouped by what they ARE, not by which package ships them. A role-bearing class nests under `Taidal/<Role>[/<Task>]` (role-first) — e.g. `Taidal/Model/Classification`, `Taidal/Trainer/Detection`, `Taidal/Logger` (loggers are task-agnostic, so no task level). An op / source / engine nests under `Taidal/<Category>[/<group>]` — `Taidal/Op/numpy`, `Taidal/Source`. The **producing package rides the node label** as an uppercased tag — `[SONAIR] TimmModel`, `[DATAFLUX] RescaleOp` — so you can tell where a node comes from without burying it in a per-package folder.

Object sockets (model / loss / metric) are **role-qualified and task-agnostic**: a classification model and a segmentation model both carry `DATAFLUX_OBJECT:model`, so a model docks into *any* trainer's `model` slot — the role still stops you wiring a loss into a model slot. The **Retag** node re-stamps an object's role to force a connection when you need it.

A model node can come from a **builder function**, not just a class. Many libraries ship their models as factory functions — torchvision's detectors are `fasterrcnn_resnet50_fpn(...)`, `ssdlite320_mobilenet_v3_large(...)`, etc. These are registered (`raidar.configurables`, with `role="model"`), so each appears under `Taidal/Model/Detection` as an object node whose widgets (`num_classes`, `weights_backbone`, …) are read straight from the function's signature. Wire its **`class`** output into a detection **Trainer**'s `model` slot — because the node is lazy, the trainer injects the dataset-derived `num_classes` at run time, so you can leave that widget blank.

### Hiding ComfyUI's built-in nodes

By default the palette shows ComfyUI's built-in nodes (KSampler, Preview Image, …) **alongside** the Taidal nodes. To launch a Taidal-only palette, pass `--no-default-nodes`:

```bash
fluxstudio --no-default-nodes
```

This (a) prunes ComfyUI's built-in / `comfy_extras` / API nodes from the *Add Node* menu, leaving only the auto-discovered Taidal nodes (and any other third-party custom nodes), and (b) surfaces the Taidal nodes at the **palette root** — `DataFlux/Op/numpy`, `Waivefront/Op/targets`, `FluxStudio` — instead of nested under a `Taidal/` folder (there's nothing left to disambiguate them from). The default `--default-nodes` keeps everything visible and the `Taidal/` grouping.

> **Note:** hiding the defaults also removes ComfyUI's native viewers (*Preview Image*, text/number displays) that the **Extract from Sample** adapter normally feeds — so use the Taidal-only palette for building/exporting pipelines, and the default palette when you want to preview samples on the canvas.

Under the hood the flag exports `FLUXSTUDIO_HIDE_DEFAULT_NODES=1` to the launched ComfyUI process (it's a subprocess); if you launch ComfyUI directly (not via the `fluxstudio` CLI) you can set that env var yourself for the same effect.

### Empty starting canvas

FluxStudio starts on an **empty canvas** rather than ComfyUI's built-in Stable-Diffusion sample workflow (Checkpoint → CLIP → KSampler → VAE → Save Image), which has nothing to do with Taidal pipelines. The same empty canvas is loaded when you close the last workflow tab. This is a bundled ComfyUI web extension (`web/empty_default_graph.js`) — no flag needed; your own saved/restored workflows open normally.

## Building a pipeline

Discovered callables are classified by their DataFlux role so they wire together by **type**, not opaque strings:

- **Source** nodes (e.g. `HuggingFaceSource`, under `Taidal/DataFlux/Source`) construct a dataset and output a `DATAFLUX_SOURCE`.
- **Op** nodes (e.g. `RescaleOp`, `StandardizeOp`, `ConvertToImageOp`) take a `DATAFLUX_SAMPLE` input **plus** their constructor config as widgets, apply the op, and output a `DATAFLUX_SAMPLE` — so ops chain.
- **Generic** nodes (plain functions) keep scalar widgets with a `STRING` fallback.

Node labels are the **verbatim** callable name (underscores become spaces; capitals are preserved — `HuggingFaceSource` stays `HuggingFaceSource`, not `Huggingfacesource`). Internal helpers (names starting with `_`) are not surfaced. Hovering any widget shows a **tooltip** sourced from the wrapped callable's docstring `Args:` block (see *Constructor-parameter tooltips* below).

Two adapter nodes (under `Taidal/FluxStudio`) let you step through a dataset and view samples with **native** ComfyUI viewers:

- **Walk Dataset (by index)** — takes a `DATAFLUX_SOURCE` and an `index`, and outputs the `Sample` at that index (`source[index]`). The `index` widget uses ComfyUI's `control_after_generate`, **defaulting to *increment*** so each **Queue Prompt** advances to the next sample in order — set the control to *fixed* (or `index` `0`) to reset, or *randomize* to sample at random. `wrap` cycles modulo the dataset length.
- **Extract from Sample** — takes a `DATAFLUX_SAMPLE` and a `field` selector (`input` / `target`) and unpacks the selected value onto **typed** sockets — `IMAGE`, `MASK`, `STRING`, `INT`, `FLOAT` — so you wire the one your viewer expects. ComfyUI types sockets statically, so instead of retyping one socket per run it surfaces every plausible type; *which* one is meaningful is derived from the sample's features (`Sample.describe()`). `IMAGE` is rendered via `waivefront.visualizers.value_to_image` (PIL / tensor / 2-D map → image; a non-visual value renders as a small text image); the `colormap` widget is a **dropdown** of the supported colormaps (the closed `waivefront.visualizers.Colormap` set — `viridis`, `magma`, `gray`, …) applied to 2-D maps, and `max_size` caps the longest side of the rendered image in pixels (default `512`, larger renders are downscaled, aspect ratio preserved). `MASK` carries a 2-D field (segmentation targets); `STRING` is a label/scalar (with `label_name` when the target has it) or a compact type summary; `INT` / `FLOAT` carry a scalar (e.g. a class id). Drop two of these next to each other (one set to `input`, one to `target`) to view both at once.

This replaces the old all-in-one "Show Sample" output node: extraction lives here, **previewing lives in ComfyUI's own nodes** — wire `IMAGE` into *Preview Image*, `STRING` into a text node, etc.

A minimal "walk an image dataset" graph:

```
HuggingFaceSource  (path=ylecun/mnist, split=train, input_feature=image, target_feature=label)
        │  DATAFLUX_SOURCE          # `path` is a free-text dataset-id field (+ cached-dataset picker), not a folder
        ▼
Walk Dataset (by index)          # Queue Prompt repeatedly to step through samples
        │  DATAFLUX_SAMPLE
        ▼
RescaleOp  (in_min=0, in_max=255)  # optional op chain — stack any Taidal op node here
        │  DATAFLUX_SAMPLE
        ▼
Extract from Sample (field=input)
        │  IMAGE                    # also: MASK / STRING / INT / FLOAT
        ▼
Preview Image                    # ← native ComfyUI viewer
```

`Extract from Sample` needs the `[waivefront]` extra to render the `IMAGE` socket (the renderer lives in waivefront); the `STRING` / `INT` / `FLOAT` outputs work without it.

### Running a trainer / evaluator / processor

A trainer / evaluator / processor node **runs itself when you Queue the graph** — it IS a terminal output node, so there is no separate "Run" node to wire. **Queue Prompt** executes the (potentially long) `.run()`. The node honours ComfyUI's **interrupt button**: it runs the work on a background thread and, when you press interrupt, injects a `KeyboardInterrupt` so the run stops cleanly at the next batch / sample boundary (PyTorch Lightning shuts down gracefully). A single long-running C kernel can't be interrupted mid-call, so cancellation lands between steps, not instantly. (To build/inspect/export a graph without training, just don't Queue it — use the **Export Graph → Taidal YAML** menu command.)

The node shows the runner's output **live** in a collapsible widget on itself: training progress, metrics, and an in-place-updating **tqdm / Lightning progress bar** stream in as the run proceeds (and still mirror to the console). Use the `▾ output` toggle to fold the log away; the captured text is runtime output and is never saved into the workflow.

**Outputs are wireable sockets.** Every confluid `@output` a runnable declares is a typed output socket on the node, filled with the run's LIVE value. For a marainer trainer / evaluator (and the sonair / deltaid / raidar specialisations) that's `computed_metrics` (the run's final scalar metrics, e.g. `test/accuracy`, `val/loss`) and `label_names` (the resolved class names) — both `STRING` sockets carrying JSON, so wire them into ComfyUI's native **Show Text** node to see them — plus `trained_model` (a `DATAFLUX_OBJECT:model` you wire straight into an evaluator node's `model` slot to chain train → eval in one graph). Add a new value `@output` upstream and it becomes a socket automatically — no FluxStudio change needed.

**Attaching loggers / callbacks.** An object node's constructor params that accept an *object* (rather than a typed/string value) render as wired `DATAFLUX_OBJECT` input sockets — so to log a run, drop a logger node (e.g. `TensorBoardLogger`, `CSVLogger`) and wire its **`instance`** output into the **Lightning Trainer** node. A param that accepts a *list* of objects (the Trainer's `logger` / `callbacks`) grows **dynamic `logger_0`, `logger_1`, …** sockets as you wire — so you can attach several loggers (or callbacks) at once, just like the `op_N` / `source_N` list inputs. String-settable knobs like `accelerator` (`"gpu"`) and `precision` stay editable widgets; only genuinely object-typed params become sockets.

## Composing ops (higher-order ops)

Every op node is **dual-mode**. Besides the `sample` input (`DATAFLUX_SAMPLE`, now *optional*) and the applied `sample` output, it has a second **`op`** output (`DATAFLUX_OP`) carrying the *constructed op itself*. So you can either chain ops by `sample` (the linear pipeline above) **or** take the `op` output and feed a **higher-order op** — one whose constructor takes a list of ops (`Parallel`, `Tee`, …).

Those higher-order nodes don't show a text widget for their op-list; they grow dynamic `op_0`, `op_1`, … `DATAFLUX_OP` inputs (a fresh empty slot appears whenever the last fills, via `web/op_list.js`). Wire each op node's `op` output into one:

```
RescaleOp      ──op──┐
StandardizeOp  ──op──┤  (op_0, op_1)
                     ▼
                  Parallel  ─sample→ Extract → Preview
Walk ──────────sample┘
```

`Parallel(ops=[RescaleOp, StandardizeOp])` applies both to the incoming sample. Leave a higher-order op's `sample` unconnected and take *its* `op` output to nest it inside another higher-order op.

> **Scope.** Works for any op whose constructor takes an `ops`/`branches` list. `Tee` (a list of *branches*) v1 treats each connected op as its own single-op branch — multi-op branches mean pre-composing a branch into one op (future). `FilterOp`/`WrappedOp` wrap a *raw Python callable*, so they aren't canvas nodes at all (bare `@configurable`, i.e. UNcategorised — neither `op` nor `engine`).

## Exporting ops to a Flux

The **op** half of a canvas graph (everything between the source and the viewer) can be exported back to a Confluid YAML op-list and reattached to *any* source in code — the inverse of how the canvas builds a pipeline. The source and the Walk/Extract adapter nodes are dropped; only the `DATAFLUX_SAMPLE → DATAFLUX_SAMPLE` op chain is emitted.

Two equivalent entry points (both call the same compiler, `fluxstudio.export.export_ops_yamls`):

- **On the canvas:** the **Taidal → "Export Taidal Ops → Confluid YAML"** menu command (`web/export_ops.js`) POSTs the current graph to the `/taidal/export_ops` route and downloads `ops.yaml` — or, when the canvas holds several disconnected op chains, **one file per chain** (`ops_1.yaml` … `ops_N.yaml`; the browser may ask once to allow multiple downloads).
- **Offline CLI:** export a saved **API-format** graph (ComfyUI dev-mode *Save (API Format)*):

  ```bash
  fluxstudio export graph.json -o ops.yaml      # omit -o to write YAML to stdout
  ```

  This works without a running ComfyUI — importing `fluxstudio` populates the node registry via entry-point discovery. With several disconnected chains, `-o ops.yaml` writes `ops_1.yaml` … `ops_N.yaml` (one file per chain); without `-o` the chains stream to stdout as `---`-separated YAML documents.

The result is a `{ops: [...]}` document of `!class:…()` markers, e.g.:

```yaml
ops:
- !class:dataflux.ops.torch.RescaleOp()
  in_min: 0.0
  in_max: 255.0
- !class:dataflux.ops.torch.RescaleOp()
  in_min: 0.0
  in_max: 1.0
  out_max: 10.0
```

Reattach it to your own source:

```python
from dataflux import Flux
from dataflux.sources import HuggingFaceSource

flux = Flux.from_ops_yaml("ops.yaml", source=HuggingFaceSource(path="ylecun/mnist"))
# or, equivalently, the manual form:
#   import confluid
#   ops = confluid.materialize(confluid.load("ops.yaml")["ops"])
#   flux = Flux(source=my_source, ops=ops)
```

> `Flux.from_ops_yaml` **materializes** the markers before attaching them: `confluid.load` leaves `!class:` markers nested under a mapping key deferred, and `Flux` refuses to iterate deferred markers by design — so a plain `load(...)["ops"]` is not enough. Use the helper (or `confluid.materialize`).

**Scope.** v1 exports linear op chains, plus **fan-out** (one op output wired to ≥2 op nodes) compiled to a `dataflux.ops.tee.Tee`. A graph holding **several disconnected chains** exports each chain individually — one complete, individually-loadable `{ops: [...]}` document per chain, in deterministic head-node order. The compiler is a faithful translator — it does **not** auto-insert `Copy`/`Stash` for branch isolation; place those op nodes explicitly. True multi-input merges (fan-in), cycles, and widget inputs driven by a link raise an `UnsupportedTopologyError` (HTTP 400 from the route) with an actionable message. (The strict single-chain API forms `compile_ops` / `export_ops_yaml` still reject a multi-chain graph — use `compile_op_chains` / `export_ops_yamls` for the per-chain behaviour.)

## Importing & exporting whole pipelines (Taidal YAML ↔ canvas)

Beyond the ops-only export above, FluxStudio round-trips a **whole** Confluid config — sources, ops, object members (model / loss / optimizer / loaders / logger / Lightning Trainer), and the runnable that ties them together — between a Confluid YAML and a full ComfyUI `workflow.json`. So you can load an existing training config (e.g. [`sonair/examples/train_efficientvit_b0_mnist.yaml`](../sonair/examples/train_efficientvit_b0_mnist.yaml)) onto the canvas, edit it visually, and save it back to YAML that runs with `marainer train -c config.yaml`.

The converter lives in `fluxstudio.graphio` (`yaml_to_workflow` / `workflow_to_yaml`); the **Fluid graph is the shared intermediate representation** (confluid owns YAML↔Fluid via `resolve`/`dump`, FluxStudio owns Fluid↔`workflow.json`). It works **without a running ComfyUI**.

### On the canvas — the **Taidal** menu

| Command | What it does |
|---|---|
| **Import Taidal YAML → Graph** | Pick a `.yaml`; its nodes + wiring are laid out on the active canvas. |
| **Import Taidal YAML → Subgraph** | Same, but packaged as a reusable `definitions.subgraphs[]` blueprint + an instance node. |
| **Import Taidal YAML → Group** | Same as Graph, wrapped in one visual group box (titled by the file). |
| **Export Graph → Taidal YAML** | Serialize the whole active graph and download the hoisted-`!ref:` YAML. |
| **Export Taidal Ops → Confluid YAML** | (above) the ops-only op-list. |

The import commands operate on the **active** graph, so they work inside a subgraph too. Backed by the `/taidal/import_yaml` and `/taidal/export_workflow` routes (`web/import_yaml.js`, `web/export_workflow.js`).

### Offline CLI

```bash
fluxstudio import config.yaml -o workflow.json          # YAML → canvas workflow
fluxstudio import config.yaml --as subgraph -o sub.json # … as a reusable subgraph blueprint
fluxstudio export-workflow workflow.json -o config.yaml # whole canvas → hoisted-!ref: YAML
```

### The exported shape (hoisted `!ref:`)

Every node becomes a **top-level key** named by the socket it feeds (path-prefixed on collision); a wire becomes a `!ref:`; shared scalar dimensions (`num_classes` / `image_size`) are hoisted. A deferred member (optimizer / loaders / Lightning Trainer) emits `!lazy:`, an eager one `!class:`. This matches the navigaitor / hand-written reference configs (e.g. [`waivefront-rfuav/config/train_efficientvit_b0.yaml`](../waivefront-rfuav/config/train_efficientvit_b0.yaml)):

```yaml
num_classes: 37
model: !class:sonair.timm_classifier.TimmModel
  model_name: efficientvit_b0
  num_classes: !ref:num_classes
optimizer: !lazy:torch.optim.AdamW
  lr: 0.0001
trainer: !class:marainer.trainer.LightningTrainer
  model: !ref:model
  optimizer: !ref:optimizer
```

### How import works

`yaml_to_workflow` broadcast-resolves the config to a Fluid **marker** graph with `confluid.resolve` (no instantiation — fast, side-effect-free) and walks it. Pass `--instantiate` (CLI) / `instantiate=True` (API) to instead walk *live-but-inert* objects from `confluid.load(..., solidify=False)` — same result, with the expensive `solidify()` (e.g. building a model backbone) suppressed.

### v1 scope & limitations

- A config member with **no FluxStudio node** (a class not tagged op/source/engine/member — e.g. a torchmetrics `MetricCollection`) is **dropped from the canvas with a warning** on import; the runnable falls back to its default. To surface such a class, tag/register it (loggers and the base `LightningTrainer`/`LightningEvaluator` are registered in `marainer.configurables`).
- Object **nodes** grow dynamic `logger_N` / `callbacks_N` sockets for list-valued object params (see *Attaching loggers / callbacks* above), and the whole-graph graphio round-trip respects them: **import** wires a `List`-valued member's items into `logger_0`, `logger_1`, … and **export** collapses those sockets back into one `logger:` list (the importer/exporter read the node's own `<name>_0` seed to decide).
- **Subgraphs** are self-contained in the `workflow.json` (`definitions.subgraphs`); on export they're **inlined** back into the flat YAML. Standalone reusable `include:` files are a follow-up.
- Round-trip is **semantic** (topology + kwargs + kinds), not byte-exact: node positions are re-derived by a deterministic layered layout (sources left → runnable right), and only non-default widget values are emitted.

## How nodes are generated

FluxStudio never hand-defines a node. At ComfyUI startup, `_discover_and_register_nodes()` (`fluxstudio/__init__.py`):

1. Enumerates the `confluid.configurables` entry-point group via `importlib.metadata`.
2. For each module, `dataflux.discovery.scan_module` finds the compatible callables.
3. `register_module_nodes` / `create_comfy_node` (`fluxstudio/bridge.py`) classify each callable (Source / Op / Generic — see *Building a pipeline*) and wrap it in a ComfyUI node class, keyed by `_node_key` as `Taidal_<package>_<category>[_<group>]_<callable>` (globally unique, so a same-named class in two modules — e.g. numpy's vs torch's `RescaleOp` — doesn't clobber its twin) and filed under a `Taidal/<Package>/<Category>[/<group>]` palette path (built by `_node_category`: top-level package via `_PACKAGE_DISPLAY_NAMES` for canonical casing, then the `@configurable` category, then the optional `group` tag). The displayed name stays the verbatim class name.

Registration is a **positive allowlist**: a class becomes a node only if its `@configurable` category is one of `{op, source, engine}`. So the palette is exactly the deliberately-tagged ops, sources, and engines. The generic dataflux **engines** (`Flux` / `JointFlux`, `category="engine"`) and the source-view `DatasetSplit` (`category="source"`) **are** buildable on the canvas: their source-typed constructor params render as wired sockets — a single `source` → a `DATAFLUX_SOURCE` input, a source-list (`fluxes`) → dynamic `source_N` inputs, an op-list (`ops`) → dynamic `op_N` `DATAFLUX_OP` inputs — so a Source/Op node (which already outputs *itself* as `DATAFLUX_SOURCE` / `DATAFLUX_OP`) wires straight into them. Excluded *by construction*: the **raw-callable op wrappers** (`FilterOp` / `WrappedOp`) — UNcategorised (bare `@configurable`), since they take a raw Python callable nothing a widget or socket supplies (so they stay out even with `engine` allowed); **task datasets** (`*_dataset`, e.g. `classification_dataset`) — a trainer-config concept navigaitor cares about, but a canvas only needs a `source` to yield Samples; uncategorised module-level helper functions; and raw sinks. You can build a dataset by wiring `Source → Op → …` *or* by feeding a `Source`/`Op` into a `Flux` / `DatasetSplit` engine node. (To surface another role, add its category to `_NODE_CATEGORIES` in `bridge.py` and tag the classes. Note: ops-export still targets linear op chains (several disconnected chains export fine — one YAML per chain) — engine-node graphs aren't exportable yet.)

A module that fails to import or scan is skipped at `debug` level, so one package's missing optional dependency never empties the palette.

> **Entry points live in installed metadata.** Discovery reads `importlib.metadata`, so editable installs *freeze* the entry-point table at install time. After adding or changing a `[project.entry-points."confluid.configurables"]` declaration in any workspace package, reinstall that editable (e.g. `aisland setup`) or its nodes silently vanish from the palette. Do **not** use `--reinstall` / `--force-reinstall` — that re-resolves the internal deps (confluid, logflow, …) from PyPI, where they don't exist.

## Type mapping

Node sockets and widgets are derived from each callable's signature by `_map_type` (`fluxstudio/bridge.py`):

| Python type | ComfyUI mapping |
|-------------|-----------------|
| `int` / `float` / `str` / `bool` (incl. `Optional[...]` / `Union[...]` of these) | scalar widgets (`INT` / `FLOAT` / `STRING` / `BOOLEAN`) |
| `dataflux.sample.Sample` (incl. `Iterator[Sample]`) | `DATAFLUX_SAMPLE` socket |
| a `Sample`-yielding source class | `DATAFLUX_SOURCE` output socket |
| `datasets.Dataset` | `HF_DATASET` socket |
| the dataset-id param of a `HuggingFace*` source (`repo_id` / `path`) | free-text `STRING` + optional cached-dataset picker (`web/hf_dataset_chooser.js` → `/taidal/hf_datasets`), tagged `taidal_widget="HF_DATASET_ID"` |
| a feature-selector param of a `HuggingFace*` source (`input_feature` / `target_feature`) | free-text `STRING` + optional column picker (`web/hf_feature_chooser.js` → `/taidal/hf_dataset_features`), tagged `taidal_widget="HF_FEATURE"` |
| the `split` param of a `HuggingFace*` source | free-text `STRING` + optional split picker (`web/hf_split_chooser.js` → `/taidal/hf_dataset_splits`), tagged `taidal_widget="HF_SPLIT"` |
| the `metadata_features` list param of a `HuggingFace*` source | comma-separated `STRING` + multi-select "add column" picker incl. `*` (`web/hf_metadata_chooser.js` → `/taidal/hf_dataset_features`), tagged `taidal_widget="HF_METADATA"` |
| `pathlib.Path`, or a param named `*path*` / `*dir*` / `*root*` / `*folder*` | directory-picker `STRING` (`web/directory_chooser.js` → `/taidal/choose_directory`) |
| anything else | `STRING` fallback |

`DATAFLUX_SOURCE` and `DATAFLUX_SAMPLE` are FluxStudio socket types that carry the live `Flux` / `Sample` objects between nodes (ComfyUI connects two sockets only when their type strings match), which is what lets ops chain into a pipeline.

The directory picker calls the `/taidal/choose_directory` route registered in `fluxstudio/__init__.py`, currently a native **macOS** folder dialog (`osascript`). Extend `TYPE_MAP` / `_map_type` to support new types — never special-case an individual node.

### HuggingFace dataset-id field

A HuggingFace dataset source's identifier is a **name**, not a folder — `dataflux.sources.HuggingFaceSource` happens to call it `path` (mirroring `datasets.load_dataset`), and sonair's `HuggingFaceClassificationDataset` calls it `repo_id`. Either way FluxStudio renders it as a free-text `STRING` (so you can type any Hub repo id like `kitofrank/RFUAV`, or a local imagefolder path), **not** the directory picker. `web/hf_dataset_chooser.js` adds an optional “📚 cached dataset” combo populated from the `/taidal/hf_datasets` route (`huggingface_hub.scan_cache_dir()`, dataset repos only) — selecting one fills the text field, which stays editable. The field is detected by the `HuggingFace*` class-name convention, so any future `HuggingFace*` source inherits it automatically.

### HuggingFace feature-selector fields

`HuggingFaceSource`'s `input_feature` / `target_feature` name which dataset *column* maps onto `Sample.input` / `.target` (defaults `image` / `label`). They render as free-text `STRING`s — but typing column names blind is error-prone, so `web/hf_feature_chooser.js` adds an optional “🏷️ columns” combo next to each, populated from the **selected** dataset's actual columns. The columns come from the `/taidal/hf_dataset_features` route (`datasets.load_dataset_builder().info.features`, falling back to streaming one example), keyed off whatever is currently in the node's dataset-id field — so it's the cached-dataset picker one level down: there you pick *which dataset*, here *which of its columns*. The combo refreshes whenever the dataset id / `name` / `split` changes (including when you pick a cached dataset), and selecting a column fills the editable text field. Detected by the same `HuggingFace*` class-name convention, so any future `HuggingFace*` source inherits it.

### HuggingFace split field

`HuggingFaceSource`'s `split` stays a free-text `STRING` — HF accepts slice syntax like `train[:90%]` or `train+test`, which a closed dropdown couldn't express — but `web/hf_split_chooser.js` adds an optional “🪓 splits” combo populated from the **selected** dataset's actual splits via the `/taidal/hf_dataset_splits` route (`datasets.get_dataset_split_names`, metadata-only). Like the column picker it keys off the current dataset-id field and refreshes when the dataset id / `name` changes; selecting a split fills the editable field. This is distinct from `DatasetSplit.split`, which is a closed `Literal["train","val","test"]` fraction-view selector and renders as a plain combo (via `_literal_options`), not this picker.

### HuggingFace metadata-columns field (multi-select)

`HuggingFaceSource.metadata_features` is a **list** — which columns ride along on `Sample.metadata`. It renders as a comma-separated `STRING` (which `_coerce_kwargs` splits back into a list), and `web/hf_metadata_chooser.js` adds a “🗂️ add column” combo. ComfyUI has no native multi-select, so the combo **appends** the picked column each time (pick repeatedly to build the list); the field stays editable. The options are the **selected** dataset's columns (reusing the `/taidal/hf_dataset_features` route) **minus** whatever is currently mapped as `input_feature` / `target_feature`, plus a special **`*`** entry meaning *every column except input/target* (the `dataflux.sources.METADATA_ALL_FEATURES` sentinel — resolved against the real dataset at load; left empty ⇒ no extra metadata). Picking `*` is exclusive (it already means "the rest"). The list refreshes when the dataset id / `name` / `split` **or** the input/target feature changes.

### Constructor-parameter tooltips

Each widget's hover tooltip is sourced from the wrapped callable's docstring `Args:` block (parsed by `confluid.parse_param_docs`) — the **same** per-parameter help [navigaitor](https://github.com/Gearlux/navigaitor) surfaces as JSON-Schema `description`s in its visual editor. Document a constructor parameter once in the upstream class's `Args:` and it shows up in both GUIs. A parameter with no `Args:` entry simply has no tooltip; the fix is to document it in dataflux / waivefront, never to hand-write a tooltip in FluxStudio.

## Connection filtering

Every `Sample` wire keeps the single `DATAFLUX_SAMPLE` socket string (so any op *can* be wired and the pipeline still chains). The richer DataFlux type contract — declared as `ACCEPTS` / `PRODUCES` on each op/source in dataflux/waivefront (see *DataFlux → Type Specs*) — is layered on top to filter incompatible connections, two ways:

- **Runtime (authoritative):** a generated op node's `execute()` checks the actual incoming sample against the op's `ACCEPTS` and raises a clear `expects … but got …` error on a mismatch.
- **Canvas (advisory):** `web/connection_validator.js` fetches each node's spec from the `/taidal/node_specs` route and rejects an incompatible Sample wire live in the editor (via litegraph's `onConnectInput`). It is a faithful port of `dataflux.typespec.compatible()` — permissive, so un-annotated ops (spec `None`) connect freely. Drag-drop only; graph-load wiring falls back to the runtime check.

Specs come exclusively from dataflux/waivefront — to fix or add a contract, edit the op's `ACCEPTS`/`PRODUCES` there, not here.

## Dependencies

| Package | Purpose |
|---------|---------|
| `dataflux` | Op discovery (`scan_module`) + pipeline engine. |
| `confluid` | `confluid.configurables` entry-point group that drives discovery. |
| `datasets` | HuggingFace `Dataset` ↔ `HF_DATASET` socket. |
| `liquifai` | App / CLI framework. |
| `logflow` | Logging. |
| `waivefront` *(optional `[waivefront]` extra)* | RF-domain ops + visualizers (spectrogram, IQ time/freq). |

## Development

```bash
git clone git@github.com:Gearlux/fluxstudio.git
cd fluxstudio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

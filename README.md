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

Taidal nodes then appear in ComfyUI's *Add Node* menu under a `Taidal/<Package>/<Category>[/<group>]` palette path — e.g. `Taidal/DataFlux/Op/numpy`, `Taidal/Waivefront/Op/targets`, `Taidal/DataFlux/Source` — named for the callable they wrap. The first level is the source package (canonical casing: `DataFlux`, `Waivefront`, `FluxStudio`), then the `@configurable` category (`Op` / `Source`), then the optional path-like `group` tag (an op with no `group` sits directly under `…/Op`).

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

Two equivalent entry points (both call the same compiler, `fluxstudio.export.export_ops_yaml`):

- **On the canvas:** the **Taidal → "Export Taidal Ops → Confluid YAML"** menu command (`web/export_ops.js`) POSTs the current graph to the `/taidal/export_ops` route and downloads `ops.yaml`.
- **Offline CLI:** export a saved **API-format** graph (ComfyUI dev-mode *Save (API Format)*):

  ```bash
  fluxstudio export graph.json -o ops.yaml      # omit -o to write YAML to stdout
  ```

  This works without a running ComfyUI — importing `fluxstudio` populates the node registry via entry-point discovery.

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

**Scope.** v1 exports a single linear op chain, plus **fan-out** (one op output wired to ≥2 op nodes) compiled to a `dataflux.ops.tee.Tee`. The compiler is a faithful translator — it does **not** auto-insert `Copy`/`Stash` for branch isolation; place those op nodes explicitly. True multi-input merges (fan-in), multiple disconnected chains, and widget inputs driven by a link raise an `UnsupportedTopologyError` (HTTP 400 from the route) with an actionable message.

## How nodes are generated

FluxStudio never hand-defines a node. At ComfyUI startup, `_discover_and_register_nodes()` (`fluxstudio/__init__.py`):

1. Enumerates the `confluid.configurables` entry-point group via `importlib.metadata`.
2. For each module, `dataflux.discovery.scan_module` finds the compatible callables.
3. `register_module_nodes` / `create_comfy_node` (`fluxstudio/bridge.py`) classify each callable (Source / Op / Generic — see *Building a pipeline*) and wrap it in a ComfyUI node class, keyed by `_node_key` as `Taidal_<package>_<category>[_<group>]_<callable>` (globally unique, so a same-named class in two modules — e.g. numpy's vs torch's `RescaleOp` — doesn't clobber its twin) and filed under a `Taidal/<Package>/<Category>[/<group>]` palette path (built by `_node_category`: top-level package via `_PACKAGE_DISPLAY_NAMES` for canonical casing, then the `@configurable` category, then the optional `group` tag). The displayed name stays the verbatim class name.

Registration is a **positive allowlist**: a class becomes a node only if its `@configurable` category is one of `{op, source, engine}`. So the palette is exactly the deliberately-tagged ops, sources, and engines. The generic dataflux **engines** (`Flux` / `JointFlux`, `category="engine"`) and the source-view `DatasetSplit` (`category="source"`) **are** buildable on the canvas: their source-typed constructor params render as wired sockets — a single `source` → a `DATAFLUX_SOURCE` input, a source-list (`fluxes`) → dynamic `source_N` inputs, an op-list (`ops`) → dynamic `op_N` `DATAFLUX_OP` inputs — so a Source/Op node (which already outputs *itself* as `DATAFLUX_SOURCE` / `DATAFLUX_OP`) wires straight into them. Excluded *by construction*: the **raw-callable op wrappers** (`FilterOp` / `WrappedOp`) — UNcategorised (bare `@configurable`), since they take a raw Python callable nothing a widget or socket supplies (so they stay out even with `engine` allowed); **task datasets** (`*_dataset`, e.g. `classification_dataset`) — a trainer-config concept navigaitor cares about, but a canvas only needs a `source` to yield Samples; uncategorised module-level helper functions; and raw sinks. You can build a dataset by wiring `Source → Op → …` *or* by feeding a `Source`/`Op` into a `Flux` / `DatasetSplit` engine node. (To surface another role, add its category to `_NODE_CATEGORIES` in `bridge.py` and tag the classes. Note: ops-export still targets linear op chains — engine-node graphs aren't exportable yet.)

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

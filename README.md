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

Taidal nodes then appear in ComfyUI's *Add Node* menu under the `Taidal/…` categories (e.g. `Taidal/Waivefront/Sinks`), named for the callable they wrap.

## Building a pipeline

Discovered callables are classified by their DataFlux role so they wire together by **type**, not opaque strings:

- **Source** nodes (e.g. `HuggingFaceSource`, under `Taidal/Dataflux/Sources`) construct a dataset and output a `DATAFLUX_SOURCE`.
- **Op** nodes (e.g. `RescaleOp`, `StandardizeOp`, `RenderSpectrogramImageOp`) take a `DATAFLUX_SAMPLE` input **plus** their constructor config as widgets, apply the op, and output a `DATAFLUX_SAMPLE` — so ops chain.
- **Generic** nodes (plain functions) keep scalar widgets with a `STRING` fallback.

Node labels are the **verbatim** callable name (underscores become spaces; capitals are preserved — `HuggingFaceSource` stays `HuggingFaceSource`, not `Huggingfacesource`). Internal helpers (names starting with `_`) are not surfaced. Hovering any widget shows a **tooltip** sourced from the wrapped callable's docstring `Args:` block (see *Constructor-parameter tooltips* below).

Two adapter nodes (under `Taidal/FluxStudio`) let you step through a dataset and view samples with **native** ComfyUI viewers:

- **Walk Dataset (by index)** — takes a `DATAFLUX_SOURCE` and an `index`, and outputs the `Sample` at that index (`source[index]`). The `index` widget uses ComfyUI's `control_after_generate`, so each **Queue Prompt** advances to the next sample — set the control to *fixed* (or `index` `0`) to reset. `wrap` cycles modulo the dataset length.
- **Extract from Sample** — takes a `DATAFLUX_SAMPLE` and a `field` selector (`input` / `target`) and unpacks the selected value onto **typed** sockets — `IMAGE`, `MASK`, `STRING`, `INT`, `FLOAT` — so you wire the one your viewer expects. ComfyUI types sockets statically, so instead of retyping one socket per run it surfaces every plausible type; *which* one is meaningful is derived from the sample's features (`Sample.describe()`). `IMAGE` is rendered via `waivefront.visualizers.value_to_image` (PIL / tensor / 2-D map → image; a non-visual value renders as a small text image); `MASK` carries a 2-D field (segmentation targets); `STRING` is a label/scalar (with `label_name` when the target has it) or a compact type summary; `INT` / `FLOAT` carry a scalar (e.g. a class id). Drop two of these next to each other (one set to `input`, one to `target`) to view both at once.

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

## How nodes are generated

FluxStudio never hand-defines a node. At ComfyUI startup, `_discover_and_register_nodes()` (`fluxstudio/__init__.py`):

1. Enumerates the `confluid.configurables` entry-point group via `importlib.metadata`.
2. For each module, `dataflux.discovery.scan_module` finds the compatible callables.
3. `register_module_nodes` / `create_comfy_node` (`fluxstudio/bridge.py`) classify each callable (Source / Op / Generic — see *Building a pipeline*) and wrap it in a ComfyUI node class, keyed `Taidal_<callable>` and filed under a `Taidal/<Module/Path>` category.

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
| `pathlib.Path`, or a param named `*path*` / `*dir*` / `*root*` / `*folder*` | directory-picker `STRING` (`web/directory_chooser.js` → `/taidal/choose_directory`) |
| anything else | `STRING` fallback |

`DATAFLUX_SOURCE` and `DATAFLUX_SAMPLE` are FluxStudio socket types that carry the live `Flux` / `Sample` objects between nodes (ComfyUI connects two sockets only when their type strings match), which is what lets ops chain into a pipeline.

The directory picker calls the `/taidal/choose_directory` route registered in `fluxstudio/__init__.py`, currently a native **macOS** folder dialog (`osascript`). Extend `TYPE_MAP` / `_map_type` to support new types — never special-case an individual node.

### HuggingFace dataset-id field

A HuggingFace dataset source's identifier is a **name**, not a folder — `dataflux.sources.HuggingFaceSource` happens to call it `path` (mirroring `datasets.load_dataset`), and sonair's `HuggingFaceClassificationDataset` calls it `repo_id`. Either way FluxStudio renders it as a free-text `STRING` (so you can type any Hub repo id like `kitofrank/RFUAV`, or a local imagefolder path), **not** the directory picker. `web/hf_dataset_chooser.js` adds an optional “📚 cached dataset” combo populated from the `/taidal/hf_datasets` route (`huggingface_hub.scan_cache_dir()`, dataset repos only) — selecting one fills the text field, which stays editable. The field is detected by the `HuggingFace*` class-name convention, so any future `HuggingFace*` source inherits it automatically.

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

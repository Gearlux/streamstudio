# FluxStudio

Node-based visual editor and step-debugger for [DataFlux](https://github.com/Gearlux/data-flux) pipelines.

FluxStudio is the GUI layer of the [Taidal](https://github.com/Gearlux) Septet. Build, inspect, and step through DataFlux pipelines in a browser; reuse every existing op, source, sink, and visualizer that has already been written for DataFlux and Waivefront.

## Installation

```bash
pip install git+https://github.com/Gearlux/fluxstudio.git@main
```

To enable RF-domain visualizers (spectrograms, IQ time-domain plots) install with the `waivefront` extra:

```bash
pip install "fluxstudio[waivefront] @ git+https://github.com/Gearlux/fluxstudio.git@main"
```

Tab completion: `fluxstudio --install-completion` (bash/zsh/fish auto-detected from `$SHELL`).

## Development

```bash
git clone git@github.com:Gearlux/fluxstudio.git
cd fluxstudio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Frontend (Vite + React + xyflow):

```bash
cd frontend
npm install
npm run build       # production build into frontend/dist/
# or:
npm run dev         # hot-reload at http://localhost:5173
```

## Quick start

```bash
# 1. List every op FluxStudio can place on the canvas.
fluxstudio discover

# 2. Step through a small pipeline headlessly. One JSON event per op.
fluxstudio run examples/tiny_pipeline.yaml

# 3. Start the FastAPI server + (built) frontend.
fluxstudio serve --port 8765
# → http://127.0.0.1:8765/
```

## Architecture

```
┌────────── browser (React + xyflow) ──────────┐
│  node palette │ graph canvas │ step controls │
│                                visualiser pane│
└──────────────────┬────────────────────────────┘
                   │ HTTP + WebSocket (/api/*)
┌──────────────────┴────────────────────────────┐
│              fluxstudio.server (FastAPI)      │
│  /api/ops              GET   discovered ops   │
│  /api/visualizers      GET   render registry  │
│  /api/graph/{from,to}-yaml  Confluid <-> JSON │
│  /api/run              POST  start a run      │
│  /api/run/{id}/ws      WS    stream events    │
│  /api/run/{id}/control POST  step/cont/abort  │
└──────────────────┬────────────────────────────┘
       ┌───────────┼────────────┬──────────────┐
       ▼           ▼            ▼              ▼
   discovery     graph       executor      visualizers
                                            (registry)
       └───────────┴────────────┴──────────────┘
                   │
                   ▼
            dataflux.Flux  +  Confluid  +  waivefront ops
```

Nothing in dataflux is patched. The executor walks `Flux.ops` one op at a time using the same `_iter_sequential` semantics; the graph layer round-trips Confluid YAML; the visualizer registry delegates to waivefront's renderers when available.

## Step-debugging model

`SteppingExecutor` runs **single-process only** — no `.parallel()`. Each `__call__` of an op is wrapped so the executor can pause before/after, emit a `StepEvent` (with the encoded post-op `Sample`), and consult a control queue (`step` / `continue` / `abort`) before continuing. Multi-process debugging remains a future feature; for now, run pipelines through `SteppingExecutor` to debug, then drop the wrapper to ship.

## Visualizer dispatch

`fluxstudio.visualizers` maintains a small registry mapping types and string hints to encoders that produce JSON-friendly payloads. Defaults cover `numpy.ndarray`, `torch.Tensor`, `PIL.Image`, scalars, and an opaque fallback. With the `waivefront` extra installed, a `spectrogram` hint is auto-registered that runs `waivefront.visualizers.RenderSpectrogramImageOp` before encoding.

Override per node by setting `viz.type_hint` on a `GraphNode`.

## Examples

| File | What it shows |
|------|---------------|
| `examples/01_discover.py` | Dump the op palette as JSON to stdout. |
| `examples/02_step_pipeline.py` | Run `tiny_pipeline.yaml` headlessly, NDJSON events to stdout. |
| `examples/tiny_pipeline.yaml` | Three-node Confluid pipeline used by tests + CI. |

## Dependencies

| Package | Purpose |
|---------|---------|
| `dataflux` | Pipeline engine, op discovery, sample triplet. |
| `confluid` | YAML ⇄ object graph for Graph round-trip. |
| `liquifai` | CLI app framework (`LiquifyApp`). |
| `logflow` | Logging. |
| `fastapi` + `uvicorn` | HTTP/WebSocket server. |
| `pydantic` | Wire-format validation for Graph & API bodies. |
| `Pillow` + `numpy` | Default visualizer encodings. |
| `waivefront` *(optional)* | RF-domain renderers (spectrogram, IQ time/freq). |

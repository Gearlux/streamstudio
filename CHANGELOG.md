# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0a1] — unreleased

First public pre-release. StreamStudio is a ComfyUI custom-node extension, not a server of
its own: it generates the node palette from what is installed, so there are no
StreamStudio-specific ops to maintain.

### Nodes are generated, never hand-written
- Every node is built at import from the `@configurable` callables discovered through the
  `confluid.configurables` entry-point group. Install a package that declares them and its
  nodes appear; nothing needs registering here.
- Seven node shapes, one builder each: source, op, model, value, object, runnable, generic.
- A class's `@configurable` category decides its node shape where it names one: an
  `engine` (`Stream`, `FlowGraph`, `JointStream`) both takes and returns a
  `RECORDSTREAM_SOURCE`, so a source wires straight into it and engines chain.
- Widgets come from the callable's signature — closed `Literal`s become combos, PEP-593
  `Interval` marks become widget bounds, and each tooltip is the parameter's `Args:` entry.
- Object sockets are role-qualified (`RECORDSTREAM_OBJECT:model`) and claim their engine, so
  a cross-framework wire is refused as you drag it. The **Retag** node is the override.

### Running from the canvas
- **Taidal: compile to Op** turns a subgraph you drew into a `Stream` op — a `record` input,
  a *Build Record* gathering the promoted outputs (one socket per label), an *Extract as Op*
  terminator, and a promoted `op` — every arrow on the canvas. The compiler turns that shape
  into `RenameField`/`SelectFields` when it runs, so the graph you drew stays the graph you see. It runs only when you ask, reports what it built (or why it could not), and is
  re-runnable: the nodes it owns are rebuilt rather than duplicated. **Every** op in the rectangle is in the pipeline: loose ops are joined
  in canvas reading order, and anything you wired by hand keeps the order you gave it. A
  The **output labels are the record contract**: promoting an *Extract from Record* on `label`
  as `text` builds `RenameField(label → text)`, and `SelectFields` closes the set — real nodes
  placed inside the rectangle, so the contract is visible and editable. A rectangle that neither
  transforms records nor promises entries says so on the node instead of showing no socket.
- A runnable node runs on execute: it restores autograd for a training runnable, streams
  stdout live into a collapsible widget, drives ComfyUI's native progress bar, and is
  interruptible mid-run.
- Every declared `@output` is a live, typed, wireable socket.
- Workflow combinators nest: a runnable wired into a combinator slot is built rather than
  run, so only the selected branch executes.

### Plugins
- A domain node set is a plugin, never a requirement: install the package by any means and its
  nodes appear, because the palette is built from what the environment holds. Nothing here
  imports one unconditionally.
- A `streamstudio.plugins` entry point lets an installed package contribute editor surface
  without this package knowing it: the named module's `ROUTES` (an aiohttp `RouteTableDef`)
  merges onto the editor's route table, and its `WEB_DIRECTORY` of frontend JS is served at
  `/extensions/<dist>/…`. One broken plugin is isolated with a warning; the editor keeps
  running.
- A producer package tags a param's widget kind by stamping `__taidal_widget_params__` on
  the class — the bridge renders the tagged free-text STRING and the producer's own web
  extension supplies the picker. No widget special-cases live in the bridge.

### Import and export
- `streamstudio import --as subgraph` turns a `flow:` document into a **pipeline blueprint**:
  one rectangle with a declared `record` in, a `record` out, and an `op` output wired from an
  *Extract as Op* terminator placed for you. Drop it on a `Stream`'s op slot. The boundary
  comes from the document itself — the head step's unfed `record` and `outputs:` — and every
  slot renders before anything is connected to it.
- The canvas compiles to a Confluid `flow:` document — one per weakly-connected component —
  runnable head-less with `recordstream run`.
- A whole config round-trips both ways between YAML and a ComfyUI `workflow.json`.
- Export-skip and export-target marks scope what is emitted.

### The launcher
- The CLI is a **liquifai app**. Per-command help is generated from the command's own
  docstring and signature (`streamstudio restart -h`), and an option works on either side of
  the command name. Three spellings changed with it: a boolean turns off with `--install-`
  rather than `--no-install`, a list is `--custom-node=[a,b]` rather than a repeated flag,
  and the palette file is `--palette-config` (liquifai reserves `--config`). Short forms are
  declared per command: `-p` (`--port`), `-b` (`--background`), `-o` (`--output`); the old
  `--bg` is gone. A flag naming no parameter is now refused with a
  suggestion instead of being ignored. Completion moves to `liquifai-install-completions`;
  `argcomplete` is no longer a dependency.
- `--port` chooses where the studio serves (default 8188, ComfyUI's own). A port that is
  already in use is refused in a second, before any launch work, with the address of what is
  there printed as a clickable URL.
- `stop` and `restart` find the studio by the port it holds, so they work whatever started
  it. `restart` re-walks the whole launch flow, which is how you pick up newly installed
  nodes; stopping an idle port is not an error.
- `--background` (`--bg`) starts the studio detached and hands the terminal back, logging to
  a per-port file under `~/.streamstudio/logs/`. Both `run` and `restart` honour it.
- `streamstudio` clones ComfyUI on first run, syncs its requirements on every run, seeds
  custom nodes from three tiers (package-declared, env, flags), symlinks this package in,
  and launches ComfyUI under the same interpreter.
- `--default-nodes-` and `--only-taidal-nodes` narrow the palette; the keep-list is a
  config file, not an environment variable.

### Quiet by default
- Importing the package emits nothing above debug. Node discovery walks one entry point per
  installed package, and those scan lines are diagnostics — visible with debug logging on,
  absent otherwise.

### In-canvas viewers
- Array/tensor histogram and confusion-matrix viewers draw on the litegraph node canvas.
  All the rendering math lives in the packages that own the data — the nodes are glue.

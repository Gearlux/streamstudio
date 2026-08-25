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
- Widgets come from the callable's signature — closed `Literal`s become combos, PEP-593
  `Interval` marks become widget bounds, and each tooltip is the parameter's `Args:` entry.
- Object sockets are role-qualified (`RECORDSTREAM_OBJECT:model`) and claim their engine, so
  a cross-framework wire is refused as you drag it. The **Retag** node is the override.

### Running from the canvas
- A runnable node runs on execute: it restores autograd for a training runnable, streams
  stdout live into a collapsible widget, drives ComfyUI's native progress bar, and is
  interruptible mid-run.
- Every declared `@output` is a live, typed, wireable socket.
- Workflow combinators nest: a runnable wired into a combinator slot is built rather than
  run, so only the selected branch executes.

### Plugins
- A `streamstudio.plugins` entry point lets an installed package contribute editor surface
  without this package knowing it: the named module's `ROUTES` (an aiohttp `RouteTableDef`)
  merges onto the editor's route table, and its `WEB_DIRECTORY` of frontend JS is served at
  `/extensions/<dist>/…`. One broken plugin is isolated with a warning; the editor keeps
  running.
- A producer package tags a param's widget kind by stamping `__taidal_widget_params__` on
  the class — the bridge renders the tagged free-text STRING and the producer's own web
  extension supplies the picker. No widget special-cases live in the bridge.

### Import and export
- The canvas compiles to a Confluid `flow:` document — one per weakly-connected component —
  runnable head-less with `recordstream run`.
- A whole config round-trips both ways between YAML and a ComfyUI `workflow.json`.
- Export-skip and export-target marks scope what is emitted.

### The launcher
- `streamstudio` clones ComfyUI on first run, syncs its requirements on every run, seeds
  custom nodes from three tiers (package-declared, env, flags), symlinks this package in,
  and launches ComfyUI under the same interpreter.
- `--no-default-nodes` and `--only-taidal-nodes` narrow the palette; the keep-list is a
  config file, not an environment variable.

### In-canvas viewers
- Array/tensor histogram and confusion-matrix viewers draw on the litegraph node canvas.
  All the rendering math lives in the packages that own the data — the nodes are glue.

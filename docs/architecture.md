# Architecture decisions

Design rationale for mechanisms whose existence or shape is not obvious from their name or the
user docs. Each record: Context → Decision → Consequences → Example → What you may change.
User-facing usage lives in the README; agent-facing rules in `AGENTS.md` — this file answers
"why does this mechanism exist?".

## The plugin seam: `streamstudio.plugins` + the widget-kind stamp (2026-08-25)

**Context.** StreamStudio is published, but some node producers integrate with private,
vendor-specific platforms — a dataset picker backed by a credentialed platform API, for example.
Such an integration needs three things a plain node cannot carry: a backend route on the editor's
server, a frontend JS extension, and a per-param widget tag telling that JS which field to
decorate. All three used to be hard-coded here (a guarded vendor import, an inline route, a
shipped JS file, and a class-NAME gate in `_widget_for`) — publishable code carrying vendor
knowledge, and a bridge that grew a special case per integration.

**Decision.** Three seams, none naming any consumer:

1. **Widget kinds are a class stamp, not a bridge rule.** `__taidal_widget_params__: Mapping[str,
   str]` (param name → opaque tag) joins the existing cross-package stamp protocol
   (`__taidal_display_name__`, `__taidal_op_params__`, `__taidal_op_list_params__`,
   `__taidal_model_params__`). `_widget_for` reads it via `getattr` FIRST — before the
   HuggingFace name conventions and before `_map_type`'s path-name rule — and emits a free-text
   STRING carrying `taidal_widget=<tag>`. No registry: the tag flows verbatim to the frontend,
   and only the producer's own JS attaches meaning to it.
2. **Routes and web assets ride a `streamstudio.plugins` entry point** whose value is a module.
   `ROUTES` (an aiohttp `RouteTableDef`) is merged onto `server.PromptServer.instance.routes` by
   re-registering each RouteDef with first-wins per-(method, path) dedup — aiohttp has no public
   table append. `WEB_DIRECTORY` is inserted into ComfyUI's `nodes.EXTENSION_WEB_DIRS` under the
   plugin's *distribution* name; ComfyUI's `/extensions` listing globs that dict per request and
   its static routes are added at server start-up, *after* custom nodes (and therefore this
   package, and therefore plugin discovery) have loaded — which is what makes import-time
   registration timing-safe, and what serves plugin JS at the same URL depth
   (`/extensions/<dist>/…`) as any extension, keeping relative `../../scripts/*.js` imports valid.
3. **Failure isolation is per entry, at warning level** — deliberately louder than the
   debug-level `confluid.configurables` scan, because a declared plugin is an explicit opt-in and
   its failure is actionable, while most configurable modules legitimately aren't node modules.

Rejected alternatives: a `taidal_widget` kind *registry* (nothing consumes kinds centrally — a
registry would be bookkeeping with no reader); making each plugin its own ComfyUI custom-node
package (requires launcher symlinking and a second discovery mechanism when entry points already
exist); serving plugin JS from a streamstudio-owned route (breaks the relative-import depth that
`EXTENSION_WEB_DIRS` preserves for free).

**Consequences.** This package carries zero vendor knowledge and every future platform
integration is a plugin, not a bridge special-case. The behavior change is deliberate: a class
whose *name* merely resembles a convention gets no picker any more — only a stamp triggers one.
Plugins depend on two ComfyUI internals (`PromptServer.instance.routes` before `add_routes()`,
and the `EXTENSION_WEB_DIRS` dict) that the existing route registration already relied on; if
ComfyUI moves them, the registrars in `streamstudio/__init__.py` are the single place to adapt.

**Example.** A producer stamps a param and ships the picker beside it:

```python
@configurable(category="value")
class PlatformDataset:
    __taidal_widget_params__ = {"name": "PLATFORM_DATASET_ID"}
    def __init__(self, name: str = ""): ...
# _widget_for(name) → ("STRING", {"default": "", "taidal_widget": "PLATFORM_DATASET_ID"})
```

```toml
[project.entry-points."streamstudio.plugins"]
my-plugin = "my_plugin.studio"        # module with ROUTES + WEB_DIRECTORY (see README → Plugins)
```

**What you may change.** Add module attributes to the plugin contract (keep them optional and
getattr-read); tighten dedup logging; extend the stamp's value type if a widget ever needs more
than a tag (do it on the stamp, not with a name rule). Do not reintroduce name-based widget
gates, per-vendor branches in `_widget_for`, or a second discovery mechanism beside the
entry-point group.

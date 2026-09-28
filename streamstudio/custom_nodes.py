"""Bundled custom-node declarations — streamstudio's own ``streamstudio.custom_nodes`` entry point.

The streamstudio launcher seeds ComfyUI's ``custom_nodes/`` from three places (see ``cli.py``):

1. the ``streamstudio.custom_nodes`` **entry-point group** — nodes declared by installed
   packages, "generally available": installed on every launch with no flags or env vars;
2. the ``STREAMSTUDIO_CUSTOM_NODES`` env var (comma-separated git URLs);
3. the repeatable ``--custom-node <git-url>`` CLI flag.

This module is streamstudio's OWN declaration for tier 1, wired in ``pyproject.toml``. ANY
installed package can contribute the same way::

    [project.entry-points."streamstudio.custom_nodes"]
    my_package = "my_package.custom_nodes:CUSTOM_NODE_URLS"

The entry-point target must resolve to a git-URL ``str`` or an iterable of them. NOTE:
adding/renaming the entry point itself changes installed ``*.dist-info`` metadata, so an
editable install must be re-installed for the launcher to see it; editing the URL tuple
below takes effect immediately (it is loaded from the module at launch time).
"""

# ComfyUI-Manager gives the launched ComfyUI an in-app store to install / update / remove
# further custom nodes, so the launcher only has to SEED it. The CLI's --no-manager flag
# filters exactly this URL back out of the declared tier.
COMFYUI_MANAGER_URL = "https://github.com/Comfy-Org/ComfyUI-Manager"

# streamstudio's bundled, generally-available custom nodes. Append a git URL here to have it
# installed for every streamstudio user on every launch.
CUSTOM_NODE_URLS: tuple[str, ...] = (COMFYUI_MANAGER_URL,)

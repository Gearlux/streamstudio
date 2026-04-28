"""Mount the prebuilt frontend (frontend/dist/) on the FastAPI app, when present.

The React app is shipped as a build artifact, not as a checked-in directory of
generated files. If `npm run build` hasn't been run yet, mount a small landing
page that explains how to build it. This keeps `fluxstudio serve` useful even
before the frontend is built — `/api/*` routes still work.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from logflow import get_logger

logger = get_logger(__name__)

# fluxstudio/ → project root → frontend/dist/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DIST_DIR = _PROJECT_ROOT / "frontend" / "dist"

_NOT_BUILT_HTML = """\
<!doctype html>
<html><head><title>FluxStudio — Frontend Not Built</title>
<style>body{font-family:-apple-system,sans-serif;max-width:640px;margin:4em auto;padding:0 1em;color:#222}
code{background:#eee;padding:.2em .4em;border-radius:3px}</style></head>
<body>
<h1>FluxStudio API is running</h1>
<p>The React frontend hasn't been built yet. Build it once with:</p>
<pre><code>cd frontend
npm install
npm run build</code></pre>
<p>Then refresh this page. Until then, the API is reachable at
<a href="/api/ops"><code>/api/ops</code></a>,
<a href="/api/visualizers"><code>/api/visualizers</code></a>, etc.</p>
</body></html>
"""


def mount_frontend(app: FastAPI) -> None:
    """Attach the SPA at `/` if `frontend/dist/` exists; otherwise a help page."""
    if _DIST_DIR.is_dir() and (_DIST_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="ui")
        logger.info(f"FluxStudio: serving prebuilt frontend from {_DIST_DIR}")
        return

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def _not_built() -> HTMLResponse:
        return HTMLResponse(_NOT_BUILT_HTML)

    logger.info(f"FluxStudio: frontend/dist not found at {_DIST_DIR}; serving placeholder.")

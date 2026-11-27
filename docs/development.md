# Developing StreamStudio

For a change to StreamStudio itself: the editor's page and how it is built, and the tests that hold it together.

## Building the canvas page

The page's source is `frontend/` (vite + React Flow + TypeScript). Its BUILD is committed too, inside the package:
`streamstudio/canvas` — `index.html`, `assets/index-*.js`, `assets/index-*.css` and `.built-from`, the record of the
source it was built from. The editor serves that folder at `/`, so every install has the page: a wheel (which carries
no `frontend/` at all) and a checkout without Node alike. **Node and npm are needed only to EDIT the page** — install
Node.js, which brings npm.

**Editing the page: commit the rebuilt `streamstudio/canvas` with the source change.** Say one label of the
palette changed in `frontend/src/canvas/Palette.tsx`. Until the page is rebuilt, a test fails — in CI too, which has
no Node — and says what to do (`tests/test_committed_page.py`; the folder shortened to `…`):

```
AssertionError: the committed editor page is older than its source — start streamstudio once, or run `cd
streamstudio/frontend && npm install && npm run build`, and commit streamstudio/streamstudio/canvas (the page in
…/streamstudio/canvas records source '005e8579…'; the source now is b3a4b936…)
```

A server builds the page as it STARTS when the page's source changed since its build — `streamstudio <folder>` does
it, and so does an app that mounts the editor when it starts (it calls `streamstudio.app.ensure_page_built()` before it
serves):

```
streamstudio: the editor's page changed since its last build — building it
streamstudio: built the editor's page in 5.5 s
```

and `git status` then shows what to commit beside the source — the test passes again:

```
 M frontend/src/canvas/Palette.tsx
 M streamstudio/canvas/.built-from
 D streamstudio/canvas/assets/index-D8HFRaK-.js
 M streamstudio/canvas/index.html
?? streamstudio/canvas/assets/index-CMOyHA-n.js
```

A fresh checkout, and every `git pull`, start silently: the committed page matches its committed source. Two
branches that both changed the page merge as a conflict in `streamstudio/canvas` (git treats the folder as binary —
`.gitattributes`): resolve it by building the page again and committing what the build writes.

What each line a start can print means (each begins `streamstudio: `):

| line | what happened |
| --- | --- |
| *(nothing)* | the page is current — the check took about 2 ms |
| `the editor's page is not built yet — building it` | `streamstudio/canvas` holds no page (it was deleted) |
| `the editor's page changed since its last build — building it` | a file the build reads changed |
| `the editor's page has no record of the source it was built from — building it` | the page holds no `.built-from` (built before the record existed); it is built once more to record its source |
| `installing the page's libraries first (npm ci) — the first time takes about a minute` | `frontend/node_modules` is missing, or `package-lock.json` is newer than npm's own record of its last install |
| `another start is building the editor's page — waiting for it` | a second server started from the same checkout while the first builds; it waits, then finds the page current |
| `built the editor's page in 2.0 s` | done; the time covers the install too |
| `the editor's page is out of date and Node is not installed — install Node.js (it brings npm) and start again` | the page's source changed and there is no Node to build it: the server starts anyway, and `/` says the same instead of serving the old page (`… is not built and Node is not installed …` when there was no page) |
| `the editor's page has no record of the source it was built from and Node is not installed — the page built earlier is served` | a page without a record may well be current: without Node it is served as it is |
| `building the editor's page failed — npm run build:page exited with code 2; its last lines:` and npm's last 20 lines | the server starts anyway, and `/` says it failed and names the command that shows why |
| `building the editor's page failed — npm ci did not finish in 300 s` | npm was stopped, with everything it started (`npm ci` may take 300 s, `npm run build:page` 120 s — an unreachable registry otherwise kept a start waiting for good) |
| `interrupted before the editor's page was built — the next start builds it` | Ctrl+C during the check: npm is stopped and the start ends (exit code 130) |

"Node is not installed" means `node` and `npm` are not on the server's `PATH`.

What counts as the page's source: everything under `frontend/src/` and `frontend/public/` except the page's tests
(`__tests__/`, `*.test.*`, `*.spec.*`) and hidden files, plus `index.html`, `package.json`, `package-lock.json`,
`vite.config.*`, `tsconfig*.json` and the `.env` files `vite build` reads that every clone has (`.env`,
`.env.production` — not `.env.test` or `.env.development*`). A change to a test file builds nothing, and neither does a
file `frontend/.gitignore` ignores — `*.local`, `.env.local` and `.env.production.local` among them. The record
`streamstudio/canvas/.built-from` is a SHA-256 over those files' relative paths and bytes — never where the checkout is
or when its files were written — so it is the same on every machine; `frontend/.gitattributes` checks the source out
with LF line endings everywhere (a Windows checkout that wrote CRLF would otherwise find the page out of date). Every
input is a file a clone carries, so the record a developer commits is the one CI and every other checkout compute. The
page reads no `import.meta.env`: a `.env.local` of your own changes nothing the browser runs, and if the page ever
reads one, its values come from `.env` / `.env.production`, which are committed.

Whether `npm ci` runs first is decided by file TIMES, because npm keeps no record of the lock it installed from: a
`package-lock.json` rewritten with the same bytes (a branch switched away and back) installs once more at the next
start that builds — about a minute the first time, seconds from npm's cache. A start whose page source did not change
installs nothing.

Two servers started at once from the same checkout never build together on macOS and Linux (there is no lock on
Windows): the second says it waits, waits, then finds the page current. Only a START checks — making an app
(`create_app`, `page_blueprint`) never builds anything, and an install, which has no `frontend/` source, is never
checked: it serves the page its package carries. A start that could not build the page (no Node, a failed build) does
not try again in the same process: an app that checks before it announces its server and again as it serves prints
its lines once, and a server process it starts in the background takes its answer over through
`page_check_environment()` — passed in the child's environment — instead of running npm again where its output is
discarded.

Building by hand works the same, and is what the page's own tools need:

```bash
cd frontend
npm install      # once, and again after package.json changes
npm run build    # begin, then tsc --noEmit && vite build -> streamstudio/canvas, then record (python3 ../streamstudio/page.py)
npm test         # the page's unit tests (vitest)
npm run dev      # optional: a hot-reloading copy on vite's own port, proxying api/ to an editor on port 8190
```

`npm run build` is three steps — `python3 ../streamstudio/page.py begin && npm run build:page && python3
../streamstudio/page.py` — and the two around vite write `.built-from` with the same fingerprint a start uses
(`streamstudio: recorded the source of the editor's page in …/streamstudio/canvas/.built-from`), so a page built by hand
is current. The first notes the fingerprint BEFORE vite reads a file, the last records that note: a file saved while
vite runs (about 3 s) is not vouched for — the build ends with `streamstudio: the editor's page's source changed since
its build began — build again (npm run build)` and no record, and the next start builds the page again. `npm run
build:page` is the build alone (`tsc --noEmit && vite build`), which is what a server's start runs: a start needs Node
and nothing else, while the hand build also needs a `python3` (3.8 or later) command on `PATH` — python.org's Windows
installer has `python` and `py` but no `python3`, which is why the start does not depend on it. vite writes no source
map (`*.map` is gitignored and not in the wheel); `npm run dev` serves the sources with maps for debugging.

Only a package that carries no build at all — `streamstudio/canvas` deleted — answers `/` with a placeholder that says
what to do:

```
streamstudio editor
The canvas page is not built. Build it once — cd streamstudio/frontend && npm install && npm run build — and
reload; the page lands in streamstudio/canvas inside the package. The API is up:
  GET /api/graphs
  GET /api/graphs/<name>
  …
```

The page served at `/` is looked up at every request, so a page built after the server's app was made is served
without a restart — unless this server's start could not build it: then `/` keeps saying why (no Node, or the
build failed) until the next start, which checks again.

The browser tests of the page (`tests/test_canvas.py`, headless chromium) run against the committed build; they skip
only when `streamstudio/canvas/index.html` is absent, and the skip reason is that same build command.

Plain `pytest` is the fast run. The slowest tests that import every installed package (the full catalogue) are marked
`slow` and left out of it; `pytest -m slow` runs them, and `pytest -m ''` runs everything.

"""The editor's page is built at a server's START when its source changed since its last build.

The canvas page is TypeScript/React in ``frontend/`` beside the package (committed source); ``npm run build:page``
(``tsc --noEmit && vite build``) writes it INTO the package, ``streamstudio/canvas`` (:func:`built_folder`), where it
is COMMITTED with the record of the source it was built from (:data:`STAMP`) — so every install
serves it, a checkout without Node and an install with no ``frontend/`` at all included, and Node is needed only by
whoever edits the page's source (user decision B, 2026-10-04). Measured before 2026-10-02: after a ``git pull`` that
changed the page, every start served the page built BEFORE the pull — or, on a machine that had never built it, a
placeholder naming :data:`BUILD_COMMAND` — until someone ran that command by hand.

:func:`ensure_page_built` is what a start calls — the editor's own CLI and a host app that mounts the editor, each
before it serves (and before it announces the server anywhere), never when an app is MADE (tests make apps by the
hundred and must never run npm):

* it hashes what the build reads (:func:`page_inputs`) and compares it with the record a successful build left inside
  the built page (:data:`STAMP`) — equal: nothing is printed and nothing runs (2.1 ms measured over the 56 inputs).
  A checkout whose committed page matches its source is current, without Node;
* otherwise it says so, runs ``npm ci`` first when the page's libraries are missing or ``package-lock.json`` is newer
  than npm's own record of its last install, then ``npm run build:page`` — the build alone, with no ``python3`` in it:
  a start needs Node and nothing else — and writes the record itself. Each npm step has a time
  limit (:data:`TIMEOUTS`): npm runs in a session of its own, and a step that outlives its limit is stopped with
  everything it started;
* without Node, or when npm fails, the server still starts: the start prints what is wrong, and the page served at
  ``/`` says it too (:func:`unbuildable`, read by ``page_blueprint`` at every request) instead of the old page — except
  a page with no record of its source (built by hand, or before the record existed), which may well be current and
  is served as it is when there is no Node to rebuild it;
* a check that could not make the page current is not repeated in the same process (a host may ask from its command
  line and again in the server it starts), and a process a start launches to serve takes that answer over through
  its environment (:func:`page_check_environment`, :data:`HANDED_DOWN`) instead of running npm again;
* two starts from one checkout never build in the same folder together: a BUILD holds an exclusive ``flock`` on the
  page folder (macOS and Linux; where ``fcntl`` does not exist — Windows — there is no lock), so the second says it
  waits, waits, and then finds the page current. A page that is current takes no lock.

A folder without page source — an install — is never checked: it serves the page its package carries. Ctrl+C while
npm runs stops npm and ends the start with one line (exit code 130), never a traceback.

Run as a script it is the two steps a build BY HAND puts around vite — ``npm run build`` is ``python3
../streamstudio/page.py begin && npm run build:page && python3 ../streamstudio/page.py``: ``begin`` takes the
:func:`fingerprint` of the source before vite reads a file (:func:`begin_build`), and the last step writes the record
(:func:`record_build`) — with that same fingerprint, refusing when a file changed meanwhile, so a hand build is current
too and never vouches for a line that is in the record and not in the bundle. It runs under whatever ``python3`` is on
``PATH`` (3.8 or later): this module imports the standard library only.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Literal, Optional, Tuple, cast, get_args

if sys.platform != "win32":  # the lock between two starts (:func:`_held`) is POSIX's flock
    import fcntl

#: The page's source folder beside the package. Read at every call, so a test points a check at a folder of its own.
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

#: Where a build of the page lands, relative to the page's source folder — ``build.outDir`` in
#: ``frontend/vite.config.ts`` says the same (``tests/test_committed_page.py`` holds the two together). It is a folder
#: INSIDE the package, so the built page is committed with it and ``package-data`` carries it into every install. Named
#: ``canvas``: not ``page`` (this module), not ``static`` (Flask serves
#: a ``static`` folder beside ``app.py`` at ``/static/``), not ``dist``/``build`` (the repository ignores those).
OUT_DIR = "../streamstudio/canvas"

#: The command that builds the page by hand: the placeholder names it, and a failed start names it as the way to see
#: npm's whole output. ``npm run build`` brackets ``npm run build:page`` (what a start runs) with this module's two
#: steps, so its page is current.
BUILD_COMMAND = "cd streamstudio/frontend && npm install && npm run build"

#: The record of what a build was made from — the hex fingerprint of :func:`page_inputs`, in a file inside the built
#: page, written AFTER a successful build and committed with it. vite empties the folder as a build starts, so a build
#: cut short leaves no record (the next start then builds once more and writes it).
STAMP = ".built-from"

#: The note a hand build leaves BEFORE vite reads a file — the fingerprint of the source at that moment — relative to
#: the page's source folder: under ``node_modules/.cache``, which git ignores and vite's own emptying of the built
#: folder does not reach. The record step writes the record from it and removes it (:func:`record_build`).
BEGUN = "node_modules/.cache/streamstudio-page-source"

#: The environment variable through which a start hands its check's answer to a process it launches to serve (a host's
#: ``--background`` child): that process takes the answer over instead of running npm again where its output is
#: discarded. Set by the starting process for its child only — never by a person — and removed by the first check that
#: reads it, so it reaches no further.
HANDED_DOWN = "STREAMSTUDIO_PAGE_CHECK"

#: The files at the top of the page folder that the build reads: the page shell, the libraries it pins, the build's
#: own configuration (vite loads the first of its six config names it finds; ``tsc`` reads ``tsconfig*.json``) and
#: the ``.env`` files ``vite build`` loads that every clone has — it runs in production mode, so ``.env.test`` and
#: ``.env.development*`` (read by vitest and the dev server) are not inputs, and neither are ``.env.local`` and
#: ``.env.production.local``: ``frontend/.gitignore`` ignores ``*.local``, so no clone has one
#: (:func:`_read_by_the_build`).
_TOP_INPUTS = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.ts",
    "vite.config.cjs",
    "vite.config.mts",
    "vite.config.cts",
    ".env",
    ".env.production",
)
_TOP_GLOBS = ("tsconfig*.json",)

#: The folders whose every file the build reads — the page's source and vite's ``public/`` — minus the page's tests
#: (vitest's ``__tests__/``, ``*.test.*``, ``*.spec.*``), hidden files (a Finder ``.DS_Store``) and ``*.local`` files
#: (git-ignored, so in no clone): none of them changes the page.
_TREES = ("src", "public")

#: How many of npm's last lines a failed build prints, and the page shows.
TAIL_LINES = 20

#: The two npm steps of a build: ``ci`` installs the page's libraries, ``build`` builds the page.
NpmStep = Literal["ci", "build"]

#: How long each npm step may run, in seconds, before the start stops it and reports the build as failed — measured:
#: the first ``npm ci`` takes about a minute, ``npm run build:page`` 2.1 s; with a registry that refuses connections
#: ``npm ci`` gave up after 70 s, with one that drops the traffic it was still waiting after 10 minutes.
TIMEOUTS: Dict[NpmStep, float] = {"ci": 300.0, "build": 120.0}

#: How each step is said, and what npm is asked.
_STEPS: Dict[NpmStep, Tuple[str, Tuple[str, ...]]] = {
    "ci": ("npm ci", ("ci",)),
    "build": ("npm run build:page", ("run", "build:page")),
}

#: How long npm's last output is read after it was stopped (its own children are stopped with it, so the pipe closes
#: at once; the bound is for a child that left npm's session).
_AFTER_STOP_S = 10.0

#: The line a start prints when Ctrl+C reaches it during the check.
_INTERRUPTED = "streamstudio: interrupted before the editor's page was built — the next start builds it"

#: What a start's check found: the page was ``current``; it was ``built`` now; the folder holds ``no-source`` (an
#: install, which serves the page its package carries); it needed a build and there was ``no-node``; the build
#: ``failed``; or the page has no record of its source and there is no Node to rebuild it, so it is served
#: ``unverified``.
PageState = Literal["current", "built", "no-source", "no-node", "failed", "unverified"]
PAGE_STATES: Tuple[str, ...] = get_args(PageState)

#: The answers that did not make the page current: a later check in the same process over the same source repeats
#: neither npm nor its lines, and :func:`page_check_environment` hands them to a serving child.
_UNSETTLED_STATES: Tuple[PageState, ...] = ("no-node", "failed", "unverified")

#: The answers after which ``/`` says what is wrong instead of serving a page (:func:`unbuildable`).
_UNBUILDABLE_STATES: Tuple[PageState, ...] = ("no-node", "failed")


@dataclass(frozen=True)
class PageCheck:
    """What :func:`ensure_page_built` found, and did.

    Attributes:
        state: See :data:`PageState`.
        said: The lines the check printed, in order (empty when there was nothing to say — and for an answer this
            process gave before, which is not said twice).
        why: For ``no-node``: how the page stands — ``is not built`` / ``is out of date``; for ``unverified``: ``has no
            record of the source it was built from``; for ``failed``: what went wrong — ``npm run build:page exited with
            code 2``, ``npm ci did not finish in 300 s``. Empty otherwise.
        output: For ``failed``: npm's last lines (:data:`TAIL_LINES`), joined by newlines.
        seconds: For ``built``: how long the install and the build took.
    """

    state: PageState
    said: Tuple[str, ...] = ()
    why: str = ""
    output: str = ""
    seconds: float = 0.0


#: The answers that did not make the page current, by the resolved built page folder: the fingerprint the check
#: found and its answer. ``page_blueprint`` serves the message of a ``no-node`` or ``failed`` one at ``/`` instead of
#: an out-of-date page; a later check that builds (or finds the page current) forgets it. Per process: the next start
#: is a new process and tries again.
_UNSETTLED: Dict[Path, Tuple[str, PageCheck]] = {}


def page_inputs(frontend: Path) -> List[Path]:
    """The files the build reads, relative to ``frontend`` and sorted by their POSIX spelling."""
    found = [Path(name) for name in _TOP_INPUTS if (frontend / name).is_file()]
    for pattern in _TOP_GLOBS:
        found.extend(path.relative_to(frontend) for path in frontend.glob(pattern) if path.is_file())
    for tree in _TREES:
        root = frontend / tree
        if root.is_dir():
            found.extend(path.relative_to(frontend) for path in root.rglob("*") if path.is_file())
    return sorted({path for path in found if _read_by_the_build(path)}, key=lambda path: path.as_posix())


def _read_by_the_build(relative: Path) -> bool:
    """A test, a hidden file or a ``*.local`` file under ``src/`` or ``public/`` is not an input; a top-level ``.env``
    file is. ``*.local`` is ``frontend/.gitignore``'s own pattern: a file it ignores is in no clone, and the record is
    compared with the source of every clone (``tests/test_committed_page.py`` holds the two together)."""
    if relative.name.endswith(".local"):
        return False
    inner = relative.parts[1:] if relative.parts[0] in _TREES else ()
    if any(part.startswith(".") or part == "__tests__" for part in inner):
        return False
    return not (inner and (".test." in relative.name or ".spec." in relative.name))


def fingerprint(frontend: Path) -> str:
    """The SHA-256 of every input's relative path and bytes — what the build would read now.

    The same on every machine for the same source: relative POSIX paths and contents only, never where the folder is
    or when its files were written. Bytes, so a line ending counts — ``frontend/.gitattributes`` checks the source out
    with LF everywhere."""
    digest = hashlib.sha256()
    for relative in page_inputs(frontend):
        data = (frontend / relative).read_bytes()
        digest.update(f"{relative.as_posix()}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()


def built_folder(frontend: Optional[Path] = None) -> Path:
    """Where a build of the page in ``frontend`` lands — :data:`OUT_DIR` from it.

    Args:
        frontend: The page folder; ``None`` = the checkout's, :data:`FRONTEND` — its build is ``streamstudio/canvas``
            inside the package, which is where an install, with no ``frontend/`` at all, finds the page too.
    """
    folder = FRONTEND if frontend is None else Path(frontend)
    return Path(os.path.normpath(folder / OUT_DIR))


class SourceChanged(RuntimeError):
    """A file the build reads changed between :func:`begin_build` and :func:`record_build`: the page built in between
    may not hold it, so no record vouches for it."""


def begin_build(frontend: Optional[Path] = None) -> Path:
    """Note the fingerprint of the source BEFORE a build by hand reads a file (:data:`BEGUN`) — the first step of
    ``npm run build``. A record made after vite finished would vouch for a file saved while it ran (measured: ~3 s of
    window, a line saved at ``rendering chunks`` was in the record and not in the bundle). Answers the note's path."""
    folder = FRONTEND if frontend is None else Path(frontend)
    note = folder / BEGUN
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(fingerprint(folder) + "\n", encoding="utf-8")
    return note


def record_build(frontend: Optional[Path] = None) -> Path:
    """Write the record of the page just built from ``frontend`` (:data:`STAMP`, :func:`fingerprint`) — what a build by
    hand ends with (``npm run build``). Answers the record's path.

    The record is the fingerprint :func:`begin_build` noted before the build, when there is a note — and only if the
    source still has it; without a note (this step run alone) it is the fingerprint of the source now. The note is
    removed either way: it belongs to one build.

    Raises:
        FileNotFoundError: The built page folder holds no ``index.html``: a record never vouches for a missing page.
        SourceChanged: A file the build reads changed since :func:`begin_build`: build again.
    """
    folder = FRONTEND if frontend is None else Path(frontend)
    built = built_folder(folder)
    if not (built / "index.html").is_file():
        raise FileNotFoundError(f"no built page in {built} (no index.html) — nothing to record")
    now = fingerprint(folder)
    note = folder / BEGUN
    begun: Optional[str] = None
    if note.is_file():
        begun = note.read_bytes().decode("ascii", errors="replace").strip()
        note.unlink()
    if begun is not None and begun != now:
        raise SourceChanged("the editor's page's source changed since its build began — build again (npm run build)")
    return _write_record(built, now)


def ensure_page_built(frontend: Optional[Path] = None) -> PageCheck:
    """Build the editor's page when its source changed since its last build — what a server calls as it STARTS.

    Prints what it does (nothing at all when the page is current). npm, Node and the disk never stop a start: the
    server starts whatever the outcome, and the page served at ``/`` says what went wrong (:func:`unbuildable`).
    Ctrl+C while it runs stops npm, prints one line and raises ``SystemExit(130)`` — the person asked the start to end.

    Args:
        frontend: The page folder; ``None`` = the checkout's, :data:`FRONTEND`. A folder without ``package.json``
            and ``src/`` (an install) is not checked: ``no-source`` — it serves the page its package carries.
    """
    folder = FRONTEND if frontend is None else Path(frontend)
    if not ((folder / "package.json").is_file() and (folder / "src").is_dir()):
        return PageCheck("no-source")
    dist = built_folder(folder)
    key = dist.resolve()
    said: List[str] = []

    def say(line: str) -> None:
        said.append(line)
        print(line, flush=True)

    source = ""
    try:
        source = fingerprint(folder)
        _take_over(key, source)
        earlier = _UNSETTLED.get(key)
        if earlier is not None and earlier[0] == source:
            return replace(earlier[1], said=())  # this process tried this source already: not again, not said twice
        check = _check(folder, dist, source, say)
    except KeyboardInterrupt:
        say(_INTERRUPTED)
        raise SystemExit(130) from None
    except (OSError, ValueError) as exc:  # a folder or a file that cannot be read or written
        say(f"streamstudio: building the editor's page failed — {exc}")
        check = PageCheck("failed", why=str(exc))
    check = replace(check, said=tuple(said))
    if check.state in _UNSETTLED_STATES:
        _UNSETTLED[key] = (source, check)
    else:
        _UNSETTLED.pop(key, None)
    return check


def unbuildable(dist: Path) -> Optional[PageCheck]:
    """What this process's last start check found for the page in ``dist`` when it could NOT make it current and the
    page must not be served (``no-node`` or ``failed``); ``None`` when no check ran on it, or the last one built it,
    found it current, or found a page with no record of its source (``unverified`` — that page is served)."""
    entry = _UNSETTLED.get(Path(dist).resolve())
    return entry[1] if entry is not None and entry[1].state in _UNBUILDABLE_STATES else None


def page_check_environment(frontend: Optional[Path] = None) -> Dict[str, str]:
    """The environment entries through which a process this one starts TO SERVE takes over this process's check.

    Empty when this process's check made the page current (or ran none): the child's own check then finds the page
    current, silently, in 2 ms. After a check that could not — no Node, a failed build, a page with no record — it
    names that answer (:data:`HANDED_DOWN`), and the child takes it over instead of running npm again where its output
    is discarded. Pass it in the child's environment: ``Popen(..., env={**os.environ, **page_check_environment()})``.

    Args:
        frontend: The page folder; ``None`` = the checkout's, :data:`FRONTEND`.
    """
    key = built_folder(frontend).resolve()
    entry = _UNSETTLED.get(key)
    if entry is None:
        return {}
    source, check = entry
    answer = {"dist": str(key), "source": source, "state": check.state, "why": check.why, "output": check.output}
    return {HANDED_DOWN: json.dumps(answer)}


def _take_over(key: Path, source: str) -> None:
    """Take over the answer the starting process handed down (:data:`HANDED_DOWN`) when it is about this page folder
    and this very source; an answer about anything else, or one that cannot be read, is dropped and this process
    checks for itself. The variable is removed either way: it was meant for this process's first check only."""
    raw = os.environ.pop(HANDED_DOWN, None)
    if raw is None:
        return
    try:
        handed = json.loads(raw)
    except ValueError:
        return
    if not isinstance(handed, dict) or handed.get("dist") != str(key) or handed.get("source") != source:
        return
    state = handed.get("state")
    if state not in _UNSETTLED_STATES:
        return
    taken = PageCheck(cast(PageState, state), why=str(handed.get("why", "")), output=str(handed.get("output", "")))
    _UNSETTLED[key] = (source, taken)


def _check(folder: Path, dist: Path, source: str, say: Callable[[str], None]) -> PageCheck:
    """Compare, and build when the page is not current. Only a build takes the folder's lock — a page that is current
    takes none — and under the lock the record is read again: another start may have built the page meanwhile."""
    built_from = _built_from(dist)
    if built_from == source:
        return PageCheck("current")
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        if built_from == "":
            why = "has no record of the source it was built from"
            say(f"streamstudio: the editor's page {why} and Node is not installed — the page built earlier is served")
            return PageCheck("unverified", why=why)
        how = "is not built" if built_from is None else "is out of date"
        say(
            f"streamstudio: the editor's page {how} and Node is not installed — "
            "install Node.js (it brings npm) and start again"
        )
        return PageCheck("no-node", why=how)
    with _held(folder, say):
        built_from = _built_from(dist)
        if built_from == source:
            return PageCheck("current")
        return _build(folder, dist, source, npm, built_from, say)


def _build(
    folder: Path, dist: Path, source: str, npm: str, built_from: Optional[str], say: Callable[[str], None]
) -> PageCheck:
    """``npm ci`` when needed, then ``npm run build:page``, and the record — npm's output shown on a failed step."""
    say(f"streamstudio: the editor's page {_what_changed(built_from)} — building it")
    started = time.perf_counter()
    steps: List[NpmStep] = ["build"]
    if _needs_install(folder):
        say("streamstudio: installing the page's libraries first (npm ci) — the first time takes about a minute")
        steps.insert(0, "ci")
    for step in steps:
        command, args = _STEPS[step]
        code, output = _run_npm(npm, args, folder, TIMEOUTS[step])
        if code == 0:
            continue
        if code is None:
            why = f"{command} did not finish in {TIMEOUTS[step]:g} s"
        else:
            why = f"{command} exited with code {code}"
        tail = output.strip("\n").splitlines()[-TAIL_LINES:]  # npm's output opens with a blank line
        say(f"streamstudio: building the editor's page failed — {why}" + ("; its last lines:" if tail else ""))
        for line in tail:
            say(f"  {line}")
        return PageCheck("failed", why=why, output="\n".join(tail))
    _write_record(dist, source)
    seconds = time.perf_counter() - started
    say(f"streamstudio: built the editor's page in {seconds:.1f} s")
    return PageCheck("built", seconds=seconds)


def _run_npm(npm: str, args: Tuple[str, ...], folder: Path, limit: float) -> Tuple[Optional[int], str]:
    """Run one npm step in a session of its own; answer its exit code (``None`` when it outlived ``limit`` seconds and
    was stopped) and its output. Ctrl+C reaches only this process (npm is outside the terminal's process group), so
    npm is stopped here before the interrupt goes on."""
    process = subprocess.Popen(
        [npm, *args],
        cwd=folder,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=limit)
        return process.returncode, output or ""
    except subprocess.TimeoutExpired:
        _stop(process)
        try:
            rest, _ = process.communicate(timeout=_AFTER_STOP_S)  # what npm wrote before it was stopped
        except subprocess.TimeoutExpired:  # pragma: no cover - a child that left npm's session holds the pipe
            rest = ""
        return None, rest or ""
    except KeyboardInterrupt:
        _stop(process)
        raise


def _stop(process: "subprocess.Popen[str]") -> None:
    """Stop npm and everything it started — its whole process group (it leads a session of its own)."""
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX only
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:  # it ended on its own meanwhile
        pass


@contextmanager
def _held(folder: Path, say: Callable[[str], None]) -> Iterator[None]:
    """Hold the page folder's exclusive lock (``flock`` on the folder itself — no lock file is left behind) for one
    build: a second start from the same checkout says it waits, waits here, then finds the page the first one built
    current. A folder that cannot be opened, or a filesystem that refuses the lock, builds without it — a lock is never
    a reason not to start."""
    handle: Optional[int] = None
    if sys.platform != "win32":
        try:
            handle = os.open(folder, os.O_RDONLY)
        except OSError:
            handle = None
    try:
        if handle is not None:
            _lock(handle, say)
        yield
    finally:
        if handle is not None:
            os.close(handle)  # closing the last descriptor of the open file releases the lock


def _lock(handle: int, say: Callable[[str], None]) -> None:
    """Take the lock — at once when it is free; otherwise say so, then wait for the start holding it."""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("streamstudio: another start is building the editor's page — waiting for it")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:  # a filesystem without flock (ENOTSUP, ENOLCK): no lock
        pass


def _write_record(dist: Path, source: str) -> Path:
    """Write the record that the page in ``dist`` was built from ``source`` (a fingerprint); answer its path."""
    stamp = dist / STAMP
    stamp.write_text(source + "\n", encoding="utf-8")
    return stamp


def _built_from(dist: Path) -> Optional[str]:
    """The fingerprint the page in ``dist`` was built from; ``""`` for a page built without a usable record (by hand,
    before the record existed, or a record that is not a fingerprint); ``None`` when ``dist`` holds no page."""
    if not (dist / "index.html").is_file():
        return None
    stamp = dist / STAMP
    if not stamp.is_file():
        return ""
    text = stamp.read_bytes().decode("ascii", errors="replace").strip()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def _what_changed(built_from: Optional[str]) -> str:
    """How a page that is not current stands, in the line a start prints before it builds."""
    if built_from is None:
        return "is not built yet"
    if built_from == "":
        return "has no record of the source it was built from"
    return "changed since its last build"


def _needs_install(folder: Path) -> bool:
    """``npm ci`` first when the page's libraries are missing, or ``package-lock.json`` is NEWER than npm's own record
    of its last install (``node_modules/.package-lock.json``, which every ``npm install`` / ``npm ci`` rewrites): a
    pull that changes the lock leaves it newer than that record.

    By TIME, not by contents: npm keeps no fingerprint of the lock it installed from. The cost, accepted: a lock
    rewritten with the same bytes (a branch switched away and back) installs once more (about a minute the first time,
    seconds from npm's cache) at the next start that builds — a page whose source did not change builds nothing, and
    installs nothing.
    """
    record = folder / "node_modules" / ".package-lock.json"
    if not record.is_file():
        return True
    lock = folder / "package-lock.json"
    return lock.is_file() and lock.stat().st_mtime_ns > record.stat().st_mtime_ns


__all__ = [
    "BEGUN",
    "BUILD_COMMAND",
    "FRONTEND",
    "HANDED_DOWN",
    "OUT_DIR",
    "PAGE_STATES",
    "STAMP",
    "TAIL_LINES",
    "TIMEOUTS",
    "NpmStep",
    "PageCheck",
    "PageState",
    "SourceChanged",
    "begin_build",
    "built_folder",
    "ensure_page_built",
    "fingerprint",
    "page_check_environment",
    "page_inputs",
    "record_build",
    "unbuildable",
]


if __name__ == "__main__":  # the two steps around vite in `npm run build` (the module docstring)
    _arguments = sys.argv[1:]
    if _arguments not in ([], ["begin"]):
        print("usage: page.py [begin]   (no argument: record the page just built)", file=sys.stderr)
        sys.exit(2)
    try:
        if _arguments:
            begin_build()
        else:
            print(f"streamstudio: recorded the source of the editor's page in {record_build()}")
    except (OSError, SourceChanged) as exc:  # no built page, an unwritable folder, or a source edited during the build
        sys.exit(f"streamstudio: {exc}")

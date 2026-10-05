"""The catalogue the page lists — every class an installed package declares — built in a HELPER PROCESS.

Building it imports every ``confluid.configurables`` entry point (torch, TensorFlow, JAX, every training and detection
backend) and describes every class. Measured 2026-10-05 at load 23, in the server's own process: 21 s — 12 s of
imports, 6 s building each class's settings — after which the server held 1.2 GB more than before. A second request
importing in another thread meanwhile waits on the same import locks (``docs/architecture.md``, "A short class name is
looked up one entry point at a time…", 2026-10-02). So the server never builds it. A helper process
(``python -P -m streamstudio.catalogue``) imports and describes, writes two answers on its stdout, and exits:

1. the LIST — every class without its settings (:func:`streamstudio.palette.listed`), sorted by class path;
2. every class's full spec, settings included — the second answer comes about 6 s after the first.

Each answer is one line of JSON. The helper points its own file descriptor 1 at stderr before importing anything, so
whatever an imported package prints lands in the server's log, never in an answer.

The server keeps ONE build per process (:data:`CATALOGUE`): the catalogue describes the installed packages, which are
the same for every editor a process mounts, as the palette's own cache always was. Serving the editor's page starts it
(:meth:`Catalogue.start`); the first request that needs the list waits for it. A class only the server's registry holds
— one a test or a host defined, no entry point naming it — is described in the server and added to the list.
"""

import atexit
import difflib
import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from typing import IO, Any, Dict, List, Optional, Sequence, cast

from confluid.registry import resolve_class

from streamstudio import palette

#: How the server runs the helper: this interpreter, ``-P`` so the working directory never shadows an installed package
#: (a folder named like a package in the workspace root does), ``-m`` this module.
HELPER: Sequence[str] = (sys.executable, "-P", "-m", "streamstudio.catalogue")

#: One entry of the list, or one class's full spec, as JSON — field names as :class:`streamstudio.palette.NodeSpec`.
Entry = Dict[str, Any]


class CatalogueUnavailable(RuntimeError):
    """The helper process stopped before it gave the answer asked for, or gave one that is not JSON."""


class _Build:
    """One run of the helper: the process, a reader thread taking its two answers, and when each arrived."""

    def __init__(self, command: Sequence[str]) -> None:
        self.process = subprocess.Popen(
            list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, encoding="utf-8"
        )
        self.entries: Optional[List[Entry]] = None
        self.specs: Optional[Dict[str, Entry]] = None
        self.failure = ""
        self.listed = threading.Event()
        self.settled = threading.Event()
        threading.Thread(target=self._read, name="streamstudio-catalogue", daemon=True).start()

    def _read(self) -> None:
        # ``listed`` is set early only once the list is in; on any failure ``failure`` is written BEFORE either event,
        # so a waiter woken without an answer always reads why.
        stdout = cast(IO[str], self.process.stdout)  # stdout=PIPE: never None
        try:
            first = stdout.readline()
            if first:
                self.entries = json.loads(first)
                self.listed.set()
                second = stdout.readline()
                if second:
                    self.specs = {spec["class_path"]: spec for spec in json.loads(second)}
        except json.JSONDecodeError as exc:
            self.failure = f"the catalogue's helper process answered something that is not JSON ({exc})"
        finally:
            code = self.process.wait()
            if not self.failure and (self.entries is None or self.specs is None):
                self.failure = (
                    f"the catalogue's helper process stopped before it answered (exit code {code}) — "
                    "what it printed is in this server's log"
                )
            self.listed.set()
            self.settled.set()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait()


class Catalogue:
    """The catalogue of one process: at most one helper at a time, its answers kept until a refresh.

    Args:
        command: How to run the helper; ``None`` = :data:`HELPER`. A test hands a script that answers what it needs.
    """

    def __init__(self, command: Optional[Sequence[str]] = None) -> None:
        self.command: Sequence[str] = HELPER if command is None else tuple(command)
        self._build: Optional[_Build] = None
        self._lock = threading.Lock()
        self._own: Dict[str, Entry] = {}
        self._stops_at_exit = False

    def start(self, refresh: bool = False) -> None:
        """Run the helper unless one runs or has answered; ``refresh`` runs a new one (a package installed or edited
        since — the helper is a fresh interpreter, so it sees both)."""
        with self._lock:
            self._started(refresh)

    def _started(self, refresh: bool) -> _Build:  # under the lock
        build = self._build
        if build is not None and (refresh or build.failure):
            build.stop()
            build = None
        if build is None:
            build = self._build = _Build(self.command)
            if not self._stops_at_exit:  # a helper never outlives its server
                atexit.register(self.stop)
                self._stops_at_exit = True
        return build

    def _current(self, refresh: bool = False) -> _Build:
        with self._lock:
            return self._started(refresh)

    def entries(self, refresh: bool = False) -> List[Entry]:
        """The list: the helper's first answer — waited for — plus the classes only this process's registry holds,
        sorted by class path.

        Raises:
            CatalogueUnavailable: The helper stopped before it answered; the next call runs a new one.
        """
        build = self._current(refresh)
        build.listed.wait()
        if build.entries is None:
            raise CatalogueUnavailable(build.failure)
        listed = {entry["class_path"]: entry for entry in build.entries}
        for target in palette.registered(scan=False):
            class_path = palette.class_path_of(target)
            if class_path in listed:
                continue
            if class_path not in self._own:  # described once: the registry only grows
                self._own[class_path] = palette.listed(palette.describe(target, settings=False))
            listed[class_path] = self._own[class_path]
        return [listed[class_path] for class_path in sorted(listed)]

    def spec(self, class_path: str) -> Entry:
        """One class's full spec, settings included — the helper's second answer, or described here when the class's
        module is already imported in this process (no import, no wait) or no helper lists the class.

        Raises:
            KeyError: No class is called that; the message names the close ones.
            CatalogueUnavailable: The helper stopped before it gave the settings.
        """
        build = self._current()
        if build.specs is not None and class_path in build.specs:
            return build.specs[class_path]
        if class_path.rpartition(".")[0] in sys.modules:
            here = self._here(class_path)
            if here is not None:
                return here
        build.listed.wait()
        if build.entries is not None and class_path not in {entry["class_path"] for entry in build.entries}:
            here = self._here(class_path)
            if here is not None:
                return here
            known = [entry["class_path"] for entry in build.entries]
            close = difflib.get_close_matches(class_path, known, n=5, cutoff=0.6)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise KeyError(f"no node class {class_path!r} in the catalogue of {len(known)} classes{hint}")
        build.settled.wait()
        if build.specs is None or class_path not in build.specs:
            raise CatalogueUnavailable(build.failure or f"the catalogue's helper gave no settings for {class_path!r}")
        return build.specs[class_path]

    @staticmethod
    def _here(class_path: str) -> Optional[Entry]:  # described in this process, or None when nothing is called that
        target = resolve_class(class_path)
        return None if target is None else asdict(palette.describe(target))

    def settled(self) -> bool:
        """The helper has given both answers (or stopped)."""
        build = self._build
        return build is not None and build.settled.is_set()

    def running(self) -> Optional["subprocess.Popen[str]"]:
        """The helper's process while it runs, else ``None``."""
        build = self._build
        return build.process if build is not None and build.process.poll() is None else None

    def stop(self) -> None:
        """End a helper that still runs (the server is stopping, or a test is done); its answers so far are dropped."""
        with self._lock:
            if self._build is not None:
                self._build.stop()
                self._build = None


#: The catalogue of this process — the routes and the page read it here at each request.
CATALOGUE = Catalogue()


def main() -> None:
    """The helper: import every entry point, write the list, then every class's full spec — one JSON line each.

    The answers go to this process's stdout, which is kept for them alone: file descriptor 1 is pointed at stderr
    first, so what an imported package prints goes to the server's log, never into an answer.
    """
    sys.stdout.flush()
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def answer(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    targets = palette.registered(scan=True)
    light = [palette.listed(palette.describe(target, settings=False)) for target in targets]
    answer(json.dumps(sorted(light, key=lambda entry: entry["class_path"])))
    full = [asdict(palette.describe(target)) for target in targets]
    answer(json.dumps(sorted(full, key=lambda spec: spec["class_path"])))


if __name__ == "__main__":
    main()

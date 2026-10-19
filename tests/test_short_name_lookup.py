"""A document's short class name (``AnnotationUpdateSink``) is found by importing ONE entry point at a time.

A registry key resolves only once the module defining it has been imported. Before, the first miss imported every
``confluid.configurables`` entry point (``confluid.load_configurables``) — measured 2026-10-02 in a fresh annotation
app: the first graph list of the editor took 13.9 s for ONE key from a domain package's ``sinks``, because the miss also
imported torch, transformers and the detection backends. Now the miss walks the entry points one by one and stops at the
first after which the name resolves; entry points of a package that is already imported go first, an entry point whose
module is already imported is not loaded again, and a name nothing defines ends with every entry point imported exactly
once per process, so a later miss costs nothing.

The entry points are real ``importlib.metadata.EntryPoint`` objects over modules written to ``tmp_path`` (the
import genuinely happens inside the lookup); ``importlib.metadata.entry_points`` serves only them for the group. A
subclass counts each ``load()``, so "imported" and "not imported" are read off the counter, not guessed from
``sys.modules``. Log lines are collected by replacing ``streamstudio.document.logger`` (loggair does not propagate
into stdlib logging, so ``caplog`` cannot see them).

Two lookups can run at once — a page's request threads, or a lookup beside the catalogue's own full import. Those
tests hold a module's import half-way with ``threading.Event``s in a module the bodies import (``ssq4_sync``), and
release it from the point the race needs (an entry point's ``load()``, a second lookup queueing on the walk's lock):
no sleeps, and a body never waits more than 10 s for a release that never comes.
"""

import importlib
import importlib.metadata
import sys
import threading
from collections import Counter
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from streamstudio import document

GROUP = "confluid.configurables"

#: ``load()`` calls per entry-point name, for the test running now.
LOADS: Counter = Counter()

#: Called with the entry point's name as its ``load()`` starts, before the import: what a race test releases from.
ON_LOAD: List[Callable[[str], None]] = []


class CountingEntryPoint(importlib.metadata.EntryPoint):
    """A real entry point that counts its ``load()`` calls in ``LOADS``."""

    def load(self) -> Any:
        LOADS[self.name] += 1
        for hook in ON_LOAD:
            hook(self.name)
        return super().load()


def entry(name: str, module: str) -> CountingEntryPoint:
    return CountingEntryPoint(name=name, value=module, group=GROUP)


def defining(cls: str) -> str:
    """A module body that registers ``cls`` as a configurable class."""
    return (
        "from confluid import configurable\n"
        "\n"
        "\n"
        "@configurable\n"
        f"class {cls}:\n"
        "    def __init__(self, n: int = 0) -> None:\n"
        "        self.n = n\n"
    )


def defining_after(cls: str, release: str) -> str:
    """A module body that records it ran, says it STARTED, waits for ``ssq4_sync.<release>``, then registers ``cls``.

    While it waits, the module is in ``sys.modules`` and ``cls`` is not registered: an import half-way through.
    """
    return (
        "import ssq4_sync\n"
        "ssq4_sync.ran.append(__name__)\n"
        "ssq4_sync.started.set()\n"
        f"assert ssq4_sync.{release}.wait(10), 'the test never released the import'\n" + defining(cls)
    )


def counted(cls: str) -> str:
    """A module body that registers ``cls`` and records in ``ssq4_sync.ran`` each time it runs."""
    return "import ssq4_sync\nssq4_sync.ran.append(__name__)\n" + defining(cls)


class Served:
    """The entry points the group serves and how often the list was read."""

    def __init__(self) -> None:
        self.entries: List[CountingEntryPoint] = []
        self.reads = 0


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """A fresh process's lookup state over the entry points a test puts in ``served.entries``."""
    LOADS.clear()
    state = Served()
    real_entry_points = importlib.metadata.entry_points

    def patched(**params: Any) -> Any:
        if params.get("group") == GROUP:
            state.reads += 1
            return list(state.entries)
        return real_entry_points(**params)

    monkeypatch.setattr(importlib.metadata, "entry_points", patched)
    monkeypatch.setattr(document, "_pending_entry_points", None)
    monkeypatch.syspath_prepend(str(tmp_path))
    ON_LOAD.clear()
    before = set(sys.modules)
    yield state
    ON_LOAD.clear()
    for name in set(sys.modules) - before:
        if name.startswith("ssq4_"):
            sys.modules.pop(name, None)


@pytest.fixture
def sync(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """``ssq4_sync``: the events a half-way import waits on, and the names of the module bodies that ran."""
    module = ModuleType("ssq4_sync")
    module.ran = []  # type: ignore[attr-defined]
    for event in ("started", "release", "queued"):
        setattr(module, event, threading.Event())
    monkeypatch.setitem(sys.modules, "ssq4_sync", module)
    return module


class QueueSignallingLock:
    """The walk's lock, setting ``queued`` the moment a SECOND lookup starts waiting on it (before it blocks)."""

    def __init__(self, queued: threading.Event) -> None:
        self._lock = threading.Lock()
        self._queued = queued
        self._entered = 0

    def __enter__(self) -> None:
        self._entered += 1
        if self._entered == 2:
            self._queued.set()
        self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def write(tmp_path: Path, module: str, body: str) -> None:
    """``module`` as a file under ``tmp_path``: ``a.b`` is ``a/b.py``, a package's own is ``a.__init__``."""
    path = tmp_path.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_a_short_name_imports_the_entry_point_that_defines_it_and_stops_there(tmp_path: Path, served: Served) -> None:
    write(tmp_path, "ssq4_plain", defining("Ssq4Plain"))
    write(tmp_path, "ssq4_sinks", defining("Ssq4LookupSink"))
    write(tmp_path, "ssq4_heavy", defining("Ssq4Heavy"))
    served.entries = [entry("plain", "ssq4_plain"), entry("sinks", "ssq4_sinks"), entry("heavy", "ssq4_heavy")]

    found = document._resolve("Ssq4LookupSink")

    assert found is not None and found.__name__ == "Ssq4LookupSink"
    assert dict(LOADS) == {"plain": 1, "sinks": 1}  # in the listed order, and no further
    assert "ssq4_heavy" not in sys.modules

    # a later miss goes on where the first one stopped: the heavy one now, the two done ones not again
    heavy = document._resolve("Ssq4Heavy")
    assert heavy is not None and heavy.__name__ == "Ssq4Heavy"
    assert dict(LOADS) == {"plain": 1, "sinks": 1, "heavy": 1}
    assert served.reads == 1  # the entry-point list is read once per process


def test_the_entry_points_of_an_already_imported_package_are_tried_first(tmp_path: Path, served: Served) -> None:
    """The real case: the sink's package (a domain package) is imported by the app already; the backends are not."""
    write(tmp_path, "ssq4_backend", defining("Ssq4Backend"))
    write(tmp_path, "ssq4_domain.__init__", "")
    write(tmp_path, "ssq4_domain.sinks", defining("Ssq4DomainSink"))
    importlib.import_module("ssq4_domain")
    served.entries = [entry("backend", "ssq4_backend"), entry("domain-sinks", "ssq4_domain.sinks")]

    found = document._resolve("Ssq4DomainSink")

    assert found is not None and found.__name__ == "Ssq4DomainSink"
    assert dict(LOADS) == {"domain-sinks": 1}  # listed second, tried first
    assert "ssq4_backend" not in sys.modules


def test_an_entry_point_whose_module_is_imported_already_is_not_imported_again(
    tmp_path: Path, served: Served, sync: ModuleType
) -> None:
    write(tmp_path, "ssq4_done", counted("Ssq4Done"))
    write(tmp_path, "ssq4_wanted", counted("Ssq4Wanted"))
    importlib.import_module("ssq4_done")  # its classes are registered already
    served.entries = [entry("done", "ssq4_done"), entry("wanted", "ssq4_wanted")]

    found = document._resolve("Ssq4Wanted")

    assert found is not None and found.__name__ == "Ssq4Wanted"
    assert sync.ran == ["ssq4_done", "ssq4_wanted"]  # each body ran once: the done one by the test, not again


def test_a_module_another_thread_is_still_importing_is_waited_for_not_taken_as_imported(
    tmp_path: Path, served: Served, sync: ModuleType
) -> None:
    """Python lists a module in ``sys.modules`` BEFORE its body runs. The catalogue imports every entry point in its
    own request thread; when it is half-way through the module defining the name, the lookup must wait for that
    import — before, it took the module for imported, dropped the entry point and answered "cannot resolve"."""
    write(tmp_path, "ssq4_slow", defining_after("Ssq4Slow", "release"))
    served.entries = [entry("slow", "ssq4_slow")]
    ON_LOAD.append(lambda name: sync.release.set())  # the lookup loading the entry point lets the other import go on
    other = threading.Thread(target=importlib.import_module, args=("ssq4_slow",))
    other.start()
    try:
        assert sync.started.wait(10)
        assert "ssq4_slow" in sys.modules and not hasattr(sys.modules["ssq4_slow"], "Ssq4Slow")  # half-way

        found = document._resolve("Ssq4Slow")
    finally:
        sync.release.set()
        other.join(10)

    assert found is not None and found.__name__ == "Ssq4Slow" and found.__module__ == "ssq4_slow"
    assert sync.ran == ["ssq4_slow"]  # the other thread's import, not a second one


@pytest.mark.parametrize(
    "listed_after", [[], ["ssq4_unrelated"]], ids=["nothing listed after it", "another entry point listed after it"]
)
def test_a_lookup_that_waited_for_another_walk_answers_what_that_walk_found_and_imports_nothing_more(
    tmp_path: Path, served: Served, sync: ModuleType, monkeypatch: pytest.MonkeyPatch, listed_after: List[str]
) -> None:
    """Two requests miss the same name at once. The second waits for the first walk's lock; once it has it, the name
    is registered — before, it walked on from where the first stopped: "cannot resolve" when nothing was left, and
    an entry point nobody needed when something was."""
    # one class per case: the registry is the process's, and a class left by the other case would resolve at once
    cls, module = f"Ssq4Queued{len(listed_after)}", f"ssq4_queued{len(listed_after)}"
    write(tmp_path, module, defining_after(cls, "queued"))
    write(tmp_path, "ssq4_unrelated", defining("Ssq4Unrelated"))
    served.entries = [entry("queued", module)] + [entry("unrelated", unrelated) for unrelated in listed_after]
    monkeypatch.setattr(document, "_entry_points_lock", QueueSignallingLock(sync.queued))
    answers: Dict[str, Optional[type]] = {}
    first = threading.Thread(target=lambda: answers.update(first=document._resolve(cls)))
    first.start()
    try:
        assert sync.started.wait(10)  # the first walk is inside the defining module: the class is not registered yet

        answers["second"] = document._resolve(cls)  # misses, queues on the lock — which releases the first's import
    finally:
        sync.queued.set()
        first.join(10)

    assert answers["first"] is not None and answers["first"].__name__ == cls
    assert answers["second"] is answers["first"]
    assert dict(LOADS) == {"queued": 1}
    assert "ssq4_unrelated" not in sys.modules


def test_a_name_two_entry_points_define_is_the_first_one_the_walk_imports(tmp_path: Path, served: Served) -> None:
    """The documented limit: the walk stops at the first entry point that registers the name, so the second
    definer is not imported and the name is not ambiguous YET. (A run, or the editor once the catalogue has
    imported everything, refuses the name as ambiguous.)"""
    write(tmp_path, "ssq4_numpy_twin", defining("Ssq4TwinOp"))
    write(tmp_path, "ssq4_torch_twin", defining("Ssq4TwinOp"))
    served.entries = [entry("numpy-twin", "ssq4_numpy_twin"), entry("torch-twin", "ssq4_torch_twin")]

    found = document._resolve("Ssq4TwinOp")

    assert found is not None and found.__module__ == "ssq4_numpy_twin"
    assert "ssq4_torch_twin" not in sys.modules


def test_a_name_no_entry_point_defines_imports_each_once_and_a_second_unknown_name_imports_nothing(
    tmp_path: Path, served: Served
) -> None:
    write(tmp_path, "ssq4_one", defining("Ssq4One"))
    write(tmp_path, "ssq4_two", defining("Ssq4Two"))
    served.entries = [entry("one", "ssq4_one"), entry("two", "ssq4_two")]

    assert document._resolve("Ssq4NoSuchClass") is None
    assert dict(LOADS) == {"one": 1, "two": 1}

    assert document._resolve("Ssq4NorThisOne") is None
    assert dict(LOADS) == {"one": 1, "two": 1}  # nothing more
    assert served.reads == 1


def test_a_dotted_name_that_does_not_resolve_walks_no_entry_point(tmp_path: Path, served: Served) -> None:
    write(tmp_path, "ssq4_any", defining("Ssq4Any"))
    served.entries = [entry("any", "ssq4_any")]

    assert document._resolve("ssq4_nowhere.Ssq4Thing") is None
    assert dict(LOADS) == {} and served.reads == 0


def test_a_broken_entry_point_is_skipped_with_one_warning_and_the_lookup_goes_on(
    tmp_path: Path, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "ssq4_broken", "raise RuntimeError('boom at import')\n")
    write(tmp_path, "ssq4_after", defining("Ssq4AfterBroken"))
    served.entries = [entry("broken", "ssq4_broken"), entry("after", "ssq4_after")]
    seen: List[str] = []
    monkeypatch.setattr(
        document, "logger", SimpleNamespace(warning=seen.append, debug=lambda m: None, trace=lambda m: None)
    )

    found = document._resolve("Ssq4AfterBroken")

    assert found is not None and found.__name__ == "Ssq4AfterBroken"
    # the line confluid.load_configurables writes for the same entry point
    assert seen == ["load_configurables: entry point 'broken' (ssq4_broken) failed to import: boom at import"]
    assert document._resolve("Ssq4StillMissing") is None
    assert LOADS["broken"] == 1 and len(seen) == 1  # tried once per process, like every other entry point

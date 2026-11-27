"""No package that depends on streamstudio is one of its own dependencies — not even in an extra, not through another
package.

Dependencies run one way: a domain's package reaches the editor (its tests open its graphs in it, a workspace mounts
it) and declares that; the editor never declares a domain. A signal domain's extra did, and 82 of this suite's tests
then ran only where the signal domain happened to be installed — skipped in CI, so a change to a signal pane broke four
of them unseen (2026-10-08). A test that needs a domain's graphs lives in that domain's suite.

The walk follows every requirement line of the installed metadata, extras included: a package whose TESTS depend on
this one depends on it too. An editable install keeps the metadata it was installed with, so a stale one can hide a
path (measured 2026-10-08: an annotaide installed before it required this package) — reinstall it (``uv pip install
-e ./<project> --no-deps``, or ``aisland setup``) before trusting a pass.
"""

import re
import tomllib
from importlib.metadata import PackageNotFoundError, requires
from pathlib import Path
from typing import Dict, List, Optional

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
SELF = "streamstudio"

#: A requirement's distribution name: what comes before any extra, version, marker or URL.
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _name(requirement: str) -> str:
    """The normalised distribution name of one requirement line (PEP 503: case and ``-_.`` runs do not count)."""
    found = _NAME.match(requirement)
    assert found, f"not a requirement: {requirement!r}"
    return re.sub(r"[-_.]+", "-", found.group(1)).lower()


def _declared() -> List[str]:
    """Every distribution this project's ``pyproject.toml`` declares — the run-time list and every extra."""
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    lines = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        lines.extend(extra)
    return sorted({_name(line) for line in lines})


def _requirements(distribution: str) -> List[str]:
    """What the INSTALLED ``distribution`` requires, every extra included; nothing for one that is not installed (the
    dev install puts every declared distribution in place, so that is a hand-built environment)."""
    try:
        lines = requires(distribution) or []
    except PackageNotFoundError:
        return []
    return sorted({_name(line) for line in lines})


def _path_back(start: str) -> Optional[List[str]]:
    """The chain of requirements from ``start`` back to this project (``[start, …, "streamstudio"]``), or ``None``."""
    parents: Dict[str, Optional[str]] = {start: None}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for required in _requirements(current):
            if required == SELF:
                chain = [SELF, current]
                while parents[chain[-1]] is not None:
                    chain.append(parents[chain[-1]])  # type: ignore[arg-type]  # not None: checked just above
                return chain[::-1]
            if required not in parents:
                parents[required] = current
                queue.append(required)
    return None


def test_no_declared_dependency_depends_on_streamstudio() -> None:
    found = {name: chain for name in _declared() if (chain := _path_back(name)) is not None}

    assert found == {}, f"{PYPROJECT}: " + "; ".join(
        f"{name} depends on {SELF}" + (f" (through {', '.join(chain[1:-1])})" if len(chain) > 2 else "")
        for name, chain in found.items()
    )


def test_a_requirement_line_is_read_to_its_distribution_name() -> None:
    assert _name("annotaide>=0.1.0; extra == 'notebook'") == "annotaide"
    assert _name("torchsig @ git+https://github.com/TorchDSP/torchsig.git") == "torchsig"
    assert _name("Pillow") == "pillow"
    assert _name("python_dotenv[cli]>=1.0.0") == "python-dotenv"
